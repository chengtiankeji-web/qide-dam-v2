"""Project — tenant 内的二级隔离单位.

Initial projects (per tenant):
    qide        / core, dam, website, aivisible
    qingxuan    / kiln-ink, qingxuan-intel
    zerun       / cmh
    hemei       / xiangyue-shunde
    chengtian   / personal-archive
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SlugMixin, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.tenant import Tenant


class Project(UUIDPrimaryKeyMixin, SlugMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Storage prefix — appended to tenant.storage_prefix
    storage_prefix: Mapped[str] = mapped_column(String(64), nullable=False)

    # Default ACL for new uploads in this project: private | project | tenant | public
    default_acl: Mapped[str] = mapped_column(
        String(16), default="project", nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # --- relationships ---
    tenant: Mapped["Tenant"] = relationship(back_populates="projects")
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project slug={self.slug!r} tenant={self.tenant_id}>"
