"""Lead · 询盘 ORM"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    String, Text, Boolean, Integer, Float, DateTime, ForeignKey, CheckConstraint,
    Index, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.crm.contact import Contact
    from app.models.crm.account import Account
    from app.models.crm.deal import Deal


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    factory_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")
    )

    # 来源
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_inbox_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    source_share_link_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("share_links.id", ondelete="SET NULL")
    )
    source_campaign: Mapped[Optional[str]] = mapped_column(String(128))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    source_referrer: Mapped[Optional[str]] = mapped_column(Text)

    # 联系人
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    contact_name: Mapped[Optional[str]] = mapped_column(String(256))
    contact_email: Mapped[Optional[str]] = mapped_column(String(256), index=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64))
    contact_company: Mapped[Optional[str]] = mapped_column(String(256))
    contact_country: Mapped[Optional[str]] = mapped_column(String(64))
    contact_role: Mapped[Optional[str]] = mapped_column(String(256))
    contact_ip: Mapped[Optional[str]] = mapped_column(String(64))
    contact_ua: Mapped[Optional[str]] = mapped_column(Text)

    # 询盘
    inquiry_text: Mapped[str] = mapped_column(Text, nullable=False)
    inquiry_attachments: Mapped[Optional[list[dict]]] = mapped_column(JSONB)
    inquiry_language: Mapped[Optional[str]] = mapped_column(String(8))

    # 6 要素分级
    has_quantity: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_budget: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_timeline: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_specification: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_decision_role: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_company_info: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    six_factor_score: Mapped[int] = mapped_column(Integer, server_default="0")
    six_factor_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB)
    classification: Mapped[Optional[str]] = mapped_column(String(1), index=True)
    classification_overridden: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    classification_overridden_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # 状态机
    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                       server_default="new", index=True)
    assigned_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_contact_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    qualified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    converted_to_deal_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lost_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[Optional[str]] = mapped_column(String(128))
    lost_competitor: Mapped[Optional[str]] = mapped_column(String(256))

    # AI
    ai_intent_summary: Mapped[Optional[str]] = mapped_column(Text)
    ai_suggested_reply: Mapped[Optional[str]] = mapped_column(Text)
    ai_competitors_mentioned: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String(128))
    )
    ai_translated_zh: Mapped[Optional[str]] = mapped_column(Text)
    ai_urgency_score: Mapped[Optional[float]] = mapped_column(Float)
    ai_quality_score: Mapped[Optional[float]] = mapped_column(Float)

    tags: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(64)))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # 时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 关系（双向）
    contact: Mapped[Optional["Contact"]] = relationship(
        "Contact", foreign_keys=[contact_id], back_populates="leads"
    )
    account: Mapped[Optional["Account"]] = relationship(
        "Account", foreign_keys=[account_id]
    )
    converted_deal: Mapped[Optional["Deal"]] = relationship(
        "Deal", foreign_keys=[converted_to_deal_id], post_update=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'qualified', 'unqualified', "
            "'nurturing', 'converted', 'lost', 'spam', 'archived')",
            name="ck_leads_status",
        ),
        CheckConstraint(
            "classification IS NULL OR classification IN ('A', 'B', 'C', 'D')",
            name="ck_leads_classification",
        ),
        CheckConstraint(
            "six_factor_score BETWEEN 0 AND 6",
            name="ck_leads_six_factor_range",
        ),
        Index("ix_leads_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_leads_tenant_factory_class", "tenant_id", "factory_slug", "classification"),
        Index("ix_leads_assigned_status", "assigned_user_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Lead id={self.id} factory={self.factory_slug} "
            f"class={self.classification} status={self.status}>"
        )
