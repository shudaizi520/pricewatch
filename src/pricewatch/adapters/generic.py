"""Conservative structured-data extraction for occasional other retailers."""

import json
from decimal import Decimal

from bs4 import BeautifulSoup
from bs4.element import Tag
from httpx import URL

from pricewatch.adapters.base import AmbiguousExtraction, ExtractionError
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
from pricewatch.fetching.types import AcquiredPage


class GenericAdapter:
    def supports(self, url: URL) -> bool:
        return url.scheme in ("http", "https")

    def extract(self, page: AcquiredPage) -> ProductSnapshot:
        soup = BeautifulSoup(page.html, "lxml")
        products: list[dict[str, object]] = []
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                raw = json.loads(tag.get_text())
            except (ValueError, TypeError):
                continue
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                if isinstance(entry, dict):
                    graph = entry.get("@graph")
                    entries_to_check = graph if isinstance(graph, list) else [entry]
                    products.extend(
                        item
                        for item in entries_to_check
                        if isinstance(item, dict) and item.get("@type") == "Product"
                    )
        if not products:
            raise ExtractionError("No structured product data found")
        product = products[0]
        name = str(product.get("name") or "").strip()
        offers = product.get("offers")
        entries = offers if isinstance(offers, list) else [offers]
        money: list[Money] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            price = entry.get("price")
            currency = entry.get("priceCurrency")
            if price is None or currency is None:
                continue
            try:
                money.append(Money.from_decimal(str(currency).upper(), str(price)))
            except (ValueError, TypeError):
                continue
        if not name or not money:
            raise ExtractionError("Product name, currency or price is missing")
        if len(set(money)) != 1:
            raise AmbiguousExtraction("Multiple structured prices require confirmation")
        canonical = soup.find("link", attrs={"rel": "canonical"})
        canonical_url = (
            str(canonical.get("href"))
            if isinstance(canonical, Tag) and canonical.get("href")
            else page.final_url
        )
        parsed = URL(canonical_url)
        if parsed.scheme not in ("http", "https") or parsed.host != URL(page.final_url).host:
            canonical_url = page.final_url
        return ProductSnapshot(
            ProductIdentity(
                URL(page.final_url).host or "other",
                sku=str(product.get("sku")) if product.get("sku") else None,
            ),
            canonical_url,
            name,
            ProductConfiguration(),
            money[0],
            evidence={"price": "jsonld.offers.price"},
            confidence=Decimal("0.90"),
        )
