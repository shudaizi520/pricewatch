import gc
import weakref
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import func, select

from pricewatch.db.base import Base
from pricewatch.db.models import CheckRun, NotificationDelivery, Observation, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
from pricewatch.fetching.browser import prepare_dell_configurator
from pricewatch.fetching.http import AcquisitionError
from pricewatch.fetching.safety import UnsafeUrlError
from pricewatch.services.checks import CheckService


@pytest.fixture
def check_service(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory() as session:
        item = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/en-us/shop/spd/x/sku_01",
            status="active",
            sku="sku_01",
        )
        session.add(item)
        session.commit()
        product_id = item.id
    yield CheckService(factory), factory, product_id
    engine.dispose()


def snapshot(
    gpu="RTX 5090",
    amount=299999,
    url="https://www.dell.com/en-us/shop/spd/x/sku_01",
    availability="in_stock",
):
    return ProductSnapshot(
        ProductIdentity("dell-us", "sku_01"),
        url,
        "Alienware",
        ProductConfiguration(gpu=gpu),
        Money("USD", amount),
        availability=availability,
    )


def test_configuration_change_pauses_comparison(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    outcome = service.accept_snapshot(product_id, snapshot(gpu="RTX 5080", amount=249999))
    assert outcome.product.status == "needs_attention"
    assert [e.kind for e in outcome.events] == ["configuration_changed"]
    with factory() as session:
        trusted_count = select(func.count(Observation.id)).where(Observation.trusted.is_(True))
        assert session.scalar(trusted_count) == 1


def test_legacy_fingerprint_and_non_price_extras_do_not_send_configuration_alert(check_service):
    service, factory, product_id = check_service

    def offer(wireless):
        return ProductSnapshot(
            ProductIdentity("dell-us", "sku_01"),
            "https://www.dell.com/en-us/shop/spd/x/sku_01",
            "Alienware",
            ProductConfiguration(
                gpu="RTX 5090",
                extras={"Keyboard": "CherryMX", "Wireless": wireless},
            ),
            Money("USD", 299999),
            availability="in_stock",
        )

    service.accept_snapshot(product_id, offer("Wi-Fi 7 A"))
    with factory.begin() as session:
        prior = session.scalar(select(Observation).where(Observation.product_id == product_id))
        prior.configuration_fingerprint = "a" * 64  # Stored with the former all-extras rule.
    outcome = service.accept_snapshot(product_id, offer("Wi-Fi 7 B"))
    assert outcome.events == []
    assert outcome.product.status == "active"
    assert outcome.product.configuration["extras"]["Wireless"] == "Wi-Fi 7 B"


def test_keyboard_change_alert_names_both_keyboards_without_folded_options(check_service):
    service, factory, product_id = check_service

    def offer(keyboard, wireless):
        return ProductSnapshot(
            ProductIdentity("dell-us", "sku_01"),
            "https://www.dell.com/en-us/shop/spd/x/sku_01",
            "Alienware",
            ProductConfiguration(
                gpu="RTX 5090", extras={"Keyboard": keyboard, "Wireless": wireless}
            ),
            Money("USD", 299999),
        )

    service.accept_snapshot(product_id, offer("Standard keyboard", "Wi-Fi 7 A"))
    with factory.begin() as session:
        prior = session.scalar(select(Observation).where(Observation.product_id == product_id))
        legacy_record = dict(prior.configuration)
        legacy_record["summary"] = "RTX 5090"  # Older records omitted keyboard.
        prior.configuration = legacy_record
    outcome = service.accept_snapshot(product_id, offer("CherryMX keyboard", "Wi-Fi 7 B"))
    assert [event.kind for event in outcome.events] == ["configuration_changed"]
    with factory() as session:
        alert = session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.event_type == "configuration_changed"
            )
        )
        assert alert is not None
        assert "原配置: RTX 5090 · 键盘: Standard keyboard" in alert.message_text
        assert "新配置: RTX 5090 · 键盘: CherryMX keyboard" in alert.message_text
        assert "Wi-Fi 7" not in alert.message_text


