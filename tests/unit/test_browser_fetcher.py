from datetime import UTC, datetime

import pytest
from httpx import URL

from pricewatch.fetching.browser import BrowserFetcher
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
