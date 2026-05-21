"""QideMatrix v1 · S8 链路健康度仪表盘 REST API · /v1/qm/health/*"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.qidematrix import (
    QmHealthMetric, QmOnboarding, QmPipelineEvent, QmWorkspace,
)
from app.schemas.qidematrix.pipeline import (
    HealthMetricOut, LinkHealthSnapshotOut,
)

router = APIRouter(prefix="/qm/health", tags=["qidematrix-health"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


@router.get("/snapshot", response_model=list[LinkHealthSnapshotOut])
async def link_health_snapshot(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    snapshot_date: date | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """链路健康度快照 · 每 workspace 一行 · 含 8 stage 当前状态"""
    target = snapshot_date or (datetime.now(UTC) - timedelta(days=1)).date()

    ws_q = select(QmWorkspace).where(QmWorkspace.deleted_at.is_(None))
    if not p.is_platform_admin:
        ws_q = ws_q.where(QmWorkspace.tenant_id == p.tenant_id)
    ws_q = ws_q.limit(limit)
    ws_result = await db.execute(ws_q)
    workspaces = list(ws_result.scalars())

    snapshots = []
    for ws in workspaces:
        # 拿当日 8 stage 状态
        m_q = await db.execute(
            select(QmHealthMetric).where(
                QmHealthMetric.workspace_id == ws.id,
                QmHealthMetric.metric_date == target,
            )
        )
        metrics = list(m_q.scalars())
        stage_statuses: dict[str, str] = {}
        blocked_stages: list[str] = []
        for m in metrics:
            stage_statuses[m.stage] = m.stage_status
            if m.stage_status in ("yellow", "red"):
                blocked_stages.append(m.stage)

        # 整体状态：red > yellow > idle > green
        overall = "green"
        if "red" in stage_statuses.values():
            overall = "red"
        elif "yellow" in stage_statuses.values():
            overall = "yellow"
        elif set(stage_statuses.values()) == {"idle"}:
            overall = "idle"

        # last event
        last_event_q = await db.execute(
            select(func.max(QmPipelineEvent.created_at)).where(
                QmPipelineEvent.workspace_id == ws.id,
            )
        )
        last_event_at = last_event_q.scalar_one_or_none()

        # 30 天 KPI 累计
        cutoff = datetime.now(UTC) - timedelta(days=30)
        kpi_q = await db.execute(
            select(
                func.coalesce(func.sum(QmHealthMetric.traffic_count), 0).label("traffic"),
                func.coalesce(func.sum(QmHealthMetric.lead_count), 0).label("leads"),
                func.coalesce(func.sum(QmHealthMetric.qualified_lead_count), 0).label("qualified"),
                func.coalesce(func.sum(QmHealthMetric.order_count), 0).label("orders"),
                func.coalesce(func.sum(QmHealthMetric.revenue_usd), 0).label("revenue"),
            ).where(
                QmHealthMetric.workspace_id == ws.id,
                QmHealthMetric.metric_date >= cutoff.date(),
            )
        )
        kpi_row = kpi_q.one()
        kpi_30d = {
            "traffic": int(kpi_row.traffic or 0),
            "leads": int(kpi_row.leads or 0),
            "qualified_leads": int(kpi_row.qualified or 0),
            "orders": int(kpi_row.orders or 0),
            "revenue_usd": float(kpi_row.revenue or 0),
        }

        snapshots.append(LinkHealthSnapshotOut(
            workspace_id=ws.id,
            factory_name=ws.display_name,
            snapshot_date=target,
            overall_status=overall,
            stage_statuses=stage_statuses,
            blocked_stages=blocked_stages,
            last_event_at=last_event_at,
            kpi_30d=kpi_30d,
        ))

    return snapshots


@router.get("/metrics/{workspace_id}", response_model=list[HealthMetricOut])
async def workspace_metrics(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    """单 workspace 时序指标 · 给客户月报 + dashboard chart 用"""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).date()
    stmt = (
        select(QmHealthMetric)
        .where(
            QmHealthMetric.workspace_id == workspace_id,
            QmHealthMetric.metric_date >= cutoff,
        )
        .order_by(QmHealthMetric.metric_date.desc(), QmHealthMetric.stage)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars())

    if rows and not p.is_platform_admin and rows[0].tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    return rows


@router.post("/recompute", status_code=202)
async def recompute_health(
    p: Principal = Depends(get_current_principal),
    target_date: date | None = Query(None),
):
    """手动触发健康度重算（任何 platform_admin 都可）"""
    if not p.is_platform_admin:
        raise _err(403, "platform_admin only")
    from app.workers.celery_app import celery_app
    celery_app.send_task(
        "qm.compute_health_metrics",
        args=[target_date.isoformat() if target_date else None],
    )
    return {"queued": True}
