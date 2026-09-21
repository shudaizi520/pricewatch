"""Verify persistent storage and migrate before starting the web worker."""

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from pricewatch.config import Settings
from pricewatch.db.session import create_engine_and_session
from pricewatch.services.backups import BackupService


def migrate(settings: Settings, ini: Path = Path("/app/alembic.ini")) -> None:
    database = Path(settings.database_url.removeprefix("sqlite:///"))
    if not settings.database_url.startswith("sqlite:////"):
        raise ValueError("Only persistent absolute-path SQLite databases are supported")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configuration = Config(str(ini))
    configuration.set_main_option("script_location", str(ini.parent / "alembic"))
    configuration.set_main_option("sqlalchemy.url", settings.database_url)
    head = ScriptDirectory.from_config(configuration).get_current_head()
    if database.is_file() and database.stat().st_size > 0:
        with closing(sqlite3.connect(str(database))) as connection:
            query = "SELECT name FROM sqlite_master WHERE type='table'"
            tables = {row[0] for row in connection.execute(query)}
            current = (
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                if "alembic_version" in tables
                else None
            )
        if current is None and "products" in tables:
            raise RuntimeError("Existing database has no migration version; manual review required")
        if current is not None and current != head:
            engine, factory = create_engine_and_session(settings)
            try:
                BackupService(factory, settings.data_dir).create("pre_migration")
            finally:
                engine.dispose()
    command.upgrade(configuration, "head")


if __name__ == "__main__":
    migrate(Settings())  # type: ignore[call-arg]
