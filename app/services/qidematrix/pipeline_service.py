"""QideMatrix v1 · 8 阶段事件总线 · publish + subscribe + state machine

设计要点：
- INSERT 到 qm_pipeline_events → trigger `qm_pipeline_events_notify()` → pg_notify('qm_event', ...)
- Workers `LISTEN qm_event` 拿到通知后处理 / 走对应 Celery 任务
- Polling fallback：每 10s 扫一次 pending 行（重启 / 漏通知 兜底）
- 死信：attempts >= 5 → status = 'parked'
- 不允许 UPDATE 核心字段（触发器层保护 · alembic 017）
- 不允许 DELETE（触发器层保护）

14 个事件类型 · 见 schemas.qidematrix.pipeline.EVENT_TYPES
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update, text, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix.pipeline import QmPipelineEvent
from app.schemas.qidematrix.pipeline import EVENT_TYPES

logger = get_logger("qm.pipeline")


# ─── 事件 → stage 映射（用作 publish 时 stage 推断 fallback）─────────

EVENT_STAGE_MAP: dict[str, str] = {
    "onboarding.submitted": "S1",
    "onboarding.completed": "S1",
    "diagnostic.requested": "S2",
    "diagnostic.ready": "S2",
    "diagnostic.failed": "S2",
    "dam.workspace_ready": "S3",
    "social.matrix_requested": "S4",
    "social.matrix_ready": "S4",
    "content.scheduled": "S5",
    "content.published": "S5",
    "lead.qualified": "S6",
    "lead.converted": "S6",
    "order.placed": "S7",
    "order.delivered": "S7",
}


# ─── 事件 → 下游处理器映射（worker 路由表）──────────────────────────

EVENT_HANDLER_MAP: dict[str, str] = {
    # event_type → Celery task name (qm.tasks_*)
    "onboarding.submitted": "qm.process_onboarding_submitted",
    "onboarding.completed": "qm.process_onboarding_completed",
    "diagnostic.requested": "qm.process_diagnostic_requested",
    "diagnostic.ready": "qm.process_diagnostic_ready",
    "diagnostic.failed": "qm.process_diagnostic_failed",
    "dam.workspace_ready": "qm.process_dam_workspace_ready",
    "social.matrix_requested": "qm.process_social_matrix_requested",
    "social.matrix_ready": "qm.process_social_matrix_ready",
    "content.scheduled": "qm.process_content_scheduled",
    "content.published": "qm.process_content_published",
    "lead.qualified": "qm.process_lead_qualified",
    "lead.converted": "qm.process_lead_converted",
    "order.placed": "qm.process_order_placed",
    "order.delivered": "qm.process_order_delivered",
}


MAX_ATTEMPTS = 5


# ═════════════════════════════════════════════════════════════════════
# 1. publish · 主入口 · 服务代码调用这个发事件
# ═════════════════════════════════════════════════════════════════════

async def publish(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    workspace_id: uuid.UUID | None = None,
    stage: str | None = None,
    actor_kind: str = "system",
    actor_id: uuid.UUID | None = None,
    subject_kind: str | None = None,
    subject_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> QmPipelineEvent:
    """发布事件 · 同 transaction 一并 INSERT · 触发器自动 NOTIFY。

    服务调用例子：
        await publish(
            db,
            tenant_id=onboarding.tenant_id,
            event_type="onboarding.completed",
            subject_kind="onboarding",
            subject_id=onboarding.id,
            payload={"onboarding_id": str(onboarding.id), "factory_name": "..."},
        )
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type} (allowed: {EVENT_TYPES})")

    if stage is None:
        stage = EVENT_STAGE_MAP.get(event_type)
        if stage is None:
            raise ValueError(f"cannot infer stage for {event_type}; pass stage= explicitly")

    now = datetime.now(UTC)
    event = QmPipelineEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        event_type=event_type,
        stage=stage,
        actor_kind=actor_kind,
        actor_id=actor_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        payload=payload or {},
        status="pending",
        attempts=0,
        created_at=now,
    )
    db.add(event)
    await db.flush()
    logger.info(
        "qm_event_published",
        event_id=str(event.id),
        event_type=event_type,
        stage=stage,
        workspace_id=str(workspace_id) if workspace_id else None,
        subject_id=str(subject_id) if subject_id else None,
    )
    return event


# ═════════════════════════════════════════════════════════════════════
# 2. claim_next_pending · workers 拉取一个 pending 事件
# ═════════════════════════════════════════════════════════════════════

