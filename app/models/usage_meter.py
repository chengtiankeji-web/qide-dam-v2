"""Per-tenant per-day usage counters — feeds billing + quota enforcement."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UsageMeter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "day", name="uq_usage_meters_tenant_day"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Counters — incremented atomically by `usage_service.bump`
    storage_bytes_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    upload_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    download_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    asset_count_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    new_asset_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ai_calls: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ai_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ai_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    webhook_deliveries: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
