"""Bounded recovery timing; no network retries or notification policy here."""

from datetime import UTC, datetime, timedelta


def failure_retry_delay(failures: int) -> timedelta:
    if failures < 1:
        raise ValueError("Failure count must be positive")
    if failures == 1:
        return timedelta(minutes=30)
    if failures == 2:
        return timedelta(hours=1)
    return timedelta(hours=3)


def failure_retry_at(
    now: datetime, failures: int, scheduled_at: datetime | None = None,
) -> datetime:
    retry = now + failure_retry_delay(failures)
    if scheduled_at is not None:
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        if now < scheduled_at < retry:
            return scheduled_at
    return retry