@pytest.mark.parametrize("amount, expected_events", [(299999, []), (279999, ["price_changed"])])
def test_missing_shipping_packaging_keeps_monitoring_original_hardware(
    check_service, amount, expected_events,
):
    service, factory, product_id = check_service
    original = replace(
        snapshot(),
        configuration=ProductConfiguration(
            gpu="RTX 5090",
            extras={
                "Keyboard": "No Keyboard Selected",
                "Packaging": "Kit,Shipping Material Box - KIT,SHP MTL,ANW,CITADEL,ARL,BA",
                "Documentation": "Regular - No Documentation",
            },
        ),
    )
    service.accept_snapshot(product_id, original)
    current = replace(
        original,
        configuration=ProductConfiguration(
            gpu="RTX 5090", extras={"Keyboard": "No Keyboard Selected"},
        ),
        price=Money("USD", amount),
    )

    outcome = service.accept_snapshot(product_id, current)

    assert outcome.product.status == "active"
    assert [event.kind for event in outcome.events] == expected_events
    with factory() as session:
        assert session.scalar(
            select(func.count(Observation.id)).where(Observation.trusted.is_(False))
        ) == 0
        assert session.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.event_type == "configuration_changed"
            )
        ) == 0


def test_missing_packaging_does_not_hide_real_gpu_change(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(
        product_id,
        replace(snapshot(), configuration=ProductConfiguration(
            gpu="RTX 5090", extras={"Packaging": "Shipping box"},
        )),
    )

    outcome = service.accept_snapshot(product_id, snapshot(gpu="RTX 5080", amount=249999))

    assert outcome.product.status == "needs_attention"
    assert [event.kind for event in outcome.events] == ["configuration_changed"]
    with factory() as session:
        assert session.scalar(
            select(func.count(Observation.id)).where(Observation.trusted.is_(True))
        ) == 1


@pytest.mark.parametrize("amount,expected_events", [(299999, []), (279999, ["price_changed"])])
def test_known_bundled_software_description_change_does_not_pause_hardware(
    check_service, amount, expected_events,
):
    service, factory, product_id = check_service
    previous = replace(snapshot(), configuration=ProductConfiguration(
        gpu="RTX 5090",
        extras={"Systems Management": "Additional Software and Sanbao warranty card"},
    ))
    service.accept_snapshot(product_id, previous)
    current = replace(previous, configuration=ProductConfiguration(
        gpu="RTX 5090", extras={"Systems Management": "Dell Windows Application"},
    ), price=Money("USD", amount))

    outcome = service.accept_snapshot(product_id, current)

    assert outcome.status == "ok"
    assert outcome.product.status == "active"
    assert [event.kind for event in outcome.events] == expected_events
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id)).where(
            Observation.trusted.is_(False)
        )) == 0


def test_other_management_software_choice_still_requires_confirmation(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, replace(snapshot(), configuration=ProductConfiguration(
        gpu="RTX 5090", extras={"Systems Management": "Dell Windows Application"},
    )))
    changed = replace(snapshot(), configuration=ProductConfiguration(
        gpu="RTX 5090", extras={"Systems Management": "Paid Fleet Management Bundle"},
    ))

    outcome = service.accept_snapshot(product_id, changed)

    assert outcome.product.status == "needs_attention"
    assert [event.kind for event in outcome.events] == ["configuration_changed"]
    with factory() as session:
        assert session.scalar(
            select(func.count(Observation.id)).where(Observation.trusted.is_(True))
        ) == 1


def test_missing_management_software_group_still_requires_confirmation(check_service):
    service, _, product_id = check_service
    service.accept_snapshot(product_id, replace(snapshot(), configuration=ProductConfiguration(
        gpu="RTX 5090", extras={"Systems Management": "Dell Windows Application"},
    )))

    outcome = service.accept_snapshot(product_id, snapshot())

    assert outcome.product.status == "needs_attention"
    assert [event.kind for event in outcome.events] == ["configuration_changed"]


