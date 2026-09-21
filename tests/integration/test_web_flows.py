import pytest
from pydantic import ValidationError

from pricewatch.config import Settings


@pytest.mark.anyio
async def test_health_endpoint(client):
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert isinstance(response.json()["version"], str)


def test_secret_rejects_short_value(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, app_secret_key="short")


def test_empty_external_url_from_compose_means_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICEWATCH_EXTERNAL_URL", "")
    settings = Settings(data_dir=tmp_path, app_secret_key="a" * 32)

    assert settings.external_url is None
