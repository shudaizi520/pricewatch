"""Readable configuration rows for current and summary-only records."""

import re

LABELS = {
    "cpu": "处理器",
    "gpu": "显卡",
    "memory": "内存",
    "storage": "存储",
    "display": "屏幕",
    "os": "系统",
}

EXTRA_LABELS = {
    "Power Supply": "电源",
    "Power Cord": "电源线",
    "Primary Battery": "电池",
    "Camera": "摄像头",
    "Keyboard": "键盘",
    "Wireless": "无线网卡",
    "Base Warranty": "保修",
}


def _legacy_label(value: str) -> str:
    if re.search(r"RTX|GeForce|Radeon", value, re.I):
        return "显卡"
    if re.search(r"\bDDR\d|\d+\s*GB\s*(?:RAM|内存)", value, re.I):
        return "内存"
    if re.search(r"\bSSD\b|\b\d+\s*TB\b", value, re.I):
        return "存储"
    if re.search(r'\bWQXGA\b|\d{2}\s*(?:"|″|inch)', value, re.I):
        return "屏幕"
    if re.search(r"\bCore\b|\bRyzen\b|处理器", value, re.I):
        return "处理器"
    return "配置"


def configuration_rows(
    record: dict[str, object] | None, include_extras: bool = False
) -> list[tuple[str, str]]:
    if not record:
        return []
    extras = record.get("extras")
    cpu = record.get("cpu")
    processor = extras.get("Processor") if isinstance(extras, dict) else None
    if (
        (not isinstance(cpu, str) or not cpu.strip())
        and isinstance(processor, str)
        and processor.strip()
    ):
        record = {**record, "cpu": processor}
    rows = [
        (label, str(record[key]).strip())
        for key, label in LABELS.items()
        if isinstance(record.get(key), str) and str(record[key]).strip()
    ]
    if isinstance(extras, dict):
        known = {value for _, value in rows}
        for key, value in extras.items():
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and value.strip()
                and value not in known
                and (include_extras or key == "Keyboard")
            ):
                rows.append((EXTRA_LABELS.get(key, key), value.strip()))
    if rows:
        return rows
    summary = record.get("summary")
    if not isinstance(summary, str):
        return []
    values = [part.strip() for part in summary.split(" · ") if part.strip()]
    return [(_legacy_label(value), value) for value in values]


def secondary_configuration_rows(record: dict[str, object] | None) -> list[tuple[str, str]]:
    """Additional page details that should not dominate a monitor card."""
    primary = configuration_rows(record)
    return [row for row in configuration_rows(record, include_extras=True) if row not in primary]
