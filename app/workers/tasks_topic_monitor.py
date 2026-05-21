"""QideMatrix · 社媒爆款话题监测 · Celery 任务（Phase A）

═══════════════════════════════════════════════════════════════════════
任务清单：
═══════════════════════════════════════════════════════════════════════

1. topic_monitor.fetch_all       · 每天 06:00 CST · 抓全部 enabled subreddit + 落 signals
2. topic_monitor.score_new       · 每天 06:30 CST · 把新 signals 跑 LLM 评分 → candidates
3. topic_monitor.daily_pipeline  · 一键串：fetch + score · 06:00 跑（默认）

beat schedule（注册在 celery_app.py）：
  · 06:00 跑 fetch_all
  · 06:30 跑 score_new
  · 同时把 6:32 接到 SEO writer 自动选 top1 候选（Phase B 接入）

设计：
  · Celery 走 sync wrapper（asyncio.run）+ 业务逻辑全部 async
  · 与 cleanup 模块同一套路（_db.py 不用 · 因为 service 是 async asyncpg 的）
  · 单租户跑：默认 workspace_id=None（全 workspace · 当前生产只有 qide-internal）

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_topic_monitor")


# ═══════════════════════════════════════════════════════════════════
# 1. fetch_all · 抓 Reddit · 落 signals
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="topic_monitor.fetch_all", bind=True, queue="default")
def fetch_all_task(self, workspace_id: str | None = None) -> dict:
    """beat 触发 · 也可手动 .delay(workspace_id='...') 调"""
    return asyncio.run(_fetch_all_async(workspace_id))


async def _fetch_all_async(workspace_id: str | None) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import topic_monitor_service as tms

    ws_uuid = uuid.UUID(workspace_id) if workspace_id else None
    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            result = await tms.fetch_and_store_all_enabled(db, workspace_id=ws_uuid)
            logger.info("topic_monitor.fetch_all.done", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("topic_monitor.fetch_all.error", error=str(exc)[:300])
            await db.rollback()
            raise


# ═══════════════════════════════════════════════════════════════════
# 2. score_new · 把未评分 signals 跑 LLM
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="topic_monitor.score_new", bind=True, queue="ai")
def score_new_task(
    self, workspace_id: str | None = None, limit: int = 200
) -> dict:
    """beat 触发 · 把还没评分的 signals 跑 LLM"""
    return asyncio.run(_score_new_async(workspace_id, limit))


async def _score_new_async(workspace_id: str | None, limit: int) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import topic_monitor_service as tms

    ws_uuid = uuid.UUID(workspace_id) if workspace_id else None
    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            result = await tms.score_unscored_signals(
                db, workspace_id=ws_uuid, limit=limit
            )
            logger.info("topic_monitor.score_new.done", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("topic_monitor.score_new.error", error=str(exc)[:300])
            await db.rollback()
            raise


# ═══════════════════════════════════════════════════════════════════
# 3. daily_pipeline · 串：fetch + score · 用于 Cowork scheduled task 触发
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="topic_monitor.daily_pipeline", bind=True, queue="default")
def daily_pipeline_task(
    self, workspace_id: str | None = None, score_limit: int = 200
) -> dict:
    """一键跑：抓 → 评分 → 返合并摘要 · 不真推消息（推由上层 scheduled task 看返回值决定）"""
    return asyncio.run(_daily_pipeline_async(workspace_id, score_limit))


async def _daily_pipeline_async(
    workspace_id: str | None, score_limit: int
) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import topic_monitor_service as tms

    ws_uuid = uuid.UUID(workspace_id) if workspace_id else None
    session_factory = get_session_factory()

    # 步骤 1 · fetch
    async with session_factory() as db:
        fetch_result = await tms.fetch_and_store_all_enabled(db, workspace_id=ws_uuid)

    # 步骤 2 · score（新会话 · 避免长事务）
    async with session_factory() as db:
        score_result = await tms.score_unscored_signals(
            db, workspace_id=ws_uuid, limit=score_limit
        )

    # 步骤 3 · 列 top 3 候选 · 给上层调度看
    async with session_factory() as db:
        top = await tms.list_top_pending_candidates(
            db, workspace_id=ws_uuid, top_n=3
        )
        top_summary = [
            {
                "id": str(c.id),
                "score": c.composite_score,
                "title": c.suggested_title or c.distilled_topic,
                "persona": c.target_buyer_persona,
            }
            for c in top
        ]

    summary = {
        "fetch": fetch_result,
        "score": score_result,
        "top_candidates": top_summary,
    }
    logger.info("topic_monitor.daily_pipeline.done", summary=summary)
    return summary
