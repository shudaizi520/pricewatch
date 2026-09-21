from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from pricewatch.app import create_app
from pricewatch.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'pricewatch.db'}",
        app_secret_key="test-secret-key-that-is-at-least-32-characters",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(settings: Settings):
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
