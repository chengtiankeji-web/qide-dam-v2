"""Webhook subscription + delivery enqueueing.

Subscriptions are created via API. Each subscription has its own random secret
used for HMAC signing of deliveries.

To trigger an event, call `enqueue_event(...)` from anywhere — it finds matching
active subscriptions and creates `WebhookDelivery` rows in `pending` status,
then dispatches the Celery `webhook.dispatch` task.
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.webhook import WebhookDelivery, WebhookSubscription

logger = get_logger(__name__)


def generate_secret() -> str:
    return secrets.token_urlsafe(48)


async def list_subscriptions(
    db: AsyncSession, *, tenant_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> list[WebhookSubscription]:
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.tenant_id == tenant_id,
        WebhookSubscription.deleted_at.is_(None),
    )
    if project_id is not None:
        stmt = stmt.where(WebhookSubscription.project_id == project_id)
    return list((await db.execute(stmt)).scalars().all())


async def create_subscription(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    target_url: str,
    events: list[str],
    project_id: uuid.UUID | None = None,
) -> WebhookSubscription:
    sub = WebhookSubscription(
        tenant_id=tenant_id,
        project_id=project_id,
        name=name,
        target_url=target_url,
        events=list(events),
        secret=generate_secret(),
    )
    db.add(sub)
    await db.flush()
    return sub


async def enqueue_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    project_id: uuid.UUID | None = None,
) -> int:
    """Materialize WebhookDelivery rows for every matching active subscription.

    Returns the number of deliveries enqueued. The actual HTTP send happens in
    the Celery task `app.workers.tasks_webhook.deliver`.
    """
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.tenant_id == tenant_id,
        WebhookSubscription.is_active.is_(True),
        WebhookSubscription.deleted_at.is_(None),
    )
    subs = (await db.execute(stmt)).scalars().all()

    enqueued = 0
    delivery_ids: list[uuid.UUID] = []
    for sub in subs:
        if sub.events and event_type not in sub.events:
            continue
        if sub.project_id is not None and project_id is not None and sub.project_id != project_id:
            continue
        delivery = WebhookDelivery(
            subscription_id=sub.id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            status="pending",
        )
        db.add(delivery)
        await db.flush()
        delivery_ids.append(delivery.id)
        enqueued += 1

    # Defer Celery imports so this module stays importable in test/migration contexts.
    if delivery_ids:
        try:
            from app.workers.tasks_webhook import deliver
            for did in delivery_ids:
                deliver.delay(str(did))
        except Exception as e:  # noqa: BLE001
            logger.warning("webhook.enqueue.celery_unavailable", error=str(e))

    return enqueued
