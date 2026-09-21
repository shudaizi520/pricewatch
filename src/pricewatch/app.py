"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from pricewatch import __version__
from pricewatch.config import Settings
from pricewatch.db.base import Base
from pricewatch.db.session import create_engine_and_session
from pricewatch.fetching.browser import AcquisitionPipeline, BrowserFetcher
from pricewatch.fetching.http import HttpFetcher
from pricewatch.services.checks import CheckService
from pricewatch.services.scheduler import SchedulerService
from pricewatch.web.routes_auth import router as auth_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured PriceWatch application instance."""

    resolved = settings or Settings()  # type: ignore[call-arg]
    engine, session_factory = create_engine_and_session(resolved)
    Base.metadata.create_all(engine)
    checker = CheckService(session_factory, AcquisitionPipeline(HttpFetcher(), BrowserFetcher()))
    scheduler = SchedulerService(session_factory, checker)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start()
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

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
