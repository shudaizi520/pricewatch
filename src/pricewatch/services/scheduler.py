"""One-process, one-runner schedule with per-product Beijing-local times."""

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import Product, Setting
from pricewatch.services.checks import CheckTrigger

_BEIJING = ZoneInfo("Asia/Shanghai")


def check_time_key(product_id: int) -> str:
    return f"product_check_time:{product_id}"


def parse_check_time(value: str) -> tuple[int, int]:
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("检查时间必须是北京时间 HH:MM")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def next_due(after: datetime, check_time: str) -> datetime:
    hour, minute = parse_check_time(check_time)
    local = after.astimezone(_BEIJING)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class Checker(Protocol):
    async def check_product(self, product_id: int, trigger: CheckTrigger) -> object: ...


@dataclass
class JobReference:
    key: int
    future: asyncio.Future[object]
    manual_requested: bool = False


class SchedulerService:
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
                setting = session.get(Setting, check_time_key(product.id))
                if setting is None or not setting.value_text:
                    product.status = "paused"
                    product.next_check_at = None
                else:
                    product.next_check_at = next_due(now, setting.value_text)
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
            setting = session.get(Setting, check_time_key(product_id))
            product.next_check_at = (
                next_due(datetime.now(UTC), setting.value_text)
                if product.status == "active" and setting is not None and setting.value_text
                else None
            )

    def request_check(self, product_id: int, trigger: CheckTrigger) -> JobReference:
        if self.worker is None:
            raise RuntimeError("Scheduler has not started")
        existing = self.pending.get(product_id)
        if existing is not None:
            if trigger == "manual":
                existing.manual_requested = True
            return existing
        reference = JobReference(
            product_id, asyncio.get_running_loop().create_future(), trigger == "manual"
        )
        self.pending[product_id] = reference
        self.queue.put_nowait((product_id, trigger))
        return reference

    async def run_due(self) -> None:
        now = datetime.now(UTC)
        with self.factory.begin() as session:
            due = session.scalars(
                select(Product).where(Product.status == "active", Product.next_check_at <= now)
            ).all()
            identifiers = []
            for item in due:
                setting = session.get(Setting, check_time_key(item.id))
                if setting is None or not setting.value_text:
                    item.status = "paused"
                    item.next_check_at = None
                else:
                    item.next_check_at = next_due(now, setting.value_text)
                    identifiers.append(item.id)
        for identifier in identifiers:
            self.request_check(identifier, "scheduled")

    async def _run(self) -> None:
        while True:
            product_id, trigger = await self.queue.get()
            job = self.pending[product_id]
            try:
                if trigger == "scheduled" and not job.manual_requested:
                    with self.factory() as session:
                        product = session.get(Product, product_id)
                        if product is None or product.status != "active":
                            job.future.set_result(None)
                            continue
                if self.checker is None:
                    raise RuntimeError("Check service is not configured")
                result = await self.checker.check_product(
                    product_id, "manual" if job.manual_requested else trigger
                )
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
