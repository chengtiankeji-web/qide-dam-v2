"""Email outbox worker · 每 60s 扫一次队列 + 真发

发送策略：
- claim_next_ready 批量拿 status='queued' 行
- 调 send_email_sync · Resend / SMTP / Stub 三档
- 成功 → mark_sent · 失败 → mark_failed（指数退避）
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_qm_email")


@celery_app.task(name="qm.send_email_batch", bind=True, queue="default")
def send_email_batch_task(self, batch_size: int = 10) -> dict:
    return asyncio.run(_send_batch_async(batch_size))


async def _send_batch_async(batch_size: int) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import email_service

    session_factory = get_session_factory()
    sent = 0
    failed = 0

    async with session_factory() as db:
        ready = await email_service.claim_next_ready(db, limit=batch_size)
        await db.commit()  # 释放锁

    for outbox in ready:
        ok, msg_id, err = email_service.send_email_sync(outbox)
        async with session_factory() as db:
            if ok:
                await email_service.mark_sent(
                    db, outbox_id=outbox.id, provider_msg_id=msg_id
                )
                sent += 1
            else:
                await email_service.mark_failed(
                    db, outbox_id=outbox.id, error=err or "unknown"
                )
                failed += 1
            await db.commit()

    if sent or failed:
        logger.info("qm.email.batch_done", sent=sent, failed=failed)

    return {"sent": sent, "failed": failed, "claimed": len(ready)}


@celery_app.task(name="qm.send_email_now", bind=True, queue="default", max_retries=3)
def send_email_now_task(self, outbox_id: str) -> dict:
    """单个邮件立即发 · 用于"重试""测试""手动重发"""
    import uuid
    return asyncio.run(_send_one_async(uuid.UUID(outbox_id)))


async def _send_one_async(outbox_id) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import email_service

    session_factory = get_session_factory()

    async with session_factory() as db:
        ob = await email_service.get_outbox(db, outbox_id=outbox_id)
        if not ob:
            return {"ok": False, "error": "outbox not found"}

    ok, msg_id, err = email_service.send_email_sync(ob)
    async with session_factory() as db:
        if ok:
            await email_service.mark_sent(db, outbox_id=outbox_id, provider_msg_id=msg_id)
        else:
            await email_service.mark_failed(db, outbox_id=outbox_id, error=err or "unknown")
        await db.commit()

    return {"ok": ok, "outbox_id": str(outbox_id), "error": err}
