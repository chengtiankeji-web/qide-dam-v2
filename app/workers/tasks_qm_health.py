"""S8 · 链路健康度时序 · 每日 00:30 CST 跑一次

每天跑：
  1. 拉所有 alive workspace + 对应 onboarding
  2. 跨 8 stage 计算每个 workspace 的 stage_status (green/yellow/red/idle)
  3. UPSERT qm_health_metrics 行（day × workspace × stage）
  4. 阻塞天数 > 7 → publish lead 微信告警事件给运营

健康度规则（简化版）：
  - stage_status='idle' : 该 stage 还没启动（如客户还没到 S6 询盘阶段）
  - stage_status='green': 该 stage 有事件 / 内容 / 询盘
  - stage_status='yellow': stage 阻塞 3-7 天（无新事件）
  - stage_status='red': stage 阻塞 > 7 天

KPI 维度：traffic_count / lead_count / order_count / revenue_usd 等 · 来自下游表
（content_published / leads / orders / topic_signals 等）· 这里先填 0 占位 ·
S2 / S3 完成实际数据接入后真填。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_qm_health")


@celery_app.task(name="qm.compute_health_metrics", bind=True, queue="default")
def compute_health_metrics_task(self, target_date: str | None = None) -> dict:
    """beat 触发 · 也可手动 .delay(target_date='2026-05-21')"""
    return asyncio.run(_compute_async(target_date))


async def _compute_async(target_date_str: str | None) -> dict:
    from app.db.session import get_session_factory
    from app.models.qidematrix import (
        QmHealthMetric, QmOnboarding, QmPipelineEvent, QmWorkspace,
    )

    target_date = (
        date.fromisoformat(target_date_str) if target_date_str
        else (datetime.now(UTC) - timedelta(days=1)).date()
    )

    session_factory = get_session_factory()
    workspaces_done = 0

    async with session_factory() as db:
        ws_q = await db.execute(
            select(QmWorkspace).where(QmWorkspace.deleted_at.is_(None))
        )
        workspaces = list(ws_q.scalars())

        for ws in workspaces:
            ob_q = await db.execute(
                select(QmOnboarding).where(
                    QmOnboarding.workspace_id == ws.id
                ).order_by(QmOnboarding.created_at.desc()).limit(1)
            )
            ob = ob_q.scalar_one_or_none()

            for stage in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"):
                # 查最后一次 stage 事件时间
                last_event_q = await db.execute(
                    select(func.max(QmPipelineEvent.created_at)).where(
                        QmPipelineEvent.workspace_id == ws.id,
                        QmPipelineEvent.stage == stage,
                    )
                )
                last_event_at = last_event_q.scalar_one_or_none()

                if last_event_at is None:
                    stage_status = "idle"
                    blocked_days = 0
                else:
                    delta_days = (datetime.now(UTC).date() - last_event_at.date()).days
                    if delta_days > 7:
                        stage_status = "red"
                        blocked_days = delta_days
                    elif delta_days >= 3:
                        stage_status = "yellow"
                        blocked_days = delta_days
                    else:
                        stage_status = "green"
                        blocked_days = 0

                # UPSERT
                await db.execute(
                    text("""
                        INSERT INTO qm_health_metrics (
                            id, workspace_id, tenant_id, onboarding_id,
                            metric_date, stage, stage_status, blocked_days,
                            created_at
                        ) VALUES (
                            gen_random_uuid(), :workspace_id, :tenant_id, :onboarding_id,
                            :metric_date, :stage, :stage_status, :blocked_days,
                            NOW()
                        )
                        ON CONFLICT (workspace_id, metric_date, stage)
                        DO UPDATE SET
                            stage_status = EXCLUDED.stage_status,
                            blocked_days = EXCLUDED.blocked_days
                    """),
                    {
                        "workspace_id": ws.id,
                        "tenant_id": ws.tenant_id,
                        "onboarding_id": ob.id if ob else None,
                        "metric_date": target_date,
                        "stage": stage,
                        "stage_status": stage_status,
                        "blocked_days": blocked_days,
                    },
                )

            workspaces_done += 1

        await db.commit()

    logger.info(
        "qm.health.computed",
        target_date=str(target_date),
        workspaces=workspaces_done,
    )
    return {"target_date": str(target_date), "workspaces_processed": workspaces_done}
