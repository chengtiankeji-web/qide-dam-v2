"""QideMatrix v1 · S8 链路健康度时序 model

每 workspace × 每天 × 每 stage 一行
绿/黄/红/idle 4 色
blocked_days > 0 时 stage_status 自动 yellow（3-7 天）/ red（>7 天）
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QmHealthMetric(Base):
    """S8 链路健康度 · 时序数据 · 每日 1 行 / workspace / stage"""
    __tablename__ = "qm_health_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    onboarding_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_onboardings.id", ondelete="SET NULL"),
        nullable=True,
    )

    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    stage: Mapped[str] = mapped_column(String(4), nullable=False)

    stage_status: Mapped[str] = mapped_column(String(20), nullable=False)
    blocked_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    traffic_count: Mapped[int] = mapped_column(Integer, default=0)
    lead_count: Mapped[int] = mapped_column(Integer, default=0)
    qualified_lead_count: Mapped[int] = mapped_column(Integer, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    content_published_count: Mapped[int] = mapped_column(Integer, default=0)
    social_posts_count: Mapped[int] = mapped_column(Integer, default=0)

    platform_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)
    geo_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
