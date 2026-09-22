"""Resolve a stable application secret for a fresh container installation."""

import os
import re
import secrets
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

_SECRET_NAME = ".pricewatch-secret"
_GENERATED_SECRET = re.compile(r"[0-9a-f]{64}")


def _read_secret(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise RuntimeError("Secret file cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Secret file must be a regular file")
        if metadata.st_mode & 0o077:
            raise RuntimeError("Secret file permissions must be private (0600)")
        with os.fdopen(descriptor, "r", encoding="ascii") as handle:
            descriptor = -1
            try:
                value = handle.read(128)
            except UnicodeError as error:
                raise RuntimeError("Secret file is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _GENERATED_SECRET.fullmatch(value) is None:
        raise RuntimeError("Secret file is invalid")
    return value


def resolve_application_secret(
    data_dir: Path, configured: str | None, database_path: Path
) -> str:
    """Keep existing installs unchanged and create a private key only for fresh data."""

    if configured:
        return configured
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / _SECRET_NAME
    try:
        return _read_secret(destination)
    except FileNotFoundError:
        pass
    if database_path.exists():
        # Another fresh starter may have written the key and migrated the DB
        # between our first read and this check.
        try:
            return _read_secret(destination)
        except FileNotFoundError:
            pass
        raise RuntimeError("Existing database has no application secret; restore its original key")

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", dir=data_dir, prefix=f"{_SECRET_NAME}-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(secrets.token_hex(32))
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        with suppress(FileExistsError):
            os.link(temporary_path, destination, follow_symlinks=False)
    finally:
        temporary_path.unlink(missing_ok=True)
    return _read_secret(destination)


def main() -> None:
    data_dir = Path(os.environ.get("PRICEWATCH_DATA_DIR", "/data"))
    database_url = os.environ.get("PRICEWATCH_DATABASE_URL", "sqlite:////data/pricewatch.db")
    if not database_url.startswith("sqlite:////"):
        raise SystemExit("PriceWatch secret setup failed: database path must be absolute SQLite")
    database_path = Path(database_url.removeprefix("sqlite:///"))
    try:
        value = resolve_application_secret(
            data_dir, os.environ.get("PRICEWATCH_APP_SECRET_KEY"), database_path
        )
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"PriceWatch secret setup failed: {error}") from None
    print(value)


if __name__ == "__main__":
    main()
