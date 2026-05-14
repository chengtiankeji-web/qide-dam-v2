"""deals_service · 商机 CRUD + 状态机 + Pipeline + forecast"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.deal import Deal
from app.services import audit_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)


# 状态机·合法转换
ALLOWED_STAGE_TRANSITIONS = {
    "prospect":     {"qualified", "on_hold", "closed_lost"},
    "qualified":    {"proposal", "on_hold", "closed_lost"},
    "proposal":     {"negotiation", "on_hold", "closed_lost"},
    "negotiation":  {"closed_won", "closed_lost", "on_hold"},
    "on_hold":      {"prospect", "qualified", "proposal", "negotiation", "closed_lost"},
    "closed_won":   set(),    # 终态
    "closed_lost":  {"prospect"},  # 允许 reactivate
}

# 各 stage 默认成交概率（人可改）
DEFAULT_PROBABILITY = {
    "prospect": 10,
    "qualified": 25,
    "proposal": 50,
    "negotiation": 75,
    "closed_won": 100,
    "closed_lost": 0,
    "on_hold": 5,
}


async def create_deal(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    factory_slug: str,
    name: str,
    account_id: uuid.UUID | None = None,
    primary_contact_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    estimated_value_usd: Decimal | None = None,
    probability_pct: int = 10,
    expected_close_date: date | None = None,
    related_sku_slugs: list[str] | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> Deal:
    """创建商机·默认 stage=prospect"""
    deal = Deal(
        tenant_id=tenant_id,
        factory_slug=factory_slug,
        name=name,
        account_id=account_id,
        primary_contact_id=primary_contact_id,
        lead_id=lead_id,
        stage="prospect",
        estimated_value_usd=estimated_value_usd,
        probability_pct=probability_pct,
        weighted_value_usd=(
            float(estimated_value_usd) * probability_pct / 100
            if estimated_value_usd else None
        ),
        expected_close_date=expected_close_date,
        related_sku_slugs=related_sku_slugs,
        owner_user_id=owner_user_id or principal.user_id,
        created_by_user_id=principal.user_id,
    )
    db.add(deal)
    await db.flush()

    await audit_service.log(
        db, principal=principal,
        action=AuditAction.DEAL_CREATED,
        target_kind="deal", target_id=deal.id,
        payload={
            "name": name, "factory_slug": factory_slug,
            "estimated_value_usd": float(estimated_value_usd) if estimated_value_usd else None,
            "from_lead_id": str(lead_id) if lead_id else None,
        },
    )
    return deal


async def transition_stage(
    db: AsyncSession,
    *,
    principal: Principal,
    deal_id: uuid.UUID,
    new_stage: str,
    won_value_usd: Decimal | None = None,
    lost_reason: str | None = None,
    lost_competitor: str | None = None,
) -> Deal:
    """商机 pipeline 状态机·只允许合法转换"""
    deal = await db.get(Deal, deal_id)
    if not deal:
        raise ValueError(f"Deal {deal_id} not found")

    current = deal.stage
    allowed = ALLOWED_STAGE_TRANSITIONS.get(current, set())
    if new_stage not in allowed:
        raise ValueError(
            f"Cannot transition from '{current}' to '{new_stage}'·"
            f"allowed: {sorted(allowed)}"
        )

    now = datetime.now(timezone.utc)
    deal.stage = new_stage
    deal.stage_changed_at = now

    # 默认更新概率（人可后续手改）
    if new_stage in DEFAULT_PROBABILITY:
        deal.probability_pct = DEFAULT_PROBABILITY[new_stage]
        if deal.estimated_value_usd:
            deal.weighted_value_usd = float(deal.estimated_value_usd) * deal.probability_pct / 100

    # 结果记录
    if new_stage == "closed_won":
        deal.won_at = now
        deal.won_value_usd = won_value_usd or deal.estimated_value_usd
        deal.actual_close_date = now.date()
        await audit_service.log(
            db, principal=principal,
            action=AuditAction.DEAL_WON,
            target_kind="deal", target_id=deal_id,
            payload={"won_value_usd": float(deal.won_value_usd) if deal.won_value_usd else None},
        )
    elif new_stage == "closed_lost":
        deal.lost_at = now
        deal.lost_reason = lost_reason
        deal.lost_competitor = lost_competitor
        deal.actual_close_date = now.date()
        await audit_service.log(
            db, principal=principal,
            action=AuditAction.DEAL_LOST,
            target_kind="deal", target_id=deal_id,
            payload={"reason": lost_reason, "competitor": lost_competitor},
        )
    else:
        await audit_service.log(
            db, principal=principal,
            action=AuditAction.DEAL_STAGE_CHANGED,
            target_kind="deal", target_id=deal_id,
            payload={"from": current, "to": new_stage},
        )

    await db.flush()
    return deal


async def list_deals(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    factory_slug: str | None = None,
    stage: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Deal]:
    q = select(Deal).where(Deal.tenant_id == tenant_id)
    if factory_slug:
        q = q.where(Deal.factory_slug == factory_slug)
    if stage:
        q = q.where(Deal.stage == stage)
    if owner_user_id:
        q = q.where(Deal.owner_user_id == owner_user_id)
    if account_id:
        q = q.where(Deal.account_id == account_id)
    q = q.order_by(Deal.updated_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all())


async def get_pipeline_forecast(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factory_slug: str | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> dict:
    """漏斗 forecast·按 stage 聚合金额"""
    open_stages = ("prospect", "qualified", "proposal", "negotiation", "on_hold")
    conditions = [Deal.tenant_id == tenant_id, Deal.stage.in_(open_stages)]
    if factory_slug:
        conditions.append(Deal.factory_slug == factory_slug)
    if owner_user_id:
        conditions.append(Deal.owner_user_id == owner_user_id)

    q = (
        select(
            Deal.stage,
            func.count(Deal.id).label("count"),
            func.coalesce(func.sum(Deal.estimated_value_usd), 0).label("total_estimated"),
            func.coalesce(func.sum(Deal.weighted_value_usd), 0).label("total_weighted"),
        )
        .where(and_(*conditions))
        .group_by(Deal.stage)
    )
    result = (await db.execute(q)).all()
    return {
        "by_stage": [
            {
                "stage": r.stage,
                "count": r.count,
                "total_estimated_usd": float(r.total_estimated),
                "total_weighted_usd": float(r.total_weighted),
            }
            for r in result
        ]
    }
