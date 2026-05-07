"""API Key — used by external systems (青玄前端, Kiln & Ink, MCP clients) and AI agents.

Sprint 1 stores SHA-256 hash; raw value shown once at creation.
Scopes follow the format: <verb>:<resource>  e.g. read:asset, write:asset, admin:project.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Display (e.g., "Qingxuan frontend production")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Prefix shown in UI: dam_live_a1b2c3..  (raw key shown only once)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # SHA-256 hash of the full raw key
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)

    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # If scoped to one project, set this; null means tenant-wide.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # v3 P0-4: explicit revoke timestamp. is_active is the legacy flag we
    # still respect, but revoked_at gives us a hard "when" for audit
    # purposes and lets the auth middleware do a single timestamp compare
    # instead of a boolean flip + scan.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    user: Mapped[User | None] = relationship(back_populates="api_keys")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiKey prefix={self.prefix!r} tenant={self.tenant_id}>"
