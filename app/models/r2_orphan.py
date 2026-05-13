"""R2Orphan — unreclaimed R2 objects from failed hard_delete operations.

v3 P1.3 (2026-05-13) · alembic 011.

When hard_delete_asset can't delete the R2 object (network blip, R2 token expired,
S3 4xx, etc.), the asset DB row gets deleted anyway. The R2 object becomes truly
orphan — billable forever, no reference anywhere. This table tracks every such
failure so retry_r2_orphans Celery task (daily) can keep trying with exponential
backoff until success.

resolved_at IS NULL → still trying
resolved_at IS NOT NULL → R2 object truly gone (or manually marked resolved)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class R2Orphan(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "r2_orphans"
    __table_args__ = (
        Index("ix_r2_orphans_pending", "next_retry_at",
              postgresql_where=text("resolved_at IS NULL")),
        Index("ix_r2_orphans_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    storage_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<R2Orphan key={self.storage_key[:30]}... "
            f"attempts={self.attempts} resolved={self.resolved_at is not None}>"
        )
