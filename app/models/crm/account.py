"""Account · 公司 ORM"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    String, Text, Integer, BigInteger, Float, DateTime, ForeignKey, CheckConstraint,
    Index, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.crm.contact import Contact


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    legal_name: Mapped[Optional[str]] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    country: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(2))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    sub_industry: Mapped[Optional[str]] = mapped_column(String(128))
    employee_count: Mapped[Optional[int]] = mapped_column(Integer)
    annual_revenue_usd: Mapped[Optional[int]] = mapped_column(BigInteger)
    founded_year: Mapped[Optional[int]] = mapped_column(Integer)

    website: Mapped[Optional[str]] = mapped_column(Text)
    primary_phone: Mapped[Optional[str]] = mapped_column(String(64))
    primary_email: Mapped[Optional[str]] = mapped_column(String(256))
    billing_address: Mapped[Optional[dict]] = mapped_column(JSONB)
    shipping_address: Mapped[Optional[dict]] = mapped_column(JSONB)

    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    primary_contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    tags: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(64)))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    ai_company_intel: Mapped[Optional[dict]] = mapped_column(JSONB)
    ai_competitor_score: Mapped[Optional[float]] = mapped_column(Float)
    ai_lead_quality_score: Mapped[Optional[float]] = mapped_column(Float)
    ai_last_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    external_ids: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    contacts: Mapped[list["Contact"]] = relationship(
        "Contact",
        primaryjoin="Account.id == Contact.account_id",
        back_populates="account",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'inactive', 'archived', 'merged', 'spam')",
            name="ck_accounts_status",
        ),
        Index("ix_accounts_tenant_country", "tenant_id", "country"),
    )
