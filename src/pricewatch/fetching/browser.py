"""Short-lived Chromium fallback with an origin-pinned network policy."""

from collections.abc import Callable
from datetime import UTC, datetime

from httpx import URL
from playwright.async_api import Route, async_playwright

from pricewatch.adapters.base import ExtractionError, ProductAdapter
from pricewatch.domain.products import ProductSnapshot
from pricewatch.fetching.http import AcquisitionError, HttpFetcher
from pricewatch.fetching.safety import Resolver, UnsafeUrlError, public_addresses, system_resolver
from pricewatch.fetching.types import AcquiredPage


class BrowserFetcher:
    def __init__(
        self,
        resolver: Resolver = system_resolver,
        max_body_bytes: int = 8 * 1024 * 1024,
        subresource_fetcher: HttpFetcher | None = None,
    ) -> None:
        self.resolver = resolver
        self.max_body_bytes = max_body_bytes
        self.subresource_fetcher = subresource_fetcher or HttpFetcher(resolver=resolver)

    async def handle_route(self, route: Route, expected_host: str) -> None:
        try:
            url = URL(route.request.url)
            public_addresses(url, self.resolver)
            if url.host == expected_host:
                await route.continue_()
                return
            if url.scheme != "https" or route.request.resource_type not in ("script", "stylesheet"):
                await route.abort()
                return
            resource = await self.subresource_fetcher.fetch(url)
            allowed_headers = {
                key: value
                for key, value in resource.headers.items()
                if key.lower() in ("content-type", "access-control-allow-origin")
            }
            await route.fulfill(status=resource.status, body=resource.body, headers=allowed_headers)
        except (AcquisitionError, UnsafeUrlError, ValueError):
            await route.abort()

    async def fetch(
        self, url: URL, browser_factory: Callable[[], object] | None = None
    ) -> AcquiredPage:
        host = url.host
        if not host:
            raise AcquisitionError("Browser target has no hostname")
        address = public_addresses(url, self.resolver)[0]
        if browser_factory is not None:
            try:
                browser_factory()
            except Exception as error:
                raise AcquisitionError("Browser startup failed") from error
            raise AcquisitionError("Browser startup failed")
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[f"--host-resolver-rules=MAP {host} {address}"],
                )
                try:
                    context = await browser.new_context(accept_downloads=False)
                    page = await context.new_page()
                    await page.route("**/*", lambda route: self.handle_route(route, host))
                    response = await page.goto(
                        str(url), wait_until="domcontentloaded", timeout=30000
                    )
                    if response is None or response.status >= 400:
                        raise AcquisitionError("Browser page was blocked or empty")
                    html = await page.content()
                    body = html.encode("utf-8")
                    if len(body) > self.max_body_bytes:
                        raise AcquisitionError("Browser page exceeds response limit")
                    return AcquiredPage(
                        str(url), page.url, response.status, body, {}, "browser", datetime.now(UTC)
                    )
                finally:
                    await browser.close()
        except AcquisitionError:
            raise
        except Exception as error:
            raise AcquisitionError("Browser acquisition failed") from error


class AcquisitionPipeline:
    def __init__(self, http: HttpFetcher, browser: BrowserFetcher) -> None:
        self.http = http
        self.browser = browser

    async def acquire(
        self, url: URL, adapter: ProductAdapter
    ) -> tuple[AcquiredPage, ProductSnapshot]:
        try:
            page = await self.http.fetch(url)
            return page, adapter.extract(page)
        except (AcquisitionError, ExtractionError):
            page = await self.browser.fetch(url)
            return page, adapter.extract(page)
