import pytest
from httpx import URL

from pricewatch.fetching.browser import BrowserFetcher
from pricewatch.fetching.http import AcquisitionError


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
