"""Atomic SQLite backups with checksum verification and controlled restore."""

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import BackupRecord

BackupReason = Literal["daily", "weekly", "manual", "pre_migration", "pre_restore"]


@dataclass(frozen=True)
class BackupManifest:
    checksum: str
    size: int


class BackupService:
    def __init__(self, factory: sessionmaker[Session], data_dir: Path) -> None:
        self.factory = factory
        self.data_dir = data_dir
        self.directory = data_dir / "backups"
        self.directory.mkdir(parents=True, exist_ok=True)
        engine = factory.kw["bind"]
        self.database = Path(engine.url.database)

    @staticmethod
    def verify(path: Path) -> BackupManifest:
        if not path.is_file():
            raise ValueError("备份文件不存在")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库校验失败")
        return BackupManifest(digest.hexdigest(), path.stat().st_size)

    def create(self, reason: BackupReason) -> BackupRecord:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"pricewatch-{reason}-{stamp}.sqlite3"
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pricewatch-", suffix=".sqlite3", dir=self.directory
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            with (
                closing(sqlite3.connect(str(self.database))) as source,
                closing(sqlite3.connect(str(temporary))) as target,
            ):
                source.backup(target)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            manifest = self.verify(temporary)
            os.replace(temporary, self.directory / filename)
            with self.factory.begin() as session:
                record = BackupRecord(
                    filename=filename,
                    reason=reason,
                    schema_version="current",
                    checksum=manifest.checksum,
                )
                session.add(record)
                session.flush()
                session.expunge(record)
            return record
        finally:
            temporary.unlink(missing_ok=True)

    def rotate(self) -> None:
        with self.factory.begin() as session:
            for reason, keep in (("daily", 7), ("weekly", 4)):
                records = session.scalars(
                    select(BackupRecord)
                    .where(BackupRecord.reason == reason)
                    .order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc())
                ).all()
                for record in records[keep:]:
                    (self.directory / record.filename).unlink(missing_ok=True)
                    session.delete(record)

    def restore(self, path: Path, confirm_checksum: str) -> None:
        actual = self.verify(path)
        if actual.checksum != confirm_checksum:
            raise ValueError("备份校验码不匹配")
        # The caller must stop the web process before restoring the database.
        self.create("pre_restore")
        with (
            closing(sqlite3.connect(str(path))) as source,
            closing(sqlite3.connect(str(self.database))) as target,
        ):
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
