"""Short-lived Chromium fallback with an origin-pinned network policy."""

import re
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from httpx import URL
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pricewatch.adapters.base import ExtractionError, ProductAdapter
from pricewatch.domain.products import ProductSnapshot
from pricewatch.fetching.dell_options import apply_selection
from pricewatch.fetching.http import AcquisitionError, HttpFetcher
from pricewatch.fetching.safety import Resolver, UnsafeUrlError, public_addresses, system_resolver
from pricewatch.fetching.socks_proxy import SafeSocksProxy
from pricewatch.fetching.types import AcquiredPage

DELL_READY_SCRIPT = r"""() => {
  const selected = [...document.querySelectorAll('.option-grid-item')]
    .filter(card => card.querySelector('.price.scoprice')?.textContent.trim() === 'Selected')
    .map(card => card.querySelector('[data-test-id="option-title"]')?.textContent.trim() || '');
  const has = pattern => selected.some(title => pattern.test(title));
  if (!has(/Core\s*Ultra|Ryzen/i) || !has(/RTX\W*\d{4}|Radeon/i) ||
      !has(/\d+\s*GB.*\bDDR/i) || !has(/\d+\s*TB.*(?:SSD|M\.2)/i) ||
      !has(/\d{2}\s*(?:"|″|inch)/i)) return false;
  const products = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .flatMap(node => {
      try {
        const value = JSON.parse(node.textContent);
        return Array.isArray(value) ? value : [value];
      } catch { return []; }
    });
  const product = products.find(value => value?.['@type'] === 'Product');
  const offer = product?.offers;
  const visible = document.body?.innerText.match(/Dell Price\s*\$([\d,]+\.\d{2})/i);
  if (!offer || offer.priceCurrency?.toUpperCase() !== 'USD' || !visible) return false;
  return Math.abs(Number(offer.price) - Number(visible[1].replaceAll(',', ''))) < 0.005;
}"""


async def navigate_with_retry(page: Page, url: str) -> Response | None:
    try:
        return await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightError as error:
        if "net::ERR_NETWORK_CHANGED" not in str(error):
            raise
        await page.wait_for_timeout(500)
        return await page.goto(url, wait_until="domcontentloaded", timeout=30000)


class BrowserFetcher:
    def __init__(
        self,
        resolver: Resolver = system_resolver,
        max_body_bytes: int = 8 * 1024 * 1024,
        subresource_fetcher: HttpFetcher | None = None,
        upstream_socks: tuple[str, int] | None = None,
    ) -> None:
        self.resolver = resolver
        self.max_body_bytes = max_body_bytes
        self.subresource_fetcher = subresource_fetcher or HttpFetcher(resolver=resolver)
        self.upstream_socks = upstream_socks

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
        self,
        url: URL,
        browser_factory: Callable[[], object] | None = None,
        dell_selection: dict[str, str] | None = None,
    ) -> AcquiredPage:
        host = url.host
        if not host:
            raise AcquisitionError("Browser target has no hostname")
        if dell_selection is not None and not (
            host in {"www.dell.com", "dell.com"} and url.path.startswith("/en-us/shop/")
        ):
            raise AcquisitionError("配置选择仅支持美国戴尔商品页")
        address = public_addresses(url, self.resolver)[0]
        if browser_factory is not None:
            try:
                browser_factory()
            except Exception as error:
                raise AcquisitionError("Browser startup failed") from error
            raise AcquisitionError("Browser startup failed")
        try:
            async with AsyncExitStack() as stack:
                launch_args = [
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                ]
                if host in {"www.dell.com", "dell.com"}:
                    upstream = self.upstream_socks if url.path.startswith("/en-us/shop/") else None
                    proxy = await stack.enter_async_context(
                        SafeSocksProxy(self.resolver, upstream_socks=upstream)
                    )
                    launch_args.extend(
                        [
                            f"--proxy-server=socks5://127.0.0.1:{proxy.port}",
                            "--proxy-bypass-list=<-loopback>",
                        ]
                    )
                else:
                    launch_args.append(f"--host-resolver-rules=MAP {host} {address}")
                playwright = await stack.enter_async_context(async_playwright())
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=launch_args,
                )
                try:
                    context = await browser.new_context(accept_downloads=False)
                    page = await context.new_page()
                    if host not in {"www.dell.com", "dell.com"}:
                        await page.route("**/*", lambda route: self.handle_route(route, host))
                    response = await navigate_with_retry(page, str(url))
                    if response is None:
                        raise AcquisitionError("浏览器未收到商品页面响应")
                    if response.status >= 400:
                        response_host = URL(getattr(response, "url", str(url))).host or host
                        response_headers = getattr(response, "headers", {})
                        server = re.sub(
                            r"[^A-Za-z0-9._-]", "", response_headers.get("server", "")
                        )[:48]
                        source = f" (来源={response_host}"
                        if server:
                            source += f", server={server}"
                        source += ")"
                        raise AcquisitionError(
                            f"浏览器访问商品页面返回 HTTP {response.status}{source}",
                            status_code=response.status,
                        )
                    if (
                        host in {"www.dell.com", "dell.com"}
                        and url.path.startswith("/en-us/shop/")
                        and "alienware18area51aa18250" in url.path
                    ):
                        try:
                            await page.wait_for_function(DELL_READY_SCRIPT, timeout=15000)
                        except PlaywrightTimeoutError as error:
                            raise AcquisitionError("戴尔商品配置或价格尚未完整加载") from error
                    configured_offer = None
                    if dell_selection is not None:
                        try:
                            configured_offer = await apply_selection(page, dell_selection)
                        except ValueError as error:
                            raise AcquisitionError(str(error)) from error
                    html = await page.content()
                    body = html.encode("utf-8")
                    if len(body) > self.max_body_bytes:
                        raise AcquisitionError("Browser page exceeds response limit")
                    return AcquiredPage(
                        str(url),
                        page.url,
                        response.status,
                        body,
                        {},
                        "browser",
                        datetime.now(UTC),
                        configured_offer,
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
        self,
        url: URL,
        adapter: ProductAdapter,
        dell_selection: dict[str, str] | None = None,
    ) -> tuple[AcquiredPage, ProductSnapshot]:
        if dell_selection:
            page = await self.browser.fetch(url, dell_selection=dell_selection)
            return page, adapter.extract(page)
        if (
            getattr(self.browser, "upstream_socks", None) is not None
            and url.host in {"www.dell.com", "dell.com"}
            and url.path.startswith("/en-us/shop/")
        ):
            page = await self.browser.fetch(url)
            return page, adapter.extract(page)
        try:
            page = await self.http.fetch(url)
            return page, adapter.extract(page)
        except (AcquisitionError, ExtractionError) as first_error:
            try:
                page = await self.browser.fetch(url)
                return page, adapter.extract(page)
            except (AcquisitionError, ExtractionError) as browser_error:
                if (
                    isinstance(first_error, AcquisitionError)
                    and isinstance(browser_error, AcquisitionError)
                    and first_error.status_code == browser_error.status_code == 403
                ):
                    raise AcquisitionError(
                        "普通请求和浏览器都被网站拒绝 (HTTP 403), 目前无法读取真实价格。",
                        status_code=403,
                    ) from browser_error
                raise AcquisitionError(
                    f"普通请求失败: {first_error}; 浏览器请求失败: {browser_error}"
                ) from browser_error
