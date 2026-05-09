"""Phase 1 (2026-05-08): 回收站定时清理 · Celery beat 每天 04:00 CST 跑

策略：
- assets.deleted_at < now - 15 天 → 永久删除（含 R2 对象 + DB 行）
- 跨所有 tenant 一起跑
- 失败的 asset 进 task return 摘要 · 不阻塞下一个

监控建议：beat 跑完 log "purged_count=N · failed=[]"。
如果 failed 不为空 · 看具体 R2 4xx 错误（rate limit / 已被外部删 / 凭证过期）。
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_cleanup")

# 默认保留 15 天 · 与文档 / 用户预期一致
DEFAULT_RETENTION_DAYS = 15


@celery_app.task(name="cleanup.purge_old_trashed", bind=True)
def purge_old_trashed_task(self, older_than_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """beat 触发 / 也可手动 .delay(older_than_days=...) 调"""
    return asyncio.run(_purge_async(older_than_days))


async def _purge_async(older_than_days: int) -> dict:
    from app.db.session import async_session_factory
    from app.services import asset_service

    async with async_session_factory() as db:
        try:
            result = await asset_service.purge_old_trashed(
                db, older_than_days=older_than_days
            )
            logger.info(
                "cleanup.purge.done",
                purged_count=result["purged_count"],
                failed_count=len(result["failed"]),
                cutoff=result["cutoff"],
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("cleanup.purge.error", error=str(exc))
            await db.rollback()
            raise
