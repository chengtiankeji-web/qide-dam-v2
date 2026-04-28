"""End-to-end post-upload pipeline orchestrator.

Chain (decided per asset.kind):
    image    → process_image  → ai_tag + ai_embed → flip status=ready  + webhook asset.processed
    video    → process_video  → ai_tag + ai_embed → flip status=ready
    document → process_document → ai_tag + ai_embed → flip status=ready
    other    → flip status=ready immediately

`ai_tag` / `ai_embed` are stubs in Sprint 1; Sprint 3 implements them.
"""
from __future__ import annotations

import uuid

from celery import chain, group

from app.core.logging import get_logger
from app.models.asset import Asset
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.pipeline")


@celery_app.task(name="pipeline.process", bind=True)
def process_pipeline(self, asset_id: str) -> dict:
    """Decide which processor to invoke based on the asset's kind, and chain."""
    with session_scope() as db:
        asset = db.get(Asset, uuid.UUID(asset_id))
        if not asset:
            return {"asset_id": asset_id, "status": "missing"}
        kind = asset.kind

    if kind == "image":
        processor = "image.process"
    elif kind == "video":
        processor = "video.process"
    elif kind == "document":
        processor = "document.process"
    else:
        # No processing needed — go straight to AI + ready
        chain(
            group(
                celery_app.signature("ai.tag", args=[asset_id]),
                celery_app.signature("ai.embed", args=[asset_id]),
            ),
            celery_app.signature("pipeline.finalize", args=[asset_id]),
        ).apply_async()
        return {"asset_id": asset_id, "kind": kind, "stage": "ai_only"}

    chain(
        celery_app.signature(processor, args=[asset_id]),
        group(
            celery_app.signature("ai.tag", args=[asset_id]),
            celery_app.signature("ai.embed", args=[asset_id]),
        ),
        celery_app.signature("pipeline.finalize", args=[asset_id]),
    ).apply_async()
    return {"asset_id": asset_id, "kind": kind, "stage": "queued"}


@celery_app.task(name="pipeline.finalize", bind=True)
def finalize(self, _ai_results, asset_id: str) -> dict:  # noqa: ARG002
    """Mark asset ready + emit asset.processed webhook."""
    from app.services.webhook_service import enqueue_event  # noqa: F401 (sync version below)
    from sqlalchemy import select

    with session_scope() as db:
        asset = db.get(Asset, uuid.UUID(asset_id))
        if not asset:
            return {"asset_id": asset_id, "status": "missing"}
        asset.status = "ready"
        db.add(asset)

        # Emit webhook synchronously via direct row insert to avoid asyncio
        from app.models.webhook import WebhookDelivery, WebhookSubscription
        subs = (
            db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.tenant_id == asset.tenant_id,
                    WebhookSubscription.is_active.is_(True),
                    WebhookSubscription.deleted_at.is_(None),
                )
            ).scalars().all()
        )
        delivery_ids: list[uuid.UUID] = []
        payload = {
            "asset_id": str(asset.id),
            "name": asset.name,
            "kind": asset.kind,
            "thumbnails": dict(asset.thumbnails or {}),
            "width": asset.width,
            "height": asset.height,
        }
        for sub in subs:
            if sub.events and "asset.processed" not in sub.events:
                continue
            d = WebhookDelivery(
                subscription_id=sub.id,
                tenant_id=asset.tenant_id,
                event_type="asset.processed",
                payload=payload,
                status="pending",
            )
            db.add(d)
            db.flush()
            delivery_ids.append(d.id)

    # Dispatch queued deliveries
    if delivery_ids:
        from app.workers.tasks_webhook import deliver
        for did in delivery_ids:
            deliver.delay(str(did))

    logger.info("pipeline.finalize.done", asset_id=asset_id)
    return {"asset_id": asset_id, "status": "ready"}