async def claim_next_pending(
    db: AsyncSession,
    *,
    event_types: list[str] | None = None,
    limit: int = 1,
) -> list[QmPipelineEvent]:
    """worker 拉一个 pending 事件 + 原子翻 status='processing'。

    使用 SELECT ... FOR UPDATE SKIP LOCKED · 多 worker 并发安全。
    """
    where_clauses = ["status = 'pending'", "attempts < :max_attempts"]
    params: dict[str, Any] = {"max_attempts": MAX_ATTEMPTS, "limit": limit}

    if event_types:
        where_clauses.append("event_type = ANY(:event_types)")
        params["event_types"] = event_types

    where_sql = " AND ".join(where_clauses)

    # CTE：拿一批 id + lock · 翻 status · 返回完整 row
    sql = text(f"""
        WITH locked AS (
            SELECT id FROM qm_pipeline_events
            WHERE {where_sql}
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE qm_pipeline_events e
        SET
            status = 'processing',
            attempts = e.attempts + 1,
            last_attempt_at = NOW()
        FROM locked
        WHERE e.id = locked.id
        RETURNING e.id
    """)

    result = await db.execute(sql, params)
    event_ids = [row[0] for row in result.fetchall()]
    if not event_ids:
        return []

    rows = await db.execute(
        select(QmPipelineEvent).where(QmPipelineEvent.id.in_(event_ids))
    )
    return list(rows.scalars().all())


# ═════════════════════════════════════════════════════════════════════
# 3. mark_delivered / mark_failed · worker 处理完后回写
# ═════════════════════════════════════════════════════════════════════

async def mark_delivered(
    db: AsyncSession, *, event_id: uuid.UUID
) -> None:
    """标记事件投递成功"""
    await db.execute(
        update(QmPipelineEvent)
        .where(QmPipelineEvent.id == event_id)
        .values(status="delivered", delivered_at=datetime.now(UTC))
    )


async def mark_failed(
    db: AsyncSession, *, event_id: uuid.UUID, error: str
) -> None:
    """标记事件投递失败 · attempts 已在 claim 时 +1

    如果 attempts >= MAX_ATTEMPTS：自动 park（死信）
    否则：status = 'pending' 重新可被 claim
    """
    sql = text("""
        UPDATE qm_pipeline_events
        SET
            status = CASE WHEN attempts >= :max_attempts THEN 'parked' ELSE 'pending' END,
            last_error = :error
        WHERE id = :event_id
    """)
    await db.execute(
        sql,
        {
            "event_id": event_id,
            "error": (error or "")[:2000],
            "max_attempts": MAX_ATTEMPTS,
        },
    )


# ═════════════════════════════════════════════════════════════════════
# 4. revive_parked · 手动救活死信
# ═════════════════════════════════════════════════════════════════════

async def revive_parked(
    db: AsyncSession, *, event_id: uuid.UUID
) -> bool:
    """运营 / Sam 手动救活 parked 事件 · 重置 attempts = 0 + status = 'pending'"""
    result = await db.execute(
        text("""
            UPDATE qm_pipeline_events
            SET status = 'pending', attempts = 0, last_error = NULL
            WHERE id = :event_id AND status = 'parked'
        """),
        {"event_id": event_id},
    )
    return result.rowcount > 0


# ═════════════════════════════════════════════════════════════════════
# 5. list_events · 查询接口 · API / admin SPA 用
# ═════════════════════════════════════════════════════════════════════

async def list_events(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    stage: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QmPipelineEvent]:
    stmt = select(QmPipelineEvent).order_by(QmPipelineEvent.created_at.desc())
    if workspace_id:
        stmt = stmt.where(QmPipelineEvent.workspace_id == workspace_id)
    if tenant_id:
        stmt = stmt.where(QmPipelineEvent.tenant_id == tenant_id)
    if stage:
        stmt = stmt.where(QmPipelineEvent.stage == stage)
    if event_type:
        stmt = stmt.where(QmPipelineEvent.event_type == event_type)
    if status:
        stmt = stmt.where(QmPipelineEvent.status == status)
    if subject_id:
        stmt = stmt.where(QmPipelineEvent.subject_id == subject_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_event(
    db: AsyncSession, *, event_id: uuid.UUID
) -> QmPipelineEvent | None:
    result = await db.execute(
        select(QmPipelineEvent).where(QmPipelineEvent.id == event_id)
    )
    return result.scalar_one_or_none()
