"""Tenant — top-level isolation boundary.

For QideDAM v2 the initial 5 tenants are:
    qide          — 佛山祁德商链科技 (中台)
    qingxuan      — 青玄国际贸易 (HK)
    zerun         — 泽润良品 (深圳)
    hemei         — 和美共创 (顺德 · 民非)
    chengtian     — 广州橙天电子商务 (Sam 个人)

Future tenants (paying customer hosting mode) reuse the same row shape.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SlugMixin, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.user import User


class Tenant(UUIDPrimaryKeyMixin, SlugMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Business entity classification (e.g., "limited" / "non-profit" / "personal")
    legal_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credit_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Storage namespace — used as the S3 prefix:
    #   <S3_BUCKET>/t/<storage_prefix>/p/<project_storage_prefix>/<asset_uuid>
    storage_prefix: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Quotas (Sprint 1 records them, Sprint 4 enforces them)
    quota_storage_bytes: Mapped[int] = mapped_column(default=10 * 1024**4, nullable=False)  # 10 TB
    quota_assets: Mapped[int] = mapped_column(default=1_000_000, nullable=False)
    quota_monthly_uploads_bytes: Mapped[int] = mapped_column(default=1024**4, nullable=False)  # 1 TB

    # Misc
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # --- relationships ---
    projects: Mapped[list[Project]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    users: Mapped[list[User]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tenant slug={self.slug!r} id={self.id}>"


def make_tenant_id_column():
    """Helper used by other tables — keeps FK definition consistent."""
    from sqlalchemy import ForeignKey
    from sqlalchemy.dialects.postgresql import UUID

    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
