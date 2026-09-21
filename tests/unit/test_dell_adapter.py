from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import URL

from pricewatch.adapters.base import AmbiguousExtraction, ExtractionError
from pricewatch.adapters.dell_us import DellUsAdapter
from pricewatch.fetching.types import AcquiredPage, ConfiguredOffer

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
    assert "5090" in (snapshot.configuration.gpu or "")
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


def test_current_dell_selected_options_and_lowercase_currency():
    html = """
    <html><head><link rel="canonical" href="https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250/aa18250_reg_01">
    <script type="application/ld+json">{
      "@type":"Product", "name":"Alienware 18 Area-51 Gaming Laptop",
      "sku":"aa18250_reg_01", "description":"Gaming laptop",
      "offers":{"priceCurrency":"usd", "price":"3999.99",
      "availability":"https://schema.org/InStock"}
    }</script></head>
    <body><h1>Alienware 18 Area-51 Gaming Laptop</h1>
    <div>Dell Price <span>$3,999.99</span></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">Intel Core Ultra 9 processor 290HX Plus</span>
      <div class="price scoprice">Selected</div></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">NVIDIA GeForce RTX 5070 8 GB GDDR7</span>
      <div class="price scoprice">Selected</div></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">32GB: 2x16GB, DDR5</span>
      <div class="price scoprice">Selected</div></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">1TB M.2 2230 SSD</span>
      <div class="price scoprice">Selected</div></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">18&quot;, WQXGA, 300Hz</span>
      <div class="price scoprice">Selected</div></div>
    <div class="option-grid-item">
      <span data-test-id="option-title">NVIDIA GeForce RTX 5090 24 GB GDDR7</span>
      <div class="price scoprice">+ $1,100.00</div></div>
    </body></html>
    """
    snapshot = DellUsAdapter().extract(page(html))
    assert snapshot.price.currency == "USD"
    assert snapshot.price.minor == 399999
    assert snapshot.configuration.cpu == "Intel Core Ultra 9 processor 290HX Plus"
    assert snapshot.configuration.gpu == "NVIDIA GeForce RTX 5070 8 GB GDDR7"
    assert snapshot.configuration.memory == "32GB: 2x16GB, DDR5"
    assert snapshot.configuration.storage == "1TB M.2 2230 SSD"
    assert snapshot.configuration.display == '18", WQXGA, 300Hz'


def test_configured_offer_uses_selected_buy_box_price_not_stale_jsonld():
    html = (FIXTURES / "options.html").read_text()
    html = html.replace(
        '8 GB GDDR7</span><div class="price scoprice">Selected',
        '8 GB GDDR7</span><div class="price scoprice">+ $0.00',
    )
    html = html.replace(
        '24 GB GDDR7</span><div class="price scoprice">+ $1,100.00',
        '24 GB GDDR7</span><div class="price scoprice">Selected',
    )
    html = html.replace("<span>$3,999.99</span>", "<span>$5,099.99</span>")
    html = html.replace(
        "</body>", '<div data-testid="processor">Intel Core Ultra 9 290HX</div></body>'
    )
    acquired = page(html)
    acquired = AcquiredPage(
        acquired.requested_url,
        acquired.final_url,
        acquired.status,
        acquired.body,
        acquired.headers,
        acquired.method,
        acquired.fetched_at,
        ConfiguredOffer(
            {
                "Graphics Card": "NVIDIA® GeForce RTX™ 5090 24 GB GDDR7",
                "Power Supply": "280W 7.4mm AC Adapter",
            },
            509999,
        ),
    )
    snapshot = DellUsAdapter().extract(acquired)
    assert snapshot.price.minor == 509999
    assert "5090" in (snapshot.configuration.gpu or "")
    assert snapshot.evidence["price"] == "browser.configured_purchase_price"


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


def test_explicit_list_price_coupon_and_discount_are_separate_from_current_price():
    html = (
        (FIXTURES / "product.html")
        .read_text()
        .replace(
            "</body>",
            '<span data-testid="list-price">$3,499.99</span>'
            '<span data-testid="discount">Save $500</span>'
            '<span data-testid="coupon">Extra 5% off with code ALIEN</span></body>',
        )
    )
    snapshot = DellUsAdapter().extract(page(html))
    assert snapshot.price.minor == 299999
    assert snapshot.list_price.minor == 349999
    assert snapshot.discount_text == "Save $500"
    assert snapshot.coupon_text == "Extra 5% off with code ALIEN"


def test_dell_price_without_identifiable_hardware_is_not_trusted():
    html = (
        (FIXTURES / "product.html")
        .read_text()
        .replace(
            '"description":"Core Ultra 9 275HX, NVIDIA RTX 5090 24GB, '
            '64 GB DDR5, 2 TB SSD, 18 inch QHD+ display",',
            "",
        )
    )
    with pytest.raises(ExtractionError):
        DellUsAdapter().extract(page(html))
