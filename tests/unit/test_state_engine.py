from datetime import UTC, datetime

from pricewatch.domain.events import DomainEvent
from pricewatch.services.checks import event_key


def test_repeated_same_transition_at_different_times_gets_distinct_keys():
    first = DomainEvent(
        1, "price_changed", datetime(2026, 9, 21, tzinfo=UTC), {"price": 300}, {"price": 200}
    )
    later = DomainEvent(
        1, "price_changed", datetime(2026, 10, 21, tzinfo=UTC), {"price": 300}, {"price": 200}
    )
    assert event_key(first) != event_key(later)
    assert event_key(first) == event_key(first)
