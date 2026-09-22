import asyncio
from datetime import UTC, datetime

import pytest
from httpx import URL
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pricewatch.adapters.base import ExtractionError
from pricewatch.fetching.browser import AcquisitionPipeline, BrowserFetcher, navigate_with_retry
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
async def test_navigation_retries_once_when_network_changes():
    class Page:
        def __init__(self):
            self.attempts = 0

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def goto(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise PlaywrightError("Page.goto: net::ERR_NETWORK_CHANGED")
            return "loaded"

    page = Page()
    assert await navigate_with_retry(page, "https://www.dell.com/example") == "loaded"
    assert page.attempts == 2


@pytest.mark.anyio
async def test_navigation_does_not_retry_a_blocked_page():
    class Page:
        attempts = 0

        async def goto(self, *_args, **_kwargs):
            self.attempts += 1
            raise PlaywrightError("Page.goto: net::ERR_BLOCKED_BY_CLIENT")

    page = Page()
    with pytest.raises(PlaywrightError, match="ERR_BLOCKED_BY_CLIENT"):
        await navigate_with_retry(page, "https://www.dell.com/example")
    assert page.attempts == 1


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
        url = "https://shop.example/item?token=private-query"

        async def route(self, *_):
            pass

        async def goto(self, *_args, **_kwargs):
            return type(
                "Response",
                (),
                {
                    "status": 403,
                    "url": self.url,
                    "headers": {"server": "edge.example", "set-cookie": "private-cookie"},
                },
            )()

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
            assert _kwargs["headless"] is False
            assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in _kwargs["args"]
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
    with pytest.raises(AcquisitionError, match="HTTP 403") as caught:
        await fetcher.fetch(URL(FakePage.url))
    assert "shop.example" in str(caught.value)
    assert "edge.example" in str(caught.value)
    assert "private-query" not in str(caught.value)
    assert "private-cookie" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "dell_url",
    [
        "https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250/aa18250_reg_01",
        "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250",
    ],
)
async def test_dell_browser_does_not_trigger_interception_403(monkeypatch, dell_url):
    class FakePage:
        url = dell_url
        routed = False

        async def route(self, *_args):
            self.routed = True

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 403 if self.routed else 200})()

        async def wait_for_function(self, *_args, **_kwargs):
            pass

        async def content(self):
            return "<html><body>Dell Price $3,999.99</body></html>"

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return self

        async def new_page(self):
            return FakePage()

        async def close(self):
            pass

    class FakePlaywright:
        chromium = None

        async def launch(self, **kwargs):
            assert any(
                arg.startswith("--proxy-server=socks5://127.0.0.1:") for arg in kwargs["args"]
            )
            assert "--proxy-bypass-list=<-loopback>" in kwargs["args"]
            assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in kwargs["args"]
            assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in kwargs["args"]
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            result = FakePlaywright()
            result.chromium = result
            return result

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr("pricewatch.fetching.browser.async_playwright", FakePlaywrightContext)
    page = await BrowserFetcher(resolver=lambda _: ["93.184.216.34"]).fetch(URL(dell_url))
    assert page.status == 200


