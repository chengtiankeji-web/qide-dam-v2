"""Phase 1 (2026-05-08) + v3 P1.3 (2026-05-13): 回收站 + 卡死状态 + R2 孤儿 reapers

═══════════════════════════════════════════════════════════════════════
任务清单（Celery beat 调度）：
═══════════════════════════════════════════════════════════════════════

1. purge_old_trashed_task · 每天 04:00 CST 跑
   - assets.deleted_at < now - 15 天 → hard_delete · 含 R2 删除
   - 老逻辑 · 保留不动

2. reap_stale_uploads_task · 每小时跑（v3 P1.3 P1 D4）
   - assets.status='uploading' AND created_at < now - 24 hours
   - HEAD R2：
       - 存在 → 自动 confirm（让 pipeline 兜底跑）
       - 不存 → 硬删 DB 行（R2 也没 · 不会泄漏）
   - 每次操作写 audit_event

3. reap_stuck_processing_task · 每小时跑（v3 P1.3 P1 D4）
   - assets.status='processing' AND ai_processed_at IS NULL AND created_at < now - 1 hour
   - 重新 enqueue pipeline（处理 worker 进程崩 / broker 丢消息场景）
   - 单 asset 重试 ≤ N=5 次后标 status=failed + audit

4. retry_r2_orphans_task · 每天 05:00 CST 跑（v3 P1.3 P1 D7）
   - r2_orphans WHERE resolved_at IS NULL AND next_retry_at <= NOW
   - 重试 R2 delete · 成功 → resolved_at=NOW + audit
   - 失败 → attempts++ + next_retry_at = NOW + 2^attempts hours
   - attempts >= 10 不再自动重试（仍可手动 admin SPA force）
═══════════════════════════════════════════════════════════════════════

监控：每个任务跑完 log result · failed_count > 0 时关注 R2 token / 网络。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_cleanup")

# ─── 配置常量 ────────────────────────────────────────────────────
DEFAULT_RETENTION_DAYS = 15
STALE_UPLOAD_HOURS = 24
STUCK_PROCESSING_HOURS = 1
STUCK_PROCESSING_MAX_RETRIES = 5
R2_ORPHAN_MAX_RETRIES = 10
R2_ORPHAN_BACKOFF_BASE_HOURS = 1  # 1, 2, 4, 8, 16, 32, 64, 128, 256, 512 hours


# ═══════════════════════════════════════════════════════════════════
# 1. 老任务 · 保留兼容
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="cleanup.purge_old_trashed", bind=True)
def purge_old_trashed_task(self, older_than_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """beat 触发 / 也可手动 .delay(older_than_days=...) 调"""
    return asyncio.run(_purge_async(older_than_days))


async def _purge_async(older_than_days: int) -> dict:
    from app.db.session import get_session_factory
    from app.services import asset_service

    session_factory = get_session_factory()
    async with session_factory() as db:
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


# ═══════════════════════════════════════════════════════════════════
# 2. P1 D4 · 卡 uploading 状态超 24h 自动收尸
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="cleanup.reap_stale_uploads", bind=True)
def reap_stale_uploads_task(self, older_than_hours: int = STALE_UPLOAD_HOURS) -> dict:
    return asyncio.run(_reap_stale_uploads(older_than_hours))


async def _reap_stale_uploads(older_than_hours: int) -> dict:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.asset import Asset
    from app.services import audit_service, storage
    from app.services.asset_service import confirm_upload, hard_delete_asset
    from app.services.audit_service import AuditAction

    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    session_factory = get_session_factory()
    confirmed = 0
    deleted = 0
    failed: list[dict] = []

    async with session_factory() as db:
        rows = (await db.execute(
            select(Asset).where(
                Asset.status == "uploading",
                Asset.created_at < cutoff,
            )
        )).scalars().all()

        for asset in rows:
            try:
                # 检查 R2 上对象是否真存在（可能上传 PUT 完了但 confirm 没调）
                head = storage.head_object(asset.storage_key)
                if head is not None:
                    # 对象在 R2 · 自动 confirm 让 pipeline 接管
                    await confirm_upload(db, tenant_id=asset.tenant_id, asset_id=asset.id)
                    confirmed += 1
                    await audit_service.audit(
                        db,
                        action=AuditAction.REAPER_STALE_UPLOAD,
                        tenant_id=asset.tenant_id,
                        project_id=asset.project_id,
                        actor_user_id=None,
                        actor_kind="system",
                        target_kind="asset",
                        target_id=asset.id,
                        status="success",
                        metadata={
                            "outcome": "auto_confirmed",
                            "stale_hours": older_than_hours,
                            "actor_label": "reap_stale_uploads_task",
                        },
                    )
                else:
                    # R2 上没有 · DB 孤儿行 · 硬删（_get_asset_for_tenant_include_trashed 接受 uploading）
                    # 先 soft 再 hard · soft 通过设 deleted_at + status
                    asset.deleted_at = datetime.now(UTC)
                    asset.status = "archived"
                    await db.flush()
                    await hard_delete_asset(db, tenant_id=asset.tenant_id, asset_id=asset.id)
                    deleted += 1
                    await audit_service.audit(
                        db,
                        action=AuditAction.REAPER_STALE_UPLOAD,
                        tenant_id=asset.tenant_id,
                        project_id=asset.project_id,
                        actor_user_id=None,
                        actor_kind="system",
                        target_kind="asset",
                        target_id=asset.id,
                        status="success",
                        metadata={
                            "outcome": "hard_deleted",
                            "stale_hours": older_than_hours,
                            "actor_label": "reap_stale_uploads_task",
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                failed.append({"asset_id": str(asset.id), "error": str(exc)})
                logger.error("reap_stale_uploads.item_failed",
                             asset_id=str(asset.id), error=str(exc))

        await db.commit()

    logger.info(
        "cleanup.reap_stale_uploads.done",
        confirmed=confirmed, deleted=deleted, failed_count=len(failed),
    )
    return {"confirmed": confirmed, "deleted": deleted, "failed": failed}


# ═══════════════════════════════════════════════════════════════════
# 3. P1 D4 · 卡 processing 超 1h 自动重试 pipeline
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="cleanup.reap_stuck_processing", bind=True)
def reap_stuck_processing_task(self, older_than_hours: int = STUCK_PROCESSING_HOURS) -> dict:
    return asyncio.run(_reap_stuck_processing(older_than_hours))


async def _reap_stuck_processing(older_than_hours: int) -> dict:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.asset import Asset
    from app.services import audit_service
    from app.services.audit_service import AuditAction

    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    session_factory = get_session_factory()
    requeued = 0
    failed_marked = 0

    async with session_factory() as db:
        # 用 custom_fields 记录重试次数（避免新加 schema 列）
        rows = (await db.execute(
            select(Asset).where(
                Asset.status == "processing",
                Asset.ai_processed_at.is_(None),
                Asset.created_at < cutoff,
                Asset.deleted_at.is_(None),
            )
        )).scalars().all()

        for asset in rows:
            cf = dict(asset.custom_fields or {})
            retries = int(cf.get("reaper_processing_retries", 0))
            if retries >= STUCK_PROCESSING_MAX_RETRIES:
                # 标 failed · 不再无限重试
                asset.status = "failed"
                cf["reaper_processing_retries"] = retries
                cf["reaper_processing_failed_at"] = datetime.now(UTC).isoformat()
                asset.custom_fields = cf
                failed_marked += 1
                await audit_service.audit(
                    db,
                    action=AuditAction.REAPER_STUCK_PROCESSING,
                    tenant_id=asset.tenant_id,
                    project_id=asset.project_id,
                    actor_user_id=None,
                    actor_kind="system",
                    target_kind="asset",
                    target_id=asset.id,
                    status="fail",
                    metadata={
                        "outcome": "marked_failed_after_max_retries",
                        "retries": retries,
                        "actor_label": "reap_stuck_processing_task",
                    },
                )
                continue

            # Re-enqueue pipeline
            try:
                from app.workers.tasks_pipeline import process_pipeline
                process_pipeline.delay(str(asset.id))
                cf["reaper_processing_retries"] = retries + 1
                cf["reaper_processing_last_retry_at"] = datetime.now(UTC).isoformat()
                asset.custom_fields = cf
                requeued += 1
                await audit_service.audit(
                    db,
                    action=AuditAction.REAPER_STUCK_PROCESSING,
                    tenant_id=asset.tenant_id,
                    project_id=asset.project_id,
                    actor_user_id=None,
                    actor_kind="system",
                    target_kind="asset",
                    target_id=asset.id,
                    status="success",
                    metadata={
                        "outcome": "requeued",
                        "retries": retries + 1,
                        "actor_label": "reap_stuck_processing_task",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("reap_stuck_processing.requeue_failed",
                             asset_id=str(asset.id), error=str(exc))

        await db.commit()

    logger.info(
        "cleanup.reap_stuck_processing.done",
        requeued=requeued, failed_marked=failed_marked,
    )
    return {"requeued": requeued, "failed_marked": failed_marked}


# ═══════════════════════════════════════════════════════════════════
# 4. P1 D7 · R2 孤儿对象重试删除
# ═══════════════════════════════════════════════════════════════════

@celery_app.task(name="cleanup.retry_r2_orphans", bind=True)
def retry_r2_orphans_task(self) -> dict:
    return asyncio.run(_retry_r2_orphans())


async def _retry_r2_orphans() -> dict:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.r2_orphan import R2Orphan
    from app.services import audit_service, storage
    from app.services.audit_service import AuditAction

    now = datetime.now(UTC)
    session_factory = get_session_factory()
    resolved = 0
    re_failed = 0
    given_up = 0

    async with session_factory() as db:
        rows = (await db.execute(
            select(R2Orphan).where(
                R2Orphan.resolved_at.is_(None),
                R2Orphan.next_retry_at <= now,
                R2Orphan.attempts < R2_ORPHAN_MAX_RETRIES,
            )
        )).scalars().all()

        for orphan in rows:
            try:
                storage.delete_object(orphan.storage_key)
                orphan.resolved_at = now
                resolved += 1
                await audit_service.audit(
                    db,
                    action=AuditAction.R2_ORPHAN_RESOLVED,
                    tenant_id=orphan.tenant_id or _default_tenant_fallback(),
                    project_id=orphan.project_id,
                    actor_user_id=None,
                    actor_kind="system",
                    target_kind="r2_orphan",
                    target_id=orphan.id,
                    status="success",
                    metadata={
                        "storage_key": orphan.storage_key,
                        "attempts": orphan.attempts,
                        "actor_label": "retry_r2_orphans_task",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                orphan.attempts += 1
                orphan.last_error = str(exc)[:500]
                orphan.next_retry_at = now + timedelta(
                    hours=R2_ORPHAN_BACKOFF_BASE_HOURS * (2 ** orphan.attempts)
                )
                re_failed += 1
                if orphan.attempts >= R2_ORPHAN_MAX_RETRIES:
                    given_up += 1

        await db.commit()

    logger.info(
        "cleanup.retry_r2_orphans.done",
        resolved=resolved, re_failed=re_failed, given_up=given_up,
    )
    return {"resolved": resolved, "re_failed": re_failed, "given_up": given_up}


def _default_tenant_fallback():
    """orphan.tenant_id 可能为 None (历史失败行) · audit 又强制 tenant_id 非空 ·
    fallback 到 qide tenant_id（platform 默认）。这种情况罕见 · 仅救灾用。"""
    import os
    fallback = os.environ.get("DEFAULT_TENANT_ID_FOR_AUDIT_FALLBACK")
    if fallback:
        import uuid as _u
        return _u.UUID(fallback)
    # 没设环境变量 · 返 nil UUID（audit_service 会 log 错但不抛）
    import uuid as _u
    return _u.UUID("00000000-0000-0000-0000-000000000000")


# ═══════════════════════════════════════════════════════════════════
# 辅助：hard_delete_asset 失败时记 R2 孤儿（asset_service.py 调）
# ═══════════════════════════════════════════════════════════════════

async def record_r2_orphan(
    db,
    *,
    tenant_id,
    project_id,
    origin_asset_id,
    storage_key: str,
    storage_bucket: str,
    error: str,
) -> None:
    """v3 P1.3 D7: asset_service.hard_delete_asset 调这个 · 把 R2 失败的 key 入孤儿表。

    next_retry_at=NOW + 1h（首次重试） · attempts=0
    """
    from sqlalchemy import select

    from app.models.r2_orphan import R2Orphan
    from app.services import audit_service
    from app.services.audit_service import AuditAction

    # 防重复（同 storage_key 唯一）
    existing = (await db.execute(
        select(R2Orphan).where(R2Orphan.storage_key == storage_key)
    )).scalar_one_or_none()
    if existing:
        # 已记录 · 重置 attempts 让它进新一轮 backoff
        existing.last_error = error[:500]
        existing.next_retry_at = datetime.now(UTC) + timedelta(hours=1)
        return

    orphan = R2Orphan(
        tenant_id=tenant_id,
        project_id=project_id,
        origin_asset_id=origin_asset_id,
        storage_key=storage_key,
        storage_bucket=storage_bucket,
        last_error=error[:500],
        attempts=0,
        next_retry_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(orphan)
    await db.flush()

    await audit_service.audit(
        db,
        action=AuditAction.R2_ORPHAN_RECORDED,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_user_id=None,
        actor_kind="system",
        target_kind="r2_orphan",
        target_id=orphan.id,
        status="success",
        metadata={
            "storage_key": storage_key,
            "origin_asset_id": str(origin_asset_id) if origin_asset_id else None,
            "error": error[:200],
            "actor_label": "hard_delete_asset_fallback",
        },
    )
