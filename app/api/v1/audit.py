"""/v1/audit — read-only listing of immutable audit events.

v3 P0-2.

Endpoints:
  GET /v1/audit                  list events (filterable; tenant-scoped)
  GET /v1/audit/{id}             single event by id
  GET /v1/audit/actions          static list of all known action codes

Append-only at the DB layer via triggers — there is no POST/PATCH/DELETE.
Events are written automatically by every other route via the
`audit_service.audit()` helper.

Permission model:
  - platform_admin can read events for ANY tenant (via ?tenant_id=)
  - tenant_admin / member with audit.read scope can read their own tenant
  - viewers cannot read audit logs
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.services.audit_service import AuditAction

router = APIRouter(prefix="/audit", tags=["audit"])


# ─── Response models ────────────────────────────────────────────────────


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    actor_kind: str
    action: str
    target_kind: str | None = None
    target_id: uuid.UUID | None = None
    status: str
    purpose: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    extra_metadata: dict
    created_at: datetime


# ─── Endpoints ──────────────────────────────────────────────────────────


@router.get("", response_model=list[AuditEventOut])
async def list_events(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_user_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(description="action prefix, e.g. 'vault.'")] = None,
    target_kind: Annotated[str | None, Query()] = None,
    target_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    since_days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEventOut]:
    """List audit events. Defaults to last 30 days for current tenant."""

    # Resolve effective tenant_id: only platform_admin can override.
    effective_tenant = principal.tenant_id
    if tenant_id and tenant_id != principal.tenant_id:
        if not principal.is_platform_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "cross-tenant audit listing requires platform_admin"
            )
        effective_tenant = tenant_id

    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    stmt = (
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == effective_tenant,
            AuditEvent.created_at >= since,
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    if actor_user_id is not None:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
    if action:
        # action is a prefix filter — "vault." catches all vault.* events
        stmt = stmt.where(AuditEvent.action.like(f"{action}%"))
    if target_kind:
        stmt = stmt.where(AuditEvent.target_kind == target_kind)
    if target_id is not None:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if status_filter:
        stmt = stmt.where(AuditEvent.status == status_filter)

    rows = (await db.execute(stmt)).scalars().all()
    return [AuditEventOut.model_validate(r) for r in rows]


@router.get("/{event_id}", response_model=AuditEventOut)
async def get_event(
    event_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> AuditEventOut:
    event = (
        await db.execute(select(AuditEvent).where(AuditEvent.id == event_id))
    ).scalar_one_or_none()
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audit event not found")
    if event.tenant_id != principal.tenant_id and not principal.is_platform_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audit event not found")  # opaque 404
    return AuditEventOut.model_validate(event)


@router.get("/_meta/actions")
async def list_known_actions(
    _: Principal = Depends(get_current_principal),
) -> dict[str, list[str]]:
    """Static enumeration — useful for admin UI to render filter dropdowns."""
    return {
        "actions": sorted(
            [
                v
                for k, v in vars(AuditAction).items()
                if not k.startswith("_") and isinstance(v, str)
            ]
        ),
    }