@pytest.mark.anyio
async def test_us_dell_browser_uses_configured_upstream_socks(monkeypatch):
    url = "https://www.dell.com/en-us/shop/desktops/spd/alienware-aurora/example"
    requests = []

    async def upstream(reader, writer):
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            requests.append(await reader.readexactly(10))
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]

    class FakePage:
        def __init__(self, page_url):
            self.url = page_url

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        async def content(self):
            return "<html><body>Dell Price $3,999.99</body></html>"

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return self

        async def new_page(self):
            return FakePage(url)

        async def close(self):
            pass

    class FakePlaywright:
        chromium = None

        async def launch(self, **kwargs):
            address = next(
                arg for arg in kwargs["args"] if arg.startswith("--proxy-server=socks5://")
            )
            local_port = int(address.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            try:
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x00"
                writer.write(b"\x05\x01\x00\x03\x0cwww.dell.com\x01\xbb")
                await writer.drain()
                assert (await reader.readexactly(10))[:2] == b"\x05\x00"
            finally:
                writer.close()
                await writer.wait_closed()
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            result = FakePlaywright()
            result.chromium = result
            return result

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr("pricewatch.fetching.browser.async_playwright", FakePlaywrightContext)
    try:
        page = await BrowserFetcher(
            resolver=lambda _: ["93.184.216.34"],
            upstream_socks=("127.0.0.1", upstream_port),
        ).fetch(URL(url))
    finally:
        upstream_server.close()
        await upstream_server.wait_closed()

    assert page.status == 200
    assert requests == [b"\x05\x01\x00\x01\x5d\xb8\xd8\x22\x01\xbb"]


@pytest.mark.anyio
async def test_failed_dell_option_reports_configure_http_status_without_leaking_url(monkeypatch):
    url = "https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250"

    class FakePage:
        def __init__(self):
            self.url = url
            self.listeners = {}

        def on(self, name, callback):
            self.listeners[name] = callback

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        async def wait_for_function(self, *_args, **_kwargs):
            return None

        async def content(self):
            return "<html><body>Dell Price $3,999.99</body></html>"

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return self

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

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
            return None

    async def failed_selection(page, _recipe):
        page.listeners["response"](
            type(
                "Response",
                (),
                {
                    "status": 403,
                    "url": "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku?token=private-value",
                },
            )()
        )
        raise ValueError("戴尔没有完成配置切换: Graphics Card / RTX 5090")

    monkeypatch.setattr("pricewatch.fetching.browser.async_playwright", FakePlaywrightContext)
    monkeypatch.setattr("pricewatch.fetching.browser.apply_selection", failed_selection)
    with pytest.raises(AcquisitionError) as caught:
        await BrowserFetcher(resolver=lambda _: ["93.184.216.34"]).fetch(
            URL(url), dell_selection={"Graphics Card": "RTX 5090"}
        )
    assert "配置接口 HTTP 403" in str(caught.value)
    assert "private-value" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "dell_url",
    [
        "https://www.dell.com/en-us/shop/laptop-computers/spd/"
        "alienware18area51aa18250/aa18250_reg_01",
        "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250",
    ],
)
async def test_dell_page_does_not_capture_unselected_configuration(monkeypatch, dell_url):

    class FakePage:
        url = dell_url

        async def route(self, *_):
            pass

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        async def wait_for_function(self, *_args, **_kwargs):
            raise PlaywrightTimeoutError("selection never loaded")

        async def content(self):
            raise AssertionError("Incomplete Dell HTML must not be captured")

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
    with pytest.raises(AcquisitionError, match="配置或价格尚未完整加载"):
        await fetcher.fetch(URL(dell_url))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "desktop_url",
    [
        "https://www.dell.com/en-us/shop/desktops/spd/alienware-aurora/example",
        "https://www.dell.com/zh-cn/shop/dell-laptops/spd/alienware18area51aa18250/aa18250_cn_01",
    ],
)
async def test_other_dell_product_does_not_require_area_51_option_grid(monkeypatch, desktop_url):
    class FakePage:
        url = desktop_url

        async def route(self, *_):
            pass

        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        async def wait_for_function(self, *_args, **_kwargs):
            raise AssertionError("Area-51 readiness rule must not apply to a desktop")

        async def content(self):
            return "<html><h1>Alienware Aurora Desktop</h1></html>"

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
    page = await BrowserFetcher(resolver=lambda _: ["93.184.216.34"]).fetch(URL(desktop_url))
    assert b"Alienware Aurora Desktop" in page.body


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
async def test_pipeline_keeps_http_403_when_browser_challenge_cannot_be_parsed():
    class DeniedHttp:
        async def fetch(self, _url):
            raise AcquisitionError("商品页面返回 HTTP 403", status_code=403)

    class ChallengeBrowser:
        async def fetch(self, url):
            return AcquiredPage(
                str(url),
                str(url),
                200,
                b"<html>Verify you are human</html>",
                {},
                "browser",
                datetime.now(UTC),
            )

    class ProductAdapter:
        def extract(self, _page):
            raise ExtractionError("页面没有商品价格; 可能是验证页")

    pipeline = AcquisitionPipeline(DeniedHttp(), ChallengeBrowser())
    with pytest.raises(AcquisitionError) as captured:
        await pipeline.acquire(URL("https://shop.example/item"), ProductAdapter())
    assert "HTTP 403" in str(captured.value)
    assert "验证页" in str(captured.value)


@pytest.mark.anyio
async def test_configured_pipeline_never_uses_unselected_http_page():
    class UnsafeDefaultHttp:
        async def fetch(self, _url):
            raise AssertionError("Configured checks must skip the default HTTP page")

    class ConfiguredBrowser:
        async def fetch(self, url, dell_selection=None):
            assert dell_selection == {"Graphics Card": "RTX 5090"}
            return AcquiredPage(
                str(url),
                str(url),
                200,
                b"<html></html>",
                {},
                "browser",
                datetime.now(UTC),
            )

    class Adapter:
        def extract(self, _page):
            return "configured"

    pipeline = AcquisitionPipeline(UnsafeDefaultHttp(), ConfiguredBrowser())
    _, snapshot = await pipeline.acquire(
        URL("https://www.dell.com/en-us/shop/spd/example"),
        Adapter(),
        dell_selection={"Graphics Card": "RTX 5090"},
    )
    assert snapshot == "configured"


@pytest.mark.anyio
async def test_us_dell_preview_with_proxy_skips_direct_http_request():
    class DirectHttp:
        async def fetch(self, _url):
            raise AssertionError("Dell preview should not make a direct request")

    class ProxiedBrowser:
        upstream_socks = ("192.168.50.199", 1070)

        async def fetch(self, url, dell_selection=None):
            assert dell_selection is None
            return AcquiredPage(
                str(url), str(url), 200, b"<html></html>", {}, "browser", datetime.now(UTC)
            )

    class Adapter:
        def extract(self, _page):
            return "proxied-preview"

    pipeline = AcquisitionPipeline(DirectHttp(), ProxiedBrowser())
    _, snapshot = await pipeline.acquire(
        URL("https://www.dell.com/en-us/shop/desktops/spd/alienware-aurora/example"),
        Adapter(),
    )

    assert snapshot == "proxied-preview"


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
