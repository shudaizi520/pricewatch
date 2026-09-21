"""Persistent PriceWatch records."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pricewatch.db.base import Base, utc_now


class Administrator(Base):
    __tablename__ = "administrators"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    theme: Mapped[str] = mapped_column(String(16), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'needs_attention', 'archived')",
            name="ck_products_status",
        ),
        CheckConstraint("check_interval_hours IN (1, 3, 6, 12, 24)", name="ck_products_interval"),
        Index("ix_products_due", "status", "next_check_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_site: Mapped[str] = mapped_column(String(80), index=True)
    requested_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(160), index=True)
    model: Mapped[str | None] = mapped_column(String(160))
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    check_interval_hours: Mapped[int] = mapped_column(Integer, default=6)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    target_price_minor: Mapped[int | None] = mapped_column(Integer)
    notify_mode: Mapped[str] = mapped_column(String(32), default="changes")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    failure_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    observations: Mapped[list["Observation"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint("price_minor >= 0", name="ck_observations_price_nonnegative"),
        CheckConstraint(
            "list_price_minor IS NULL OR list_price_minor >= 0",
            name="ck_observations_list_price_nonnegative",
        ),
        Index("ix_observations_product_time", "product_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    price_minor: Mapped[int] = mapped_column(Integer)
    list_price_minor: Mapped[int | None] = mapped_column(Integer)
    discount_text: Mapped[str | None] = mapped_column(String(240))
    coupon_text: Mapped[str | None] = mapped_column(String(500))
    availability: Mapped[str] = mapped_column(String(40), default="unknown")
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    product: Mapped[Product] = relationship(back_populates="observations")


class CheckRun(Base):
    __tablename__ = "check_runs"
    __table_args__ = (Index("ix_check_runs_product_time", "product_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(24))
    outcome: Mapped[str] = mapped_column(String(32))
    acquisition_method: Mapped[str | None] = mapped_column(String(24))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    destination_type: Mapped[str] = mapped_column(String(32), default="feishu")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300), unique=True)
    reason: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(80))
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
