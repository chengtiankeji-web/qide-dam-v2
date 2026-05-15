"""QideMatrix 社媒矩阵 · 账号管家 6 张 ORM 模型"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QmSocialAccount(Base):
    """平台账号档案 · 矩阵账号管家核心"""
    __tablename__ = "qm_social_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 平台 + 用途
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False, default="main")
    account_handle: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    persona: Mapped[dict] = mapped_column(JSON, default=dict)

    # 隔离三要素
    browser_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    proxy_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    geo_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    geo_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 凭证（QideDAM Vault 引用 · 不存明文）
    credentials_vault_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # 状态 + 健康度
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    health_score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_warning_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 风控配额
    daily_post_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_follow_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_like_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QmBrowserProfile(Base):
    """浏览器环境 · AdsPower / Multilogin / Dolphin Anty 等 third-party 抽象"""
    __tablename__ = "qm_browser_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="adspower")
    external_profile_id: Mapped[str] = mapped_column(String(100), nullable=False)
    profile_name: Mapped[str] = mapped_column(String(200), nullable=False)

    fingerprint_summary: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmProxyPool(Base):
    """代理 IP 池 · Bright Data / Smartproxy / IPRoyal 等"""
    __tablename__ = "qm_proxy_pool"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    proxy_type: Mapped[str] = mapped_column(String(20), nullable=False)

    country: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)

    host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credentials_vault_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    monthly_quota_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_gb_this_month: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmSocialPost(Base):
    """跨平台内容 · 1 原稿 + N 个平台改写版本"""
    __tablename__ = "qm_social_posts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    original_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_media_asset_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), default=list
    )

    platform_variants: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    content_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_use_case: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QmPostSchedule(Base):
    """发布调度 · 计划 vs 实际时间双轨"""
    __tablename__ = "qm_post_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_social_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_social_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    jitter_seconds: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    platform_post_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    platform_post_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmAccountHealthEvent(Base):
    """账号健康度事件 · 不可篡改（PG trigger 保护）"""
    __tablename__ = "qm_account_health_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qm_workspaces.id"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_social_accounts.id"),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    proxy_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    browser_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    triggered_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    health_score_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_score_after: Mapped[int | None] = mapped_column(Integer, nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
