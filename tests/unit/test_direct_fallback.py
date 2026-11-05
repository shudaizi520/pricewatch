from types import SimpleNamespace

import pytest
from httpx import URL

from pricewatch.app import create_app
from pricewatch.fetching.browser import AcquisitionPipeline, BrowserFetcher
from pricewatch.fetching.http import AcquisitionError, HttpFetcher


def test_direct_fallback_is_disabled_by_default_and_requires_a_primary_proxy(settings):
    assert settings.dell_direct_fallback is False
    for enabled, proxy, expected in [
        (False, True, False), (True, False, False), (True, True, True),
    ]:
        values = {"dell_direct_fallback": enabled}
        if proxy:
            values.update(dell_socks_proxy_host="192.0.2.1", dell_socks_proxy_port=1080)
        app = create_app(settings.model_copy(update=values))
        try:
            pipeline = app.state.check_service.pipeline
            assert pipeline.allow_dell_direct_fallback is expected
        finally:
            app.state.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("url", [
    "http://www.dell.com/en-us/shop/spd/model",
    "https://evil.example/en-us/shop/spd/model",
    "https://www.dell.com/en-uk/shop/spd/model",
    "https://user:secret@www.dell.com/en-us/shop/spd/model",
    "https://www.dell.com:8443/en-us/shop/spd/model",
    "https://www.dell.com/en-us/shop/spd/model?token=secret",
    "https://www.dell.com/en-us/shop/spd/model#fragment",
])
async def test_direct_fallback_rejects_unsafe_or_ambiguous_targets_before_any_request(url):
    direct = SimpleNamespace(upstream_socks=None)
    pipeline = AcquisitionPipeline(
        HttpFetcher(), BrowserFetcher(upstream_socks=("192.0.2.1", 1080)), direct_browser=direct,
    )
    with pytest.raises(AcquisitionError, match="安全限制"):
        await pipeline.acquire_direct(URL(url), None, {"Graphics Card": "RTX 5090"})


@pytest.mark.anyio
async def test_direct_fallback_without_opt_in_cannot_fetch_even_a_valid_dell_url():
    pipeline = AcquisitionPipeline(
        HttpFetcher(), BrowserFetcher(upstream_socks=("192.0.2.1", 1080)),
    )
    with pytest.raises(AcquisitionError, match="未授权"):
        await pipeline.acquire_direct(URL("https://www.dell.com/en-us/shop/spd/model"), None,
                                      {"Graphics Card": "RTX 5090"})
