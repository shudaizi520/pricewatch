"""Conservative Dell US product extraction."""

import json
import re
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from httpx import URL

from pricewatch.adapters.base import AmbiguousExtraction, ExtractionError
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
from pricewatch.fetching.types import AcquiredPage

PRICE_PRIORITY = (
    "embedded.product.sale_price",
    "jsonld.offers.price",
    "meta.product.price.amount",
    "visible.purchase_price",
)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _find_description(description: str, expression: str) -> str | None:
    match = re.search(expression, description, flags=re.I)
    return match.group(0).strip() if match else None


def _visible(soup: BeautifulSoup, identifier: str) -> str | None:
    element = soup.select_one(f'[data-testid="{identifier}"]')
    return element.get_text(" ", strip=True) if isinstance(element, Tag) else None


class DellUsAdapter:
    def supports(self, url: URL) -> bool:
        return url.host in {"dell.com", "www.dell.com"} and url.path.startswith("/en-us/")

    def extract(self, page: AcquiredPage) -> ProductSnapshot:
        if not self.supports(URL(page.final_url)):
            raise ExtractionError("Not a Dell US product page")
        soup = BeautifulSoup(page.html, "lxml")
        structured: dict[str, object] = {}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                content = json.loads(script.get_text())
            except (ValueError, TypeError):
                continue
            products = content if isinstance(content, list) else [content]
            for candidate in products:
                if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                    structured = candidate
                    break

        offers = structured.get("offers")
        offer = offers if isinstance(offers, dict) else {}
        candidates: dict[str, Money] = {}
        currency = _text(offer.get("priceCurrency"))
        if offer.get("price") and currency:
            candidates["jsonld.offers.price"] = Money.from_decimal(currency, _text(offer["price"]))
        amount = soup.find("meta", attrs={"property": "product:price:amount"})
        meta_currency = soup.find("meta", attrs={"property": "product:price:currency"})
        if isinstance(amount, Tag) and isinstance(meta_currency, Tag):
            candidates["meta.product.price.amount"] = Money.from_decimal(
                _text(meta_currency.get("content")), _text(amount.get("content"))
            )
        visible_price = _visible(soup, "sale-price")
        if visible_price:
            digits = visible_price.replace("$", "").replace(",", "").strip()
            candidates["visible.purchase_price"] = Money.from_decimal("USD", digits)
        if not candidates:
            raise ExtractionError("No trusted Dell public purchase price found")
        if any(candidate.currency != "USD" for candidate in candidates.values()):
            raise ExtractionError("Dell US offer did not use USD")
        if len({candidate.minor for candidate in candidates.values()}) > 1:
            raise AmbiguousExtraction("Conflicting Dell purchase prices")
        source = next(source for source in PRICE_PRIORITY if source in candidates)
        price = candidates[source]

        canonical_tag = soup.find("link", attrs={"rel": "canonical"})
        canonical = (
            _text(canonical_tag.get("href"))
            if isinstance(canonical_tag, Tag) and canonical_tag.get("href")
            else page.final_url
        )
        if not self.supports(URL(canonical)):
            canonical = page.final_url
        sku = _text(structured.get("sku")) or urlparse(canonical).path.rstrip("/").split("/")[-1]
        sku = sku.lower()
        if not re.fullmatch(r"[a-z0-9_-]{3,160}", sku):
            raise ExtractionError("Dell product SKU is missing or invalid")
        description = _text(structured.get("description"))
        configuration = ProductConfiguration(
            cpu=_visible(soup, "processor")
            or _find_description(description, r"(?:Intel )?Core Ultra\s*\d+\s*[A-Z0-9]+"),
            gpu=_visible(soup, "graphics")
            or _find_description(
                description, r"(?:NVIDIA )?(?:GeForce )?RTX\s*\d{4}(?:\s*\d+\s*GB)?"
            ),
            memory=_visible(soup, "memory") or _find_description(description, r"\d+\s*GB\s*DDR\d+"),
            storage=_visible(soup, "storage") or _find_description(description, r"\d+\s*TB\s*SSD"),
            display=_visible(soup, "display")
            or _find_description(description, r"\d+\s*(?:inch|\")\s*[A-Z0-9+]+\s*display"),
        )
        if not configuration.gpu or not (
            configuration.cpu or configuration.memory or configuration.storage
        ):
            raise ExtractionError("Dell hardware configuration is incomplete")
        heading = soup.find("h1")
        name = _text(structured.get("name")) or (
            heading.get_text(" ", strip=True) if isinstance(heading, Tag) else ""
        )
        if not name:
            raise ExtractionError("Dell product name is missing")
        availability_text = _text(offer.get("availability")) or (_visible(soup, "stock") or "")
        availability = (
            "in_stock" if re.search(r"InStock|In stock", availability_text, re.I) else "unknown"
        )
        if re.search(r"OutOfStock|Out of stock", availability_text, re.I):
            availability = "out_of_stock"
        list_price = None
        original_price = _visible(soup, "list-price")
        if original_price:
            try:
                parsed_list_price = Money.from_decimal(
                    "USD", original_price.replace("$", "").replace(",", "").strip()
                )
                if parsed_list_price.minor > price.minor:
                    list_price = parsed_list_price
            except ValueError:
                pass
        discount = _visible(soup, "discount")
        coupon = _visible(soup, "coupon")
        return ProductSnapshot(
            identity=ProductIdentity("dell-us", sku=sku),
            canonical_url=canonical,
            name=name,
            configuration=configuration,
            price=price,
            list_price=list_price,
            discount_text=discount[:240] if discount else None,
            coupon_text=coupon[:500] if coupon else None,
            availability=availability,
            evidence={"price": source, "sku": "jsonld.sku" if structured.get("sku") else "url"},
            confidence=Decimal("0.95") if source != "visible.purchase_price" else Decimal("0.80"),
        )
