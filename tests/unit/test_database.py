from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pricewatch.config import Settings
from pricewatch.db.base import Base
from pricewatch.db.models import Observation, Product
from pricewatch.db.session import create_engine_and_session


@pytest.fixture
def database(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'database.db'}",
        app_secret_key="database-test-key-that-is-at-least-32-chars",
    )
    engine, session_factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    try:
        yield engine, session_factory
    finally:
        engine.dispose()


def test_sqlite_enables_foreign_keys_and_wal(database):
    engine, _ = database

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"


def test_money_is_stored_as_integer_minor_units(database):
    _, session_factory = database
    with session_factory() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            status="active",
        )
        session.add(product)
        session.flush()
        session.add(
            Observation(
                product_id=product.id,
                currency="USD",
                price_minor=299999,
                trusted=True,
            )
        )
        session.commit()

        assert session.scalar(select(Observation.price_minor)) == 299999


def test_negative_price_is_rejected_by_database(database):
    _, session_factory = database
    with session_factory() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            status="active",
        )
        session.add(product)
        session.flush()
        session.add(Observation(product_id=product.id, currency="USD", price_minor=-1))

        with pytest.raises(IntegrityError):
            session.commit()


def test_dell_selection_survives_database_round_trip(database):
    _, session_factory = database
    with session_factory.begin() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/en-us/shop/model",
            dell_selection={"Graphics Card": "RTX 5090"},
        )
        session.add(product)
        session.flush()
        product_id = product.id
    with session_factory() as session:
        stored = session.get(Product, product_id)
        assert stored is not None
        assert stored.dell_selection == {"Graphics Card": "RTX 5090"}
