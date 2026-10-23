"""Only explicitly known, same-product Dell category migrations are candidates."""

import pytest
from httpx import URL

from pricewatch.fetching.dell_entries import dell_check_entries

ROOT = "https://www.dell.com/en-us/shop/"
MODEL = "alienware18area51aa18250"
SKU = "aa18250_reg_01"
OLD = ROOT + "laptop-computers/spd/" + MODEL
NEW = ROOT + "dell-laptops/spd/" + MODEL


def urls(original=OLD, canonical=OLD + "/" + SKU, sku=SKU):
    return [str(url) for _, url in dell_check_entries(URL(original), URL(canonical), sku)]


def test_known_laptop_category_has_four_distinct_bounded_entries():
    assert urls() == [OLD, OLD + "/" + SKU, NEW, NEW + "/" + SKU]


def test_previously_verified_new_category_is_reused_before_old_original():
    assert urls(canonical=NEW + "/" + SKU) == [
        NEW, NEW + "/" + SKU, OLD, OLD + "/" + SKU,
    ]


def test_existing_new_category_does_not_generate_random_category_variants():
    assert urls(NEW, NEW + "/" + SKU) == [NEW, NEW + "/" + SKU]


def test_identical_family_input_is_deduplicated_and_remains_bounded():
    assert urls(canonical=OLD) == [OLD, NEW, NEW + "/" + SKU]


def test_last_working_saved_sku_is_reused_before_known_failing_family():
    entries = dell_check_entries(URL(OLD), URL(OLD + "/" + SKU), SKU, OLD + "/" + SKU)
    assert [str(url) for _, url in entries] == [OLD + "/" + SKU, OLD, NEW, NEW + "/" + SKU]


@pytest.mark.parametrize("hint", ["https://evil.example/private", NEW+"/other_sku", "bad url"])
def test_stale_or_unsafe_hint_never_adds_requests(hint):
    entries = dell_check_entries(URL(OLD), URL(OLD + "/" + SKU), SKU, hint)
    assert [str(url) for _, url in entries] == urls()


@pytest.mark.parametrize("original,canonical,sku", [
    (OLD, OLD + "/other_sku", SKU),
    (OLD.replace(MODEL, "other-model"), OLD + "/" + SKU, SKU),
    (OLD + "?token=do-not-copy", OLD + "/" + SKU, SKU),
    (OLD, OLD + "/" + SKU + "#fragment", SKU),
    (OLD, OLD + "/" + SKU, "../other"),
    (OLD.replace("laptop-computers", "desktop-computers"),
     OLD.replace("laptop-computers", "desktop-computers") + "/" + SKU, SKU),
    (OLD.replace("https:", "http:"), OLD + "/" + SKU, SKU),
    (OLD.replace("www.dell.com", "evil.example"), OLD + "/" + SKU, SKU),
    (OLD.replace("www.dell.com", "user:secret@www.dell.com"), OLD + "/" + SKU, SKU),
    (OLD.replace("www.dell.com", "www.dell.com:8443"), OLD + "/" + SKU, SKU),
    (OLD.replace("en-us", "en-uk"), OLD + "/" + SKU, SKU),
    (OLD.replace(MODEL, MODEL + "%2fother"), OLD + "/" + SKU, SKU),
])
def test_unsafe_or_ambiguous_inputs_never_generate_a_category_alias(original, canonical, sku):
    assert all("/dell-laptops/" not in url for url in urls(original, canonical, sku))
