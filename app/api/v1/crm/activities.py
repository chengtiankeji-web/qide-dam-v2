"""/v1/crm/activities · 通用活动 timeline REST API"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.services.crm import activities_service

router = APIRouter()


# ── Schemas ────────────────────────────────────────────

class ActivityCreate(BaseModel):
    activity_type: str = Field(
        ..., pattern="^(email|call|meeting|note|task|whatsapp|sms|dm|visit|quote_sent|quote_viewed|status_change)$"
    )
    entity_type: str = Field(..., pattern="^(lead|contact|account|deal|quote)$")
    entity_id: uuid.UUID
    subject: str | None = Field(None, max_length=512)
    description: str | None = None
    duration_minutes: int | None = Field(None, ge=0)
    meeting_location: str | None = Field(None, max_length=256)
    meeting_attendees: list[str] | None = None
    meeting_outcome: str | None = None
    task_due_at: datetime | None = None
    task_priority: str | None = Field(None, pattern="^(low|medium|high|urgent)$")
    metadata: dict | None = None
    attachments: list[dict] | None = None


class ActivityOut(BaseModel):
    # ORM 属性叫 extra_metadata (SQLAlchemy `metadata` 保留) · API 仍叫 metadata · 用 alias 映射
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    activity_type: str
    entity_type: str
    entity_id: uuid.UUID
    subject: str | None
    description: str | None
    performed_at: datetime
    performed_by_user_id: uuid.UUID | None
    duration_minutes: int | None
    email_message_id: str | None
    email_from: str | None
    email_to: list[str] | None
    email_subject: str | None
    email_body_preview: str | None
    email_opened_at: datetime | None
    email_clicked_at: datetime | None
    meeting_location: str | None
    meeting_attendees: list[str] | None
    meeting_outcome: str | None
    task_due_at: datetime | None
    task_completed_at: datetime | None
    task_priority: str | None
    metadata: dict | None = Field(None, alias="extra_metadata")
    attachments: list[dict] | None
    created_at: datetime


class ActivityListOut(BaseModel):
    items: list[ActivityOut]
    total: int


# ── Endpoints ──────────────────────────────────────────

@router.post("", response_model=ActivityOut, status_code=http_status.HTTP_201_CREATED)
async def create_activity(
    payload: ActivityCreate,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ActivityOut:
    activity = await activities_service.create_activity(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        activity_type=payload.activity_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        subject=payload.subject,
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        meeting_location=payload.meeting_location,
        meeting_attendees=payload.meeting_attendees,
        meeting_outcome=payload.meeting_outcome,
        task_due_at=payload.task_due_at,
        task_priority=payload.task_priority,
        extra_metadata=payload.metadata,
        attachments=payload.attachments,
    )
    await db.commit()
    return ActivityOut.model_validate(activity)


@router.get("", response_model=ActivityListOut)
async def list_activities(
    entity_type: str | None = Query(None, pattern="^(lead|contact|account|deal|quote)$"),
    entity_id: uuid.UUID | None = Query(None),
    activity_type: str | None = Query(None),
    performed_by_user_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ActivityListOut:
    rows = await activities_service.list_activities(
        db,
        tenant_id=principal.tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        activity_type=activity_type,
        performed_by_user_id=performed_by_user_id,
        limit=limit,
        offset=offset,
    )
    return ActivityListOut(
        items=[ActivityOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.post("/{activity_id}/complete-task", response_model=ActivityOut)
async def complete_task(
    activity_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ActivityOut:
    try:
        activity = await activities_service.complete_task(db, activity_id=activity_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return ActivityOut.model_validate(activity)


@router.get("/overdue-tasks", response_model=ActivityListOut)
async def get_overdue_tasks(
    user_id: uuid.UUID | None = Query(None),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ActivityListOut:
    rows = await activities_service.get_overdue_tasks(
        db, tenant_id=principal.tenant_id, user_id=user_id,
    )
    return ActivityListOut(
        items=[ActivityOut.model_validate(r) for r in rows],
        total=len(rows),
    )
