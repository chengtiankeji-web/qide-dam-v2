"""QideMatrix · Subscription + BillingEvent + UsageMeter 模型"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QmSubscription(Base):
    """订阅记录 · 一个 workspace 通常对应一份 active subscription"""
    __tablename__ = "qm_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(String(20), nullable=False)  # trial/standard/enterprise
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    billing_cycle: Mapped[str] = mapped_column(String(10), nullable=False, default="monthly")
    price_cny_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payment_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)  # wechat/stripe/manual
    payment_provider_subscription_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmBillingEvent(Base):
    """计费审计 · 不可篡改（trigger 保护 · 见 alembic 014）"""
    __tablename__ = "qm_billing_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qm_workspaces.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qm_subscriptions.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_cny_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_provider_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmUsageMeter(Base):
    """按月用量 · 计费 + 限流参考"""
    __tablename__ = "qm_usage_meters"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False)

    ai_calls_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_tokens_input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ai_tokens_output: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ai_cost_cny_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflow_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
