"""Deployment-file contracts independent of the local Docker daemon."""

import os
import sqlite3
import subprocess
import tomllib
from contextlib import closing
from pathlib import Path

import yaml
from alembic.config import Config

from alembic import command
from pricewatch import __version__
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


def test_ghcr_compose_installs_without_source_or_manual_secret():
    config = yaml.safe_load((ROOT / "compose.ghcr.yml").read_text())
    assert set(config["services"]) == {"pricewatch"}
    service = config["services"]["pricewatch"]
    assert service["image"] == "ghcr.io/shudaizi520/pricewatch:1.0.3"
    assert "build" not in service
    assert service["volumes"] == ["pricewatch-data:/data"]
    assert "pricewatch-data" in config["volumes"]
    assert "PRICEWATCH_APP_SECRET_KEY" not in service.get("environment", {})
    assert service["ports"] == ["${PRICEWATCH_BIND:-127.0.0.1}:8080:8080"]
    assert service["restart"] == "unless-stopped"
    assert "http://127.0.0.1:8080/healthz" in str(service["healthcheck"]["test"])


def test_package_and_health_version_match_source_release():
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert package["project"]["version"] == "1.0.3"
    assert __version__ == "1.0.3"


def test_release_workflow_publishes_github_release_after_image():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["publish"]["environment"] == "production"
    assert jobs["publish"]["permissions"] == {"contents": "read", "packages": "write"}
    signed_tag = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("name") == "Check approved signed stable tag"
    )
    assert "refs/heads/main" in signed_tag["run"]
    version_check = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("name") == "Validate source and installer version"
    )
    assert "pyproject.toml" in version_check["run"]
    assert "compose.ghcr.yml" in version_check["run"]
    assert any(
        step.get("name") == "Publish linux/amd64 image with SBOM"
        for step in jobs["publish"]["steps"]
    )
    job = jobs["release"]
    assert job["needs"] == "publish"
    assert job["environment"] == "production"
    assert job["permissions"] == {"contents": "write"}
    release = next(step for step in job["steps"] if step.get("name") == "Publish GitHub Release")
    assert release["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert "gh release create" in release["run"]
    assert "--verify-tag" in release["run"]
    assert "--latest" in release["run"]
    assert "compose.ghcr.yml" in release["run"]
    assert "gh release upload" in release["run"]
    assert "gh release edit" in release["run"]
    assert "releases/latest" in release["run"]
    assert ".assets[].name" in release["run"]


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


def test_container_start_resolves_secret_before_database_migration(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = pricewatch.runtime_secret ]; then\n"
        "  printf 'generated-container-secret-at-least-32-chars\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = pricewatch.bootstrap ]; then\n"
        "  printf '%s' \"${PRICEWATCH_APP_SECRET_KEY:-}\" > \"$PW_TEST_MARKER\"\n"
        "  exit 42\n"
        "fi\n"
        "exit 99\n"
    )
    fake_python.chmod(0o755)
    marker = tmp_path / "seen-secret"
    environment = os.environ.copy()
    environment.pop("PRICEWATCH_APP_SECRET_KEY", None)
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["PW_TEST_MARKER"] = str(marker)

    result = subprocess.run(
        ["sh", str(ROOT / "docker/start.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 42
    assert marker.read_text() == "generated-container-secret-at-least-32-chars"
    assert "generated-container-secret-at-least-32-chars" not in result.stdout + result.stderr


def test_compose_requires_secret():
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    value = config["services"]["pricewatch"]["environment"]["PRICEWATCH_APP_SECRET_KEY"]
    assert ":?" in value


def test_compose_passes_optional_dell_socks_settings():
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    environment = config["services"]["pricewatch"]["environment"]

    assert environment["PRICEWATCH_DELL_SOCKS_PROXY_HOST"] == (
        "${PRICEWATCH_DELL_SOCKS_PROXY_HOST:-}"
    )
    assert environment["PRICEWATCH_DELL_SOCKS_PROXY_PORT"] == (
        "${PRICEWATCH_DELL_SOCKS_PROXY_PORT:-}"
    )


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
