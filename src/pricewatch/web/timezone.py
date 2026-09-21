"""User-facing Beijing time; database timestamps remain UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC).astimezone(BEIJING)
        if value.tzinfo is None
        else value.astimezone(BEIJING)
    )


def beijing_label(value: datetime | None) -> str:
    localized = beijing_time(value)
    return localized.strftime("%Y-%m-%d %H:%M") if localized else "—"
