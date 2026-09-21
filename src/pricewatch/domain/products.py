"""Immutable product identities, configurations and prices."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^\w]", "", value.casefold(), flags=re.UNICODE)


_NON_PRICE_EXTRA_GROUPS = frozenset(
    _normalize(name)
    for name in (
        "Operating System Languages",
        "Wireless",
        "Primary Battery",
        "Power Supply",
        "Documentation",
        "Power Cord",
        "Camera",
        "Keep - specific to Alienware",
        "Base Warranty",
    )
)


@dataclass(frozen=True, slots=True)
class Money:
    currency: str
    minor: int

    def __post_init__(self) -> None:
        if type(self.minor) is not int or self.minor < 0:
            raise TypeError("minor must be a non-negative integer")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be a three-letter ISO code")

    @classmethod
    def from_decimal(cls, currency: str, amount: str | Decimal) -> "Money":
        decimal = Decimal(amount)
        if (
            not decimal.is_finite()
            or decimal < 0
            or decimal * 100 != (decimal * 100).to_integral_value()
        ):
            raise ValueError("invalid money amount")
        return cls(currency, int(decimal * 100))


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    site: str
    sku: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ProductConfiguration:
    cpu: str | None = None
    gpu: str | None = None
    memory: str | None = None
    storage: str | None = None
    display: str | None = None
    os: str | None = None
    extras: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ProductConfiguration":
        def value(key: str) -> str | None:
            raw = record.get(key)
            return raw if isinstance(raw, str) else None

        raw_extras = record.get("extras")
        extras = (
            {
                key: item
                for key, item in raw_extras.items()
                if isinstance(key, str) and isinstance(item, str)
            }
            if isinstance(raw_extras, dict)
            else {}
        )
        return cls(
            cpu=value("cpu"),
            gpu=value("gpu"),
            memory=value("memory"),
            storage=value("storage"),
            display=value("display"),
            os=value("os"),
            extras=extras,
        )

    def fingerprint(self) -> str:
        parts: dict[str, object] = {
            key: _normalize(getattr(self, key))
            for key in ("cpu", "gpu", "memory", "storage", "display", "os")
        }
        parts["extras"] = {
            key: _normalize(value)
            for key, value in sorted(self.extras.items())
            if _normalize(key) not in _NON_PRICE_EXTRA_GROUPS
        }
        payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def summary(self) -> str:
        parts = [
            value
            for value in (self.cpu, self.gpu, self.memory, self.storage, self.display)
            if value
        ]
        if keyboard := self.extras.get("Keyboard"):
            parts.append(f"键盘: {keyboard}")
        return " · ".join(parts)

    def as_record(self) -> dict[str, object]:
        fields = ("cpu", "gpu", "memory", "storage", "display", "os")
        record: dict[str, object] = {key: value for key in fields if (value := getattr(self, key))}
        record["extras"] = self.extras
        record["summary"] = self.summary()
        return record


@dataclass(frozen=True, slots=True)
class ProductSnapshot:
    identity: ProductIdentity
    canonical_url: str
    name: str
    configuration: ProductConfiguration
    price: Money
    list_price: Money | None = None
    discount_text: str | None = None
    coupon_text: str | None = None
    availability: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: Decimal = Decimal("1")
