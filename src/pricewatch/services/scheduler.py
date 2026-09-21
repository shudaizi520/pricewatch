"""One-process, one-runner schedule with Beijing-local six-hour defaults."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import Product
from pricewatch.services.checks import CheckTrigger

_BEIJING = ZoneInfo("Asia/Shanghai")


def next_due(after: datetime, hours: int, product_id: int) -> datetime:
    if hours not in (1, 3, 6, 12, 24):
        raise ValueError("Invalid check interval")
    local = after.astimezone(_BEIJING)
    # Local midnight is the common anchor for every approved interval.
    for offset in range(0, 48):
        candidate = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=offset
        )
        if candidate.hour % hours != 0:
            continue
        due = (candidate + timedelta(seconds=product_id % 90)).astimezone(UTC)
        if due > after:
            return due
    raise RuntimeError("Could not find a future check slot")


class Checker(Protocol):
    async def check_product(self, product_id: int, trigger: CheckTrigger) -> object: ...


@dataclass(frozen=True)
class JobReference:
    key: int
    future: asyncio.Future[object]


class SchedulerService:
    default_local_hours = (0, 6, 12, 18)

    def __init__(self, factory: sessionmaker[Session], checker: Checker | None) -> None:
        self.factory = factory
        self.checker = checker
        self.scheduler = AsyncIOScheduler(timezone=_BEIJING)
        self.pending: dict[int, JobReference] = {}
        self.queue: asyncio.Queue[tuple[int, CheckTrigger]] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.worker is not None:
            return
        now = datetime.now(UTC)
        with self.factory.begin() as session:
            for product in session.scalars(select(Product).where(Product.status == "active")):
                product.next_check_at = next_due(now, product.check_interval_hours, product.id)
        self.worker = asyncio.create_task(self._run())
        self.scheduler.add_job(self.run_due, "interval", seconds=30, id="due", max_instances=1)
        self.scheduler.start()

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.worker is not None:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None
        for job in self.pending.values():
            if not job.future.done():
                job.future.cancel()
        self.pending.clear()

    def schedule_product(self, product_id: int) -> None:
        with self.factory.begin() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise LookupError("Product not found")
            product.next_check_at = next_due(
                datetime.now(UTC), product.check_interval_hours, product_id
            )

    def request_check(self, product_id: int, trigger: CheckTrigger) -> JobReference:
        if self.worker is None:
            raise RuntimeError("Scheduler has not started")
        existing = self.pending.get(product_id)
        if existing is not None:
            return existing
        reference = JobReference(product_id, asyncio.get_running_loop().create_future())
        self.pending[product_id] = reference
        self.queue.put_nowait((product_id, trigger))
        return reference

    async def run_due(self) -> None:
        now = datetime.now(UTC)
        with self.factory.begin() as session:
            due = session.scalars(
                select(Product).where(Product.status == "active", Product.next_check_at <= now)
            ).all()
            identifiers = [item.id for item in due]
            for item in due:
                item.next_check_at = next_due(now, item.check_interval_hours, item.id)
        for identifier in identifiers:
            self.request_check(identifier, "scheduled")

    async def _run(self) -> None:
        while True:
            product_id, trigger = await self.queue.get()
            job = self.pending[product_id]
            try:
                if self.checker is None:
                    raise RuntimeError("Check service is not configured")
                result = await self.checker.check_product(product_id, trigger)
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not job.future.done():
                    job.future.set_exception(error)
            finally:
                self.pending.pop(product_id, None)
                self.queue.task_done()
