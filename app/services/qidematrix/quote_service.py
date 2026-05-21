"""S6 · 自动报价 + NNN 合同自动套用

主要功能：
1. auto_generate_quote · 基于 lead + 工厂能力 + 客户产品 LLM 生成报价
2. mark_sent / mark_accepted / mark_rejected · 状态机
3. apply_nnn_template · 选目的国 + 套合同模板

LLM use_case = "intake_extract" (qwen-plus · 中等复杂度)
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix.order import QmQuote
from app.services import ai_service
from app.services.qidematrix import pipeline_service

logger = get_logger("qm.quote")


# ─── NNN 合同模板按目的国 ──────────────────────────────────────────

NNN_TEMPLATE_BY_COUNTRY: dict[str, str] = {
    "US": "/templates/nnn-us-2026.docx",
    "CA": "/templates/nnn-us-2026.docx",        # 沿用 US 版
    "AU": "/templates/nnn-au-2026.docx",
    "NZ": "/templates/nnn-au-2026.docx",         # 沿用 AU 版
    "GB": "/templates/nnn-eu-uk-2026.docx",
    "DE": "/templates/nnn-eu-uk-2026.docx",
    "FR": "/templates/nnn-eu-uk-2026.docx",
    "NL": "/templates/nnn-eu-uk-2026.docx",
    # 默认 fallback
    "_default": "/templates/nnn-us-2026.docx",
}


def get_nnn_template_path(buyer_country: str | None) -> str:
    if not buyer_country:
        return NNN_TEMPLATE_BY_COUNTRY["_default"]
    return NNN_TEMPLATE_BY_COUNTRY.get(
        buyer_country.upper(), NNN_TEMPLATE_BY_COUNTRY["_default"]
    )


# ─── AI 报价生成 ──────────────────────────────────────────────────

AUTO_QUOTE_SYSTEM = """你是中国出口工厂的报价专家 · 熟悉外贸 FOB/CIF/EXW · 大湾区出厂价 · 海运空运成本。

任务：基于客户询盘 + 工厂能力 + 类似产品行情 · 生成一个报价 JSON。

输出 JSON 严格 schema：
{
  "product_name": "产品名称（英文 · 海外买家看的）",
  "quantity": 整数,
  "unit_price_usd": 浮点 2 位 · 单价 USD,
  "incoterms": "FOB|CIF|EXW|DDP",
  "lead_time_days": 整数,
  "line_items": [
    {"name": "Base unit price", "amount_usd": 0.0, "qty": N},
    {"name": "Custom packaging", "amount_usd": 0.0, "qty": N},
    {"name": "Mold fee (one-time)", "amount_usd": 0.0, "qty": 1}
  ],
  "valid_days": 整数 · 报价有效期天数,
  "notes": "字符串 · 30-100 字 · 备注 / 优惠 / 起订量说明 (英文)"
}

定价规则：
- 单价不可低于工厂成本 1.3× · 也不可超过同类市场价 2×
- MOQ < 500 → 加 15-25% premium
- 急单（lead_time < 15 days）→ 加 10% premium · 同步在 notes 说明
- 中长期合作（>1000 units）→ tier pricing · 体现在 line_items
- 默认 FOB · 除非买家指定 CIF / DDP

不允许：
- 报价低于成本（亏本不接）
- 编造规格（不知道的字段写 "TBD" + notes 说明）
- 用中文（输出全英文 · 客户是海外买家）
"""


async def auto_generate_quote(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    lead_summary: str,
    buyer_country: str | None = None,
    buyer_email: str | None = None,
    buyer_name: str | None = None,
    factory_capabilities: str = "",
) -> QmQuote:
    """LLM 自动生成报价 · 落 status='draft'

    生产中 lead_summary 应该来自 CRM v7 leads 表 · factory_capabilities
    来自 onboarding · 这里两个都是字符串以便单测。
    """
    user_prompt = f"""## 客户询盘
{lead_summary[:2000]}

## 工厂能力
{factory_capabilities[:1500] or '（未提供 · 用类目通用能力估算）'}

## 买家国别
{buyer_country or '未知 · 按 US 估算运费'}

