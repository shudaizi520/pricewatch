"""Create PriceWatch tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "administrators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("theme", sa.String(16), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("value_text", sa.Text()),
        sa.Column("value_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_site", sa.String(80), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("name", sa.String(300)),
        sa.Column("sku", sa.String(160)),
        sa.Column("model", sa.String(160)),
        sa.Column("configuration", sa.JSON()),
        sa.Column("configuration_fingerprint", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("check_interval_hours", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("target_price_minor", sa.Integer()),
        sa.Column("notify_mode", sa.String(32), nullable=False, server_default="changes"),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'needs_attention', 'archived')",
            name="ck_products_status",
        ),
        sa.CheckConstraint(
            "check_interval_hours IN (1, 3, 6, 12, 24)", name="ck_products_interval"
        ),
    )
    op.create_index("ix_products_source_site", "products", ["source_site"])
    op.create_index("ix_products_sku", "products", ["sku"])
    op.create_index("ix_products_status", "products", ["status"])
    op.create_index("ix_products_next_check_at", "products", ["next_check_at"])
    op.create_index("ix_products_due", "products", ["status", "next_check_at"])
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("list_price_minor", sa.Integer()),
        sa.Column("discount_text", sa.String(240)),
        sa.Column("coupon_text", sa.String(500)),
        sa.Column("availability", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("configuration", sa.JSON()),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON()),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price_minor >= 0", name="ck_observations_price_nonnegative"),
        sa.CheckConstraint(
            "list_price_minor IS NULL OR list_price_minor >= 0",
            name="ck_observations_list_price_nonnegative",
        ),
    )
    op.create_index("ix_observations_product_id", "observations", ["product_id"])
    op.create_index("ix_observations_observed_at", "observations", ["observed_at"])
    op.create_index("ix_observations_product_time", "observations", ["product_id", "observed_at"])
    op.create_table(
        "check_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("acquisition_method", sa.String(24)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("error_category", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_check_runs_product_id", "check_runs", ["product_id"])
    op.create_index("ix_check_runs_created_at", "check_runs", ["created_at"])
    op.create_index("ix_check_runs_product_time", "check_runs", ["product_id", "created_at"])
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE")),
        sa.Column("event_key", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("destination_type", sa.String(32), nullable=False, server_default="feishu"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("message_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_notification_deliveries_product_id", "notification_deliveries", ["product_id"]
    )
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])
    op.create_table(
        "backup_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(300), nullable=False, unique=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_backup_records_created_at", "backup_records", ["created_at"])


def downgrade() -> None:
    op.drop_table("backup_records")
    op.drop_table("notification_deliveries")
    op.drop_table("check_runs")
    op.drop_table("observations")
    op.drop_table("products")
    op.drop_table("settings")
    op.drop_table("administrators")
