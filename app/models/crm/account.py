"""Account · 公司 ORM"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    legal_name: Mapped[str | None] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    country: Mapped[str | None] = mapped_column(String(64), index=True)
    country_code: Mapped[str | None] = mapped_column(String(2))
    industry: Mapped[str | None] = mapped_column(String(128))
    sub_industry: Mapped[str | None] = mapped_column(String(128))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    annual_revenue_usd: Mapped[int | None] = mapped_column(BigInteger)
    founded_year: Mapped[int | None] = mapped_column(Integer)

    website: Mapped[str | None] = mapped_column(Text)
    primary_phone: Mapped[str | None] = mapped_column(String(64))
    primary_email: Mapped[str | None] = mapped_column(String(256))
    billing_address: Mapped[dict | None] = mapped_column(JSONB)
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    primary_contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    notes: Mapped[str | None] = mapped_column(Text)

    ai_company_intel: Mapped[dict | None] = mapped_column(JSONB)
    ai_competitor_score: Mapped[float | None] = mapped_column(Float)
    ai_lead_quality_score: Mapped[float | None] = mapped_column(Float)
    ai_last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    external_ids: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    contacts: Mapped[list[Contact]] = relationship(
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
