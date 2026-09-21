def test_structured_configuration_is_displayed_as_labeled_rows():
    from pricewatch.web.configuration import configuration_rows

    rows = configuration_rows({"cpu": "Core Ultra 9", "gpu": "RTX 5090"})
    assert rows == [("处理器", "Core Ultra 9"), ("显卡", "RTX 5090")]


def test_legacy_summary_is_split_into_readable_rows():
    from pricewatch.web.configuration import configuration_rows

    rows = configuration_rows({"summary": "Core Ultra 9 · RTX 5090"})
    assert [value for _, value in rows] == ["Core Ultra 9", "RTX 5090"]
