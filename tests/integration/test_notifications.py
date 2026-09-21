from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from pricewatch.db.base import Base
from pricewatch.db.models import NotificationDelivery, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.domain.events import DomainEvent
from pricewatch.services.notifications import NotificationService, feishu_apprise_url


def test_signed_robot_payload_uses_feishu_hmac(monkeypatch, environment):
    import base64
    import hashlib
    import hmac

    from pricewatch.services.notifications import FeishuSignedTransport

    factory, _ = environment
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"code": 0}

    def fake_post(url, *, json, timeout, follow_redirects):
        captured.update(url=url, payload=json)
        return Response()

    monkeypatch.setattr("pricewatch.services.notifications.httpx.post", fake_post)
    monkeypatch.setattr("pricewatch.services.notifications.time.time", lambda: 1700000000)
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/0123456789abcdef0123456789abcdef"
    service = NotificationService(
        factory, SecretStr(webhook), signing_secret=SecretStr("private-sign-key")
    )
    assert isinstance(service.transport, FeishuSignedTransport)
    assert service.test_feishu().sent
    assert captured["url"] == webhook
    payload = captured["payload"]
    assert payload["timestamp"] == "1700000000"
    expected = base64.b64encode(
        hmac.new(b"1700000000\nprivate-sign-key", b"", hashlib.sha256).digest()
    ).decode()
    assert payload["sign"] == expected
    assert payload["msg_type"] == "text"
    assert "飞书通知已连接" in payload["content"]["text"]


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


def test_feishu_price_message_omits_folded_options_but_identifies_keyboard(environment):
    from pricewatch.services.notifications import format_message

    factory, product_id = environment
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.configuration = {
            "gpu": "RTX 5090",
            "summary": "RTX 5090 · Killer Wi-Fi 7 · 360W power",
            "extras": {
                "Keyboard": "CherryMX",
                "Wireless": "Killer Wi-Fi 7",
                "Power Supply": "360W power",
                "Documentation": "No Documentation",
            },
        }
    with factory() as session:
        product = session.get(Product, product_id)
        body = format_message(price_event(product_id), product)
    assert "RTX 5090" in body
    assert "CherryMX" in body
    assert "Killer Wi-Fi 7" not in body
    assert "360W power" not in body
    assert "No Documentation" not in body


def test_feishu_price_message_lists_price_relevant_configuration_one_per_line(environment):
    from pricewatch.services.notifications import format_message

    factory, product_id = environment
    with factory.begin() as session:
        product = session.get(Product, product_id)
        product.configuration = {
            "cpu": "Core Ultra 9",
            "gpu": "RTX 5090",
            "memory": "64GB DDR5",
            "storage": "2TB SSD",
            "display": '18" WQXGA',
            "os": "Windows 11 Home",
            "extras": {
                "Keyboard": "CherryMX RGB",
                "Wireless": "Wi-Fi 7",
                "Documentation": "No Documentation",
            },
        }
    with factory() as session:
        body = format_message(price_event(product_id), session.get(Product, product_id))

    lines = body.splitlines()
    for line in (
        "处理器: Core Ultra 9",
        "显卡: RTX 5090",
        "内存: 64GB DDR5",
        "存储: 2TB SSD",
        '屏幕: 18" WQXGA',
        "系统: Windows 11 Home",
        "键盘: CherryMX RGB",
    ):
        assert line in lines
    assert any(line.startswith("价格: $3,000.00 → $2,900.00") for line in lines)
    assert "北京时间: 2026-09-21 10:00" in lines
    assert "商品链接: https://www.dell.com/x" in lines
    assert "Wi-Fi 7" not in body
    assert "No Documentation" not in body
    assert " · Core Ultra 9" not in body


def test_feishu_initial_price_has_a_separate_labeled_price_line(environment):
    from pricewatch.services.notifications import format_message

    factory, product_id = environment
    event = DomainEvent(
        product_id,
        "initial_observation",
        datetime(2026, 9, 21, 2, tzinfo=UTC),
        {},
        {"price_minor": 674999},
    )
    with factory() as session:
        body = format_message(event, session.get(Product, product_id))

    lines = body.splitlines()
    assert "配置: RTX 5090" in lines
    assert "价格: $6,749.99" in lines
    assert "北京时间: 2026-09-21 10:00" in lines


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
