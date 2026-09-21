"""Typed product state-change events."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DomainEvent:
    product_id: int
    kind: str
    observed_at: datetime
    old: dict[str, Any]
    new: dict[str, Any]

    def key(self) -> str:
        payload = {
            "product_id": self.product_id,
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat(),
            "old": self.old,
            "new": self.new,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
