"""Deduplicated Feishu delivery through Apprise."""

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from apprise import Apprise
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import NotificationDelivery, Product
from pricewatch.domain.events import DomainEvent

_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
_FEISHU_HOSTS = {"open.feishu.cn", "open.larksuite.com"}


def feishu_apprise_url(webhook: str) -> str:
    """Accept only the official webhook origin or an Apprise Feishu token URL."""
    parsed = urlsplit(webhook)
    if parsed.scheme == "feishu" and parsed.netloc and not parsed.path and not parsed.query:
        token = parsed.netloc
    elif (
        parsed.scheme == "https"
        and parsed.hostname in _FEISHU_HOSTS
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path.startswith("/open-apis/bot/v2/hook/")
    ):
        token = parsed.path.removeprefix("/open-apis/bot/v2/hook/")
    else:
        raise ValueError("请输入有效的飞书机器人 Webhook")
    if not _TOKEN.fullmatch(token):
        raise ValueError("飞书机器人 Webhook 格式不正确")
    return f"feishu://{token}"


class NotificationTransport(Protocol):
    def send(self, destination: str, title: str, body: str) -> bool: ...


class AppriseTransport:
    def send(self, destination: str, title: str, body: str) -> bool:
        notifier = Apprise()
        if not notifier.add(destination):
            return False
        return bool(notifier.notify(title=title, body=body))


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    status: str


def format_message(event: DomainEvent, product: Product) -> str:
    name = product.name or "监控商品"
    config = (product.configuration or {}).get("summary", "配置待确认")
    time = event.observed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    labels = {
        "initial_observation": "首次价格",
        "price_changed": "价格变化",
        "offer_changed": "优惠变化",
        "stock_changed": "库存变化",
        "configuration_changed": "配置变化 请确认",
        "check_failed": "连续检查失败",
        "check_recovered": "检查已恢复",
    }
    lines = [f"{name} · {labels.get(event.kind, event.kind)}", str(config)]
    previous = event.old.get("price_minor")
    current = event.new.get("price_minor")
    if isinstance(current, int):
        price = f"${current / 100:,.2f}"
        if isinstance(previous, int):
            delta = current - previous
            lines.append(f"${previous / 100:,.2f} → {price} ({delta / 100:+,.2f} USD)")
        else:
            lines.append(price)
    stock = event.new.get("availability")
    if stock:
        lines.append(f"库存: {stock}")
    lines.extend([f"北京时间 {time}", product.canonical_url or product.requested_url])
    return "\n".join(lines)


class NotificationService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        webhook: SecretStr,
        transport: NotificationTransport | None = None,
    ) -> None:
        self.factory = factory
        self.destination = feishu_apprise_url(webhook.get_secret_value())
        self.transport = transport or AppriseTransport()

    def test_feishu(self, webhook: SecretStr | None = None) -> DeliveryResult:
        target = feishu_apprise_url(webhook.get_secret_value()) if webhook else self.destination
        try:
            sent = self.transport.send(target, "PriceWatch 测试", "飞书通知已连接。")
        except Exception:
            return DeliveryResult(False, "failed")
        return DeliveryResult(sent, "sent" if sent else "failed")

    def deliver(self, event: DomainEvent) -> DeliveryResult:
        key = event.key()
        with self.factory.begin() as session:
            row = session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.event_key == key)
            )
            product = session.get(Product, event.product_id)
            if product is None or not product.notifications_enabled:
                return DeliveryResult(False, "disabled")
            if row is not None and (row.status == "sent" or row.attempts >= 3):
                return DeliveryResult(False, row.status)
            if row is None:
                row = NotificationDelivery(
                    product_id=event.product_id, event_key=key, event_type=event.kind
                )
                session.add(row)
            row.attempts = (row.attempts or 0) + 1
            row.status = "pending"
            message = format_message(event, product)
            row.message_text = message
        try:
            sent = self.transport.send(self.destination, "PriceWatch", message)
        except Exception:
            sent = False
        with self.factory.begin() as session:
            row = session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.event_key == key)
            )
            if row is not None:
                row.status = "sent" if sent else "failed"
                row.last_error = None if sent else "通知发送失败 请检查飞书配置或网络"
        return DeliveryResult(sent, "sent" if sent else "failed")

    def retry_failed(self) -> int:
        """Retry prior messages without creating a new logical event."""
        with self.factory() as session:
            identifiers = session.scalars(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.status == "failed", NotificationDelivery.attempts < 3
                )
            ).all()
        delivered = 0
        for identifier in identifiers:
            with self.factory.begin() as session:
                row = session.get(NotificationDelivery, identifier)
                if row is None or row.status != "failed" or not row.message_text:
                    continue
                row.attempts += 1
                row.status = "pending"
                message = row.message_text
            try:
                sent = self.transport.send(self.destination, "PriceWatch", message)
            except Exception:
                sent = False
            with self.factory.begin() as session:
                row = session.get(NotificationDelivery, identifier)
                if row is not None:
                    row.status = "sent" if sent else "failed"
                    row.last_error = None if sent else "通知发送失败 请检查飞书配置或网络"
            delivered += int(sent)
        return delivered
