"""QideMatrix · Workspace / Member / Invitation 模型

Workspace 是 QideMatrix 的订阅 + 计费 + 隔离单元。一个 tenant 可以有多个 workspace
（祁德自己用 qide-internal · 客户 A 买的是 customer-a-workspace）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class QmWorkspace(Base):
    """工作空间 · QideMatrix 的核心隔离单元"""
    __tablename__ = "qm_workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # 订阅档位 + 配额
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="trial")
    plan_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    plan_storage_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_ai_calls_monthly: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 行业 + 本地化 + 白标
    industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    locale: Mapped[str] = mapped_column(String(10), default="zh-CN")
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    members: Mapped[list["QmWorkspaceMember"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class QmWorkspaceMember(Base):
    """工作空间成员 · workspace_id × user_id × role"""
    __tablename__ = "qm_workspace_members"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    workspace: Mapped[QmWorkspace] = relationship(back_populates="members")


class QmInvitation(Base):
    """邀请 · 一次性 token · 链接接受"""
    __tablename__ = "qm_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
