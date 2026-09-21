from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pricewatch.db.base import Base
from pricewatch.db.models import BackupRecord, CheckRun, NotificationDelivery, Observation, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.services.backups import BackupService
from pricewatch.services.retention import RetentionService


@pytest.fixture
def storage(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    yield engine, factory, settings
    engine.dispose()


def test_sqlite_backup_checksum_and_restore_round_trip(storage):
    engine, factory, settings = storage
    with factory.begin() as session:
        session.add(Product(source_site="dell-us", requested_url="https://www.dell.com/x"))
    service = BackupService(factory, settings.data_dir)
    record = service.create("manual")
    path = settings.data_dir / "backups" / record.filename
    assert service.verify(path).checksum == record.checksum
    with factory.begin() as session:
        session.add(Product(source_site="dell-us", requested_url="https://www.dell.com/y"))
    engine.dispose()
    service.restore(path, record.checksum)
    resumed, resumed_factory = create_engine_and_session(settings)
    with resumed_factory() as session:
        assert len(session.scalars(select(Product)).all()) == 1
    resumed.dispose()


def test_retention_preserves_prices_but_prunes_diagnostics(storage):
    _, factory, _ = storage
    old = datetime.now(UTC) - timedelta(days=150)
    with factory.begin() as session:
        product = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(product)
        session.flush()
        session.add(
            CheckRun(product_id=product.id, trigger="scheduled", outcome="ok", created_at=old)
        )
        session.add(
            NotificationDelivery(
                product_id=product.id,
                event_key="a" * 64,
                event_type="price_changed",
                status="sent",
                created_at=old,
            )
        )
        session.add(
            Observation(
                product_id=product.id,
                price_minor=300000,
                currency="USD",
                configuration_fingerprint="a",
                observed_at=old,
            )
        )
    report = RetentionService(factory).run(datetime.now(UTC))
    assert report.check_runs_deleted == 1
    assert report.deliveries_deleted == 1
    assert report.observations_deleted == 0
    with factory() as session:
        assert len(session.scalars(select(Observation)).all()) == 1


def test_backup_rotation_keeps_recent_daily_and_weekly(storage):
    _, factory, settings = storage
    service = BackupService(factory, settings.data_dir)
    for index in range(12):
        with factory.begin() as session:
            session.add(
                Product(source_site="dell-us", requested_url=f"https://www.dell.com/{index}")
            )
        service.create("daily")
    for index in range(6):
        with factory.begin() as session:
            session.add(
                Product(source_site="dell-us", requested_url=f"https://www.dell.com/weekly/{index}")
            )
        service.create("weekly")
    service.rotate()
    with factory() as session:
        daily = session.scalars(select(BackupRecord).where(BackupRecord.reason == "daily")).all()
        weekly = session.scalars(select(BackupRecord).where(BackupRecord.reason == "weekly")).all()
        assert len(daily) == 7
        assert len(weekly) == 4
