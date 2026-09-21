"""PriceWatch command-line entry point."""

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from pricewatch.config import Settings
from pricewatch.db.models import Administrator
from pricewatch.db.session import create_engine_and_session
from pricewatch.services.auth import hash_password
from pricewatch.services.backups import BackupService

app = typer.Typer(
    name="pricewatch",
    help="Operate the PriceWatch service and its local data.",
    no_args_is_help=True,
)
admin_app = typer.Typer(help="Administrator recovery commands.")
app.add_typer(admin_app, name="admin")
backup_app = typer.Typer(help="Offline database backup operations.")
app.add_typer(backup_app, name="backup")


@backup_app.command("restore")
def restore_backup(
    file: Annotated[Path, typer.Option("--file", exists=True, file_okay=True, dir_okay=False)],
    confirm_checksum: Annotated[str, typer.Option("--confirm-checksum")],
) -> None:
    """Restore only while the PriceWatch container/web process is stopped."""
    settings = Settings()  # type: ignore[call-arg]
    engine, factory = create_engine_and_session(settings)
    try:
        BackupService(factory, settings.data_dir).restore(file, confirm_checksum)
    finally:
        engine.dispose()
    typer.echo("Backup restored. A pre-restore copy of the previous database was kept.")


@admin_app.command("reset-password")
def reset_password(
    username: Annotated[str, typer.Option("--username")],
    password_file: Annotated[typer.FileText, typer.Option("--password-file")],
) -> None:
    """Reset an existing administrator password from a protected file."""

    password = password_file.read().rstrip("\r\n")
    if len(password) < 12:
        raise typer.BadParameter("password must contain at least 12 characters")
    settings = Settings()  # type: ignore[call-arg]
    engine, factory = create_engine_and_session(settings)
    try:
        with factory() as session:
            admin = session.scalar(select(Administrator).where(Administrator.username == username))
            if admin is None:
                raise typer.BadParameter("administrator not found")
            admin.password_hash = hash_password(password)
            session.commit()
    finally:
        engine.dispose()
    typer.echo("Administrator password updated.")