def test_unchanged_checks_store_at_most_one_daily_checkpoint(check_service):
    service, factory, product_id = check_service
    for hour in (1, 7, 13, 19):
        outcome = service.accept_snapshot(
            product_id, snapshot(), observed_at=datetime(2026, 9, 21, hour, tzinfo=UTC)
        )
    assert outcome.events == []
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1


def test_price_and_stock_changes_emit_distinct_events(check_service):
    service, _, product_id = check_service
    first = service.accept_snapshot(product_id, snapshot())
    changed = service.accept_snapshot(
        product_id, snapshot(amount=279999, availability="out_of_stock")
    )
    assert [event.kind for event in first.events] == ["initial_observation"]
    assert {event.kind for event in changed.events} == {"price_changed", "stock_changed"}


@pytest.mark.parametrize("kind", ["stock_changed", "offer_changed", "check_recovered"])
@pytest.mark.parametrize(
    "currency,price_line", [("USD", "价格: $2,799.99"), ("EUR", "价格: EUR 2,799.99")]
)
def test_success_notifications_include_the_current_snapshot_price(
    check_service, kind, currency, price_line
):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, replace(snapshot(), price=Money(currency, 299999)))
    current = replace(snapshot(), price=Money(currency, 279999))
    if kind == "stock_changed":
        current = replace(current, availability="out_of_stock")
    elif kind == "offer_changed":
        current = replace(current, coupon_text="SAVE10")
    else:
        service.record_failure(product_id, "blocked")
        service.record_failure(product_id, "blocked")

    outcome = service.accept_snapshot(product_id, current)
    assert outcome.status == "ok"
    with factory() as session:
        message = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.event_type == kind)
        )
        assert message is not None
        assert price_line in message.message_text.splitlines()
        assert "2,999.99" not in message.message_text


def test_failure_notification_only_identifies_product_and_failure_reason(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    service.record_failure(product_id, "blocked", error_message="页面访问超时")
    service.record_failure(product_id, "blocked", error_message="页面访问超时")

    with factory() as session:
        message = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.event_type == "check_failed")
        )
        lines = [line for line in message.message_text.splitlines() if line]
        assert "Alienware · 检查重试失败" in lines
        assert "失败原因: blocked · 页面访问超时" in lines
        assert len(lines) == 2


@pytest.mark.parametrize("kind", ["stock_changed", "offer_changed", "check_recovered"])
def test_adding_current_price_does_not_reannounce_an_already_reached_target(check_service, kind):
    service, factory, product_id = check_service
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.notify_mode = "target_or_change"
        product.target_price_minor = 300000
    current = snapshot()
    service.accept_snapshot(product_id, current)
    if kind == "stock_changed":
        current = replace(current, availability="out_of_stock")
    elif kind == "offer_changed":
        current = replace(current, coupon_text="SAVE10")
    else:
        service.record_failure(product_id, "blocked")
        service.record_failure(product_id, "blocked")

    service.accept_snapshot(product_id, current)
    with factory() as session:
        message = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.event_type == kind)
        )
        assert "价格: $2,999.99" in message.message_text.splitlines()
        assert "目标价已达到" not in message.message_text


def test_redirect_updates_canonical_url_only_when_identity_matches(check_service):
    service, _, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    outcome = service.accept_snapshot(
        product_id, snapshot(url="https://www.dell.com/en-us/new/sku_01")
    )
    assert outcome.product.canonical_url.endswith("/sku_01")


