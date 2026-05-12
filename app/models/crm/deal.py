"""Deal · 商机 ORM"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    String, Text, Integer, Numeric, Date, DateTime, ForeignKey,
    CheckConstraint, Index, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    primary_contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    stage: Mapped[str] = mapped_column(String(32), nullable=False,
                                       server_default="prospect", index=True)
    stage_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    estimated_value_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    probability_pct: Mapped[Optional[int]] = mapped_column(Integer, server_default="50")
    weighted_value_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), server_default="USD")

    related_sku_slugs: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(128)))
    related_quote_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(ARRAY(UUID(as_uuid=True)))

    expected_close_date: Mapped[Optional[date]] = mapped_column(Date)
    actual_close_date: Mapped[Optional[date]] = mapped_column(Date)

    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    won_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    won_value_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    lost_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[Optional[str]] = mapped_column(String(128))
    lost_competitor: Mapped[Optional[str]] = mapped_column(String(256))

    tags: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(64)))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
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
from app.models.crm.quote import Quote  # noqa: E402
from app.models.crm.activity import CRMActivity  # noqa: E402
