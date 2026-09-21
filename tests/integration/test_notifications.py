from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from pricewatch.db.base import Base
from pricewatch.db.models import NotificationDelivery, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.domain.events import DomainEvent
from pricewatch.services.notifications import NotificationService, feishu_apprise_url


class FakeTransport:
    def __init__(self, outcomes=(True,)):
        self.calls = 0
        self.outcomes = iter(outcomes)
        self.messages = []

    def send(self, destination, title, body):
        self.calls += 1
        self.messages.append(body)
        return next(self.outcomes)


@pytest.fixture
def environment(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            name="Alienware",
            configuration={"summary": "RTX 5090"},
        )
        session.add(product)
        session.flush()
        product_id = product.id
    yield factory, product_id
    engine.dispose()


def price_event(product_id):
    return DomainEvent(
        product_id,
        "price_changed",
        datetime(2026, 9, 21, 2, tzinfo=UTC),
        {"price_minor": 300000},
        {"price_minor": 290000},
    )


def test_feishu_url_validates_host_and_token():
    token = "0123456789abcdef0123456789abcdef"
    assert (
        feishu_apprise_url(f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}")
        == f"feishu://{token}"
    )
    with pytest.raises(ValueError):
        feishu_apprise_url(f"https://example.org/open-apis/bot/v2/hook/{token}")


def test_manual_test_redacts_webhook(environment, caplog):
    factory, _ = environment
    transport = FakeTransport()
    service = NotificationService(
        factory,
        SecretStr("https://open.feishu.cn/open-apis/bot/v2/hook/0123456789abcdef0123456789abcdef"),
        transport,
    )
    assert service.test_feishu().sent
    assert "0123456789abcdef" not in caplog.text


def test_duplicate_event_is_not_sent_twice(environment):
    factory, product_id = environment
    transport = FakeTransport()
    service = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), transport
    )
    event = price_event(product_id)
    assert service.deliver(event).sent
    assert not service.deliver(event).sent
    assert transport.calls == 1
    assert "¥" not in transport.messages[0]
    assert "$2,900.00" in transport.messages[0]
    assert "RTX 5090" in transport.messages[0]
    assert "10:00" in transport.messages[0]
    with factory() as session:
        assert len(session.scalars(select(NotificationDelivery)).all()) == 1


def test_failed_delivery_retries_without_new_logical_event(environment):
    factory, product_id = environment
    transport = FakeTransport((False, True))
    service = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), transport
    )
    event = price_event(product_id)
    assert not service.deliver(event).sent
    assert service.deliver(event).sent
    assert transport.calls == 2
    with factory() as session:
        rows = session.scalars(select(NotificationDelivery)).all()
        assert len(rows) == 1
        assert rows[0].attempts == 2
        assert rows[0].status == "sent"


def test_failed_delivery_can_retry_after_restart(environment):
    factory, product_id = environment
    failed = FakeTransport((False,))
    first = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), failed
    )
    first.deliver(price_event(product_id))
    successful = FakeTransport((True,))
    restarted = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), successful
    )
    assert restarted.retry_failed() == 1
    assert "$2,900.00" in successful.messages[0]
    assert restarted.retry_failed() == 0


def test_interrupted_pending_delivery_is_retried(environment):
    factory, product_id = environment
    event = price_event(product_id)
    with factory.begin() as session:
        session.add(
            NotificationDelivery(
                product_id=product_id,
                event_key=event.key(),
                event_type=event.kind,
                status="pending",
                attempts=1,
                message_text="之前排队的通知",
            )
        )
    transport = FakeTransport((True,))
    service = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), transport
    )
    assert service.retry_failed() == 1
    assert transport.messages == ["之前排队的通知"]


def test_target_price_is_called_out_in_existing_change_message(environment):
    factory, product_id = environment
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.target_price_minor = 295000
        product.notify_mode = "target_or_change"
    transport = FakeTransport()
    service = NotificationService(
        factory, SecretStr("feishu://0123456789abcdef0123456789abcdef"), transport
    )
    service.deliver(price_event(product_id))
    assert "目标价已达到" in transport.messages[0]
    assert transport.calls == 1


def test_offer_and_stock_messages_show_what_changed(environment):
    factory, product_id = environment
    with factory() as session:
        product = session.get(Product, product_id)
        offer = DomainEvent(
            product_id,
            "offer_changed",
            datetime(2026, 9, 21, 2, tzinfo=UTC),
            {"coupon": None, "discount": "Save $100"},
            {"coupon": "SAVE10", "discount": "Save $200"},
        )
        stock = DomainEvent(
            product_id,
            "stock_changed",
            datetime(2026, 9, 21, 3, tzinfo=UTC),
            {"availability": "out_of_stock"},
            {"availability": "in_stock"},
        )
        from pricewatch.services.notifications import format_message

        assert "SAVE10" in format_message(offer, product)
        assert "Save $100" in format_message(offer, product)
        assert "out_of_stock → in_stock" in format_message(stock, product)


def test_non_usd_price_is_not_labeled_as_dollars(environment):
    factory, product_id = environment
    with factory() as session:
        product = session.get(Product, product_id)
        event = DomainEvent(
            product_id,
            "price_changed",
            datetime(2026, 9, 21, 2, tzinfo=UTC),
            {"price_minor": 300000},
            {"price_minor": 290000, "currency": "EUR"},
        )
        from pricewatch.services.notifications import format_message

        assert "EUR 2,900.00" in format_message(event, product)