@freeze_time("2026-09-29 00:00:00")
def test_failure_retries_after_thirty_minutes_then_alerts_once_and_recovers_once(
    check_service,
):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    first = service.record_failure(product_id, "blocked", error_message="页面访问超时")
    assert first.events == []
    assert first.product.next_check_at == datetime(2026, 9, 29, 0, 30, tzinfo=UTC)

    second = service.record_failure(product_id, "blocked", error_message="页面访问超时")
    assert [event.kind for event in second.events] == ["check_failed"]
    assert service.record_failure(product_id, "blocked").events == []
    assert [event.kind for event in service.accept_snapshot(product_id, snapshot()).events] == [
        "check_recovered"
    ]
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1
        alert = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.event_type == "check_failed")
        )
        assert alert is not None
        assert "检查重试失败" in alert.message_text
        assert "失败原因: blocked · 页面访问超时" in alert.message_text


def test_upgrade_reports_a_preexisting_second_failure_on_the_next_failure(check_service):
    service, factory, product_id = check_service
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.consecutive_failures = 2
        product.failure_reported = False

    outcome = service.record_failure(product_id, "blocked")

    assert [event.kind for event in outcome.events] == ["check_failed"]


def test_generic_without_sku_pauses_if_product_name_changes(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(
            source_site="www.hp.com", requested_url="https://www.hp.com/shop/laptop", name="OMEN 16"
        )
        session.add(item)
        session.flush()
        product_id = item.id
    service = CheckService(factory)

    def generic(name, amount):
        return ProductSnapshot(
            ProductIdentity("www.hp.com"),
            "https://www.hp.com/shop/laptop",
            name,
            ProductConfiguration(),
            Money("USD", amount),
        )

    service.accept_snapshot(product_id, generic("OMEN 16", 200000))
    outcome = service.accept_snapshot(product_id, generic("Victus 16", 150000))
    assert outcome.product.status == "needs_attention"
    assert [event.kind for event in outcome.events] == ["configuration_changed"]
    engine.dispose()


def test_initial_event_is_durable_even_before_webhook_configured(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    with factory() as session:
        deliveries = session.scalars(select(NotificationDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "pending"
        assert "$2,999.99" in deliveries[0].message_text


@pytest.fixture
def dell_entry_check(check_service):
    service, factory, product_id = check_service
    canonical = "https://www.dell.com/en-us/shop/spd/alienware/sku_01"
    original = "https://www.dell.com/en-us/shop/spd/alienware"
    trusted = replace(
        snapshot(url=canonical),
        configuration=ProductConfiguration(gpu="RTX 5090", extras={"Graphics Card": "RTX 5090"}),
    )
    service.accept_snapshot(product_id, trusted)
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.requested_url = original
        product.dell_selection = {"Graphics Card": "RTX 5090"}
    return service, factory, product_id, canonical, original, trusted


@pytest.mark.anyio
async def test_usable_dell_original_entry_skips_rejected_direct_link(dell_entry_check):
    service, factory, product_id, canonical, original, trusted = dell_entry_check
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            assert dell_selection == {"Graphics Card": "RTX 5090"}
            if str(url) == canonical:
                raise AcquisitionError("HTTP 403", status_code=403)
            assert str(url) == original
            return None, replace(trusted, price=Money("USD", 279999))

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "ok"
    assert calls == [original]
    with factory() as session:
        product = session.get(Product, product_id)
        assert product.canonical_url == canonical
        assert product.requested_url == original
        assert product.consecutive_failures == 0
        assert session.scalar(select(Observation.price_minor).order_by(
            Observation.id.desc()
        )) == 279999


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 404, 410])
@pytest.mark.parametrize("recovering", [False, True])
async def test_blocked_dell_original_retries_direct_link_with_same_selection(
    dell_entry_check, status, recovering,
):
    service, factory, product_id, canonical, original, trusted = dell_entry_check
    calls = []
    if recovering:
        with factory.begin() as session:
            product = session.get(Product, product_id)
            product.consecutive_failures = 2
            product.failure_reported = True

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            assert dell_selection == {"Graphics Card": "RTX 5090"}
            if str(url) == original:
                raise AcquisitionError("Unavailable original", status_code=status)
            assert str(url) == canonical
            return None, replace(trusted, price=Money("USD", 279999))

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "ok"
    assert calls == [original, canonical]
    assert [e.kind for e in outcome.events] == ["price_changed"] + (
        ["check_recovered"] if recovering else []
    )
    with factory() as session:
        assert session.get(Product, product_id).consecutive_failures == 0
        assert session.scalars(select(Observation.price_minor).where(
            Observation.trusted.is_(True)
        ).order_by(Observation.id)).all() == [299999, 279999]
        if recovering:
            alert = session.scalar(select(NotificationDelivery).where(
                NotificationDelivery.event_type == "check_recovered"
            ))
            assert "价格: $2,799.99" in alert.message_text.splitlines()
            assert "$2,999.99" not in alert.message_text


async def _customizer_entry_failure(stage):
    class Entry:
        @property
        def last(self):
            return self

        async def count(self):
            return 0 if stage == "missing" else 1

        async def scroll_into_view_if_needed(self):
            if stage == "activation":
                raise PlaywrightError("private-token-and-url")

        async def click(self):
            pass

    class FamilyPage:
        async def wait_for_function(self, *_args, **_kwargs):
            raise PlaywrightTimeoutError("private-token-and-url")

        def get_by_text(self, text, exact=False):
            assert text == "Build your own" and exact
            return Entry()

    with pytest.raises(AcquisitionError) as failure:
        await prepare_dell_configurator(FamilyPage())
    return failure.value


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["missing", "activation", "loading"])
@pytest.mark.parametrize("recovering", [False, True])
async def test_http_200_family_without_customizer_retries_saved_sku_entry(
    dell_entry_check, stage, recovering,
):
    service, factory, product_id, canonical, original, trusted = dell_entry_check
    unavailable = await _customizer_entry_failure(stage)
    calls = []
    if recovering:
        with factory.begin() as session:
            product = session.get(Product, product_id)
            product.consecutive_failures = 2
            product.failure_reported = True

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            assert dell_selection == {"Graphics Card": "RTX 5090"}
            if str(url) == original:
                raise unavailable
            assert str(url) == canonical
            return None, replace(trusted, price=Money("USD", 279999))

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "ok"
    assert calls == [original, canonical]
    assert [e.kind for e in outcome.events] == ["price_changed"] + (
        ["check_recovered"] if recovering else []
    )
    with factory() as session:
        product = session.get(Product, product_id)
        assert product.consecutive_failures == 0
        assert product.sku == "sku_01"
        assert product.requested_url == original
        assert session.scalars(select(Observation.price_minor).where(
            Observation.trusted.is_(True)
        ).order_by(Observation.id)).all() == [299999, 279999]
        if recovering:
            alert = session.scalar(select(NotificationDelivery).where(
                NotificationDelivery.event_type == "check_recovered"
            ))
            assert "价格: $2,799.99" in alert.message_text.splitlines()


@pytest.mark.anyio
async def test_unavailable_customizer_then_blocked_sku_preserves_both_errors_once(
    dell_entry_check,
):
    service, factory, product_id, canonical, original, _ = dell_entry_check
    unavailable = await _customizer_entry_failure("missing")
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            if str(url) == original:
                raise unavailable
            assert str(url) == canonical
            raise AcquisitionError("商品页面返回 HTTP 403", status_code=403)

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "failed"
    assert calls == [original, canonical]
    with factory() as session:
        assert session.get(Product, product_id).consecutive_failures == 1
        assert session.scalar(select(func.count(CheckRun.id)).where(
            CheckRun.trigger == "scheduled"
        )) == 1
        assert session.scalar(select(func.count(Observation.id))) == 1
        run = session.scalar(select(CheckRun).where(CheckRun.trigger == "scheduled"))
        assert "自定义配置器入口" in run.error_message
        assert "HTTP 403" in run.error_message
        assert "直达入口" in run.error_message
        assert "private-token" not in run.error_message


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["sku", "gpu", "currency", "selection"])
async def test_customizer_fallback_never_accepts_a_different_monitored_offer(
    dell_entry_check, mutation,
):
    service, factory, product_id, canonical, original, trusted = dell_entry_check
    unavailable = await _customizer_entry_failure("missing")
    current = replace(trusted, price=Money("USD", 279999))
    if mutation == "sku":
        current = replace(current, identity=ProductIdentity("dell-us", "other_sku"))
    elif mutation == "currency":
        current = replace(current, price=Money("EUR", 279999))
    else:
        current = replace(current, configuration=ProductConfiguration(
            gpu="RTX 5080", extras={"Graphics Card": "RTX 5080" if mutation == "selection"
                                  else "RTX 5090"},
        ))
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            if str(url) == original:
                raise unavailable
            assert str(url) == canonical
            return None, current

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert calls == [original, canonical]
    assert outcome.status == ("failed" if mutation == "selection" else "needs_attention")
    with factory() as session:
        trusted_rows = session.scalars(
            select(Observation).where(Observation.trusted.is_(True))
        ).all()
        assert [(row.currency, row.price_minor) for row in trusted_rows] == [("USD", 299999)]


