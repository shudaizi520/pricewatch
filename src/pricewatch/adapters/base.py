"""Retailer adapter protocol and extraction failures."""

from typing import Protocol

from httpx import URL

from pricewatch.domain.products import ProductSnapshot
from pricewatch.fetching.types import AcquiredPage


class ExtractionError(ValueError):
    """The fetched page contains no trustworthy product offer."""


class AmbiguousExtraction(ExtractionError):
    """Several plausible product prices conflict."""


class ProductAdapter(Protocol):
    def supports(self, url: URL) -> bool: ...

    def extract(self, page: AcquiredPage) -> ProductSnapshot: ...
