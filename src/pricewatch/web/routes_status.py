"""Compact administrator diagnostics without exposing secrets."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from pricewatch.db.models import CheckRun, Product
from pricewatch.web.dependencies import csrf_token, require_admin
from pricewatch.web.timezone import beijing_label

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
templates.env.filters["beijing"] = beijing_label


@router.get("/status")
async def status(request: Request) -> Response:
    admin = require_admin(request)
    with request.app.state.session_factory() as session:
        active = session.scalar(select(func.count(Product.id)).where(Product.status == "active"))
        last = session.scalar(select(CheckRun).order_by(CheckRun.created_at.desc()).limit(1))
        recent = session.scalars(
            select(CheckRun).order_by(CheckRun.created_at.desc()).limit(10)
        ).all()
        products = session.scalars(select(Product).where(Product.status == "active")).all()
    pending = len(request.app.state.scheduler.pending)
    if request.query_params.get("refresh") and pending == 0:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="status.html",
        context={
            "admin": admin,
            "csrf_token": csrf_token(request),
            "active": active,
            "last": last,
            "recent": recent,
            "products": products,
            "pending": pending,
            "now": datetime.now(UTC),
        },
    )