请基于以上信息严格输出报价 JSON。
"""

    parsed, usage = ai_service.complete_json_for(
        "intake_extract",
        user_prompt,
        system=AUTO_QUOTE_SYSTEM,
        max_tokens=1500,
        temperature=0.2,
    )

    now = datetime.now(UTC)

    if not isinstance(parsed, dict):
        # LLM 失败 → 落空 draft + status=draft + 标记 manual 介入
        logger.warning("qm.quote.llm_failed", lead_id=str(lead_id) if lead_id else None)
        quote = QmQuote(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lead_id=lead_id,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            buyer_country=buyer_country,
            product_name="(needs manual review)",
            quantity=1,
            unit_price_usd=Decimal("0.00"),
            currency="USD",
            incoterms="FOB",
            line_items=[],
            generation_method="manual",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        db.add(quote)
        await db.flush()
        return quote

    try:
        quantity = max(1, int(parsed.get("quantity", 1)))
        unit_price = Decimal(str(parsed.get("unit_price_usd", "0")))
        valid_days = max(1, int(parsed.get("valid_days", 30)))

        quote = QmQuote(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lead_id=lead_id,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            buyer_country=buyer_country,
            product_name=parsed.get("product_name", "Product")[:200],
            quantity=quantity,
            unit_price_usd=unit_price,
            currency="USD",
            incoterms=parsed.get("incoterms", "FOB")[:10],
            lead_time_days=int(parsed.get("lead_time_days", 30)),
            valid_until=(now + timedelta(days=valid_days)).date(),
            line_items=parsed.get("line_items", []),
            total_value_usd=unit_price * quantity,
            model_name="qwen-plus",
            generation_method="ai",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        db.add(quote)
        await db.flush()

        logger.info(
            "qm.quote.auto_generated",
            quote_id=str(quote.id),
            product=quote.product_name,
            qty=quote.quantity,
            unit_usd=str(quote.unit_price_usd),
        )
        return quote
    except (ValueError, TypeError, KeyError) as exc:
        logger.error("qm.quote.parse_failed", error=str(exc)[:300])
        # 落兜底 draft 让运营手填
        quote = QmQuote(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lead_id=lead_id,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            buyer_country=buyer_country,
            product_name="(parse failed · needs review)",
            quantity=1,
            unit_price_usd=Decimal("0.00"),
            currency="USD",
            incoterms="FOB",
            line_items=[],
            generation_method="manual",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        db.add(quote)
        await db.flush()
        return quote


async def mark_sent(db: AsyncSession, *, quote_id: uuid.UUID) -> QmQuote | None:
    """报价发给买家 · 状态 → sent"""
    result = await db.execute(select(QmQuote).where(QmQuote.id == quote_id))
    q = result.scalar_one_or_none()
    if not q:
        return None
    q.status = "sent"
    q.sent_at = datetime.now(UTC)
    q.updated_at = datetime.now(UTC)
    return q


async def mark_accepted(
    db: AsyncSession, *, quote_id: uuid.UUID
) -> QmQuote | None:
    """买家接受报价 · 同步触发 lead.converted 事件 → S7 派单"""
    result = await db.execute(select(QmQuote).where(QmQuote.id == quote_id))
    q = result.scalar_one_or_none()
    if not q:
        return None
    q.status = "accepted"
    q.accepted_at = datetime.now(UTC)
    q.updated_at = datetime.now(UTC)

    await pipeline_service.publish(
        db,
        tenant_id=q.tenant_id,
        workspace_id=q.workspace_id,
        event_type="lead.converted",
        subject_kind="quote",
        subject_id=q.id,
        payload={
            "quote_id": str(q.id),
            "lead_id": str(q.lead_id) if q.lead_id else None,
            "buyer_name": q.buyer_name,
            "buyer_country": q.buyer_country,
            "total_usd": float(q.total_value_usd or 0),
        },
    )
    return q


async def mark_rejected(
    db: AsyncSession, *, quote_id: uuid.UUID, reason: str | None = None
) -> QmQuote | None:
    result = await db.execute(select(QmQuote).where(QmQuote.id == quote_id))
    q = result.scalar_one_or_none()
    if not q:
        return None
    q.status = "rejected"
    q.rejected_reason = reason
    q.updated_at = datetime.now(UTC)
    return q