@pytest.mark.anyio
@pytest.mark.parametrize("original", [
    "https://www.dell.com/en-us/shop/spd/alienware/sku_01",
    "https://other.example/en-us/shop/spd/alienware",
    "https://www.dell.com/en-uk/shop/spd/alienware",
    "https://www.dell.com:8443/en-us/shop/spd/alienware",
    "https://user:secret@www.dell.com/en-us/shop/spd/alienware",
])
async def test_customizer_failure_does_not_expand_entry_retry_to_unsafe_or_identical_url(
    dell_entry_check, original,
):
    service, factory, product_id, canonical, _, _ = dell_entry_check
    with factory.begin() as session:
        session.get(Product, product_id).requested_url = original
    unavailable = await _customizer_entry_failure("missing")
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            raise unavailable

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "failed"
    assert calls == [canonical]


@pytest.mark.anyio
async def test_two_failed_entries_count_once_and_preserve_both_failure_reasons(dell_entry_check):
    service, factory, product_id, canonical, original, _ = dell_entry_check
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            status = 403 if str(url) == original else 404
            raise AcquisitionError(f"HTTP {status}", status_code=status)

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == "failed"
    assert calls == [original, canonical]
    assert outcome.events == []
    with factory() as session:
        assert session.get(Product, product_id).consecutive_failures == 1
        assert session.scalar(select(func.count(CheckRun.id)).where(
            CheckRun.trigger == "scheduled"
        )) == 1
        assert session.scalar(select(func.count(Observation.id))) == 1
        run = session.scalar(select(CheckRun).where(CheckRun.trigger == "scheduled"))
        assert "原始入口 HTTP 403" in run.error_message
        assert "直达入口" in run.error_message
        assert "HTTP 404" in run.error_message
        assert "https://" not in run.error_message


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["sku", "gpu", "currency", "selection"])
async def test_original_entry_cannot_replace_monitored_identity(dell_entry_check, mutation):
    service, factory, product_id, canonical, original, trusted = dell_entry_check
    current = replace(trusted, price=Money("USD", 279999))
    if mutation == "sku":
        current = replace(current, identity=ProductIdentity("dell-us", "other_sku"))
    elif mutation == "currency":
        current = replace(current, price=Money("EUR", 279999))
    else:
        current = replace(current, configuration=ProductConfiguration(
            gpu="RTX 5080", extras={"Graphics Card": "RTX 5080" if mutation == "selection"
                                  else "RTX 5090"},
        ))

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            if str(url) == canonical:
                raise AcquisitionError("HTTP 403", status_code=403)
            assert str(url) == original
            return None, current

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")

    assert outcome.status == ("failed" if mutation == "selection" else "needs_attention")
    with factory() as session:
        trusted_rows = session.scalars(
            select(Observation).where(Observation.trusted.is_(True))
        ).all()
        assert [(row.currency, row.price_minor) for row in trusted_rows] == [("USD", 299999)]


