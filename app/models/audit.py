"""AuditEvent — append-only event stream with database-enforced immutability.

v3 P0-2. Backed by alembic 004_v3_security migration, which also installs
PostgreSQL triggers that RAISE EXCEPTION on UPDATE or DELETE — so even a
compromised application role cannot rewrite history.

Event taxonomy (action namespace : verb)
----------------------------------------
auth.login_success / auth.login_fail / auth.refresh_revoked / auth.totp_*
member.invited / member.accepted / member.removed / member.role_changed
asset.uploaded / asset.previewed / asset.downloaded / asset.updated
  / asset.permission_changed / asset.deleted
vault.created / vault.updated / vault.read_requested / vault.revealed
  / vault.copied / vault.export_attempted / vault.decrypt_failed
ai.search_called / ai.asset_snippet_read / ai.answer_delivered
  / ai.tool_denied (when secret-mask refused a request)

Required fields by event class
------------------------------
- vault.* events MUST set `target_kind='vault_item'` and `target_id`
- ai.* events MUST set `purpose` (otherwise the API rejects the call)
- All events SHOULD set ip + user_agent when an HTTP request is the trigger

Querying
--------
The (tenant_id, created_at) index covers the typical "show me last 7
days for this tenant" admin view; the (target_kind, target_id) index
covers "what happened to this asset" detail views.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

# Stable enums — keep DB CHECK constraints in alembic 004 in sync.
ACTOR_KINDS = ("user", "api_key", "system", "ai")
EVENT_STATUSES = ("success", "fail", "denied")


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Single immutable record of one auditable action.

    Note: this model has NO TimestampMixin (which would add updated_at).
    Audit events have only created_at because they are not mutable.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_actor", "actor_user_id"),
        Index("ix_audit_events_action", "action"),
        Index("ix_audit_events_target", "target_kind", "target_id"),
    )

    # --- Scope ---
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # --- Actor (who did this) ---
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")

    # --- Action + target (what happened) ---
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # --- Outcome + intent ---
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="success")
    # Required for ai.* and any read of confidential/secret data
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Request context ---
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Free-form payload ---
    # Search-query hashes, file sizes, edition numbers, asset names, etc.
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",  # column name in DB (alembic uses `metadata`); attribute renamed to avoid clashing with SQLAlchemy Base.metadata
        JSONB,
        default=dict,
        nullable=False,
    )

    # --- Timestamp ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AuditEvent action={self.action!r} actor={self.actor_user_id} "
            f"target={self.target_kind}:{self.target_id} status={self.status}>"
        )
