"""activities_service · 通用 timeline · 任何 entity 都能追加活动

支持类型：
  - email          邮件（来源 Resend webhook / 手工记）
  - call           电话
  - meeting        会议
  - note           笔记
  - task           任务（含 due_at / completed_at）
  - whatsapp       WhatsApp 消息
  - sms            SMS
  - dm             社媒 DM
  - visit          客户访问
  - quote_sent     报价单发送（系统自动）
  - quote_viewed   报价单查看
  - status_change  状态变更（lead/deal）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.models.crm.activity import CRMActivity


async def create_activity(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    activity_type: str,
    entity_type: str,  # lead/contact/account/deal/quote
    entity_id: uuid.UUID,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    meeting_location: Optional[str] = None,
    meeting_attendees: Optional[list[str]] = None,
    meeting_outcome: Optional[str] = None,
    task_due_at: Optional[datetime] = None,
    task_priority: Optional[str] = None,
    metadata: Optional[dict] = None,
    attachments: Optional[list[dict]] = None,
) -> CRMActivity:
    activity = CRMActivity(
        tenant_id=tenant_id,
        activity_type=activity_type,
        entity_type=entity_type,
        entity_id=entity_id,
        subject=subject,
        description=description,
        performed_by_user_id=principal.user_id,
        duration_minutes=duration_minutes,
        meeting_location=meeting_location,
        meeting_attendees=meeting_attendees,
        meeting_outcome=meeting_outcome,
        task_due_at=task_due_at,
        task_priority=task_priority,
        metadata=metadata,
        attachments=attachments,
    )
    db.add(activity)
    await db.flush()
    return activity


async def list_activities(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: Optional[str] = None,
    entity_id: Optional[uuid.UUID] = None,
    activity_type: Optional[str] = None,
    performed_by_user_id: Optional[uuid.UUID] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CRMActivity]:
    """列活动 · 默认按 performed_at 倒序"""
    conditions = [CRMActivity.tenant_id == tenant_id]
    if entity_type:
        conditions.append(CRMActivity.entity_type == entity_type)
    if entity_id:
        conditions.append(CRMActivity.entity_id == entity_id)
    if activity_type:
        conditions.append(CRMActivity.activity_type == activity_type)
    if performed_by_user_id:
        conditions.append(CRMActivity.performed_by_user_id == performed_by_user_id)

    q = (
        select(CRMActivity)
        .where(and_(*conditions))
        .order_by(CRMActivity.performed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(q)).scalars().all())


async def complete_task(
    db: AsyncSession,
    *,
    activity_id: uuid.UUID,
) -> CRMActivity:
    """task_completed_at = now"""
    activity = await db.get(CRMActivity, activity_id)
    if not activity:
        raise ValueError("Activity not found")
    if activity.activity_type != "task":
        raise ValueError("Not a task")
    activity.task_completed_at = datetime.now(timezone.utc)
    await db.flush()
    return activity


async def get_overdue_tasks(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
) -> list[CRMActivity]:
    """逾期任务 · 给 sales dashboard 红色提示用"""
    now = datetime.now(timezone.utc)
    conditions = [
        CRMActivity.tenant_id == tenant_id,
        CRMActivity.activity_type == "task",
        CRMActivity.task_completed_at.is_(None),
        CRMActivity.task_due_at < now,
    ]
    if user_id:
        conditions.append(CRMActivity.performed_by_user_id == user_id)

    q = (
        select(CRMActivity)
        .where(and_(*conditions))
        .order_by(CRMActivity.task_due_at.asc())
    )
    return list((await db.execute(q)).scalars().all())
