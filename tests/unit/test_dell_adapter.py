from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import URL

from pricewatch.adapters.base import AmbiguousExtraction
from pricewatch.adapters.dell_us import DellUsAdapter
from pricewatch.fetching.types import AcquiredPage

URL_STRING = (
    "https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250/aa18250_reg_01"
)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "dell"


def page(body: str, final_url: str = URL_STRING) -> AcquiredPage:
    return AcquiredPage(URL_STRING, final_url, 200, body.encode(), {}, "http", datetime.now(UTC))


def test_extracts_synthetic_alienware_snapshot():
    snapshot = DellUsAdapter().extract(page((FIXTURES / "product.html").read_text()))
    assert snapshot.identity.site == "dell-us"
    assert snapshot.identity.sku == "aa18250_reg_01"
    assert "RTX 5090" in (snapshot.configuration.gpu or "")
    assert snapshot.configuration.memory == "64 GB DDR5"
    assert snapshot.price.currency == "USD"
    assert snapshot.price.minor == 299999
    assert snapshot.confidence >= Decimal("0.90")


def test_financing_and_savings_are_not_selected_as_sale_price():
    snapshot = DellUsAdapter().extract(page((FIXTURES / "product.html").read_text()))
    assert snapshot.price.minor not in {18900, 12500}


def test_browser_fallback_markup_extracts_primary_price():
    snapshot = DellUsAdapter().extract(page((FIXTURES / "product-browser.html").read_text()))
    assert snapshot.price.minor == 299999
    assert snapshot.configuration.memory == "64 GB DDR5"


def test_conflicting_structured_current_prices_require_review():
    html = (
        (FIXTURES / "product.html")
        .read_text()
        .replace(
            "</head>",
            '<meta property="product:price:amount" content="3999.99">'
            '<meta property="product:price:currency" content="USD"></head>',
        )
    )
    with pytest.raises(AmbiguousExtraction):
        DellUsAdapter().extract(page(html))


def test_supports_dell_us_product_not_other_regions():
    adapter = DellUsAdapter()
    assert adapter.supports(URL(URL_STRING))
    assert not adapter.supports(URL("https://www.dell.com/en-uk/shop/spd/example"))
