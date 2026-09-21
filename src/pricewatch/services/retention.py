"""Bounded diagnostics retention; product price history is indefinite."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import CheckRun, NotificationDelivery


@dataclass(frozen=True)
class RetentionReport:
    check_runs_deleted: int
    deliveries_deleted: int
    observations_deleted: int = 0


class RetentionService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def run(self, now: datetime) -> RetentionReport:
        with self.factory.begin() as session:
            runs = session.execute(
                delete(CheckRun).where(CheckRun.created_at < now - timedelta(days=30))
            )
            sent = session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.created_at < now - timedelta(days=90),
                    NotificationDelivery.status == "sent",
                )
            )
            return RetentionReport(
                getattr(runs, "rowcount", 0) or 0, getattr(sent, "rowcount", 0) or 0
            )
