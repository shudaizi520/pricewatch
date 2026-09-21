"""Choose a retailer parser from a validated product URL."""

from httpx import URL

from pricewatch.adapters.base import ProductAdapter
from pricewatch.adapters.dell_us import DellUsAdapter
from pricewatch.adapters.generic import GenericAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self.dell = DellUsAdapter()

    def for_url(self, url: URL) -> ProductAdapter:
        if self.dell.supports(url):
            return self.dell
        return GenericAdapter()
