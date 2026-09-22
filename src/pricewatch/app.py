"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from pricewatch import __version__
from pricewatch.config import Settings
from pricewatch.db.base import Base
from pricewatch.db.models import Setting
from pricewatch.db.session import create_engine_and_session
from pricewatch.fetching.browser import AcquisitionPipeline, BrowserFetcher
from pricewatch.fetching.http import HttpFetcher
from pricewatch.services.backups import BackupService
from pricewatch.services.checks import CheckService
from pricewatch.services.crypto import SecretBox
from pricewatch.services.notifications import NotificationService
from pricewatch.services.retention import RetentionService
from pricewatch.services.scheduler import SchedulerService
from pricewatch.web.routes_auth import router as auth_router
from pricewatch.web.routes_products import router as products_router
from pricewatch.web.routes_settings import router as settings_router
from pricewatch.web.routes_status import router as status_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured PriceWatch application instance."""

    resolved = settings or Settings()  # type: ignore[call-arg]
    engine, session_factory = create_engine_and_session(resolved)
    Base.metadata.create_all(engine)
    checker = CheckService(session_factory, AcquisitionPipeline(HttpFetcher(), BrowserFetcher()))
    scheduler = SchedulerService(session_factory, checker)
    backups = BackupService(session_factory, resolved.data_dir)
    retention = RetentionService(session_factory)
    with session_factory() as session:
        saved_webhook = session.get(Setting, "feishu_webhook")
        if saved_webhook is not None and saved_webhook.value_text:
            box = SecretBox(resolved.app_secret_key.get_secret_value())
            secret = box.decrypt(saved_webhook.value_text)
            saved_signing = session.get(Setting, "feishu_signing_secret")
            signing = (
                SecretStr(box.decrypt(saved_signing.value_text))
                if saved_signing is not None and saved_signing.value_text
                else None
            )
            checker.notifier = NotificationService(
                session_factory, SecretStr(secret), signing_secret=signing
            )

    async def maintain() -> None:
        backups.create("daily")
        if datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).weekday() == 6:
            backups.create("weekly")
        backups.rotate()
        retention.run(datetime.now(UTC))

    async def retry_notifications() -> None:
        if checker.notifier is not None:
            checker.notifier.retry_failed()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start()
        scheduler.scheduler.add_job(maintain, "cron", hour=3, minute=30, id="maintenance")
        scheduler.scheduler.add_job(retry_notifications, "interval", minutes=30, id="retry")
        try:
            yield
        finally:
            await scheduler.stop()
            engine.dispose()

    app = FastAPI(title="PriceWatch", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = resolved
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.check_service = checker
    app.state.scheduler = scheduler
    app.state.backups = backups
    app.state.previews = {}
    app.state.option_catalogs = {}
    static_dir = Path(__file__).parent / "static"
    app.state.cards_css_version = sha256((static_dir / "cards.css").read_bytes()).hexdigest()[:12]
    app.state.app_js_version = sha256((static_dir / "app.js").read_bytes()).hexdigest()[:12]
    secure_cookie = bool(
        resolved.external_url and str(resolved.external_url).startswith("https://")
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=resolved.app_secret_key.get_secret_value(),
        session_cookie="pricewatch_session",
        max_age=30 * 24 * 60 * 60,
        same_site="lax",
        https_only=secure_cookie,
    )
    app.include_router(auth_router)
    app.include_router(products_router)
    app.include_router(settings_router)
    app.include_router(status_router)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise HTTPException(503, "database unavailable") from error
        return {"status": "ok", "version": __version__}

    return app