@pytest.mark.anyio
@pytest.mark.parametrize("original,status,expected_entry", [
    ("https://www.dell.com/en-us/shop/spd/alienware", None, "original"),
    ("https://www.dell.com/en-us/shop/spd/alienware", 429, "original"),
    ("https://www.dell.com/en-us/shop/spd/alienware", 500, "original"),
    ("https://www.dell.com/en-us/shop/spd/alienware/sku_01", 403, "canonical"),
    ("https://other.example/en-us/shop/spd/alienware", 403, "canonical"),
    ("https://www.dell.com/en-uk/shop/spd/alienware", 403, "canonical"),
    ("http://www.dell.com/en-us/shop/spd/alienware", 403, "canonical"),
    ("https://www.dell.com:8443/en-us/shop/spd/alienware", 403, "canonical"),
    ("https://user:password@www.dell.com/en-us/shop/spd/alienware", 403, "canonical"),
])
async def test_preferred_entry_and_retry_are_bounded_to_safe_distinct_us_dell_url(
    dell_entry_check, original, status, expected_entry,
):
    service, factory, product_id, canonical, _, _ = dell_entry_check
    with factory.begin() as session:
        session.get(Product, product_id).requested_url = original
    calls = []

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            calls.append(str(url))
            raise AcquisitionError("Unavailable canonical", status_code=status)

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")
    assert outcome.status == "failed"
    assert calls == [original if expected_entry == "original" else canonical]


