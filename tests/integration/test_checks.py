from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from pricewatch.db.base import Base
from pricewatch.db.models import Observation, Product
from pricewatch.db.session import create_engine_and_session
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
from pricewatch.services.checks import CheckService


@pytest.fixture
def check_service(settings):
    engine, factory = create_engine_and_session(settings)
    Base.metadata.create_all(engine)
    with factory() as session:
        item = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/en-us/shop/spd/x/sku_01",
            status="active",
            sku="sku_01",
        )
        session.add(item)
        session.commit()
        product_id = item.id
    yield CheckService(factory), factory, product_id
    engine.dispose()


def snapshot(
    gpu="RTX 5090",
    amount=299999,
    url="https://www.dell.com/en-us/shop/spd/x/sku_01",
    availability="in_stock",
):
    return ProductSnapshot(
        ProductIdentity("dell-us", "sku_01"),
        url,
        "Alienware",
        ProductConfiguration(gpu=gpu),
        Money("USD", amount),
        availability=availability,
    )


def test_configuration_change_pauses_comparison(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    outcome = service.accept_snapshot(product_id, snapshot(gpu="RTX 5080", amount=249999))
    assert outcome.product.status == "needs_attention"
    assert [e.kind for e in outcome.events] == ["configuration_changed"]
    with factory() as session:
        trusted_count = select(func.count(Observation.id)).where(Observation.trusted.is_(True))
        assert session.scalar(trusted_count) == 1


def test_unchanged_checks_store_at_most_one_daily_checkpoint(check_service):
    service, factory, product_id = check_service
    for hour in (1, 7, 13, 19):
        outcome = service.accept_snapshot(
            product_id, snapshot(), observed_at=datetime(2026, 9, 21, hour, tzinfo=UTC)
        )
    assert outcome.events == []
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1


def test_price_and_stock_changes_emit_distinct_events(check_service):
    service, _, product_id = check_service
    first = service.accept_snapshot(product_id, snapshot())
    changed = service.accept_snapshot(
        product_id, snapshot(amount=279999, availability="out_of_stock")
    )
    assert [event.kind for event in first.events] == ["initial_observation"]
    assert {event.kind for event in changed.events} == {"price_changed", "stock_changed"}


def test_redirect_updates_canonical_url_only_when_identity_matches(check_service):
    service, _, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    outcome = service.accept_snapshot(
        product_id, snapshot(url="https://www.dell.com/en-us/new/sku_01")
    )
    assert outcome.product.canonical_url.endswith("/sku_01")


def test_failure_alerts_once_at_three_and_recovers_once(check_service):
    service, factory, product_id = check_service
    service.accept_snapshot(product_id, snapshot())
    assert service.record_failure(product_id, "blocked").events == []
    assert service.record_failure(product_id, "blocked").events == []
    assert [event.kind for event in service.record_failure(product_id, "blocked").events] == [
        "check_failed"
    ]
    assert service.record_failure(product_id, "blocked").events == []
    assert [event.kind for event in service.accept_snapshot(product_id, snapshot()).events] == [
        "check_recovered"
    ]
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1


@pytest.mark.anyio
async def test_non_unique_dell_sku_lookup_requires_attention(check_service):
    service, factory, product_id = check_service

    class FailingPipeline:
        async def acquire(self, url, adapter):
            raise RuntimeError("page disappeared")

    async def lookup(sku):
        assert sku == "sku_01"
        return [snapshot(), snapshot(gpu="RTX 5080")]

    service.pipeline = FailingPipeline()
    service.dell_lookup = lookup
    outcome = await service.check_product(product_id, "scheduled")
    assert outcome.product.status == "needs_attention"
    with factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 0
