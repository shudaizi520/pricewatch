"""Shared SQLAlchemy declarative base and timestamp helpers."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utc_now() -> datetime:
    """Return an aware UTC timestamp for ORM defaults."""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for persisted PriceWatch models."""
