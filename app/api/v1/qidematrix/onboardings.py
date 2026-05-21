"""QideMatrix v1 · S1 入驻 REST API · /v1/qm/onboardings/*

端点：
  POST   /v1/qm/onboardings                 · 提交入驻申请（CMH 表单 / 公开接受）
  GET    /v1/qm/onboardings                 · 列表（运营队列页用）
  GET    /v1/qm/onboardings/{id}            · 单条详情
  PATCH  /v1/qm/onboardings/{id}            · 更新状态 / workspace（运营专用）
  POST   /v1/qm/onboardings/{id}/assign     · 运营点"接单"·启 S4
  POST   /v1/qm/onboardings/{id}/provision  · 手动重试 DAM workspace 创建
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix.pipeline import (
    OnboardingAssignIn,
    OnboardingOut,
    OnboardingSubmitIn,
    OnboardingUpdateIn,
    OperatorQueueOut,
)
from app.services.qidematrix import onboarding_service, pipeline_service

router = APIRouter(prefix="/qm/onboardings", tags=["qidematrix-onboardings"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


@router.post("", response_model=OnboardingOut, status_code=201)
async def submit_onboarding(
    payload: OnboardingSubmitIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    p: Principal | None = Depends(get_current_principal),
):
    """提交入驻申请 · 公开接口（CMH 表单走这里 · 不强制鉴权 · 但有 source_ref 防重）

    若用 API Key 鉴权 → tenant_id 走 Principal · 否则用 payload.tenant_id 或公共默认
    """
    if p and p.tenant_id:
        tenant_id = p.tenant_id
    elif payload.tenant_id:
        tenant_id = payload.tenant_id
    else:
        # 默认归到 zerun (CMH 法律主体 · 板块②)
        # 生产应该 settings.QM_DEFAULT_TENANT_ID
        import os
        default_tenant = os.getenv("QM_DEFAULT_TENANT_ID")
        if not default_tenant:
            raise _err(400, "tenant_id required (no default configured)")
        tenant_id = uuid.UUID(default_tenant)

    ob = await onboarding_service.submit_onboarding(
        db,
        tenant_id=tenant_id,
        payload=payload.model_dump(exclude={"tenant_id"}),
        actor_id=p.user_id if p else None,
        actor_kind="user" if p else "external",
    )
    await db.commit()
    return ob


@router.get("", response_model=list[OnboardingOut])
async def list_onboardings(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    stage_status: str | None = Query(None),
    current_stage: str | None = Query(None),
    operator_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """列入驻申请 · platform_admin 看全部 · 否则看本 tenant"""
    tenant_filter = None if p.is_platform_admin else p.tenant_id
    rows = await onboarding_service.list_onboardings(
        db,
        tenant_id=tenant_filter,
        stage_status=stage_status,
        current_stage=current_stage,
        operator_id=operator_id,
        limit=limit,
        offset=offset,
    )
    return rows


@router.get("/queue", response_model=list[OperatorQueueOut])
async def operator_queue(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    only_unassigned: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    """运营接单队列页 · 优先看 ready / pending 的"""
    from app.models.qidematrix.pipeline import QmDiagnostic, QmOnboarding
    from sqlalchemy import select

    stmt = (
        select(QmOnboarding, QmDiagnostic)
        .outerjoin(QmDiagnostic, QmOnboarding.diagnostic_id == QmDiagnostic.id)
        .order_by(QmOnboarding.created_at.desc())
    )
    if not p.is_platform_admin:
        stmt = stmt.where(QmOnboarding.tenant_id == p.tenant_id)
    if only_unassigned:
        stmt = stmt.where(QmOnboarding.assigned_operator_id.is_(None))
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)

    queue = []
    for ob, diag in result.all():
        # 阻塞天数：仅算 stage_status='processing'/'blocked' 的滞留天数
        from datetime import UTC, datetime
        blocked = 0
        if ob.stage_status in ("processing", "blocked"):
            blocked = (datetime.now(UTC) - ob.updated_at).days
        queue.append(OperatorQueueOut(
            onboarding_id=ob.id,
            factory_name=ob.factory_name,
            contact_name=ob.contact_name,
            contact_email=ob.contact_email,
            product_categories=ob.product_categories,
            target_markets=ob.target_markets,
            monthly_budget=ob.monthly_budget,
            current_stage=ob.current_stage,
            stage_status=ob.stage_status,
            blocked_days=blocked,
            recommended_tier=diag.recommended_tier if diag else None,
            readiness_score=diag.readiness_score if diag else None,
            diagnostic_status=diag.status if diag else None,
            assigned_operator_id=ob.assigned_operator_id,
            created_at=ob.created_at,
        ))
    return queue


@router.get("/{onboarding_id}", response_model=OnboardingOut)
async def get_onboarding(
    onboarding_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    ob = await onboarding_service.get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise _err(404, "onboarding not found")
    if not p.is_platform_admin and ob.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    return ob


@router.patch("/{onboarding_id}", response_model=OnboardingOut)
async def update_onboarding(
    onboarding_id: uuid.UUID,
    payload: OnboardingUpdateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """运营手动更新（推阶段、改派单）"""
    ob = await onboarding_service.get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise _err(404, "onboarding not found")
    if not p.is_platform_admin and ob.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    if payload.stage_status:
        ob.stage_status = payload.stage_status
    if payload.current_stage:
        ob.current_stage = payload.current_stage
    if payload.assigned_operator_id:
        ob.assigned_operator_id = payload.assigned_operator_id
    if payload.workspace_id:
        ob.workspace_id = payload.workspace_id

    from datetime import UTC, datetime
    ob.updated_at = datetime.now(UTC)
    await db.commit()
    return ob


@router.post("/{onboarding_id}/assign", response_model=OnboardingOut)
async def assign_operator(
    onboarding_id: uuid.UUID,
    payload: OnboardingAssignIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """运营点"接单" → 启 S4 工作流"""
    operator_id = payload.operator_id or p.user_id
    if not operator_id:
        raise _err(400, "operator_id required")

    ob = await onboarding_service.assign_operator(
        db, onboarding_id=onboarding_id, operator_id=operator_id, actor_id=p.user_id,
    )
    await db.commit()
    return ob


@router.post("/{onboarding_id}/provision", status_code=202)
async def provision_workspace(
    onboarding_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """手动重试 DAM workspace 创建（任何 stage_status 都可触发）"""
    ob = await onboarding_service.get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise _err(404, "onboarding not found")
    if not p.is_platform_admin and ob.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    from app.workers.celery_app import celery_app
    celery_app.send_task("qm.provision_workspace", args=[str(onboarding_id)])

    return {"queued": True, "task": "qm.provision_workspace", "onboarding_id": str(onboarding_id)}
