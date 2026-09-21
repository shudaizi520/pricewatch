import pytest

from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot


def test_configuration_fingerprint_ignores_case_and_whitespace():
    left = ProductConfiguration(gpu=" NVIDIA  RTX 5090 ", memory="64 GB")
    right = ProductConfiguration(gpu="nvidia rtx 5090", memory="64GB")
    assert left.fingerprint() == right.fingerprint()


def test_configuration_fingerprint_not_affected_by_price_or_url():
    config = ProductConfiguration(cpu="Ultra 9", gpu="RTX 5090")
    first = ProductSnapshot(
        ProductIdentity("dell-us", "sku"),
        "https://dell.com/a",
        "Laptop",
        config,
        Money("USD", 300000),
    )
    second = ProductSnapshot(
        ProductIdentity("dell-us", "sku"),
        "https://dell.com/b",
        "Laptop",
        config,
        Money("USD", 200000),
    )
    assert first.configuration.fingerprint() == second.configuration.fingerprint()


def test_money_rejects_floating_point():
    with pytest.raises(TypeError):
        Money(currency="USD", minor=2999.99)


def test_money_rejects_negative_and_bad_currency():
    with pytest.raises((TypeError, ValueError)):
        Money("USD", -1)
    with pytest.raises(ValueError):
        Money("US", 100)
