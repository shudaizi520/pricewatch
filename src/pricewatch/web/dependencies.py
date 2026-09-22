"""Authentication and CSRF web dependencies."""

import secrets

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pricewatch.db.models import Administrator


def has_admin(request: Request) -> bool:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        return session.scalar(select(Administrator.id).limit(1)) is not None


def login_destination(request: Request) -> str:
    return "/login" if has_admin(request) else "/initialize"


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not isinstance(expected, str) or not isinstance(submitted, str):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    if not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_admin(request: Request) -> Administrator:
    admin_id = request.session.get("admin_id")
    if not isinstance(admin_id, int):
        raise HTTPException(status_code=303, headers={"Location": login_destination(request)})
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        admin = session.get(Administrator, admin_id)
        if admin is None:
            request.session.clear()
            raise HTTPException(status_code=303, headers={"Location": login_destination(request)})
        session.expunge(admin)
        return admin
