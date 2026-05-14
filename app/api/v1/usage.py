"""Usage — daily counters + per-period summary for billing/admin."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.schemas.usage import UsageDayOut, UsageSummaryOut
from app.services import usage_service

router = APIRouter()


# v3 P1.3 四修 (2026-05-13 深夜): Dashboard 全 0 根因 ·
# usage_meters 历史从没 bump 过（13,500 资产都是旧版 confirm 没新 bump 代码） ·
# 加这个端点直接从 assets 表算实时数 · 不依赖 usage_meters · Dashboard 一律 100% 准

class LiveSummaryOut(BaseModel):
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None = None
    # 实时计数（含所有 status · 不含 deleted_at IS NOT NULL）
    total_count: int
    total_storage_bytes: int
    # 状态分布
    ready_count: int
    processing_count: int
    uploading_count: int
    failed_count: int
    # 30 天内新增（按 created_at）
    new_in_30d_count: int
    new_in_30d_bytes: int
    # AI 跑过的（ai_processed_at NOT NULL）
    ai_processed_count: int
    # 回收站
    trashed_count: int


@router.get("/live-summary", response_model=LiveSummaryOut)
async def get_live_summary(
    project_id: uuid.UUID | None = Query(None),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> LiveSummaryOut:
    """v3 P1.3 #4 (2026-05-13 深夜) · Dashboard 实时计数 · 不依赖 usage_meters · 直接 assets 表 SQL aggregate"""
    if project_id and not p.can_access_project(project_id):
        from fastapi import HTTPException, status as _s
        raise HTTPException(_s.HTTP_403_FORBIDDEN, "no access")

    # platform_admin 选了 project 跨租户的 · effective tenant 从 project 反查
    effective_tid = p.tenant_id
    if project_id and p.is_platform_admin:
        from app.models.project import Project as _P
        proj = (await db.execute(select(_P).where(_P.id == project_id))).scalar_one_or_none()
        if proj:
            effective_tid = proj.tenant_id

    base_filters = [Asset.tenant_id == effective_tid]
    if project_id:
        base_filters.append(Asset.project_id == project_id)
    alive_filter = Asset.deleted_at.is_(None)
    trashed_filter = Asset.deleted_at.is_not(None)
    cutoff_30d = datetime.now(UTC) - timedelta(days=30)

    # 大查询 · 一条 SQL 拿所有 aggregate
    q = select(
        func.count(Asset.id).filter(alive_filter).label("total_count"),
        func.coalesce(func.sum(Asset.size_bytes).filter(alive_filter), 0).label("total_bytes"),
        func.count(Asset.id).filter(alive_filter, Asset.status == "ready").label("ready_count"),
        func.count(Asset.id).filter(alive_filter, Asset.status == "processing").label("processing_count"),
        func.count(Asset.id).filter(alive_filter, Asset.status == "uploading").label("uploading_count"),
        func.count(Asset.id).filter(alive_filter, Asset.status == "failed").label("failed_count"),
        func.count(Asset.id).filter(alive_filter, Asset.created_at >= cutoff_30d).label("new_30d_count"),
        func.coalesce(
            func.sum(Asset.size_bytes).filter(alive_filter, Asset.created_at >= cutoff_30d), 0
        ).label("new_30d_bytes"),
        func.count(Asset.id).filter(alive_filter, Asset.ai_processed_at.is_not(None)).label("ai_processed"),
        func.count(Asset.id).filter(trashed_filter).label("trashed_count"),
    ).where(*base_filters)

    row = (await db.execute(q)).one()

    return LiveSummaryOut(
        tenant_id=effective_tid,
        project_id=project_id,
        total_count=int(row.total_count or 0),
        total_storage_bytes=int(row.total_bytes or 0),
        ready_count=int(row.ready_count or 0),
        processing_count=int(row.processing_count or 0),
        uploading_count=int(row.uploading_count or 0),
        failed_count=int(row.failed_count or 0),
        new_in_30d_count=int(row.new_30d_count or 0),
        new_in_30d_bytes=int(row.new_30d_bytes or 0),
        ai_processed_count=int(row.ai_processed or 0),
        trashed_count=int(row.trashed_count or 0),
    )


@router.get("/summary", response_model=UsageSummaryOut)
async def get_summary(
    period_from: date | None = Query(None),
    period_to: date | None = Query(None),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryOut:
    today = date.today()
    period_from = period_from or (today - timedelta(days=30))
    period_to = period_to or today
    rows = await usage_service.summary(
        db, tenant_id=p.tenant_id, period_from=period_from, period_to=period_to
    )
    days = [UsageDayOut.model_validate(r) for r in rows]
    return UsageSummaryOut(
        tenant_id=p.tenant_id,
        period_from=period_from,
        period_to=period_to,
        days=days,
        total_storage_bytes=max((d.storage_bytes_total for d in days), default=0),
        total_upload_bytes=sum(d.upload_bytes for d in days),
        total_download_bytes=sum(d.download_bytes for d in days),
        total_ai_calls=sum(d.ai_calls for d in days),
        total_webhook_deliveries=sum(d.webhook_deliveries for d in days),
    )
