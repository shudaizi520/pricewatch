from datetime import UTC, datetime

import pytest

from pricewatch.adapters.base import AmbiguousExtraction, ExtractionError
from pricewatch.adapters.generic import GenericAdapter
from pricewatch.domain.products import Money
from pricewatch.fetching.types import AcquiredPage


def page(html: str) -> AcquiredPage:
    return AcquiredPage(
        "https://www.hp.com/us-en/shop/pdp/example",
        "https://www.hp.com/us-en/shop/pdp/example",
        200,
        html.encode(),
        {},
        "http",
        datetime.now(UTC),
    )


def test_generic_adapter_accepts_schema_org_product():
    html = (
        '<script type="application/ld+json">{"@type":"Product","name":"OMEN MAX 16",'
        '"offers":{"price":"3499.99","priceCurrency":"USD"}}</script>'
    )
    snapshot = GenericAdapter().extract(page(html))
    assert snapshot.name == "OMEN MAX 16"
    assert snapshot.price == Money("USD", 349999)
    assert snapshot.configuration.gpu is None


def test_generic_adapter_accepts_lowercase_iso_currency_from_dell_china():
    html = (
        '<script type="application/ld+json">{"@type":"Product",'
        '"name":"Alienware 外星人 18 Area-51 游戏笔记本",'
        '"sku":"aa18250_reg_01",'
        '"offers":{"price":"37414.3","priceCurrency":"cny"}}</script>'
    )
    snapshot = GenericAdapter().extract(page(html))
    assert snapshot.identity.sku == "aa18250_reg_01"
    assert snapshot.price == Money("CNY", 3741430)


def test_ambiguous_prices_require_confirmation():
    html = (
        '<script type="application/ld+json">{"@type":"Product","name":"OMEN",'
        '"offers":[{"price":"3499.99","priceCurrency":"USD"},'
        '{"price":"2999.99","priceCurrency":"USD"}]}</script>'
    )
    with pytest.raises(AmbiguousExtraction):
        GenericAdapter().extract(page(html))


def test_generic_adapter_rejects_price_without_currency():
    html = (
        '<script type="application/ld+json">{"@type":"Product","name":"OMEN",'
        '"offers":{"price":"2999.99"}}</script>'
    )
    with pytest.raises(ExtractionError):
        GenericAdapter().extract(page(html))


def test_relative_or_cross_site_canonical_is_not_adopted():
    product = (
        '<script type="application/ld+json">{"@type":"Product","name":"OMEN",'
        '"offers":{"price":"2999.99","priceCurrency":"USD"}}</script>'
    )
    for href in ("/another-product", "https://example.com/other"):
        html = f'<link rel="canonical" href="{href}">{product}'
        assert GenericAdapter().extract(page(html)).canonical_url == page(html).final_url
