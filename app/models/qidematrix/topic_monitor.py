"""QideMatrix · 话题监测 ORM · 3 张表"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QmTopicSource(Base):
    """监测源配置 · subreddit / HN / Twitter / etc"""
    __tablename__ = "qm_topic_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fetch_top_n: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    fetch_comments_n: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    fetch_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fetch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmTopicSignal(Base):
    """抓到的原始信号（post + top 评论）"""
    __tablename__ = "qm_topic_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_topic_sources.id", ondelete="CASCADE"),
        nullable=False,
    )

    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_comments: Mapped[list] = mapped_column(JSON, default=list)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class QmTopicCandidate(Base):
    """LLM 评分候选话题 + SEO writer 工作流状态"""
    __tablename__ = "qm_topic_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_topic_signals.id", ondelete="CASCADE"),
        nullable=False,
    )

    # AI 评分（0-10）
    b2b_relevance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_intent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_novelty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    factory_match: Mapped[int | None] = mapped_column(Integer, nullable=True)
    composite_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # AI 提炼
    distilled_topic: Mapped[str | None] = mapped_column(String(500), nullable=True)
    distilled_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    suggested_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    target_buyer_persona: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 工作流
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    shortlisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shortlisted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    written_post_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qm_social_posts.id"), nullable=True
    )
    wrote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 元数据
    ai_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ai_cost_cny_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
