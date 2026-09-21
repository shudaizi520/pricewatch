"""Acquired HTML response type."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AcquiredPage:
    requested_url: str
    final_url: str
    status: int
    body: bytes
    headers: dict[str, str]
    method: str
    fetched_at: datetime

    @property
    def html(self) -> str:
        return self.body.decode("utf-8", errors="replace")
