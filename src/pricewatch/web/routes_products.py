"""Authenticated product dashboard and management workflows."""

import csv
import io
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from pricewatch.adapters.base import ExtractionError
from pricewatch.db.models import Administrator, Observation, Product
from pricewatch.domain.events import DomainEvent
from pricewatch.domain.products import Money, ProductSnapshot
from pricewatch.fetching.http import AcquisitionError
from pricewatch.fetching.safety import UnsafeUrlError, validate_public_url
from pricewatch.web.dependencies import csrf_token, require_admin, verify_csrf

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


def _context(request: Request, **values: object) -> dict[str, object]:
    admin = require_admin(request)
    return {"request": request, "admin": admin, "csrf_token": csrf_token(request), **values}


def _product(request: Request, product_id: int) -> Product:
    require_admin(request)
    with request.app.state.session_factory() as session:
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(404)
        session.expunge(product)
        return product  # type: ignore[no-any-return]


def _sparkline(observations: list[Observation]) -> str:
    prices = [row.price_minor for row in reversed(observations)]
    if not prices:
        return "0,24 135,24"
    low, high = min(prices), max(prices)
    span = max(high - low, 1)
    return " ".join(
        f"{round(index * 135 / max(len(prices) - 1, 1))},{round(43 - (price - low) * 38 / span)}"
        for index, price in enumerate(prices)
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    require_admin(request)
    with request.app.state.session_factory() as session:
        products = session.scalars(
            select(Product).where(Product.status != "archived").order_by(Product.id.desc())
        ).all()
        cards = []
        for product in products:
            observations = session.scalars(
                select(Observation)
                .where(Observation.product_id == product.id, Observation.trusted.is_(True))
                .order_by(Observation.observed_at.desc())
                .limit(30)
            ).all()
            latest = observations[0] if observations else None
            cards.append(
                {
                    "product": product,
                    "latest": latest,
                    "lowest": session.scalar(
                        select(func.min(Observation.price_minor)).where(
                            Observation.product_id == product.id,
                            Observation.trusted.is_(True),
                            Observation.currency == latest.currency,
                        )
                    ) if latest else None,
                    "sparkline": _sparkline(observations),
                }
            )
        usd_lows = [
            card["lowest"]
            for card in cards
            if card["latest"] is not None and card["latest"].currency == "USD"
            and card["lowest"] is not None
        ]
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_context(request, cards=cards, usd_low=min(usd_lows) if usd_lows else None),
    )


@router.get("/products/new", response_class=HTMLResponse)
async def product_add(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="product_add.html",
        context=_context(request, preview=None, error=None),
    )


