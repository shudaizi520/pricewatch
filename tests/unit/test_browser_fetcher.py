from datetime import UTC, datetime

import pytest
from httpx import URL

from pricewatch.fetching.browser import AcquisitionPipeline, BrowserFetcher
from pricewatch.fetching.http import AcquisitionError
from pricewatch.fetching.types import AcquiredPage


class FakeRequest:
    url = "http://169.254.169.254/latest/meta-data"

    def __init__(self):
        self.aborted = False


class FakeRoute:
    def __init__(self):
        self.request = FakeRequest()

    async def abort(self):
        self.request.aborted = True

    async def continue_(self):
        raise AssertionError("private request was allowed")


@pytest.mark.anyio
async def test_browser_blocks_private_subresource():
    fetcher = BrowserFetcher(resolver=lambda _: ["93.184.216.34"])
    route = FakeRoute()
    await fetcher.handle_route(route, "shop.example")
    assert route.request.aborted is True


@pytest.mark.anyio
async def test_browser_failure_is_categorized():
    fetcher = BrowserFetcher(resolver=lambda _: ["93.184.216.34"])
    with pytest.raises(AcquisitionError):
        await fetcher.fetch(URL("https://shop.example/item"), browser_factory=lambda: None)


@pytest.mark.anyio
async def test_browser_reports_actual_blocking_status(monkeypatch):
    class FakePage:
        url = "https://shop.example/item"

        async def route(self, *_):
            pass

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 403})()

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return self

        async def new_page(self):
            return FakePage()

        async def close(self):
            pass

    class FakePlaywright:
        chromium = None

        async def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            result = FakePlaywright()
            result.chromium = result
            return result

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr("pricewatch.fetching.browser.async_playwright", FakePlaywrightContext)
    fetcher = BrowserFetcher(resolver=lambda _: ["93.184.216.34"])
    with pytest.raises(AcquisitionError, match="HTTP 403"):
        await fetcher.fetch(URL("https://shop.example/item"))


@pytest.mark.anyio
async def test_pipeline_keeps_both_denials_instead_of_hiding_http_403():
    class FailingFetcher:
        def __init__(self, message, status_code):
            self.message = message
            self.status_code = status_code

        async def fetch(self, _url):
            raise AcquisitionError(self.message, status_code=self.status_code)

    pipeline = AcquisitionPipeline(
        FailingFetcher("HTTP 403 from product page", 403),
        FailingFetcher("Browser returned HTTP 403", 403),
    )
    with pytest.raises(AcquisitionError) as captured:
        await pipeline.acquire(URL("https://shop.example/item"), object())
    assert "HTTP 403" in str(captured.value)
    assert "普通请求和浏览器" in str(captured.value)


@pytest.mark.anyio
async def test_cross_site_script_uses_safe_fetcher_then_fulfills():
    class ScriptRequest:
        url = "https://static.example.net/product.js"
        resource_type = "script"

    class ScriptRoute:
        request = ScriptRequest()
        response = None
        aborted = False

        async def abort(self):
            self.aborted = True

        async def fulfill(self, **kwargs):
            self.response = kwargs

    class SafeFetcher:
        async def fetch(self, url):
            assert url.host == "static.example.net"
            return AcquiredPage(
                str(url),
                str(url),
                200,
                b"window.product=1",
                {"content-type": "application/javascript"},
                "http",
                datetime.now(UTC),
            )

    route = ScriptRoute()
    await BrowserFetcher(
        resolver=lambda _: ["93.184.216.34"], subresource_fetcher=SafeFetcher()
    ).handle_route(route, "shop.example")
    assert route.response["body"] == b"window.product=1"
    assert not route.aborted
