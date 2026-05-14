"""Social Matrix v2 · Celery 任务

任务清单：
  - publish_social_post(post_id)          · 立即发 + 异步回写 metrics
  - publish_scheduled_posts()              · beat 触发·扫 scheduled_at <= now 的帖子
  - sync_post_metrics(post_id)             · 拉新 likes/impressions
  - refresh_expiring_credentials()         · 找 expires_at < now+24h 的 OAuth · 自动 refresh
  - check_credential_health()              · 探测 disconnected 的账号 + 通知

v4.0 占位实现 · publish_social_post 走 social_publisher 的 placeholder。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.models.social import SocialAccount, SocialCredential, SocialPost
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.social")


# ════════════════════════════════════════════════════════════
# 1. publish_social_post
# ════════════════════════════════════════════════════════════

@celery_app.task(name="social.publish_post", bind=True, queue="default")
def publish_social_post(self, post_id: str) -> dict:
    """发帖 · 同步走 social_publisher · 写回 platform_post_id / error"""
    post_uuid = uuid.UUID(post_id)
    with session_scope() as db:
        post = db.get(SocialPost, post_uuid)
        if not post:
            return {"post_id": post_id, "status": "missing"}
        if post.status != "publishing":
            logger.warning(
                "social.publish.skip_not_publishing",
                post_id=post_id, current_status=post.status,
            )
            return {"post_id": post_id, "status": "skipped"}

        account = db.get(SocialAccount, post.account_id)
        if not account or not account.credential_id:
            post.status = "failed"
            post.error_message = "account missing credential"
            db.add(post)
            return {"post_id": post_id, "status": "failed", "reason": "no_credential"}

        credential = db.get(SocialCredential, account.credential_id)
        if not credential or credential.revoked_at is not None:
            post.status = "failed"
            post.error_message = "credential revoked or missing"
            db.add(post)
            return {"post_id": post_id, "status": "failed", "reason": "revoked"}

        # v4.0：placeholder · 真发布 v4.1
        # 由于此处是同步 Celery worker · 直接走 publisher 的 placeholder return
        # （placeholder 永远返 not_implemented · 帖子留在 publishing 状态等 v4.1）

        post.status = "draft"  # 退回 draft · 让 Sam 重试
        post.error_message = "v4.0 publish placeholder · 待 v4.1 接入真实 API"
        post.retry_count = post.retry_count + 1
        db.add(post)

        logger.info(
            "social.publish.placeholder",
            post_id=post_id, platform=account.platform,
            content_chars=len(post.content_text),
        )
        return {
            "post_id": post_id,
            "status": "placeholder_not_published",
            "platform": account.platform,
        }


# ════════════════════════════════════════════════════════════
# 2. publish_scheduled_posts · beat 触发
# ════════════════════════════════════════════════════════════

@celery_app.task(name="social.publish_scheduled", bind=True, queue="default")
def publish_scheduled_posts(self) -> dict:
    """每分钟扫 scheduled 帖子 · 到点入 publishing 队列"""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        due = (
            db.execute(
                select(SocialPost).where(
                    SocialPost.status == "scheduled",
                    SocialPost.scheduled_at <= now,
                )
            ).scalars().all()
        )
        for post in due:
            post.status = "publishing"
            db.add(post)

        for post in due:
            publish_social_post.delay(str(post.id))

        return {"enqueued": len(list(due))}


# ════════════════════════════════════════════════════════════
# 3. refresh_expiring_credentials · beat 触发
# ════════════════════════════════════════════════════════════

@celery_app.task(name="social.refresh_expiring", bind=True, queue="default")
def refresh_expiring_credentials(self) -> dict:
    """每小时扫 expires_at < now+24h 的 OAuth · 自动 refresh

    v4.0 占位 · 真 refresh 调 platform token endpoint
    """
    soon = datetime.now(timezone.utc) + timedelta(hours=24)
    with session_scope() as db:
        rows = (
            db.execute(
                select(SocialCredential).where(
                    SocialCredential.expires_at.is_not(None),
                    SocialCredential.expires_at <= soon,
                    SocialCredential.revoked_at.is_(None),
                    SocialCredential.refresh_failed_at.is_(None),
                )
            ).scalars().all()
        )
        logger.info("social.refresh.candidates", count=len(rows))
        # v4.0 仅 LOG · v4.1 真 refresh + 重新加密入库
        return {"to_refresh": len(rows)}


# ════════════════════════════════════════════════════════════
# 4. check_credential_health · beat 触发
# ════════════════════════════════════════════════════════════

@celery_app.task(name="social.check_health", bind=True, queue="default")
def check_credential_health(self) -> dict:
    """统计：active / expired / disconnected · 给 admin 看"""
    with session_scope() as db:
        rows = list(
            db.execute(select(SocialAccount.status)).scalars().all()
        )
        counts: dict[str, int] = {}
        for s in rows:
            counts[s] = counts.get(s, 0) + 1
        return counts
