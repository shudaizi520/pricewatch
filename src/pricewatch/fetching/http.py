"""Bounded, DNS-pinned HTTP acquisition with redirect validation."""

from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from pricewatch.fetching.safety import Resolver, public_addresses, system_resolver
from pricewatch.fetching.types import AcquiredPage


class AcquisitionError(RuntimeError):
    """A product page cannot be acquired within the configured limits."""


class HttpFetcher:
    def __init__(
        self,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        max_body_bytes: int = 8 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        self.resolver = resolver
        self.transport = transport
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects

    async def fetch(self, url: httpx.URL) -> AcquiredPage:
        original = str(url)
        current = url
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=httpx.Timeout(30),
            trust_env=False,
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                address = public_addresses(current, self.resolver)[0]
                host = current.host
                if host is None:
                    raise AcquisitionError("Missing product hostname")
                pinned = current.copy_with(host=address)
                authority = host if current.port is None else f"{host}:{current.port}"
                request = client.build_request(
                    "GET",
                    pinned,
                    headers={"Host": authority, "Accept": "text/html,application/xhtml+xml"},
                )
                request.extensions["sni_hostname"] = host
                try:
                    async with client.stream(
                        "GET", request.url, headers=request.headers, extensions=request.extensions
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or redirect_count >= self.max_redirects:
                                raise AcquisitionError("Redirect limit reached")
                            current = httpx.URL(urljoin(str(current), location))
                            continue
                        if response.status_code >= 400:
                            raise AcquisitionError(f"HTTP {response.status_code} from product page")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_body_bytes:
                                raise AcquisitionError("Product page exceeds response limit")
                            chunks.append(chunk)
                        return AcquiredPage(
                            requested_url=original,
                            final_url=str(current),
                            status=response.status_code,
                            body=b"".join(chunks),
                            headers=dict(response.headers),
                            method="http",
                            fetched_at=datetime.now(UTC),
                        )
                except httpx.HTTPError as error:
                    raise AcquisitionError("Could not fetch product page") from error
        raise AcquisitionError("Redirect limit reached")
