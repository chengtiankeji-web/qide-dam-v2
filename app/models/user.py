"""User — bound to a tenant; can have access to multiple projects via ACL.

Sprint 1 keeps roles simple: tenant_admin / member / viewer.
A future "platform_admin" role gates cross-tenant ops.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.api_key import ApiKey
    from app.models.tenant import Tenant


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    role: Mapped[str] = mapped_column(
        String(32), default="member", nullable=False
    )  # platform_admin | tenant_admin | member | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # JSON list of project IDs this user has access to ("*" means all in tenant)
    project_access: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # v3 P0-4: bump-to-revoke. JWTs carry `tv` claim that auth middleware
    # compares to this column on every request. Bumping invalidates every
    # JWT issued before the bump.
    #
    # Triggers for bumping:
    #   - user removed from tenant
    #   - user changes their own password
    #   - admin force-revokes a session (compromised account)
    #   - user toggles 2FA
    token_version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )

    # --- relationships ---
    tenant: Mapped[Tenant] = relationship(back_populates="users")
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User email={self.email!r} tenant={self.tenant_id}>"
