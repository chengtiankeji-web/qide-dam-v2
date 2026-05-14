"""Quote · 报价单 ORM（v7.1 起完善实装·当前 v7 MVP 占位）"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quote_number: Mapped[str] = mapped_column(String(64), nullable=False)

    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    factory_slug: Mapped[str | None] = mapped_column(String(64))

    line_items: Mapped[list[dict]] = mapped_column(JSONB, nullable=False,
                                                   server_default="[]")
    subtotal_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2),
                                                  nullable=False, server_default="0")
    discount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    tax_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    shipping_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2),
                                                nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(String(8), server_default="USD")

    validity_days: Mapped[int] = mapped_column(Integer, server_default="30")
    payment_terms: Mapped[str | None] = mapped_column(String(128))
    delivery_terms: Mapped[str | None] = mapped_column(String(32))
    delivery_port: Mapped[str | None] = mapped_column(String(128))
    estimated_lead_time_days: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                        server_default="draft", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_to_email: Mapped[str | None] = mapped_column(String(256))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pdf_storage_key: Mapped[str | None] = mapped_column(Text)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    internal_notes: Mapped[str | None] = mapped_column(Text)
    customer_notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "quote_number", name="uq_quotes_tenant_number"),
        CheckConstraint(
            "status IN ('draft', 'sent', 'viewed', 'accepted', 'declined', "
            "'expired', 'revised', 'cancelled')",
            name="ck_quotes_status",
        ),
        CheckConstraint(
            "delivery_terms IS NULL OR delivery_terms IN "
            "('EXW', 'FOB', 'CIF', 'CFR', 'DDP', 'DAP', 'FCA', 'CPT', 'CIP', 'DPU')",
            name="ck_quotes_incoterms",
        ),
    )
