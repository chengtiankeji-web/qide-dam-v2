"""Webhook subscriptions + delivery log.

Subscribers are external systems (青玄 Worker / 乡约小程序云函数 / customer apps)
that get notified when assets change state.

Delivery is signed with HMAC-SHA256: header `X-Qide-Signature: t=<ts>,v1=<hex>`
where `v1 = HMAC_SHA256(secret, "<ts>.<raw_body>")`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Common event types — keep in lock-step with `EVENT_TYPES` in webhook_service.py
EVENT_TYPES = (
    "asset.created",
    "asset.uploaded",        # presigned PUT confirmed
    "asset.processed",       # Celery image/video/doc done
    "asset.ai_tagged",       # Sprint 3
    "asset.updated",
    "asset.deleted",
    "collection.updated",    # Sprint 4
)

DELIVERY_STATUS = ("pending", "succeeded", "failed", "dead")


class WebhookSubscription(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "webhook_subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Failure / suspension tracking
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )


class WebhookDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only log — 30-day retention via cron in Sprint 4."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_sub_status", "subscription_id", "status"),
        Index("ix_webhook_deliveries_event_type", "event_type"),
    )

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription: Mapped[WebhookSubscription] = relationship(back_populates="deliveries")


class MultipartUpload(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tracks an in-flight S3 multipart upload session.

    Lifecycle: init → many sign-part calls → complete (or abort).
    Stale rows older than 24h are aborted by Celery `cleanup.multipart_abort`.
    """

    __tablename__ = "multipart_uploads"
    __table_args__ = (Index("ix_multipart_asset", "asset_id", unique=True),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parts_meta: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    aborted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
