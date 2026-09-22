"""First-run initialization and administrator session routes."""

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import Administrator
from pricewatch.services.auth import LoginThrottle, hash_password, verify_password
from pricewatch.web.dependencies import csrf_token, verify_csrf
from pricewatch.web.forms import FormError, validate_password, validate_username

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
throttle = LoginThrottle()


def _factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def _has_admin(factory: sessionmaker[Session]) -> bool:
    with factory() as session:
        return bool(session.scalar(select(func.count(Administrator.id))))


def _throttle_key(request: Request, username: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}:{username.casefold()}"


@router.get("/initialize", response_class=HTMLResponse)
async def initialize_page(request: Request) -> HTMLResponse:
    if _has_admin(_factory(request)):
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="initialize.html",
        context={"csrf_token": csrf_token(request), "error": None, "username": ""},
    )


@router.post("/initialize", response_class=HTMLResponse)
async def initialize(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    confirm_password: str = Form(),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    factory = _factory(request)
    if _has_admin(factory):
        raise HTTPException(status_code=404)
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="initialize.html",
            context={
                "csrf_token": csrf_token(request),
                "error": "两次密码不一致",
                "username": username,
            },
            status_code=422,
        )
    try:
        valid_username = validate_username(username)
        valid_password = validate_password(password)
    except FormError as error:
        return templates.TemplateResponse(
            request=request,
            name="initialize.html",
            context={"csrf_token": csrf_token(request), "error": str(error), "username": username},
            status_code=422,
        )
    with factory() as session:
        admin = Administrator(
            username=valid_username,
            password_hash=hash_password(valid_password),
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
    request.session.clear()
    request.session["admin_id"] = admin_id
    csrf_token(request)
    return RedirectResponse("/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token(request), "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    key = _throttle_key(request, username)
    if throttle.blocked(key):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    with _factory(request)() as session:
        admin = session.scalar(select(Administrator).where(Administrator.username == username))
        if admin is None or not verify_password(password, admin.password_hash):
            throttle.record_failure(key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf_token": csrf_token(request), "error": "账号或密码错误"},
                status_code=401,
            )
        admin_id = admin.id
    throttle.clear(key)
    request.session.clear()
    request.session["admin_id"] = admin_id
    csrf_token(request)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(
    request: Request, submitted_csrf: str | None = Form(None, alias="csrf_token")
) -> RedirectResponse:
    verify_csrf(request, submitted_csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
