import asyncio
from datetime import UTC, datetime

import pytest

from pricewatch.db.base import Base
from pricewatch.db.models import CheckRun, Product, Setting
from pricewatch.db.session import create_engine_and_session
from pricewatch.services.scheduler import SchedulerService, next_due


def test_each_product_uses_its_own_beijing_time():
    before = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)  # 09:00 Beijing
    assert next_due(before, "09:30") == datetime(2026, 9, 21, 1, 30, tzinfo=UTC)
    assert next_due(before, "14:15") == datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
    assert next_due(datetime(2026, 9, 21, 1, 30, tzinfo=UTC), "09:30") == datetime(
        2026, 9, 22, 1, 30, tzinfo=UTC
    )


@pytest.mark.anyio
async def test_coalesces_manual_and_scheduled_and_runs_sequentially(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        session.add(Product(id=7, source_site="dell-us", requested_url="https://www.dell.com/x"))
        session.add(Setting(key="product_check_time:7", value_text="08:00"))
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
async def test_turning_auto_off_skips_a_scheduled_check_waiting_in_queue(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingChecker:
        def __init__(self):
            self.calls = []

        async def check_product(self, product_id, trigger):
            self.calls.append((product_id, trigger))
            if product_id == 99:
                entered.set()
                await release.wait()
            return product_id

    with factory.begin() as session:
        item = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(item)
        session.flush()
        identifier = item.id
        session.add(Setting(key=f"product_check_time:{identifier}", value_text="08:00"))
    checker = BlockingChecker()
    scheduler = SchedulerService(factory, checker)
    scheduler.start()
    try:
        blocker = scheduler.request_check(99, "manual")
        await entered.wait()
        scheduled = scheduler.request_check(identifier, "scheduled")
        with factory.begin() as session:
            session.get(Product, identifier).status = "paused"
        release.set()
        await blocker.future
        assert await asyncio.wait_for(scheduled.future, 1) is None
        assert checker.calls == [(99, "manual")]
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_manual_click_still_runs_when_it_coalesces_with_disabled_scheduled_check(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingChecker:
        def __init__(self):
            self.calls = []

        async def check_product(self, product_id, trigger):
            self.calls.append((product_id, trigger))
            if product_id == 99:
                entered.set()
                await release.wait()
            return product_id

    with factory.begin() as session:
        item = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(item)
        session.flush()
        identifier = item.id
        session.add(Setting(key=f"product_check_time:{identifier}", value_text="08:00"))
    checker = BlockingChecker()
    scheduler = SchedulerService(factory, checker)
    scheduler.start()
    try:
        blocker = scheduler.request_check(99, "manual")
        await entered.wait()
        scheduled = scheduler.request_check(identifier, "scheduled")
        with factory.begin() as session:
            session.get(Product, identifier).status = "paused"
        manual = scheduler.request_check(identifier, "manual")
        assert manual is scheduled
        release.set()
        await blocker.future
        assert await asyncio.wait_for(manual.future, 1) == identifier
        assert checker.calls == [(99, "manual"), (identifier, "manual")]
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_unexpected_worker_error_records_failed_scheduled_check(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(item)
        session.flush()
        product_id = item.id
        session.add(Setting(key=f"product_check_time:{product_id}", value_text="08:00"))
        session.add(CheckRun(product_id=product_id, trigger="initial", outcome="ok"))

    class BrokenChecker:
        async def check_product(self, product_id, trigger):
            raise RuntimeError("unexpected internal failure")

    scheduler = SchedulerService(factory, BrokenChecker())
    scheduler.start()
    try:
        job = scheduler.request_check(product_id, "scheduled")
        with pytest.raises(RuntimeError, match="unexpected internal failure"):
            await asyncio.wait_for(job.future, 1)
        with factory() as session:
            runs = (
                session.query(CheckRun)
                .filter_by(product_id=product_id)
                .order_by(CheckRun.id)
                .all()
            )
            product = session.get(Product, product_id)
            assert [(run.trigger, run.outcome) for run in runs] == [
                ("initial", "ok"),
                ("scheduled", "failed"),
            ]
            assert runs[-1].error_category == "RuntimeError"
            assert runs[-1].error_message is None
            assert product is not None and product.last_checked_at is not None
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_worker_does_not_duplicate_run_if_checker_recorded_one_before_error(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(item)
        session.flush()
        product_id = item.id

    class PartlyCompletedChecker:
        async def check_product(self, product_id, trigger):
            with factory.begin() as session:
                session.add(CheckRun(product_id=product_id, trigger=trigger, outcome="ok"))
            raise RuntimeError("notification failed after price was stored")

    scheduler = SchedulerService(factory, PartlyCompletedChecker())
    scheduler.start()
    try:
        job = scheduler.request_check(product_id, "manual")
        with pytest.raises(RuntimeError, match="notification failed"):
            await asyncio.wait_for(job.future, 1)
        with factory() as session:
            runs = session.query(CheckRun).filter_by(product_id=product_id).all()
            assert [(run.trigger, run.outcome) for run in runs] == [("manual", "ok")]
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_restart_disables_legacy_product_without_a_chosen_time(settings):
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
            assert refreshed.status == "paused"
            assert refreshed.next_check_at is None
    finally:
        await scheduler.stop()
        engine.dispose()


@pytest.mark.anyio
async def test_restart_schedules_only_product_with_selected_time(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        item = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            check_interval_hours=6,
            next_check_at=datetime(2026, 12, 31, 20, 0, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        identifier = item.id
        session.add(Setting(key=f"product_check_time:{identifier}", value_text="16:20"))
    scheduler = SchedulerService(factory, None)
    scheduler.start()
    try:
        with factory() as session:
            refreshed = session.get(Product, identifier)
            assert refreshed is not None
            expected = next_due(datetime.now(UTC), "16:20")
            assert refreshed.next_check_at.replace(tzinfo=UTC) == expected
            assert refreshed.status == "active"
    finally:
        await scheduler.stop()
        engine.dispose()
