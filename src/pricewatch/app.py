"""FastAPI application factory."""

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from pricewatch import __version__
from pricewatch.config import Settings
from pricewatch.db.base import Base
from pricewatch.db.session import create_engine_and_session
from pricewatch.web.routes_auth import router as auth_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured PriceWatch application instance."""

    resolved = settings or Settings()  # type: ignore[call-arg]
    app = FastAPI(title="PriceWatch", docs_url=None, redoc_url=None)
    app.state.settings = resolved
    engine, session_factory = create_engine_and_session(resolved)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
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
