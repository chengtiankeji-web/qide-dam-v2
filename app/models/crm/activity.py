"""CRMActivity · 通用活动 timeline ORM"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CRMActivity(Base):
    __tablename__ = "crm_activities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 多态关联
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    subject: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    performed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    # 邮件特有
    email_message_id: Mapped[str | None] = mapped_column(Text)
    email_from: Mapped[str | None] = mapped_column(String(256))
    email_to: Mapped[list[str] | None] = mapped_column(ARRAY(String(256)))
    email_subject: Mapped[str | None] = mapped_column(String(512))
    email_body_preview: Mapped[str | None] = mapped_column(Text)
    email_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 会议
    meeting_location: Mapped[str | None] = mapped_column(String(256))
    meeting_attendees: Mapped[list[str] | None] = mapped_column(ARRAY(String(256)))
    meeting_outcome: Mapped[str | None] = mapped_column(Text)

    # Task
    task_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_priority: Mapped[str | None] = mapped_column(String(16))

    # DB 列名保持 metadata（与 alembic 009 一致）· Python 属性改名避开 SQLAlchemy reserved
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    attachments: Mapped[list[dict] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('lead', 'contact', 'account', 'deal', 'quote')",
            name="ck_activities_entity_type",
        ),
        Index("ix_activities_entity", "entity_type", "entity_id", "performed_at"),
        Index("ix_activities_tenant_time", "tenant_id", "performed_at"),
    )
