"""Contact · 联系人 ORM"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.crm.account import Account
    from app.models.crm.lead import Lead


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )

    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    role_category: Mapped[str | None] = mapped_column(String(64))
    department: Mapped[str | None] = mapped_column(String(128))
    seniority_level: Mapped[str | None] = mapped_column(String(64))

    email: Mapped[str | None] = mapped_column(String(256), index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone: Mapped[str | None] = mapped_column(String(64))
    mobile: Mapped[str | None] = mapped_column(String(64))
    whatsapp: Mapped[str | None] = mapped_column(String(64))
    wechat: Mapped[str | None] = mapped_column(String(128))

    linkedin_url: Mapped[str | None] = mapped_column(Text)
    social_handles: Mapped[dict | None] = mapped_column(JSONB)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str | None] = mapped_column(String(64))
    source_inbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    opt_in_marketing: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    opt_in_marketing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    notes: Mapped[str | None] = mapped_column(Text)

    ai_personality_profile: Mapped[dict | None] = mapped_column(JSONB)
    ai_last_engagement_summary: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    account: Mapped[Account | None] = relationship(
        "Account",
        primaryjoin="Contact.account_id == Account.id",
        back_populates="contacts",
    )
    leads: Mapped[list[Lead]] = relationship(
        "Lead", foreign_keys="Lead.contact_id", back_populates="contact"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'inactive', 'bounced', 'unsubscribed', 'spam', 'archived')",
            name="ck_contacts_status",
        ),
        CheckConstraint(
            "role_category IS NULL OR role_category IN "
            "('decision_maker', 'influencer', 'user', 'admin', 'gatekeeper', 'unknown')",
            name="ck_contacts_role_category",
        ),
        Index("ix_contacts_tenant_email", "tenant_id", "email"),
    )
