from datetime import UTC, datetime, timedelta

import pytest

from pricewatch.domain.recovery import failure_retry_at, failure_retry_delay


@pytest.mark.parametrize("count,minutes", [(1, 30), (2, 60), (3, 180), (1000000, 180)])
def test_recovery_backoff_is_capped_without_unbounded_requests(count, minutes):
    assert failure_retry_delay(count) == timedelta(minutes=minutes)


@pytest.mark.parametrize("count", [0, -1])
def test_recovery_requires_an_actual_failure(count):
    with pytest.raises(ValueError):
        failure_retry_delay(count)


def test_recovery_preserves_nearer_naive_sqlite_schedule_but_not_expired_due_time():
    now = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert failure_retry_at(now, 4, datetime(2026, 11, 5, 2, 10)) == now + timedelta(minutes=10)
    assert failure_retry_at(now, 4, datetime(2026, 11, 5, 1, 0)) == now + timedelta(hours=3)
