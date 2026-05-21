"""QideMatrix v1 · S2 诊断 REST API · /v1/qm/diagnostics/*"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix.pipeline import DiagnosticOut, DiagnosticRegenerateIn
from app.services.qidematrix import diagnostic_service, onboarding_service

router = APIRouter(prefix="/qm/diagnostics", tags=["qidematrix-diagnostics"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


@router.get("", response_model=list[DiagnosticOut])
async def list_diagnostics(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    tenant_filter = None if p.is_platform_admin else p.tenant_id
    return await diagnostic_service.list_diagnostics(
        db, tenant_id=tenant_filter, status=status, limit=limit,
    )


@router.get("/{diagnostic_id}", response_model=DiagnosticOut)
async def get_diagnostic(
    diagnostic_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    diag = await diagnostic_service.get_diagnostic(db, diagnostic_id=diagnostic_id)
    if not diag:
        raise _err(404, "diagnostic not found")
    if not p.is_platform_admin and diag.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    return diag


@router.get("/by-onboarding/{onboarding_id}", response_model=DiagnosticOut | None)
async def get_diagnostic_by_onboarding(
    onboarding_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    ob = await onboarding_service.get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise _err(404, "onboarding not found")
    if not p.is_platform_admin and ob.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    diag = await diagnostic_service.get_diagnostic_by_onboarding(
        db, onboarding_id=onboarding_id
    )
    return diag


@router.post("/{diagnostic_id}/regenerate", status_code=202)
async def regenerate_diagnostic(
    diagnostic_id: uuid.UUID,
    payload: DiagnosticRegenerateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """手动重生 · 重置 status='pending' · 触发 diagnostic.requested 事件"""
    diag = await diagnostic_service.get_diagnostic(db, diagnostic_id=diagnostic_id)
    if not diag:
        raise _err(404, "diagnostic not found")
    if not p.is_platform_admin and diag.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    from datetime import UTC, datetime

    from app.services.qidematrix import pipeline_service

    diag.status = "pending"
    diag.error_message = payload.reason
    diag.updated_at = datetime.now(UTC)

    await pipeline_service.publish(
        db,
        tenant_id=diag.tenant_id,
        workspace_id=diag.workspace_id,
        event_type="diagnostic.requested",
        actor_kind="user",
        actor_id=p.user_id,
        subject_kind="diagnostic",
        subject_id=diag.id,
        payload={
            "diagnostic_id": str(diag.id),
            "reason": payload.reason or "manual regenerate",
            "override_model": payload.override_model,
        },
    )
    await db.commit()
    return {"queued": True, "diagnostic_id": str(diag.id)}


@router.post("/{diagnostic_id}/re-render-pdf", status_code=202)
async def re_render_pdf(
    diagnostic_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """重新渲染 PDF（用于：PDF link 过期 / 字体修复后批量重传）"""
    diag = await diagnostic_service.get_diagnostic(db, diagnostic_id=diagnostic_id)
    if not diag:
        raise _err(404, "diagnostic not found")
    if not p.is_platform_admin and diag.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    if diag.status != "ready":
        raise _err(400, f"diagnostic status={diag.status} (must be 'ready')")

    from app.workers.celery_app import celery_app
    celery_app.send_task("qm.render_diagnostic_pdf", args=[str(diagnostic_id)])
    return {"queued": True}
