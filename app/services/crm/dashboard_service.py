"""dashboard_service · CRM 销售仪表盘聚合查询

业务视角：
  - 本月新询盘数 + 同比涨跌
  - A 类待跟进 + 红色提示
  - 活跃 deals 总值 + forecast
  - 本月成交 + 平均周期
  - 漏斗图（lead → qualified → deal → won）
  - 渠道分布 + Top 工厂

复用 v3 现有 usage_meters 思路·但 CRM 自带聚合
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm.deal import Deal
from app.models.crm.lead import Lead


async def get_dashboard_summary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factory_slug: Optional[str] = None,
) -> dict:
    """主仪表盘聚合·一次查询返所有指标"""
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    last_month_start = (
        datetime(now.year - 1, 12, 1, tzinfo=timezone.utc) if now.month == 1
        else datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
    )

    base_lead_filter = [Lead.tenant_id == tenant_id]
    base_deal_filter = [Deal.tenant_id == tenant_id]
    if factory_slug:
        base_lead_filter.append(Lead.factory_slug == factory_slug)
        base_deal_filter.append(Deal.factory_slug == factory_slug)

    # ── 1. 本月新询盘 ─────────────────────────────────
    q = select(func.count(Lead.id)).where(
        and_(*base_lead_filter, Lead.created_at >= month_start)
    )
    new_leads_this_month = (await db.execute(q)).scalar() or 0

    # ── 2. 上月对比 ──────────────────────────────────
    q = select(func.count(Lead.id)).where(
        and_(
            *base_lead_filter,
            Lead.created_at >= last_month_start,
            Lead.created_at < month_start,
        )
    )
    new_leads_last_month = (await db.execute(q)).scalar() or 0

    growth_pct = None
    if new_leads_last_month > 0:
        growth_pct = round(
            (new_leads_this_month - new_leads_last_month) / new_leads_last_month * 100, 1
        )

    # ── 3. A 类待跟进 ─────────────────────────────────
    q = select(func.count(Lead.id)).where(
        and_(
            *base_lead_filter,
            Lead.classification == "A",
            Lead.status.in_(["new", "contacted"]),
        )
    )
    a_class_pending = (await db.execute(q)).scalar() or 0

    # ── 4. 活跃 deals（open stages）─────────────────
    open_stages = ("prospect", "qualified", "proposal", "negotiation")
    q = select(
        func.count(Deal.id).label("count"),
        func.coalesce(func.sum(Deal.estimated_value_usd), 0).label("total_est"),
        func.coalesce(func.sum(Deal.weighted_value_usd), 0).label("total_weighted"),
    ).where(and_(*base_deal_filter, Deal.stage.in_(open_stages)))
    result = (await db.execute(q)).one()
    active_deals_count = result.count
    active_deals_total_usd = float(result.total_est)
    active_deals_weighted_usd = float(result.total_weighted)

    # ── 5. 本月成交 ──────────────────────────────────
    q = select(
        func.count(Deal.id).label("count"),
        func.coalesce(func.sum(Deal.won_value_usd), 0).label("total"),
    ).where(
        and_(
            *base_deal_filter,
            Deal.stage == "closed_won",
            Deal.won_at >= month_start,
        )
    )
    result = (await db.execute(q)).one()
    won_count_this_month = result.count
    won_value_this_month = float(result.total)

    # ── 6. 平均成交周期（最近 90 天 closed_won）──────
    ninety_days_ago = now - timedelta(days=90)
    q = select(
        func.avg(
            func.extract("epoch", Deal.won_at - Deal.created_at) / 86400
        ).label("avg_days")
    ).where(
        and_(
            *base_deal_filter,
            Deal.stage == "closed_won",
            Deal.won_at >= ninety_days_ago,
        )
    )
    avg_cycle_days = (await db.execute(q)).scalar()
    avg_cycle_days = round(float(avg_cycle_days), 1) if avg_cycle_days else None

    # ── 7. 漏斗（最近 30 天）──────────────────────────
    thirty_days_ago = now - timedelta(days=30)

    funnel = {}
    # 询盘总数
    q = select(func.count(Lead.id)).where(
        and_(*base_lead_filter, Lead.created_at >= thirty_days_ago)
    )
    funnel["leads_total"] = (await db.execute(q)).scalar() or 0

    # qualified
    q = select(func.count(Lead.id)).where(
        and_(
            *base_lead_filter,
            Lead.created_at >= thirty_days_ago,
            Lead.status.in_(["qualified", "converted"]),
        )
    )
    funnel["leads_qualified"] = (await db.execute(q)).scalar() or 0

    # 转 deal
    q = select(func.count(Deal.id)).where(
        and_(*base_deal_filter, Deal.created_at >= thirty_days_ago)
    )
    funnel["deals_total"] = (await db.execute(q)).scalar() or 0

    # closed_won
    q = select(func.count(Deal.id)).where(
        and_(
            *base_deal_filter,
            Deal.created_at >= thirty_days_ago,
            Deal.stage == "closed_won",
        )
    )
    funnel["deals_won"] = (await db.execute(q)).scalar() or 0

    # ── 8. 渠道分布（最近 30 天）──────────────────────
    q = (
        select(Lead.source, func.count(Lead.id).label("count"))
        .where(and_(*base_lead_filter, Lead.created_at >= thirty_days_ago))
        .group_by(Lead.source)
        .order_by(func.count(Lead.id).desc())
    )
    source_distribution = [
        {"source": row.source, "count": row.count}
        for row in (await db.execute(q)).all()
    ]

    # ── 9. Top 5 工厂（按 lead 数）──────────────────
    q = (
        select(Lead.factory_slug, func.count(Lead.id).label("count"))
        .where(and_(Lead.tenant_id == tenant_id, Lead.created_at >= thirty_days_ago))
        .group_by(Lead.factory_slug)
        .order_by(func.count(Lead.id).desc())
        .limit(5)
    )
    top_factories = [
        {"factory_slug": row.factory_slug, "leads_count": row.count}
        for row in (await db.execute(q)).all()
    ]

    # ── 10. 分类分布 ─────────────────────────────────
    q = (
        select(Lead.classification, func.count(Lead.id).label("count"))
        .where(and_(*base_lead_filter, Lead.created_at >= thirty_days_ago))
        .group_by(Lead.classification)
    )
    classification_dist = {
        row.classification or "unclassified": row.count
        for row in (await db.execute(q)).all()
    }

    return {
        "summary": {
            "new_leads_this_month": new_leads_this_month,
            "new_leads_last_month": new_leads_last_month,
            "growth_pct": growth_pct,
            "a_class_pending": a_class_pending,
            "active_deals_count": active_deals_count,
            "active_deals_total_usd": active_deals_total_usd,
            "active_deals_weighted_usd": active_deals_weighted_usd,
            "won_count_this_month": won_count_this_month,
            "won_value_this_month_usd": won_value_this_month,
            "avg_cycle_days": avg_cycle_days,
        },
        "funnel_30d": funnel,
        "source_distribution_30d": source_distribution,
        "top_factories_30d": top_factories,
        "classification_distribution_30d": classification_dist,
        "generated_at": now.isoformat(),
        "factory_filter": factory_slug,
    }
