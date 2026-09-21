"""Deployment-file contracts independent of the local Docker daemon."""

import sqlite3
from contextlib import closing
from pathlib import Path

import yaml
from alembic.config import Config

from alembic import command
from pricewatch.bootstrap import migrate
from pricewatch.config import Settings

ROOT = Path(__file__).parents[2]


def test_compose_single_service_persistent_data_and_healthcheck():
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert set(config["services"]) == {"pricewatch"}
    service = config["services"]["pricewatch"]
    assert any(str(volume).endswith(":/data") for volume in service["volumes"])
    assert "http://127.0.0.1:8080/healthz" in str(service["healthcheck"]["test"])
    assert service["restart"] == "unless-stopped"


def test_image_non_root_one_worker_and_graphical_chromium():
    dockerfile = (ROOT / "Dockerfile").read_text()
    startup = (ROOT / "docker/start.sh").read_text()
    assert "USER 10001:10001" in dockerfile
    assert "playwright install chromium" in dockerfile
    assert 'CMD ["sh", "/app/docker/start.sh"]' in dockerfile
    assert "Xvfb :99" in startup
    assert "export DISPLAY=:99" in startup
    assert "exec uvicorn" in startup
    assert "--workers 1" in startup
    assert "python -m pricewatch.bootstrap" in startup


def test_compose_requires_secret():
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    value = config["services"]["pricewatch"]["environment"]["PRICEWATCH_APP_SECRET_KEY"]
    assert ":?" in value


def test_startup_applies_schema_without_creating_unneeded_backup(tmp_path):
    database = tmp_path / "pricewatch.db"
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{database}",
        app_secret_key="test-secret-key-that-is-at-least-32-characters",
    )
    migrate(settings, ROOT / "alembic.ini")
    migrate(settings, ROOT / "alembic.ini")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002"


def test_upgrade_from_initial_schema_preserves_existing_card(tmp_path):
    database = tmp_path / "pricewatch.db"
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{database}",
        app_secret_key="test-secret-key-that-is-at-least-32-characters",
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "0001")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO products (source_site, requested_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("dell-us", "https://www.dell.com/en-us/shop/old", "2026-09-21", "2026-09-21"),
        )
        connection.commit()
    migrate(settings, ROOT / "alembic.ini")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002"
        assert connection.execute(
            "SELECT requested_url, dell_selection FROM products"
        ).fetchone() == ("https://www.dell.com/en-us/shop/old", None)
