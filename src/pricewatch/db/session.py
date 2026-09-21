"""SQLite engine and session construction."""

import sqlite3
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.config import Settings


def _configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_engine_and_session(
    settings: Settings,
) -> tuple[Engine, sessionmaker[Session]]:
    """Create a SQLite engine with safety pragmas and its session factory."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
    if settings.database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a transaction-scoped session and roll back failures."""

    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
