from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pricewatch.fetching.dell_options import (
    active_price_minor,
    catalog_from_html,
    configured_price_minor,
    selected_from_html,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "dell" / "options.html"


def test_catalog_uses_dell_group_and_option_titles_not_price_deltas():
    catalog = catalog_from_html(FIXTURE.read_text())
    assert catalog["Graphics Card"] == [
        "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7",
        "NVIDIA® GeForce RTX™ 5090 24 GB GDDR7",
    ]
    assert catalog["Power Supply"] == [
        "280W 7.4mm AC Adapter",
        "360W Small Form Factor Adapter",
    ]


def test_selected_options_are_bound_to_groups():
    selected = selected_from_html(FIXTURE.read_text())
    assert selected == {
        "Graphics Card": "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7",
        "Power Supply": "280W 7.4mm AC Adapter",
    }


def test_configured_price_uses_visible_purchase_block_not_static_jsonld():
    html = FIXTURE.read_text().replace("<span>$3,999.99</span>", "<span>$5,099.99</span>")
    assert configured_price_minor(html) == 509999


def test_active_browser_text_accepts_dell_whitespace_and_same_price_repeated():
    assert active_price_minor("Dell Price\n$ 5,099.99\nDell Price $5,099.99") == 509999


@pytest.mark.parametrize("price", ["$4,000", "EUR 4,000.00", "-$1.00"])
def test_configured_price_rejects_missing_or_non_usd_offer(price):
    html = FIXTURE.read_text().replace("<span>$3,999.99</span>", f"<span>{price}</span>")
    with pytest.raises(ValueError):
        configured_price_minor(html)


def test_duplicate_group_is_rejected():
    html = FIXTURE.read_text()
    first_group = html.split('<div class="accordion-box">', 1)[1].split(
        '<div class="accordion-box">', 1
    )[0]
    duplicated = html.replace(
        "</section>", '<div class="accordion-box">' + first_group + "</section>"
    )
    with pytest.raises(ValueError):
        catalog_from_html(duplicated)


def test_duplicate_unselected_option_label_is_deduplicated_in_catalog():
    soup = BeautifulSoup(FIXTURE.read_text(), "lxml")
    group = soup.select_one('[aria-label="Graphics Card"]')
    group.append(deepcopy(group.select(".option-grid-wrapper")[1]))
    assert len(catalog_from_html(str(soup))["Graphics Card"]) == 2
