"""Trusted observation comparison and persistent check outcomes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from httpx import URL
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.adapters.registry import AdapterRegistry
from pricewatch.db.models import CheckRun, Observation, Product
from pricewatch.domain.events import DomainEvent
from pricewatch.domain.products import ProductSnapshot
from pricewatch.fetching.browser import AcquisitionPipeline

CheckTrigger = Literal["initial", "manual", "scheduled"]


def event_key(event: DomainEvent) -> str:
    return event.key()


@dataclass(slots=True)
class CheckOutcome:
    product: Product
    events: list[DomainEvent]
    status: str

    def events_of_type(self, kind: str) -> list[DomainEvent]:
        return [event for event in self.events if event.kind == kind]


class CheckService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        pipeline: AcquisitionPipeline | None = None,
        registry: AdapterRegistry | None = None,
        dell_lookup: Callable[[str], Awaitable[list[ProductSnapshot]]] | None = None,
    ) -> None:
        self.factory = factory
        self.pipeline = pipeline
        self.registry = registry or AdapterRegistry()
        self.dell_lookup = dell_lookup

    def accept_snapshot(
        self,
        product_id: int,
        snapshot: ProductSnapshot,
        observed_at: datetime | None = None,
        trigger: CheckTrigger = "manual",
    ) -> CheckOutcome:
        now = observed_at or datetime.now(UTC)
        events: list[DomainEvent] = []
        with self.factory.begin() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise LookupError("Product not found")
            previous = session.scalar(
                select(Observation)
                .where(Observation.product_id == product_id)
                .order_by(Observation.observed_at.desc(), Observation.id.desc())
                .limit(1)
            )
            fingerprint = snapshot.configuration.fingerprint()
            if product.sku and snapshot.identity.sku and product.sku != snapshot.identity.sku:
                product.status = "needs_attention"
                events.append(
                    DomainEvent(
                        product_id,
                        "configuration_changed",
                        now,
                        {"sku": product.sku},
                        {"sku": snapshot.identity.sku},
                    )
                )
            elif previous is not None and previous.configuration_fingerprint != fingerprint:
                product.status = "needs_attention"
                events.append(
                    DomainEvent(
                        product_id,
                        "configuration_changed",
                        now,
                        {"fingerprint": previous.configuration_fingerprint},
                        {"fingerprint": fingerprint},
                    )
                )
            else:
                if previous is None:
                    events.append(
                        DomainEvent(
                            product_id,
                            "initial_observation",
                            now,
                            {},
                            {
                                "price_minor": snapshot.price.minor,
                                "currency": snapshot.price.currency,
                            },
                        )
                    )
                else:
                    if previous.currency != snapshot.price.currency:
                        product.status = "needs_attention"
                        events.append(
                            DomainEvent(
                                product_id,
                                "configuration_changed",
                                now,
                                {"currency": previous.currency},
                                {"currency": snapshot.price.currency},
                            )
                        )
                    if product.status != "needs_attention":
                        if previous.price_minor != snapshot.price.minor:
                            events.append(
                                DomainEvent(
                                    product_id,
                                    "price_changed",
                                    now,
                                    {"price_minor": previous.price_minor},
                                    {"price_minor": snapshot.price.minor},
                                )
                            )
                        if previous.availability != snapshot.availability:
                            events.append(
                                DomainEvent(
                                    product_id,
                                    "stock_changed",
                                    now,
                                    {"availability": previous.availability},
                                    {"availability": snapshot.availability},
                                )
                            )
                        if (
                            previous.list_price_minor
                            != (snapshot.list_price.minor if snapshot.list_price else None)
                            or previous.coupon_text != snapshot.coupon_text
                            or previous.discount_text != snapshot.discount_text
                        ):
                            events.append(
                                DomainEvent(
                                    product_id,
                                    "offer_changed",
                                    now,
                                    {
                                        "coupon": previous.coupon_text,
                                        "discount": previous.discount_text,
                                    },
                                    {
                                        "coupon": snapshot.coupon_text,
                                        "discount": snapshot.discount_text,
                                    },
                                )
                            )
                if product.status != "needs_attention":
                    daily_checkpoint = previous is None or previous.observed_at.date() < now.date()
                    if events or daily_checkpoint:
                        session.add(
                            Observation(
                                product_id=product_id,
                                currency=snapshot.price.currency,
                                price_minor=snapshot.price.minor,
                                list_price_minor=snapshot.list_price.minor
                                if snapshot.list_price
                                else None,
                                discount_text=snapshot.discount_text,
                                coupon_text=snapshot.coupon_text,
                                availability=snapshot.availability,
                                configuration_fingerprint=fingerprint,
                                configuration={"summary": snapshot.configuration.summary()},
                                evidence=snapshot.evidence,
                                observed_at=now,
                                trusted=True,
                            )
                        )
                    if product.failure_reported:
                        events.append(DomainEvent(product_id, "check_recovered", now, {}, {}))
                    product.failure_reported = False
                    product.consecutive_failures = 0
                    product.canonical_url = snapshot.canonical_url
                    product.name = snapshot.name
                    product.sku = snapshot.identity.sku or product.sku
                    product.configuration_fingerprint = fingerprint
                    product.configuration = {"summary": snapshot.configuration.summary()}
                    product.last_success_at = now
            product.last_checked_at = now
            session.add(
                CheckRun(
                    product_id=product_id,
                    trigger=trigger,
                    outcome="ok" if product.status != "needs_attention" else "needs_attention",
                    created_at=now,
                )
            )
            session.flush()
            session.expunge(product)
        return CheckOutcome(
            product, events, "ok" if product.status != "needs_attention" else "needs_attention"
        )

    def record_failure(
        self, product_id: int, category: str, trigger: CheckTrigger = "scheduled"
    ) -> CheckOutcome:
        now = datetime.now(UTC)
        events: list[DomainEvent] = []
        with self.factory.begin() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise LookupError("Product not found")
            product.consecutive_failures += 1
            product.last_checked_at = now
            if product.consecutive_failures == 3 and not product.failure_reported:
                product.failure_reported = True
                events.append(
                    DomainEvent(product_id, "check_failed", now, {}, {"category": category})
                )
            session.add(
                CheckRun(
                    product_id=product_id,
                    trigger=trigger,
                    outcome="failed",
                    error_category=category,
                    created_at=now,
                )
            )
            session.flush()
            session.expunge(product)
        return CheckOutcome(product, events, "failed")

    async def check_product(self, product_id: int, trigger: CheckTrigger) -> CheckOutcome:
        if self.pipeline is None:
            raise RuntimeError("Acquisition pipeline is not configured")
        with self.factory() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise LookupError("Product not found")
            url = URL(product.canonical_url or product.requested_url)
            sku = product.sku
            site = product.source_site
        try:
            _, snapshot = await self.pipeline.acquire(url, self.registry.for_url(url))
        except Exception as error:
            if site == "dell-us" and sku and self.dell_lookup:
                candidates = await self.dell_lookup(sku)
                if len(candidates) == 1 and candidates[0].identity.sku == sku:
                    return self.accept_snapshot(product_id, candidates[0], trigger=trigger)
                if len(candidates) > 1:
                    with self.factory.begin() as session:
                        item = session.get(Product, product_id)
                        if item is not None:
                            item.status = "needs_attention"
            return self.record_failure(product_id, type(error).__name__, trigger)
        return self.accept_snapshot(product_id, snapshot, trigger=trigger)
