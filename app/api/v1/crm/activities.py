"""/v1/crm/activities · 通用活动 timeline REST API"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require_authenticated
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
    subject: Optional[str] = Field(None, max_length=512)
    description: Optional[str] = None
    duration_minutes: Optional[int] = Field(None, ge=0)
    meeting_location: Optional[str] = Field(None, max_length=256)
    meeting_attendees: Optional[list[str]] = None
    meeting_outcome: Optional[str] = None
    task_due_at: Optional[datetime] = None
    task_priority: Optional[str] = Field(None, pattern="^(low|medium|high|urgent)$")
    metadata: Optional[dict] = None
    attachments: Optional[list[dict]] = None


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    activity_type: str
    entity_type: str
    entity_id: uuid.UUID
    subject: Optional[str]
    description: Optional[str]
    performed_at: datetime
    performed_by_user_id: Optional[uuid.UUID]
    duration_minutes: Optional[int]
    email_message_id: Optional[str]
    email_from: Optional[str]
    email_to: Optional[list[str]]
    email_subject: Optional[str]
    email_body_preview: Optional[str]
    email_opened_at: Optional[datetime]
    email_clicked_at: Optional[datetime]
    meeting_location: Optional[str]
    meeting_attendees: Optional[list[str]]
    meeting_outcome: Optional[str]
    task_due_at: Optional[datetime]
    task_completed_at: Optional[datetime]
    task_priority: Optional[str]
    metadata: Optional[dict]
    attachments: Optional[list[dict]]
    created_at: datetime


class ActivityListOut(BaseModel):
    items: list[ActivityOut]
    total: int


# ── Endpoints ──────────────────────────────────────────

@router.post("/", response_model=ActivityOut, status_code=http_status.HTTP_201_CREATED)
async def create_activity(
    payload: ActivityCreate,
    principal: Principal = Depends(require_authenticated),
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
        metadata=payload.metadata,
        attachments=payload.attachments,
    )
    await db.commit()
    return ActivityOut.model_validate(activity)


@router.get("/", response_model=ActivityListOut)
async def list_activities(
    entity_type: Optional[str] = Query(None, pattern="^(lead|contact|account|deal|quote)$"),
    entity_id: Optional[uuid.UUID] = Query(None),
    activity_type: Optional[str] = Query(None),
    performed_by_user_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_authenticated),
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
    principal: Principal = Depends(require_authenticated),
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
    user_id: Optional[uuid.UUID] = Query(None),
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ActivityListOut:
    rows = await activities_service.get_overdue_tasks(
        db, tenant_id=principal.tenant_id, user_id=user_id,
    )
    return ActivityListOut(
        items=[ActivityOut.model_validate(r) for r in rows],
        total=len(rows),
    )
