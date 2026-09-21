from datetime import UTC

import httpx
import pytest

from pricewatch.fetching.http import AcquisitionError, HttpFetcher
from pricewatch.fetching.safety import UnsafeUrlError


def resolver(_: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.anyio
async def test_redirect_target_is_revalidated():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == "shop.example"
        assert request.url.host == "93.184.216.34"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    fetcher = HttpFetcher(resolver=resolver, transport=httpx.MockTransport(respond))
    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch(httpx.URL("https://shop.example/item"))


@pytest.mark.anyio
async def test_fetch_preserves_public_host_and_bounds_response():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.extensions["sni_hostname"] == "shop.example"
        assert request.headers["host"] == "shop.example"
        return httpx.Response(200, content=b"<h1>Product</h1>")

    fetcher = HttpFetcher(resolver=resolver, transport=httpx.MockTransport(respond))
    page = await fetcher.fetch(httpx.URL("https://shop.example/item"))
    assert page.html == "<h1>Product</h1>"
    assert page.final_url == "https://shop.example/item"
    assert page.fetched_at.tzinfo == UTC


@pytest.mark.anyio
async def test_response_above_limit_is_rejected():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 30))
    fetcher = HttpFetcher(resolver=resolver, transport=transport, max_body_bytes=16)
    with pytest.raises(AcquisitionError):
        await fetcher.fetch(httpx.URL("https://shop.example/item"))


@pytest.mark.anyio
async def test_rebinding_never_connects_to_new_private_answer():
    calls = 0

    def rebinding(_: str) -> list[str]:
        nonlocal calls
        calls += 1
        return ["93.184.216.34"] if calls == 1 else ["10.0.0.2"]

    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"safe"))
    fetcher = HttpFetcher(resolver=rebinding, transport=transport)
    page = await fetcher.fetch(httpx.URL("https://shop.example/item"))
    assert page.html == "safe"
    assert calls == 1


@pytest.mark.anyio
async def test_timeout_is_reported_as_acquisition_error():
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    fetcher = HttpFetcher(resolver=resolver, transport=httpx.MockTransport(timeout))
    with pytest.raises(AcquisitionError):
        await fetcher.fetch(httpx.URL("https://shop.example/item"))


@pytest.mark.anyio
async def test_http_denial_keeps_status_for_browser_fallback_diagnosis():
    transport = httpx.MockTransport(lambda _: httpx.Response(403))
    fetcher = HttpFetcher(resolver=resolver, transport=transport)
    with pytest.raises(AcquisitionError) as captured:
        await fetcher.fetch(httpx.URL("https://shop.example/item"))
    assert captured.value.status_code == 403
