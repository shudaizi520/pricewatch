import gc
import weakref
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from pricewatch.db.base import Base
from pricewatch.db.models import NotificationDelivery, Observation, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
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


def test_redirect_updates_canonical_url_only_when_identity_matches(check_service):
    service, _, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    outcome = service.accept_snapshot(
        product_id, snapshot(url="https://www.dell.com/en-us/new/sku_01")
    )
    assert outcome.product.canonical_url.endswith("/sku_01")


def test_failure_alerts_once_at_three_and_recovers_once(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    assert service.record_failure(product_id, "blocked").events == []
    assert service.record_failure(product_id, "blocked").events == []
    assert [event.kind for event in service.record_failure(product_id, "blocked").events] == [
        "check_failed"
    ]
    assert service.record_failure(product_id, "blocked").events == []
    assert [event.kind for event in service.accept_snapshot(product_id, snapshot()).events] == [
        "check_recovered"
    ]
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1


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
