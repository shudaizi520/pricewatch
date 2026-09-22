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


def test_non_price_dell_extras_do_not_change_configuration_identity():
    first = ProductConfiguration(
        gpu="RTX 5090",
        extras={
            "Keyboard": "CherryMX",
            "Wireless": "Wi-Fi 7 A",
            "Operating System Languages": "English",
            "Documentation": "No Documentation",
            "Power Supply": "360W",
        },
    )
    second = ProductConfiguration(
        gpu="RTX 5090",
        extras={
            "Keyboard": "CherryMX",
            "Wireless": "Wi-Fi 7 B",
            "Operating System Languages": "English, French",
            "Documentation": "Regular Documentation",
            "Power Supply": "330W",
        },
    )
    assert first.fingerprint() == second.fingerprint()


def test_keyboard_choice_still_changes_configuration_identity():
    standard = ProductConfiguration(gpu="RTX 5090", extras={"Keyboard": "Standard"})
    cherry = ProductConfiguration(gpu="RTX 5090", extras={"Keyboard": "CherryMX"})
    assert standard.fingerprint() != cherry.fingerprint()


def test_money_rejects_floating_point():
    with pytest.raises(TypeError):
        Money(currency="USD", minor=2999.99)


def test_money_rejects_negative_and_bad_currency():
    with pytest.raises((TypeError, ValueError)):
        Money("USD", -1)
    with pytest.raises(ValueError):
        Money("US", 100)


def test_configuration_record_keeps_named_hardware_and_legacy_summary():
    record = ProductConfiguration(
        cpu="Core Ultra 9", gpu="RTX 5090", extras={"keyboard": "CherryMX"}
    ).as_record()
    assert record["cpu"] == "Core Ultra 9"
    assert record["gpu"] == "RTX 5090"
    assert record["extras"] == {"keyboard": "CherryMX"}
    assert record["summary"] == "Core Ultra 9 · RTX 5090"
