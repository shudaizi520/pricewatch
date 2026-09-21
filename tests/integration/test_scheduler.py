import asyncio
from datetime import UTC, datetime

import pytest

from pricewatch.db.base import Base
from pricewatch.db.models import Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.services.scheduler import SchedulerService, next_due


def test_default_beijing_slots_and_jitter():
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)  # 09:00 Beijing
    assert SchedulerService.default_local_hours == (0, 6, 12, 18)
    due = next_due(now, 6, 3)
    assert due.date() == now.date()
    assert due.hour == 4
    assert 0 <= due.second <= 89
    assert next_due(due, 6, 3) > due


@pytest.mark.anyio
async def test_coalesces_manual_and_scheduled_and_runs_sequentially(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingChecker:
        active = 0
        maximum = 0
        calls = 0

        async def check_product(self, product_id, trigger):
            self.calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            entered.set()
            await release.wait()
            self.active -= 1
            return product_id

    checker = BlockingChecker()
    scheduler = SchedulerService(factory, checker)
    scheduler.start()
    try:
        first = scheduler.request_check(7, "scheduled")
        await entered.wait()
        second = scheduler.request_check(7, "manual")
        third = scheduler.request_check(8, "manual")
        assert first.key == second.key
        release.set()
        assert await first.future == 7
        assert await third.future == 8
        assert checker.calls == 2
        assert checker.maximum == 1
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_restart_discards_stale_due_time(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            next_check_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        identifier = item.id
    scheduler = SchedulerService(factory, None)
    scheduler.start()
    try:
        with factory() as session:
            refreshed = session.get(Product, identifier)
            assert refreshed is not None
            assert refreshed.next_check_at.replace(tzinfo=UTC) > datetime.now(UTC)
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_restart_realigns_old_future_slot_to_midnight(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            next_check_at=datetime(2026, 12, 31, 20, 0, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        identifier = item.id
    scheduler = SchedulerService(factory, None)
    scheduler.start()
    try:
        with factory() as session:
            refreshed = session.get(Product, identifier)
            assert refreshed is not None
            expected = next_due(datetime.now(UTC), 6, identifier)
            assert refreshed.next_check_at.replace(tzinfo=UTC) == expected
    finally:
        await scheduler.stop()
        engine.dispose()
