"""Social Matrix v2 · ORM models · 与 alembic 008 对齐

3 张表：
  - SocialCredential · OAuth token / refresh / API key（AES-256-GCM 加密）
  - SocialAccount    · 工厂 × 平台账号
  - SocialPost       · 帖子草稿 / 已发布 / 撤回

关键设计：
- credential 的 payload_ciphertext 永不外暴露·api 层只返 expires_at + scopes + status
- account.metrics JSONB 是缓存·真值走 platform API 现拉
- post 状态机：draft → pending_approval → approved → scheduled → publishing → published
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    String, Text, Boolean, Integer, DateTime, LargeBinary,
    ForeignKey, CheckConstraint, UniqueConstraint, Index, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


# ════════════════════════════════════════════════════════════
# SocialCredential
# ════════════════════════════════════════════════════════════

class SocialCredential(Base):
    __tablename__ = "social_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # 加密 envelope
    kek_id: Mapped[str] = mapped_column(String(32), nullable=False)
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dek_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    refresh_failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[Optional[str]] = mapped_column(Text)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "credential_type IN ('oauth2', 'oauth1', 'api-key', 'long-lived-token')",
            name="ck_social_credentials_type",
        ),
        CheckConstraint(
            "platform IN ('linkedin', 'linkedin-personal', 'meta-page', "
            "'instagram-business', 'tiktok-business', 'youtube', 'x')",
            name="ck_social_credentials_platform",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SocialCredential id={self.id} platform={self.platform} "
            f"type={self.credential_type}>"
        )


# ════════════════════════════════════════════════════════════
# SocialAccount
# ════════════════════════════════════════════════════════════

class SocialAccount(Base):
    __tablename__ = "social_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    factory_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_account_id: Mapped[str] = mapped_column(String(128), nullable=False)

    display_name: Mapped[Optional[str]] = mapped_column(String(256))
    profile_url: Mapped[Optional[str]] = mapped_column(Text)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)

    credential_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_credentials.id", ondelete="SET NULL"),
    )
    credential: Mapped[Optional["SocialCredential"]] = relationship(
        "SocialCredential", foreign_keys=[credential_id],
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active", index=True,
    )
    last_post_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    warning_count: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )

    posts: Mapped[list["SocialPost"]] = relationship(
        "SocialPost", back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "factory_slug", "platform", "platform_account_id",
            name="uq_social_accounts",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'suspended', 'disconnected', 'pending_oauth')",
            name="ck_social_accounts_status",
        ),
        CheckConstraint(
            "platform IN ('linkedin', 'linkedin-personal', 'meta-page', "
            "'instagram-business', 'tiktok-business', 'youtube', 'x')",
            name="ck_social_accounts_platform",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SocialAccount id={self.id} factory={self.factory_slug} "
            f"platform={self.platform} status={self.status}>"
        )


# ════════════════════════════════════════════════════════════
# SocialPost
# ════════════════════════════════════════════════════════════

class SocialPost(Base):
    __tablename__ = "social_posts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    factory_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_language: Mapped[Optional[str]] = mapped_column(
        String(8), server_default="en",
    )
    asset_ids: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"),
    )
    link_url: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", index=True,
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    platform_post_id: Mapped[Optional[str]] = mapped_column(String(256))
    platform_post_url: Mapped[Optional[str]] = mapped_column(Text)

    metrics_likes: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_comments: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_shares: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_impressions: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_clicks: Mapped[int] = mapped_column(Integer, server_default="0")
    metrics_last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )

    error_message: Mapped[Optional[str]] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")

    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    approved_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )

    account: Mapped["SocialAccount"] = relationship(
        "SocialAccount", back_populates="posts",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'scheduled', "
            "'publishing', 'published', 'failed', 'deleted')",
            name="ck_social_posts_status",
        ),
        Index("ix_social_posts_account_status", "account_id", "status"),
        Index(
            "ix_social_posts_scheduled",
            "scheduled_at",
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SocialPost id={self.id} factory={self.factory_slug} "
            f"platform={self.account.platform if self.account else '?'} "
            f"status={self.status}>"
        )
