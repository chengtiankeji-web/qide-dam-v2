"""QideMatrix v1 · S6/S7 报价 + 派单 REST API · /v1/qm/quotes + /v1/qm/orders"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.qidematrix.order import QmOrder, QmQuote
from app.schemas.qidematrix.pipeline import (
    OrderCreateIn,
    OrderOut,
    OrderUpdateIn,
    QuoteCreateIn,
    QuoteOut,
)
from app.services.qidematrix import pipeline_service

router = APIRouter(prefix="/qm", tags=["qidematrix-orders"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _gen_order_number() -> str:
    """QM-2026-05-21-A3F92K · 易识别 · 可印合同"""
    ts = datetime.now(UTC).strftime("%Y%m%d")
    rnd = secrets.token_hex(3).upper()
    return f"QM-{ts}-{rnd}"


# ═════════════════════════════════════════════════════════════════════
# Quotes
# ═════════════════════════════════════════════════════════════════════

@router.post("/quotes", response_model=QuoteOut, status_code=201)
async def create_quote(
    payload: QuoteCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.tenant_id:
        raise _err(401, "tenant required")

    total = payload.unit_price_usd * payload.quantity
    now = datetime.now(UTC)
    quote = QmQuote(
        id=uuid.uuid4(),
        tenant_id=p.tenant_id,
        workspace_id=payload.workspace_id,
        lead_id=payload.lead_id,
        buyer_email=payload.buyer_email,
        buyer_name=payload.buyer_name,
        buyer_country=payload.buyer_country,
        buyer_company=payload.buyer_company,
        product_name=payload.product_name,
        product_sku=payload.product_sku,
        quantity=payload.quantity,
        unit_price_usd=payload.unit_price_usd,
        currency="USD",
        incoterms=payload.incoterms,
        lead_time_days=payload.lead_time_days,
        valid_until=payload.valid_until,
        line_items=payload.line_items,
        total_value_usd=total,
        generation_method=payload.generation_method,
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(quote)
    await db.flush()
    await db.commit()
    return quote


@router.get("/quotes", response_model=list[QuoteOut])
async def list_quotes(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    workspace_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(QmQuote).order_by(QmQuote.created_at.desc())
    if workspace_id:
        stmt = stmt.where(QmQuote.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(QmQuote.status == status)
    if not p.is_platform_admin:
        stmt = stmt.where(QmQuote.tenant_id == p.tenant_id)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/quotes/{quote_id}", response_model=QuoteOut)
async def get_quote(
    quote_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QmQuote).where(QmQuote.id == quote_id))
    q = result.scalar_one_or_none()
    if not q:
        raise _err(404, "quote not found")
    if not p.is_platform_admin and q.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    return q


# ═════════════════════════════════════════════════════════════════════
# Orders
# ═════════════════════════════════════════════════════════════════════

@router.post("/orders", response_model=OrderOut, status_code=201)
async def create_order(
    payload: OrderCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """S7 派单"""
    if not p.tenant_id:
        raise _err(401, "tenant required")

    now = datetime.now(UTC)
    order = QmOrder(
        id=uuid.uuid4(),
        tenant_id=p.tenant_id,
        workspace_id=payload.workspace_id,
        quote_id=payload.quote_id,
        lead_id=payload.lead_id,
        order_number=_gen_order_number(),
        buyer_email=payload.buyer_email,
        buyer_name=payload.buyer_name,
        buyer_country=payload.buyer_country,
        shipping_address=payload.shipping_address,
        assigned_factory_kind=payload.assigned_factory_kind,
        assigned_factory_id=payload.assigned_factory_id,
        assigned_factory_name=payload.assigned_factory_name,
        product_line_items=payload.product_line_items,
        total_value_usd=payload.total_value_usd,
        incoterms=payload.incoterms,
        status="pending",
        current_stage="placed",
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    await db.flush()

    await pipeline_service.publish(
        db,
        tenant_id=p.tenant_id,
        workspace_id=payload.workspace_id,
        event_type="order.placed",
        actor_kind="user",
        actor_id=p.user_id,
        subject_kind="order",
        subject_id=order.id,
        payload={
            "order_id": str(order.id),
            "order_number": order.order_number,
            "factory_kind": order.assigned_factory_kind,
            "factory_id": order.assigned_factory_id,
            "total_usd": float(order.total_value_usd),
        },
    )
    await db.commit()
    return order


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    workspace_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(QmOrder).order_by(QmOrder.created_at.desc())
    if workspace_id:
        stmt = stmt.where(QmOrder.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(QmOrder.status == status)
    if not p.is_platform_admin:
        stmt = stmt.where(QmOrder.tenant_id == p.tenant_id)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QmOrder).where(QmOrder.id == order_id))
    o = result.scalar_one_or_none()
    if not o:
        raise _err(404, "order not found")
    if not p.is_platform_admin and o.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")
    return o


@router.patch("/orders/{order_id}", response_model=OrderOut)
async def update_order(
    order_id: uuid.UUID,
    payload: OrderUpdateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QmOrder).where(QmOrder.id == order_id))
    o = result.scalar_one_or_none()
    if not o:
        raise _err(404, "order not found")
    if not p.is_platform_admin and o.tenant_id != p.tenant_id:
        raise _err(403, "forbidden")

    for field in (
        "status", "current_stage", "chosen_logistics", "tracking_number",
        "shipped_at", "delivered_at", "payment_method",
        "payment_received_at", "payment_amount_usd", "hs_codes",
        "customs_status",
    ):
        v = getattr(payload, field, None)
        if v is not None:
            setattr(o, field, v)

    o.updated_at = datetime.now(UTC)

    # 自动发 order.delivered 事件
    if payload.status == "delivered" and not o.delivered_at:
        o.delivered_at = datetime.now(UTC)
        await pipeline_service.publish(
            db,
            tenant_id=o.tenant_id,
            workspace_id=o.workspace_id,
            event_type="order.delivered",
            actor_kind="user",
            actor_id=p.user_id,
            subject_kind="order",
            subject_id=o.id,
            payload={
                "order_id": str(o.id),
                "order_number": o.order_number,
            },
        )

    await db.commit()
    return o
