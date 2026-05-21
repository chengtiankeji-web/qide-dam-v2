"""QideMatrix v1 · 事件总线 + 邮件 outbox REST API

端点：
  GET   /v1/qm/pipeline-events       · 列事件 · admin 看全部
  GET   /v1/qm/pipeline-events/{id}  · 单条详情
  POST  /v1/qm/pipeline-events/{id}/revive · 救活 parked 事件
  POST  /v1/qm/pipeline-events/drain · 手动触发一次 drain

  GET   /v1/qm/emails                · outbox 列表
  POST  /v1/qm/emails/{id}/resend    · 手动重发
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix.pipeline import EmailOutboxOut, PipelineEventOut
from app.services.qidematrix import email_service, pipeline_service

router = APIRouter(prefix="/qm", tags=["qidematrix-pipeline"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# ─── Events ─────────────────────────────────────────────────────────

@router.get("/pipeline-events", response_model=list[PipelineEventOut])
async def list_pipeline_events(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    workspace_id: uuid.UUID | None = Query(None),
    stage: str | None = Query(None),
    event_type: str | None = Query(None),
    status: str | None = Query(None),
    subject_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    tenant_filter = None if p.is_platform_admin else p.tenant_id
    rows = await pipeline_service.list_events(
        db,
        workspace_id=workspace_id,
        tenant_id=tenant_filter,
        stage=stage,
        event_type=event_type,
        status=status,
        subject_id=subject_id,
        limit=limit,
        offset=offset,
    )
    return rows


@router.get("/pipeline-events/{event_id}", response_model=PipelineEventOut)
async def get_pipeline_event(
    event_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    event = await pipeline_service.get_event(db, event_id=event_id)
    if not event:
        raise _err(404, "event not found")
    if not p.is_platform_admin and event.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    return event


@router.post("/pipeline-events/{event_id}/revive", status_code=200)
async def revive_event(
    event_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.is_platform_admin:
        raise _err(403, "platform_admin only")
    ok = await pipeline_service.revive_parked(db, event_id=event_id)
    await db.commit()
    if not ok:
        raise _err(404, "event not parked (or not found)")
    return {"revived": True, "event_id": str(event_id)}


@router.post("/pipeline-events/drain", status_code=202)
async def manual_drain(
    p: Principal = Depends(get_current_principal),
    batch_size: int = Query(20, ge=1, le=100),
):
    """手动触发一次 drain（调试 / 部署后 smoke 用）"""
    if not p.is_platform_admin:
        raise _err(403, "platform_admin only")
    from app.workers.celery_app import celery_app
    celery_app.send_task("qm.pipeline_drain", kwargs={"batch_size": batch_size})
    return {"queued": True}


# ─── Emails ─────────────────────────────────────────────────────────

@router.get("/emails", response_model=list[EmailOutboxOut])
async def list_emails(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    tenant_filter = None if p.is_platform_admin else p.tenant_id
    return await email_service.list_outbox(
        db, tenant_id=tenant_filter, status=status, limit=limit,
    )


@router.post("/emails/{outbox_id}/resend", status_code=202)
async def resend_email(
    outbox_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    ob = await email_service.get_outbox(db, outbox_id=outbox_id)
    if not ob:
        raise _err(404, "outbox not found")
    if not p.is_platform_admin and ob.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    # 重置 status=queued + attempts=0
    from datetime import UTC, datetime
    from sqlalchemy import update
    from app.models.qidematrix.pipeline import QmEmailOutbox

    await db.execute(
        update(QmEmailOutbox).where(QmEmailOutbox.id == outbox_id).values(
            status="queued",
            attempts=0,
            send_after=datetime.now(UTC),
            last_error=None,
        )
    )
    await db.commit()

    from app.workers.celery_app import celery_app
    celery_app.send_task("qm.send_email_now", args=[str(outbox_id)])
    return {"queued": True}
