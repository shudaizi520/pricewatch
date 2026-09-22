"""A fresh container gets a private, stable secret without operator input."""

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def resolve(data_dir: Path, *, configured: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PRICEWATCH_APP_SECRET_KEY", None)
    environment["PRICEWATCH_DATA_DIR"] = str(data_dir)
    environment["PRICEWATCH_DATABASE_URL"] = f"sqlite:///{data_dir / 'pricewatch.db'}"
    if configured is not None:
        environment["PRICEWATCH_APP_SECRET_KEY"] = configured
    return subprocess.run(
        [sys.executable, "-m", "pricewatch.runtime_secret"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_fresh_data_creates_private_stable_secret(tmp_path: Path) -> None:
    first = resolve(tmp_path)
    second = resolve(tmp_path)

    assert first.returncode == second.returncode == 0
    assert re.fullmatch(r"[0-9a-f]{64}\n", first.stdout)
    assert first.stdout == second.stdout
    secret_file = tmp_path / ".pricewatch-secret"
    assert secret_file.read_text().strip() == first.stdout.strip()
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert first.stdout.strip() not in first.stderr + second.stderr


def test_explicit_secret_preserves_existing_install(tmp_path: Path) -> None:
    existing = "already-configured-app-secret-at-least-32-chars"
    result = resolve(tmp_path, configured=existing)

    assert result.returncode == 0
    assert result.stdout == f"{existing}\n"
    assert not (tmp_path / ".pricewatch-secret").exists()


def test_existing_database_without_secret_refuses_new_key(tmp_path: Path) -> None:
    (tmp_path / "pricewatch.db").write_bytes(b"existing database")

    result = resolve(tmp_path)

    assert result.returncode != 0
    assert not (tmp_path / ".pricewatch-secret").exists()
    assert "existing database" in result.stderr.lower()


def test_symlink_secret_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("not-an-app-secret")
    (tmp_path / ".pricewatch-secret").symlink_to(target)

    result = resolve(tmp_path)

    assert result.returncode != 0
    assert "not-an-app-secret" not in result.stdout + result.stderr
    assert "secret file" in result.stderr.lower()


def test_world_readable_secret_is_rejected(tmp_path: Path) -> None:
    secret_file = tmp_path / ".pricewatch-secret"
    secret_file.write_text("a" * 64)
    secret_file.chmod(0o644)

    result = resolve(tmp_path)

    assert result.returncode != 0
    assert "a" * 64 not in result.stdout + result.stderr
    assert "permissions" in result.stderr.lower()


def test_non_ascii_secret_fails_without_traceback(tmp_path: Path) -> None:
    secret_file = tmp_path / ".pricewatch-secret"
    secret_file.write_bytes(b"\xff" * 64)
    secret_file.chmod(0o600)

    result = resolve(tmp_path)

    assert result.returncode != 0
    assert "secret file is invalid" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


def test_parallel_first_starts_share_one_complete_secret(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: resolve(tmp_path), range(4)))

    assert all(result.returncode == 0 for result in results)
    assert len({result.stdout for result in results}) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\n", results[0].stdout)
    assert (tmp_path / ".pricewatch-secret").read_text().strip() == results[0].stdout.strip()
