"""Webhook subscription CRUD + delivery log read."""
from __future__ import annotations

import uuid
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.schemas.webhook import (
    WebhookDeliveryOut,
    WebhookSubscriptionCreate,
    WebhookSubscriptionCreateOut,
    WebhookSubscriptionOut,
)
from app.services import webhook_service

router = APIRouter()


@router.get("/subscriptions", response_model=list[WebhookSubscriptionOut])
async def list_subs(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookSubscriptionOut]:
    subs = await webhook_service.list_subscriptions(db, tenant_id=p.tenant_id)
    return [WebhookSubscriptionOut.model_validate(s) for s in subs]


@router.post("/subscriptions", response_model=WebhookSubscriptionCreateOut, status_code=201)
async def create_sub(
    payload: WebhookSubscriptionCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> WebhookSubscriptionCreateOut:
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    if payload.project_id and not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    sub = await webhook_service.create_subscription(
        db,
        tenant_id=p.tenant_id,
        name=payload.name,
        target_url=str(payload.target_url),
        events=payload.events,
        project_id=payload.project_id,
    )
    return WebhookSubscriptionCreateOut(
        id=sub.id,
        tenant_id=sub.tenant_id,
        project_id=sub.project_id,
        name=sub.name,
        target_url=sub.target_url,
        events=sub.events,
        is_active=sub.is_active,
        consecutive_failures=sub.consecutive_failures,
        suspended_at=sub.suspended_at,
        last_delivered_at=sub.last_delivered_at,
        created_at=sub.created_at,
        secret=sub.secret,
    )


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_sub(
    subscription_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    sub = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == p.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    sub.is_active = False
    from datetime import datetime
    sub.deleted_at = datetime.now(UTC)
    await db.flush()


@router.get("/deliveries", response_model=list[WebhookDeliveryOut])
async def list_deliveries(
    subscription_id: uuid.UUID | None = Query(None),
    status_: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookDeliveryOut]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.tenant_id == p.tenant_id)
    if subscription_id:
        stmt = stmt.where(WebhookDelivery.subscription_id == subscription_id)
    if status_:
        stmt = stmt.where(WebhookDelivery.status == status_)
    stmt = stmt.order_by(WebhookDelivery.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [WebhookDeliveryOut.model_validate(r) for r in rows]
