"""Deduplicated Feishu delivery through Apprise."""

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from apprise import Apprise
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import NotificationDelivery, Product
from pricewatch.domain.events import DomainEvent
from pricewatch.domain.products import ProductConfiguration

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


class FeishuSignedTransport:
    """Use the official robot signing fields when signature verification is enabled."""

    def __init__(self, webhook: str, secret: SecretStr) -> None:
        token_url = feishu_apprise_url(webhook)
        token = token_url.removeprefix("feishu://")
        self.webhook = (
            webhook
            if webhook.startswith("https://")
            else f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
        )
        self.secret = secret

    def send(self, destination: str, title: str, body: str) -> bool:
        timestamp = str(int(time.time()))
        key = f"{timestamp}\n{self.secret.get_secret_value()}".encode()
        signature = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
        try:
            response = httpx.post(
                self.webhook,
                json={
                    "timestamp": timestamp,
                    "sign": signature,
                    "msg_type": "text",
                    "content": {"text": f"{title}\n{body}"},
                },
                timeout=10,
                follow_redirects=False,
            )
            return response.status_code == 200 and response.json().get("code") == 0
        except (httpx.HTTPError, ValueError, TypeError):
            return False


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    status: str


def _message_configuration(record: dict[str, object] | None) -> list[str]:
    if not record:
        return ["配置待确认"]
    config = ProductConfiguration.from_record(record)
    parts = [
        f"{label}: {value}"
        for label, value in (
            ("处理器", config.cpu),
            ("显卡", config.gpu),
            ("内存", config.memory),
            ("存储", config.storage),
            ("屏幕", config.display),
            ("系统", config.os),
        )
        if value
    ]
    keyboard = config.extras.get("Keyboard")
    if keyboard:
        parts.append(f"键盘: {keyboard}")
    if parts:
        return parts
    if config.extras:
        return ["配置待确认"]
    summary = record.get("summary")
    return [f"配置: {summary}"] if isinstance(summary, str) and summary.strip() else ["配置待确认"]


def format_message(event: DomainEvent, product: Product) -> str:
    name = product.name or "监控商品"
    config = _message_configuration(product.configuration)
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
    lines = [f"{name} · {labels.get(event.kind, event.kind)}", "", *config, ""]
    previous = event.old.get("price_minor")
    current = event.new.get("price_minor")
    currency = event.new.get("currency") or "USD"
    price_prefix = "$" if currency == "USD" else f"{currency} "
    if isinstance(current, int):
        price = f"{price_prefix}{current / 100:,.2f}"
        if isinstance(previous, int):
            delta = current - previous
            lines.append(
                f"价格: {price_prefix}{previous / 100:,.2f} → {price}"
                f" ({delta / 100:+,.2f} {currency})"
            )
        else:
            lines.append(f"价格: {price}")
        if (
            product.notify_mode == "target_or_change"
            and product.target_price_minor is not None
            and current <= product.target_price_minor
            and (not isinstance(previous, int) or previous > product.target_price_minor)
        ):
            lines.append("目标价已达到")
    stock = event.new.get("availability")
    if stock:
        old_stock = event.old.get("availability")
        lines.append(f"库存: {old_stock} → {stock}" if old_stock else f"库存: {stock}")
    if event.kind == "offer_changed":
        for key, label in (("coupon", "优惠码"), ("discount", "优惠")):
            old_value, new_value = event.old.get(key), event.new.get(key)
            if old_value != new_value:
                lines.append(f"{label}: {old_value or '无'} → {new_value or '无'}")
    if event.kind == "configuration_changed":
        old_description = event.old.get("summary") or event.old.get("name") or event.old.get("sku")
        new_description = event.new.get("summary") or event.new.get("name") or event.new.get("sku")
        if old_description or new_description:
            lines.append(f"原配置: {old_description or '未知'}")
            lines.append(f"新配置: {new_description or '未知'}")
    lines.extend(
        [f"北京时间: {time}", f"商品链接: {product.canonical_url or product.requested_url}"]
    )
    return "\n".join(lines)


class NotificationService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        webhook: SecretStr,
        transport: NotificationTransport | None = None,
        signing_secret: SecretStr | None = None,
    ) -> None:
        self.factory = factory
        self.destination = feishu_apprise_url(webhook.get_secret_value())
        self.transport = transport or (
            FeishuSignedTransport(webhook.get_secret_value(), signing_secret)
            if signing_secret is not None
            else AppriseTransport()
        )

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
                    NotificationDelivery.status.in_(("failed", "pending")),
                    NotificationDelivery.attempts < 3,
                )
            ).all()
        delivered = 0
        for identifier in identifiers:
            with self.factory.begin() as session:
                row = session.get(NotificationDelivery, identifier)
                if row is None or row.status not in ("failed", "pending") or not row.message_text:
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
