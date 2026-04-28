"""Asset — the heart of QideDAM v2.

Compared to v0:
- Multi-tenant (tenant_id + project_id)
- 3-layer ACL: private | project | tenant | public
- Version chain (asset_versions table) — every replace creates a new row,
  the parent asset row is the "current pointer"
- AI metadata (auto_tags, ai_summary, ai_alt_text, ai_visual_description)
- Vector embedding column (pgvector, 768-dim — works with bge-base-zh /
  multilingual-e5-base / sentence-transformers all-MiniLM-L12-v2 padded)
- Source attribution: where did the file come from (uploaded / migrated / mcp / webhook)

The pgvector column is created via raw SQL in the alembic migration (since
SQLAlchemy core doesn't ship a Vector type). We declare it here as String
typed-as-pgvector via type_decorator so ORM stays happy.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.project import Project


# ----- enums (kept as plain str for simplicity; checked in alembic with CHECK) -----

ACL_LEVELS = ("private", "project", "tenant", "public")
ASSET_KINDS = ("image", "video", "audio", "document", "archive", "model3d", "other")
ASSET_STATUSES = ("uploading", "processing", "ready", "failed", "archived")
ASSET_SOURCES = ("upload", "migration", "mcp", "webhook", "system")


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Current pointer for an asset. Historical bytes live in `asset_versions`."""

    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_tenant_project", "tenant_id", "project_id"),
        Index("ix_assets_kind_status", "kind", "status"),
        Index("ix_assets_sha256", "sha256"),
    )

    # --- ownership ---
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- identity ---
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- file ---
    kind: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- storage ---
    # Full S3 key, e.g. t/qide/p/dam/2026/04/27/<uuid>.jpg
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    storage_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    # Public URL (CDN) if asset is public; otherwise null
    public_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- status ---
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="upload", nullable=False)

    # --- ACL ---
    acl: Mapped[str] = mapped_column(String(16), default="project", nullable=False)

    # --- media metadata (filled in by Celery workers) ---
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Thumbnail keys (S3 keys, not URLs): {"sm": "...", "md": "...", "lg": "..."}
    thumbnails: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Free-form technical metadata (EXIF, ID3, codec info, etc.)
    technical_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # --- AI metadata (Sprint 3 fills in; Sprint 1 just creates the columns) ---
    auto_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, nullable=False
    )
    manual_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, nullable=False
    )
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_visual_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- versioning ---
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # --- misc ---
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # --- relationships ---
    project: Mapped["Project"] = relationship(back_populates="assets")
    versions: Mapped[list["AssetVersion"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetVersion.version_no.desc()",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Asset name={self.name!r} kind={self.kind} v{self.current_version}>"


class AssetVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable history of asset bytes — every replace appends a new row."""

    __tablename__ = "asset_versions"
    __table_args__ = (
        Index("ix_asset_versions_asset_version", "asset_id", "version_no", unique=True),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    asset: Mapped["Asset"] = relationship(back_populates="versions")
