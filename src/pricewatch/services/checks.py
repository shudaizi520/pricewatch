"""Trusted observation comparison and persistent check outcomes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from httpx import URL
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.adapters.base import ExtractionError
from pricewatch.adapters.registry import AdapterRegistry
from pricewatch.db.models import CheckRun, NotificationDelivery, Observation, Product
from pricewatch.domain.events import DomainEvent
from pricewatch.domain.products import ProductSnapshot
from pricewatch.fetching.browser import AcquisitionPipeline
from pricewatch.fetching.http import AcquisitionError
from pricewatch.services.notifications import NotificationService, format_message

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
        notifier: NotificationService | None = None,
    ) -> None:
        self.factory = factory
        self.pipeline = pipeline
        self.registry = registry or AdapterRegistry()
        self.dell_lookup = dell_lookup
        self.notifier = notifier

    def _notify(self, events: list[DomainEvent]) -> None:
        if self.notifier is not None:
            for event in events:
                self.notifier.deliver(event)

    @staticmethod
    def _queue_events(session: Session, product: Product, events: list[DomainEvent]) -> None:
        if product.notifications_enabled:
            for event in events:
                session.add(
                    NotificationDelivery(
                        product_id=product.id,
                        event_key=event.key(),
                        event_type=event.kind,
                        status="pending",
                        attempts=0,
                        message_text=format_message(event, product),
                    )
                )

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
                .where(Observation.product_id == product_id, Observation.trusted.is_(True))
                .order_by(Observation.observed_at.desc(), Observation.id.desc())
                .limit(1)
            )
            pending = session.scalar(
                select(Observation)
                .where(Observation.product_id == product_id, Observation.trusted.is_(False))
                .order_by(Observation.observed_at.desc(), Observation.id.desc())
                .limit(1)
            )
            fingerprint = snapshot.configuration.fingerprint()
            identity_drift = snapshot.identity.site != product.source_site or (
                product.source_site != "dell-us"
                and previous is not None
                and (
                    (
                        product.name is not None
                        and product.name.casefold() != snapshot.name.casefold()
                    )
                    or (
                        not product.sku
                        and product.canonical_url is not None
                        and product.canonical_url != snapshot.canonical_url
                    )
                )
            )
            if identity_drift:
                product.status = "needs_attention"
                events.append(
                    DomainEvent(
                        product_id,
                        "configuration_changed",
                        now,
                        {"name": product.name, "url": product.canonical_url},
                        {"name": snapshot.name, "url": snapshot.canonical_url},
                    )
                )
            elif product.sku and snapshot.identity.sku and product.sku != snapshot.identity.sku:
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
                        {
                            "fingerprint": previous.configuration_fingerprint,
                            "summary": (previous.configuration or {}).get("summary"),
                        },
                        {"fingerprint": fingerprint, "summary": snapshot.configuration.summary()},
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
                                    {
                                        "price_minor": snapshot.price.minor,
                                        "currency": snapshot.price.currency,
                                    },
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
                                configuration=snapshot.configuration.as_record(),
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
                    product.configuration = snapshot.configuration.as_record()
                    product.last_success_at = now
            if product.status == "needs_attention":
                if pending is None or pending.configuration_fingerprint != fingerprint:
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
                            configuration={
                                **snapshot.configuration.as_record(),
                                "sku": snapshot.identity.sku,
                                "canonical_url": snapshot.canonical_url,
                                "name": snapshot.name,
                            },
                            evidence=snapshot.evidence,
                            observed_at=now,
                            trusted=False,
                        )
                    )
                elif (
                    pending is not None
                    and (pending.configuration or {}).get("name") == snapshot.name
                ):
                    events = []
            product.last_checked_at = now
            session.add(
                CheckRun(
                    product_id=product_id,
                    trigger=trigger,
                    outcome="ok" if product.status != "needs_attention" else "needs_attention",
                    created_at=now,
                )
            )
            self._queue_events(session, product, events)
            session.flush()
            session.expunge(product)
        self._notify(events)
        return CheckOutcome(
            product, events, "ok" if product.status != "needs_attention" else "needs_attention"
        )

    def record_failure(
        self,
        product_id: int,
        category: str,
        trigger: CheckTrigger = "scheduled",
        error_message: str | None = None,
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
                    error_message=error_message,
                    created_at=now,
                )
            )
            self._queue_events(session, product, events)
            session.flush()
            session.expunge(product)
        self._notify(events)
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
            dell_selection = product.dell_selection
        try:
            adapter = self.registry.for_url(url)
            if dell_selection:
                _, snapshot = await self.pipeline.acquire(
                    url, adapter, dell_selection=dell_selection
                )
                if any(
                    snapshot.configuration.extras.get(group) != label
                    for group, label in dell_selection.items()
                ):
                    raise ExtractionError("戴尔返回的配置与此卡片选择不符")
            else:
                _, snapshot = await self.pipeline.acquire(url, adapter)
        except Exception as error:
            if not dell_selection and site == "dell-us" and sku and self.dell_lookup:
                candidates = await self.dell_lookup(sku)
                if len(candidates) == 1 and candidates[0].identity.sku == sku:
                    return self.accept_snapshot(product_id, candidates[0], trigger=trigger)
                if len(candidates) > 1:
                    with self.factory.begin() as session:
                        item = session.get(Product, product_id)
                        if item is not None:
                            item.status = "needs_attention"
            detail = (
                str(error)[:240] if isinstance(error, (AcquisitionError, ExtractionError)) else None
            )
            return self.record_failure(product_id, type(error).__name__, trigger, detail)
        return self.accept_snapshot(product_id, snapshot, trigger=trigger)