@router.post("/products/preview", response_class=HTMLResponse)
async def product_preview(
    request: Request, url: str = Form(), submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    try:
        target = validate_public_url(url)
        pipeline = request.app.state.check_service.pipeline
        if pipeline is None:
            raise ValueError("检查器尚未启动")
        _, snapshot = await pipeline.acquire(
            target, request.app.state.check_service.registry.for_url(target)
        )
    except (UnsafeUrlError, ValueError, RuntimeError, AcquisitionError, ExtractionError) as error:
        return templates.TemplateResponse(
            request=request,
            name="product_add.html",
            context=_context(request, preview=None, error=str(error)),
            status_code=422,
        )
    request.app.state.previews = {
        key: value
        for key, value in request.app.state.previews.items()
        if value[2] >= datetime.now(UTC) and value[0] != admin.id
    }
    token = secrets.token_urlsafe(32)
    request.app.state.previews[token] = (
        admin.id,
        snapshot,
        datetime.now(UTC) + timedelta(minutes=15),
    )
    return templates.TemplateResponse(
        request=request,
        name="product_add.html",
        context=_context(request, preview=snapshot, preview_token=token, error=None),
    )


@router.post("/products/confirm")
async def product_confirm(
    request: Request, preview_token: str = Form(), submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    entry = request.app.state.previews.pop(preview_token, None)
    if entry is None or entry[0] != admin.id or entry[2] < datetime.now(UTC):
        raise HTTPException(400, "预览已过期 请重新获取")
    snapshot: ProductSnapshot = entry[1]
    with request.app.state.session_factory.begin() as session:
        product = Product(
            source_site=snapshot.identity.site,
            requested_url=snapshot.canonical_url,
            canonical_url=snapshot.canonical_url,
            name=snapshot.name,
            sku=snapshot.identity.sku,
        )
        session.add(product)
        session.flush()
        product_id = product.id
    request.app.state.check_service.accept_snapshot(product_id, snapshot, trigger="initial")
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: int) -> Response:
    product = _product(request, product_id)
    with request.app.state.session_factory() as session:
        history = session.scalars(
            select(Observation)
            .where(Observation.product_id == product_id, Observation.trusted.is_(True))
            .order_by(Observation.observed_at.desc())
            .limit(100)
        ).all()
        pending = session.scalar(
            select(Observation)
            .where(Observation.product_id == product_id, Observation.trusted.is_(False))
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(1)
        )
    return templates.TemplateResponse(
        request=request,
        name="product_detail.html",
        context=_context(request, product=product, history=history, pending=pending),
    )


@router.post("/products/{product_id}/check")
async def manual_check(
    request: Request, product_id: int, submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    request.app.state.scheduler.request_check(product_id, "manual")
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/settings")
async def product_settings(
    request: Request,
    product_id: int,
    target_price: str = Form(""),
    notify_mode: str = Form("changes"),
    check_interval_hours: int = Form(6),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    if notify_mode not in ("changes", "target_or_change") or check_interval_hours not in (
        1,
        3,
        6,
        12,
        24,
    ):
        raise HTTPException(422)
    try:
        target = (
            Money.from_decimal("USD", Decimal(target_price)).minor if target_price.strip() else None
        )
    except (InvalidOperation, ValueError, TypeError) as error:
        raise HTTPException(422, "目标价格不正确") from error
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        product.target_price_minor = target
        product.notify_mode = notify_mode
        product.check_interval_hours = check_interval_hours
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/pause")
async def pause_product(
    request: Request, product_id: int, submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        product.status = "active" if product.status == "paused" else "paused"
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/archive")
async def archive_product(
    request: Request, product_id: int, submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        product.status = "archived"
    return RedirectResponse("/", status_code=303)


@router.get("/products/{product_id}/delete", response_class=HTMLResponse)
async def delete_confirmation(request: Request, product_id: int) -> Response:
    product = _product(request, product_id)
    return templates.TemplateResponse(
        request=request,
        name="product_delete.html",
        context=_context(request, product=product),
    )


@router.post("/products/{product_id}/delete")
async def delete_product(
    request: Request,
    product_id: int,
    confirmation: str = Form(),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    product = _product(request, product_id)
    if product.status != "archived" or confirmation != "DELETE":
        raise HTTPException(400, "请先归档并输入 DELETE 确认")
    with request.app.state.session_factory.begin() as session:
        stored = session.get(Product, product_id)
        session.delete(stored)
    return RedirectResponse("/", status_code=303)


@router.post("/products/{product_id}/confirm-configuration")
async def confirm_configuration(
    request: Request,
    product_id: int,
    action: str = Form("adopt"),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    if action not in ("adopt", "separate"):
        raise HTTPException(422)
    initial_event = None
    target_product_id = product_id
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        if product.status != "needs_attention":
            raise HTTPException(409)
        pending = session.scalar(
            select(Observation)
            .where(Observation.product_id == product_id, Observation.trusted.is_(False))
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(1)
        )
        if pending is None:
            raise HTTPException(409, "没有待确认的新配置")
        if action == "separate":
            fresh = Product(
                source_site=product.source_site,
                requested_url=(pending.configuration or {}).get("canonical_url")
                or product.requested_url,
                canonical_url=(pending.configuration or {}).get("canonical_url")
                or product.canonical_url,
                name=(pending.configuration or {}).get("name") or product.name,
                sku=(pending.configuration or {}).get("sku"),
                configuration={"summary": (pending.configuration or {}).get("summary", "")},
                configuration_fingerprint=pending.configuration_fingerprint,
                check_interval_hours=product.check_interval_hours,
                last_checked_at=pending.observed_at,
                last_success_at=pending.observed_at,
            )
            session.add(fresh)
            session.flush()
            pending.product_id = fresh.id
            pending.trusted = True
            product.status = "paused"
            target_product_id = fresh.id
            initial_event = DomainEvent(
                fresh.id,
                "initial_observation",
                datetime.now(UTC),
                {},
                {"price_minor": pending.price_minor, "currency": pending.currency},
            )
            request.app.state.check_service._queue_events(session, fresh, [initial_event])
        else:
            pending.trusted = True
            product.configuration = {"summary": (pending.configuration or {}).get("summary", "")}
            product.configuration_fingerprint = pending.configuration_fingerprint
            product.sku = (pending.configuration or {}).get("sku") or product.sku
            product.name = (pending.configuration or {}).get("name") or product.name
            product.canonical_url = (pending.configuration or {}).get(
                "canonical_url"
            ) or product.canonical_url
            product.last_success_at = pending.observed_at
            product.status = "active"
    if action == "separate":
        request.app.state.scheduler.schedule_product(target_product_id)
        if initial_event is not None:
            request.app.state.check_service._notify([initial_event])
    return RedirectResponse(f"/products/{target_product_id}", status_code=303)


@router.get("/products/{product_id}/history.csv")
async def history_csv(request: Request, product_id: int) -> Response:
    _product(request, product_id)
    with request.app.state.session_factory() as session:
        rows = session.scalars(
            select(Observation)
            .where(Observation.product_id == product_id, Observation.trusted.is_(True))
            .order_by(Observation.observed_at.asc())
        ).all()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("observed_at_utc", "currency", "price_minor", "price", "availability"))
        for row in rows:
            writer.writerow(
                (
                    row.observed_at.replace(tzinfo=UTC).isoformat(),
                    row.currency,
                    row.price_minor,
                    f"{row.price_minor / 100:.2f}",
                    row.availability,
                )
            )
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="product-{product_id}.csv"'},
    )


@router.post("/theme")
async def set_theme(
    request: Request, theme: str = Form(), submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    if theme not in ("system", "light", "dark"):
        raise HTTPException(422)
    with request.app.state.session_factory.begin() as session:
        stored = session.get(Administrator, admin.id)
        assert stored is not None
        stored.theme = theme
    return RedirectResponse("/", status_code=303)
