"""Celery app — Sprint 1 stub. Heavy media processing tasks land in Sprint 2.

CRITICAL: when adding tasks, register them with `autodiscover_tasks` listing the
exact module paths. Passing only the package name silently fails to register
sub-modules, causing `KeyError: 'task_name'` at runtime.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "qide-dam",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.tasks_image",
        "app.workers.tasks_video",
        "app.workers.tasks_document",
        "app.workers.tasks_ai",
        "app.workers.tasks_webhook",
        "app.workers.tasks_pipeline",
        "app.workers.tasks_cleanup",
        "app.workers.tasks_intake",  # v4 Smart Intake
        "app.workers.tasks_social",  # v4 Social Matrix
        "app.workers.tasks_topic_monitor",  # v5 QideMatrix · Reddit 话题监测 Phase A
        # v1 QideMatrix · 8 阶段业务流（2026-05-21）
        "app.workers.tasks_qm_pipeline",
        "app.workers.tasks_qm_diagnostic",
        "app.workers.tasks_qm_dam_provisioning",
        "app.workers.tasks_qm_email",
        "app.workers.tasks_qm_health",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=4,
    task_track_started=True,
    task_reject_on_worker_lost=True,
    task_default_queue="default",
    task_routes={
        "app.workers.tasks_image.*": {"queue": "media"},
        "app.workers.tasks_video.*": {"queue": "media"},
        "app.workers.tasks_document.*": {"queue": "media"},
        "app.workers.tasks_ai.*": {"queue": "ai"},
        "app.workers.tasks_webhook.*": {"queue": "webhook"},
        "cleanup.*": {"queue": "default"},
        "topic_monitor.score_new": {"queue": "ai"},
        "topic_monitor.*": {"queue": "default"},
        # v1 QideMatrix queues
        "qm.process_diagnostic_*": {"queue": "ai"},
        "qm.render_diagnostic_pdf": {"queue": "media"},
        "qm.process_*": {"queue": "default"},
        "qm.send_email_batch": {"queue": "default"},
        "qm.compute_health_metrics": {"queue": "default"},
        "qm.pipeline_drain": {"queue": "default"},
    },
    # Phase 1 (2026-05-08): 回收站每天 04:00 CST 自动清 15 天前的 soft-deleted
    # v3 P1.3 (2026-05-13): + 3 个 reaper 任务
    beat_schedule={
        "purge-old-trashed-daily": {
            "task": "cleanup.purge_old_trashed",
            "schedule": crontab(hour=4, minute=0),  # 每天 04:00 (timezone=Asia/Shanghai)
            "kwargs": {"older_than_days": 15},
        },
        # v3 P1.3 D4: status=uploading 超 24h 自动收（HEAD R2 存在则 confirm · 不存在则硬删）
        "reap-stale-uploads-hourly": {
            "task": "cleanup.reap_stale_uploads",
            "schedule": crontab(minute=0),  # 每小时整点
            "kwargs": {"older_than_hours": 24},
        },
        # v3 P1.3 D4: status=processing 超 1h Celery 没动 → 重新 enqueue pipeline
        # 错开 :15 避开整点 reap_stale_uploads 抢资源
        "reap-stuck-processing-hourly": {
            "task": "cleanup.reap_stuck_processing",
            "schedule": crontab(minute=15),
            "kwargs": {"older_than_hours": 1},
        },
        # v3 P1.3 D7: R2 删失败孤儿 · 每天 05:00 CST backoff 重试
        # 错开 04:00 purge_old_trashed（避免一起跑撞 R2 速率）
        "retry-r2-orphans-daily": {
            "task": "cleanup.retry_r2_orphans",
            "schedule": crontab(hour=5, minute=0),  # 每天 05:00 (timezone=Asia/Shanghai)
        },
        # v5 QideMatrix Phase A (2026-05-15) · Reddit 话题监测
        # 06:00 抓 + 06:30 评分 · 06:32 接 SEO writer 自动选 top1（Phase B）
        "qm-topic-fetch-daily": {
            "task": "topic_monitor.fetch_all",
            "schedule": crontab(hour=6, minute=0),  # 每天 06:00 CST
        },
        "qm-topic-score-daily": {
            "task": "topic_monitor.score_new",
            "schedule": crontab(hour=6, minute=30),  # 每天 06:30 CST · 给 fetch 留 30 min
            "kwargs": {"limit": 200},
        },
        # v1 QideMatrix pipeline drain · 每 30 秒扫一次 pending 事件（兜底 · LISTEN/NOTIFY 主路径）
        "qm-pipeline-drain": {
            "task": "qm.pipeline_drain",
            "schedule": 30.0,  # seconds
            "kwargs": {"batch_size": 20},
        },
        # v1 邮件 outbox 每分钟扫一次
        "qm-email-send-loop": {
            "task": "qm.send_email_batch",
            "schedule": 60.0,
            "kwargs": {"batch_size": 10},
        },
        # v1 S8 健康度每日 00:30 CST 跑（错开 FAQ 引擎）
        "qm-health-compute-daily": {
            "task": "qm.compute_health_metrics",
            "schedule": crontab(hour=0, minute=30),
        },
    },
)


# Sprint 1 ships only stub modules so worker boots; Sprint 2 fills them in.
if __name__ == "__main__":  # pragma: no cover
    celery_app.start()
