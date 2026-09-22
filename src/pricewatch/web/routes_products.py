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
from sqlalchemy.orm import Session

from pricewatch.adapters.base import ExtractionError
from pricewatch.db.models import Administrator, Observation, Product, Setting
from pricewatch.domain.events import DomainEvent
from pricewatch.domain.products import Money, ProductSnapshot
from pricewatch.fetching.dell_options import catalog_from_html
from pricewatch.fetching.http import AcquisitionError
from pricewatch.fetching.safety import UnsafeUrlError, validate_public_url
from pricewatch.services.scheduler import check_time_key, parse_check_time
from pricewatch.web.configuration import configuration_rows, secondary_configuration_rows
from pricewatch.web.dependencies import csrf_token, require_admin, verify_csrf
from pricewatch.web.timezone import beijing_label, beijing_time

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
templates.env.filters["beijing"] = beijing_label


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


def _display_price(currency: str, minor: int) -> str:
    return f"{'$' if currency == 'USD' else currency + ' '}{minor / 100:.2f}"


def _chart_points(observations: list[Observation]) -> list[dict[str, object]]:
    ordered = list(reversed(observations))
    if not ordered:
        return []
    low = min(item.price_minor for item in ordered)
    high = max(item.price_minor for item in ordered)
    span = high - low
    return [
        {
            "x": round(18 + index * 564 / max(len(ordered) - 1, 1)),
            "y": round(58 - (item.price_minor - low) * 40 / span) if span else 58,
            "date": beijing_label(item.observed_at),
            "price": _display_price(item.currency, item.price_minor),
        }
        for index, item in enumerate(ordered)
    ]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    require_admin(request)
    with request.app.state.session_factory() as session:
        products = session.scalars(
            select(Product).where(Product.status != "archived").order_by(Product.id.desc())
        ).all()
        cards = []
        for product in products:
            check_time_setting = session.get(Setting, check_time_key(product.id))
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
                    )
                    if latest
                    else None,
                    "sparkline": _sparkline(observations),
                    "configuration_rows": configuration_rows(product.configuration),
                    "secondary_rows": secondary_configuration_rows(product.configuration),
                    "display_price": _display_price(latest.currency, latest.price_minor)
                    if latest
                    else "—",
                    "currency": latest.currency if latest else "",
                    "check_time": check_time_setting.value_text if check_time_setting else None,
                }
            )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_context(request, cards=cards),
    )


@router.get("/products/new", response_class=HTMLResponse)
async def product_add(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="product_add.html",
        context=_context(request, preview=None, error=None, catalog=None),
    )


