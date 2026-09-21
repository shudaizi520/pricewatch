from pricewatch.web.configuration import configuration_rows


def test_structured_configuration_is_displayed_as_labeled_rows():
    rows = configuration_rows({"cpu": "Core Ultra 9", "gpu": "RTX 5090"})
    assert rows == [("处理器", "Core Ultra 9"), ("显卡", "RTX 5090")]


def test_detail_can_show_auto_selected_power_supply_separately():
    rows = configuration_rows(
        {"gpu": "RTX 5090", "extras": {"Graphics Card": "RTX 5090", "Power Supply": "360W"}},
        include_extras=True,
    )
    assert rows == [("显卡", "RTX 5090"), ("电源", "360W")]


def test_legacy_summary_is_split_into_readable_rows():
    rows = configuration_rows({"summary": "Core Ultra 9 · RTX 5090"})
    assert [value for _, value in rows] == ["Core Ultra 9", "RTX 5090"]
