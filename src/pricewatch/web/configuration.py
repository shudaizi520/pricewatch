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


def configuration_rows(record: dict[str, object] | None) -> list[tuple[str, str]]:
    if not record:
        return []
    rows = [
        (label, str(record[key]).strip())
        for key, label in LABELS.items()
        if isinstance(record.get(key), str) and str(record[key]).strip()
    ]
    if rows:
        return rows
    summary = record.get("summary")
    if not isinstance(summary, str):
        return []
    values = [part.strip() for part in summary.split(" · ") if part.strip()]
    return [(_legacy_label(value), value) for value in values]
