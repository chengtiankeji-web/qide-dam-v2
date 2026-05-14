"""Smart Intake v4 · REST API · /v1/intake/*

端点：
  POST   /v1/intake/jobs                          创建任务
  GET    /v1/intake/jobs                          列表
  GET    /v1/intake/jobs/{id}                     详情（含 clusters）
  GET    /v1/intake/jobs/{id}/summary             仪表盘聚合
  POST   /v1/intake/jobs/{id}/transition          状态机（approve / reject / cancel）
  GET    /v1/intake/jobs/{id}/items               文件列表
  PATCH  /v1/intake/items/{id}                    用户 override（subdir / filename / tags）
  POST   /v1/intake/jobs/{id}/items/_bulk/decide  批量 approve / reject
  POST   /v1/intake/clusters/{id}/rename          BD 改 SKU slug
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.intake import IntakeCluster, IntakeItem, IntakeJob
from app.schemas.intake import (
    BulkDecisionIn,
    BulkDecisionOut,
    ClusterRenameIn,
    IntakeClusterOut,
    IntakeItemOut,
    IntakeItemOverride,
    IntakeJobCreate,
    IntakeJobOut,
    IntakeJobSummary,
    IntakeJobTransition,
)
from app.services import intake_service

router = APIRouter()


# ════════════════════════════════════════════════════════════
# Job CRUD
# ════════════════════════════════════════════════════════════

@router.post("/jobs", response_model=IntakeJobOut, status_code=201)
async def create_intake_job(
    payload: IntakeJobCreate,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeJobOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        job = await intake_service.create_job(
            db,
            principal=p,
            tenant_id=p.tenant_id,
            project_id=payload.project_id,
            factory_slug=payload.factory_slug,
            source_path=payload.source_path,
            options=payload.options,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # 触发 Celery scan（异步）·不阻塞 response
    try:
        from app.workers.tasks_intake import run_intake_pipeline
        run_intake_pipeline.delay(str(job.id))
    except Exception as exc:  # noqa: BLE001
        # Celery 不可用时·不阻塞·任务保持 scanning · 手动触发
        from app.core.logging import get_logger
        get_logger("intake.api").warning(
            "celery_enqueue_failed", job_id=str(job.id), error=str(exc)
        )

    return IntakeJobOut.model_validate(job)


@router.get("/jobs", response_model=list[IntakeJobOut])
async def list_intake_jobs(
    project_id: uuid.UUID | None = Query(None),
    factory_slug: str | None = Query(None, max_length=64),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[IntakeJobOut]:
    if project_id is not None and not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    rows = await intake_service.list_jobs(
        db,
        tenant_id=p.tenant_id,
        project_id=project_id,
        factory_slug=factory_slug,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return [IntakeJobOut.model_validate(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=IntakeJobOut)
async def get_intake_job(
    job_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeJobOut:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id, eager_clusters=False,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    return IntakeJobOut.model_validate(job)


@router.get("/jobs/{job_id}/summary", response_model=IntakeJobSummary)
async def get_intake_job_summary(
    job_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeJobSummary:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    data = await intake_service.job_summary(db, job_id=job_id)
    return IntakeJobSummary(**data)


@router.post("/jobs/{job_id}/transition", response_model=IntakeJobOut)
async def transition_intake_job(
    job_id: uuid.UUID,
    payload: IntakeJobTransition,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeJobOut:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    try:
        job = await intake_service.transition_status(
            db,
            job=job,
            new_status=payload.new_status,
            principal=p,
            reason=payload.reason,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # approve 后立刻入队 push 任务
    if payload.new_status == "approved":
        try:
            from app.workers.tasks_intake import push_to_dam
            push_to_dam.delay(str(job.id))
        except Exception as exc:  # noqa: BLE001
            from app.core.logging import get_logger
            get_logger("intake.api").warning(
                "celery_enqueue_failed_push",
                job_id=str(job.id), error=str(exc),
            )

    return IntakeJobOut.model_validate(job)


# ════════════════════════════════════════════════════════════
# Item
# ════════════════════════════════════════════════════════════

@router.get("/jobs/{job_id}/items", response_model=list[IntakeItemOut])
async def list_intake_items(
    job_id: uuid.UUID,
    category: str | None = Query(None, max_length=64),
    sku_slug: str | None = Query(None, max_length=128),
    flagged_only: bool = Query(False),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[IntakeItemOut]:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    rows = await intake_service.list_items(
        db,
        job_id=job_id,
        category=category,
        sku_slug=sku_slug,
        flagged_only=flagged_only,
        limit=limit,
        offset=offset,
    )
    return [IntakeItemOut.model_validate(r) for r in rows]


@router.patch("/items/{item_id}", response_model=IntakeItemOut)
async def override_intake_item(
    item_id: uuid.UUID,
    payload: IntakeItemOverride,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeItemOut:
    # 联表载入 job 拿 tenant_id / project_id
    q = (
        select(IntakeItem)
        .where(IntakeItem.id == item_id)
        .join(IntakeJob, IntakeJob.id == IntakeItem.job_id)
        .where(IntakeJob.tenant_id == p.tenant_id)
    )
    result = await db.execute(q)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake item not found")
    # 再 load job 做权限检查 + audit
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=item.job_id,
    )
    if not job or not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    # 确保 item.job 关系可用（audit 要用 job.tenant_id / project_id）
    item.job = job
    override = payload.model_dump(exclude_none=True)
    item = await intake_service.approve_item(
        db,
        principal=p,
        item=item,
        override=override or None,
        request=request,
    )
    return IntakeItemOut.model_validate(item)


@router.post(
    "/jobs/{job_id}/items/_bulk/decide",
    response_model=BulkDecisionOut,
)
async def bulk_decide_items(
    job_id: uuid.UUID,
    payload: BulkDecisionIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> BulkDecisionOut:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    affected = await intake_service.bulk_decide(
        db,
        principal=p,
        job_id=job_id,
        item_ids=payload.item_ids,
        decision=payload.decision,
        request=request,
    )
    return BulkDecisionOut(affected=affected)


# ════════════════════════════════════════════════════════════
# Cluster
# ════════════════════════════════════════════════════════════

@router.get("/jobs/{job_id}/clusters", response_model=list[IntakeClusterOut])
async def list_intake_clusters(
    job_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[IntakeClusterOut]:
    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=job_id, eager_clusters=True,
    )
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake job not found")
    if not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    return [IntakeClusterOut.model_validate(c) for c in job.clusters]


@router.post("/clusters/{cluster_id}/rename", response_model=IntakeClusterOut)
async def rename_intake_cluster(
    cluster_id: uuid.UUID,
    payload: ClusterRenameIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> IntakeClusterOut:
    q = (
        select(IntakeCluster)
        .where(IntakeCluster.id == cluster_id)
        .join(IntakeJob, IntakeJob.id == IntakeCluster.job_id)
        .where(IntakeJob.tenant_id == p.tenant_id)
    )
    result = await db.execute(q)
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intake cluster not found")

    job = await intake_service.get_job(
        db, tenant_id=p.tenant_id, job_id=cluster.job_id,
    )
    if not job or not p.can_access_project(job.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    cluster.job = job  # 给 audit 用
    cluster = await intake_service.rename_cluster(
        db,
        principal=p,
        cluster=cluster,
        new_slug=payload.new_slug,
        request=request,
    )
    return IntakeClusterOut.model_validate(cluster)