@pytest.mark.anyio
@pytest.mark.parametrize("last_error,expected_cause", [
    (UnsafeUrlError("The product hostname could not be resolved"), "could not be resolved"),
    (RuntimeError("private-secret-must-not-be-logged"), "RuntimeError"),
])
async def test_second_entry_safety_or_unexpected_error_keeps_safe_diagnostics(
    dell_entry_check, last_error, expected_cause,
):
    service, factory, product_id, canonical, original, _ = dell_entry_check

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            if str(url) == original:
                raise AcquisitionError("HTTP 403", status_code=403)
            assert str(url) == canonical
            raise last_error

    service.pipeline = Pipeline()
    outcome = await service.check_product(product_id, "scheduled")
    assert outcome.status == "failed"
    with factory() as session:
        run = session.scalar(select(CheckRun).where(CheckRun.trigger == "scheduled"))
        assert "原始入口 HTTP 403" in (run.error_message or "")
        assert expected_cause in run.error_message
        assert "private-secret" not in run.error_message
        assert session.get(Product, product_id).consecutive_failures == 1


@pytest.mark.anyio
async def test_both_entry_failures_keep_retry_and_notification_policy(dell_entry_check):
    service, factory, product_id, _, _, _ = dell_entry_check

    class Pipeline:
        async def acquire(self, url, _adapter, dell_selection):
            raise AcquisitionError("HTTP 403", status_code=403)

    service.pipeline = Pipeline()
    first = await service.check_product(product_id, "scheduled")
    assert first.events == []
    second = await service.check_product(product_id, "scheduled")
    assert [event.kind for event in second.events] == ["check_failed"]
    with factory() as session:
        product = session.get(Product, product_id)
        assert product.consecutive_failures == 2
        assert product.failure_reported is True
        alert = session.scalar(select(NotificationDelivery).where(
            NotificationDelivery.event_type == "check_failed"
        ))
        assert "原始入口 HTTP 403" in alert.message_text
        assert "直达入口" in alert.message_text
        assert "$2,999.99" not in alert.message_text


