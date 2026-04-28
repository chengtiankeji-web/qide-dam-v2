"""Celery app — Sprint 1 stub. Heavy media processing tasks land in Sprint 2.

CRITICAL: when adding tasks, register them with `autodiscover_tasks` listing the
exact module paths. Passing only the package name silently fails to register
sub-modules, causing `KeyError: 'task_name'` at runtime.
"""
from __future__ import annotations

from celery import Celery

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
    },
)


# Sprint 1 ships only stub modules so worker boots; Sprint 2 fills them in.
if __name__ == "__main__":  # pragma: no cover
    celery_app.start()
