import pytest
from pydantic import ValidationError

from pricewatch.app import create_app
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


def test_dell_socks_proxy_requires_host_and_port_together(tmp_path):
    with pytest.raises(ValidationError):
        Settings(
            data_dir=tmp_path,
            app_secret_key="a" * 32,
            dell_socks_proxy_host="192.168.50.199",
        )


def test_dell_socks_proxy_accepts_lan_address_and_port(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        app_secret_key="a" * 32,
        dell_socks_proxy_host="192.168.50.199",
        dell_socks_proxy_port=1070,
    )

    assert str(settings.dell_socks_proxy_host) == "192.168.50.199"
    assert settings.dell_socks_proxy_port == 1070


def test_app_passes_dell_proxy_to_fetcher(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'pricewatch.db'}",
        app_secret_key="a" * 32,
        dell_socks_proxy_host="192.168.50.199",
        dell_socks_proxy_port=1070,
    )

    app = create_app(settings)

    assert app.state.check_service.pipeline.browser.upstream_socks == (
        "192.168.50.199",
        1070,
    )