@router.post("/products/options", response_class=HTMLResponse)
async def product_options(
    request: Request, url: str = Form(), submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    try:
        target = validate_public_url(url)
        if target.host not in {"dell.com", "www.dell.com"} or not target.path.startswith(
            "/en-us/shop/"
        ):
            raise ValueError("配置选择仅支持美国戴尔商品页")
        pipeline = request.app.state.check_service.pipeline
        if pipeline is None:
            raise ValueError("检查器尚未启动")
        acquired = await pipeline.browser.fetch(target)
        catalog = catalog_from_html(acquired.html)
        if not catalog:
            raise ValueError("戴尔页面未提供可选配置")
    except (UnsafeUrlError, ValueError, RuntimeError, AcquisitionError) as error:
        return templates.TemplateResponse(
            request=request,
            name="product_add.html",
            context=_context(request, preview=None, catalog=None, error=str(error), url=url),
            status_code=422,
        )
    request.app.state.option_catalogs = {
        key: value
        for key, value in request.app.state.option_catalogs.items()
        if value[3] >= datetime.now(UTC) and value[0] != admin.id
    }
    token = secrets.token_urlsafe(32)
    request.app.state.option_catalogs[token] = (
        admin.id,
        str(target),
        catalog,
        datetime.now(UTC) + timedelta(minutes=15),
    )
    return templates.TemplateResponse(
        request=request,
        name="product_add.html",
        context=_context(
            request, preview=None, catalog=catalog, catalog_token=token, url=url, error=None
        ),
    )


@router.post("/products/preview", response_class=HTMLResponse)
async def product_preview(
    request: Request,
    url: str = Form(),
    catalog_token: str = Form(""),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    try:
        target = validate_public_url(url)
        recipe: dict[str, str] | None = None
        if catalog_token:
            entry = request.app.state.option_catalogs.get(catalog_token)
            if (
                entry is None
                or entry[0] != admin.id
                or entry[1] != str(target)
                or entry[3] < datetime.now(UTC)
            ):
                raise ValueError("配置选择已过期, 请重新读取")
            form = await request.form()
            recipe = {}
            for group, choices in entry[2].items():
                label = str(form.get(f"option__{group}", ""))
                if label:
                    if label not in choices:
                        raise ValueError(f"选项已不可用: {group} / {label}")
                    recipe[group] = label
            if not recipe:
                recipe = None
        pipeline = request.app.state.check_service.pipeline
        if pipeline is None:
            raise ValueError("检查器尚未启动")
        adapter = request.app.state.check_service.registry.for_url(target)
        if recipe:
            _, snapshot = await pipeline.acquire(target, adapter, dell_selection=recipe)
        else:
            _, snapshot = await pipeline.acquire(target, adapter)
    except (UnsafeUrlError, ValueError, RuntimeError, AcquisitionError, ExtractionError) as error:
        return templates.TemplateResponse(
            request=request,
            name="product_add.html",
            context=_context(request, preview=None, catalog=None, error=str(error), url=url),
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
        recipe,
    )
    return templates.TemplateResponse(
        request=request,
        name="product_add.html",
        context=_context(
            request,
            preview=snapshot,
            preview_token=token,
            error=None,
            catalog=None,
            recipe=recipe,
            preview_rows=configuration_rows(
                snapshot.configuration.as_record(), include_extras=True
            ),
            url=url,
        ),
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
    recipe: dict[str, str] | None = entry[3] if len(entry) > 3 else None
    with request.app.state.session_factory.begin() as session:
        candidates = session.scalars(
            select(Product).where(
                Product.source_site == snapshot.identity.site, Product.sku == snapshot.identity.sku
            )
        ).all()
        for existing in candidates:
            if existing.dell_selection == recipe and existing.status != "archived":
                return RedirectResponse(f"/products/{existing.id}", status_code=303)
        product = Product(
            source_site=snapshot.identity.site,
            requested_url=snapshot.canonical_url,
            canonical_url=snapshot.canonical_url,
            name=snapshot.name,
            sku=snapshot.identity.sku,
            dell_selection=recipe,
            status="paused",
        )
        session.add(product)
        session.flush()
        product_id = product.id
    request.app.state.check_service.accept_snapshot(product_id, snapshot, trigger="initial")
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.get("/products/archived", response_class=HTMLResponse)
async def archived_products(request: Request) -> Response:
    require_admin(request)
    with request.app.state.session_factory() as session:
        products = session.scalars(
            select(Product).where(Product.status == "archived").order_by(Product.id.desc())
        ).all()
        for product in products:
            session.expunge(product)
    return templates.TemplateResponse(
        request=request,
        name="product_archived.html",
        context=_context(request, products=products),
    )


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
        check_time_setting = session.get(Setting, check_time_key(product_id))
        check_time = check_time_setting.value_text if check_time_setting else None
    return templates.TemplateResponse(
        request=request,
        name="product_detail.html",
        context=_context(
            request,
            product=product,
            history=history,
            pending=pending,
            configuration_rows=configuration_rows(product.configuration, include_extras=True),
            chart_points=_chart_points(history),
            check_time=check_time,
        ),
    )


@router.post("/products/{product_id}/check")
async def manual_check(
    request: Request,
    product_id: int,
    return_to: str = Form("detail"),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    request.app.state.scheduler.request_check(product_id, "manual")
    destination = "/" if return_to == "dashboard" else f"/products/{product_id}"
    return RedirectResponse(destination, status_code=303)


def _time_conflicts(session: Session, product_id: int, check_time: str) -> bool:
    for active_id in session.scalars(
        select(Product.id).where(Product.status == "active", Product.id != product_id)
    ):
        setting = session.get(Setting, check_time_key(active_id))
        if setting is not None and setting.value_text == check_time:
            return True
    return False


@router.post("/products/{product_id}/check-time")
async def product_check_time(
    request: Request,
    product_id: int,
    check_time: str = Form(),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    try:
        parse_check_time(check_time)
    except ValueError:
        return RedirectResponse("/?schedule_error=invalid", status_code=303)
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        if product.status == "active" and _time_conflicts(session, product_id, check_time):
            return RedirectResponse("/?schedule_error=conflict", status_code=303)
        setting = session.get(Setting, check_time_key(product_id))
        if setting is None:
            session.add(Setting(key=check_time_key(product_id), value_text=check_time))
        else:
            setting.value_text = check_time
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse("/", status_code=303)


@router.post("/products/{product_id}/settings")
async def product_settings(
    request: Request,
    product_id: int,
    target_price: str = Form(""),
    notify_mode: str = Form("changes"),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    if notify_mode not in ("changes", "target_or_change"):
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
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/pause")
async def pause_product(
    request: Request,
    product_id: int,
    return_to: str = Form("detail"),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    destination = "/" if return_to == "dashboard" else f"/products/{product_id}"
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        assert product is not None
        if product.status == "active":
            product.status = "paused"
        elif product.status == "paused":
            setting = session.get(Setting, check_time_key(product_id))
            if setting is None or not setting.value_text:
                return RedirectResponse(f"{destination}?schedule_error=missing", status_code=303)
            if _time_conflicts(session, product_id, setting.value_text):
                return RedirectResponse(f"{destination}?schedule_error=conflict", status_code=303)
            product.status = "active"
        else:
            raise HTTPException(409)
    request.app.state.scheduler.schedule_product(product_id)
    return RedirectResponse(destination, status_code=303)


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


@router.post("/products/{product_id}/restore")
async def restore_product(
    request: Request, product_id: int, submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    _product(request, product_id)
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        if product is None or product.status != "archived":
            raise HTTPException(409)
        product.status = "paused"
        product.next_check_at = None
    return RedirectResponse(f"/products/{product_id}", status_code=303)


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
        setting = session.get(Setting, check_time_key(product_id))
        if setting is not None:
            session.delete(setting)
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
        if action == "separate" and product.dell_selection:
            raise HTTPException(409, "此卡片已绑定戴尔配置; 请从添加商品创建另一张配置卡片")
        pending = session.scalar(
            select(Observation)
            .where(Observation.product_id == product_id, Observation.trusted.is_(False))
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(1)
        )
        if pending is None:
            raise HTTPException(409, "没有待确认的新配置")
        pending_configuration = pending.configuration or {}
        structured_configuration = {
            key: value
            for key, value in pending_configuration.items()
            if key not in ("sku", "canonical_url", "name")
        }
        if action == "separate":
            fresh = Product(
                source_site=product.source_site,
                requested_url=pending_configuration.get("canonical_url") or product.requested_url,
                canonical_url=pending_configuration.get("canonical_url") or product.canonical_url,
                name=pending_configuration.get("name") or product.name,
                sku=pending_configuration.get("sku"),
                configuration=structured_configuration,
                configuration_fingerprint=pending.configuration_fingerprint,
                check_interval_hours=product.check_interval_hours,
                last_checked_at=pending.observed_at,
                last_success_at=pending.observed_at,
                status="paused",
            )
            session.add(fresh)
            session.flush()
            pending.product_id = fresh.id
            pending.trusted = True
            product.status = "paused"
            product.next_check_at = None
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
            product.configuration = structured_configuration
            product.configuration_fingerprint = pending.configuration_fingerprint
            product.sku = pending_configuration.get("sku") or product.sku
            product.name = pending_configuration.get("name") or product.name
            product.canonical_url = (
                pending_configuration.get("canonical_url") or product.canonical_url
            )
            product.last_success_at = pending.observed_at
            product.status = "paused"
            product.next_check_at = None
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
        writer.writerow(("observed_at_beijing", "currency", "price_minor", "price", "availability"))
        for row in rows:
            local_time = beijing_time(row.observed_at)
            assert local_time is not None
            writer.writerow(
                (
                    local_time.isoformat(),
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
