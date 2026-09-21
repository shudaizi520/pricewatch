"""FastAPI application factory."""

from fastapi import FastAPI

from pricewatch import __version__
from pricewatch.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured PriceWatch application instance."""

    resolved = settings or Settings()  # type: ignore[call-arg]
    app = FastAPI(title="PriceWatch", docs_url=None, redoc_url=None)
    app.state.settings = resolved

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