@pytest.mark.anyio
async def test_non_unique_dell_sku_lookup_requires_attention(check_service):
    service, factory, product_id = check_service

    class FailingPipeline:
        async def acquire(self, url, adapter):
            raise RuntimeError("page disappeared")

    async def lookup(sku):
        assert sku == "sku_01"
        return [snapshot(), snapshot(gpu="RTX 5080")]

    service.pipeline = FailingPipeline()
    service.dell_lookup = lookup
    outcome = await service.check_product(product_id, "scheduled")
    assert outcome.product.status == "needs_attention"
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 0


@pytest.mark.anyio
async def test_same_url_cards_recheck_their_own_dell_selection(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    address = "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250"
    with factory.begin() as session:
        cards = []
        for gpu in ("RTX 5070", "RTX 5090"):
            item = Product(
                source_site="dell-us",
                requested_url=address,
                sku="aa18250_reg_01",
                dell_selection={"Graphics Card": gpu},
            )
            session.add(item)
            session.flush()
            cards.append(item.id)

    class ConfiguredPipeline:
        async def acquire(self, url, _adapter, dell_selection=None):
            assert dell_selection is not None
            gpu = dell_selection["Graphics Card"]
            amount = 399999 if gpu == "RTX 5070" else 509999
            return None, ProductSnapshot(
                ProductIdentity("dell-us", "aa18250_reg_01"),
                str(url),
                "Alienware",
                ProductConfiguration(cpu="Core Ultra 9", gpu=gpu, extras={"Graphics Card": gpu}),
                Money("USD", amount),
            )

    service = CheckService(factory, pipeline=ConfiguredPipeline())
    await service.check_product(cards[0], "manual")
    await service.check_product(cards[1], "manual")
    with factory() as session:
        left = session.scalars(select(Observation).where(Observation.product_id == cards[0])).all()
        right = session.scalars(select(Observation).where(Observation.product_id == cards[1])).all()
        assert [row.price_minor for row in left] == [399999]
        assert [row.price_minor for row in right] == [509999]
    engine.dispose()


@pytest.mark.anyio
async def test_wrong_dell_selection_never_overwrites_trusted_price(check_service):
    service, factory, product_id = check_service
    with factory.begin() as session:
        session.get(Product, product_id).dell_selection = {"Graphics Card": "RTX 5090"}
    trusted = ProductSnapshot(
        ProductIdentity("dell-us", "sku_01"),
        "https://www.dell.com/en-us/shop/spd/x/sku_01",
        "Alienware",
        ProductConfiguration(
            cpu="Core Ultra 9", gpu="RTX 5090", extras={"Graphics Card": "RTX 5090"}
        ),
        Money("USD", 509999),
    )
    service.accept_snapshot(product_id, trusted)

    class WrongPipeline:
        async def acquire(self, url, _adapter, dell_selection=None):
            assert dell_selection == {"Graphics Card": "RTX 5090"}
            return None, snapshot(gpu="RTX 5070", amount=399999)

    service.pipeline = WrongPipeline()
    outcome = await service.check_product(product_id, "scheduled")
    assert outcome.status == "failed"
    with factory() as session:
        rows = session.scalars(
            select(Observation).where(Observation.product_id == product_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].price_minor == 509999


@pytest.mark.anyio
async def test_completed_check_collects_transient_parser_cycles(check_service):
    service, _, product_id = check_service
    references = []

    class ParserNode:
        pass

    class CyclicPipeline:
        async def acquire(self, _url, _adapter, dell_selection=None):
            assert dell_selection is None
            node = ParserNode()
            node.parent = node
            references.append(weakref.ref(node))
            return None, snapshot()

    service.pipeline = CyclicPipeline()
    collection_was_enabled = gc.isenabled()
    gc.disable()
    try:
        outcome = await service.check_product(product_id, "manual")
        assert outcome.status == "ok"
        assert references[0]() is None
    finally:
        if collection_was_enabled:
            gc.enable()
        gc.collect()
