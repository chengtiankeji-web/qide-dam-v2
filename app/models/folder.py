"""Folder — hierarchical project-internal grouping (tree).

Uses `path` (materialized path) for fast subtree queries:
    /                  (project root)
    /campaigns/
    /campaigns/2026-spring/
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Folder(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_folders_project_path"),
        Index("ix_folders_project_path_prefix", "project_id", "path"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Materialized path: '/' for root, '/campaigns/' for top-level, '/campaigns/2026/' nested
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
