"""Deal · 商机 ORM"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    factory_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    primary_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    stage: Mapped[str] = mapped_column(String(32), nullable=False,
                                       server_default="prospect", index=True)
    stage_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    estimated_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    probability_pct: Mapped[int | None] = mapped_column(Integer, server_default="50")
    weighted_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), server_default="USD")

    related_sku_slugs: Mapped[list[str] | None] = mapped_column(ARRAY(String(128)))
    related_quote_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    expected_close_date: Mapped[date | None] = mapped_column(Date)
    actual_close_date: Mapped[date | None] = mapped_column(Date)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    won_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(128))
    lost_competitor: Mapped[str | None] = mapped_column(String(256))

    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            "stage IN ('prospect', 'qualified', 'proposal', 'negotiation', "
            "'closed_won', 'closed_lost', 'on_hold')",
            name="ck_deals_stage",
        ),
        CheckConstraint(
            "probability_pct BETWEEN 0 AND 100", name="ck_deals_probability"
        ),
        Index("ix_deals_owner_stage", "owner_user_id", "stage"),
    )


# Quote 和 CRMActivity 暂占位（v7.1 实装）
