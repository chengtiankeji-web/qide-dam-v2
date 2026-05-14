"""MCP CRM tools · 让 AI 助手能查 / 创 / 转换 lead + 看 pipeline

工具清单（共 6 个）：
  1. list_leads(factory_slug?, classification?, status?, limit)
  2. get_lead(lead_id)
  3. create_lead(factory_slug, source, inquiry_text, contact_name?, contact_email?, ...)
  4. search_leads(query, limit)
  5. transition_lead(lead_id, new_status, reason?)
  6. get_pipeline_forecast(factory_slug?, days_ahead=30)

⚠️ 安全：
  - 6 个工具走同 _resolve_principal 路径·api_key 限定 tenant + project
  - lead 的 inquiry_text 可能含 PII (邮箱/电话) · 列表只返 summary 不返全文
  - get_lead 返 full · 写 ai.asset_snippet_read 审计（purpose 必传·MCP 调时 LLM 应提供）
  - mutating 工具（create_lead / transition_lead）只接受 platform_admin 或 member 的 key·viewer 拒
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.db.session import AsyncSessionLocal
from app.mcp.server import _resolve_principal, mcp
from app.models.crm.deal import Deal
from app.models.crm.lead import Lead
from app.models.user import User

# ════════════════════════════════════════════════════════════
# 工具 helpers
# ════════════════════════════════════════════════════════════

async def _api_key_to_principal(api_key, db: AsyncSession) -> Principal:
    """把 ApiKey → 内部 Principal · 沿用 deps.Principal 的 dataclass 字段"""
    user_id = api_key.created_by_user_id
    # platform_admin 标志：若 created_by user.is_platform_admin = True · 否则 False
    is_pa = False
    if user_id:
        user = await db.get(User, user_id)
        if user and user.is_platform_admin:
            is_pa = True
    return Principal(
        tenant_id=api_key.tenant_id,
        user_id=user_id,
        is_platform_admin=is_pa,
        project_id=api_key.project_id,
        role="member",
    )


def _lead_summary(lead: Lead) -> dict[str, Any]:
    """lead 列表项·脱敏（不返 inquiry_text 全文）"""
    return {
        "id": str(lead.id),
        "factory_slug": lead.factory_slug,
        "source": lead.source,
        "contact_name": lead.contact_name,
        "contact_company": lead.contact_company,
        "contact_country": lead.contact_country,
        "classification": lead.classification,
        "six_factor_score": lead.six_factor_score,
        "status": lead.status,
        "ai_intent_summary": (lead.ai_intent_summary or "")[:200],
        "tags": lead.tags or [],
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "last_activity_at": lead.last_activity_at.isoformat()
        if lead.last_activity_at else None,
    }


def _lead_full(lead: Lead) -> dict[str, Any]:
    """lead 详情·含 inquiry_text + AI 字段"""
    return {
        **_lead_summary(lead),
        "contact_email": lead.contact_email,
        "contact_phone": lead.contact_phone,
        "contact_role": lead.contact_role,
        "inquiry_text": lead.inquiry_text,
        "inquiry_language": lead.inquiry_language,
        "six_factor_breakdown": lead.six_factor_breakdown,
        "ai_suggested_reply": lead.ai_suggested_reply,
        "ai_translated_zh": lead.ai_translated_zh,
        "ai_urgency_score": lead.ai_urgency_score,
        "ai_quality_score": lead.ai_quality_score,
        "ai_competitors_mentioned": lead.ai_competitors_mentioned,
        "converted_to_deal_id": str(lead.converted_to_deal_id)
        if lead.converted_to_deal_id else None,
        "lost_reason": lead.lost_reason,
        "notes": lead.notes,
    }


# ════════════════════════════════════════════════════════════
# 1. list_leads
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def list_leads(
    factory_slug: str | None = None,
    classification: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List leads (inquiries) in tenant. Returns summary fields, not full inquiry text.

    Filters: factory_slug (e.g. 'yixinheng'), classification ('A'|'B'|'C'|'D'),
    status (new|contacted|qualified|...|converted|lost).
    """
    if limit < 1 or limit > 200:
        raise ValueError("limit must be 1-200")
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        q = select(Lead).where(Lead.tenant_id == api_key.tenant_id)
        if factory_slug:
            q = q.where(Lead.factory_slug == factory_slug)
        if classification:
            if classification not in ("A", "B", "C", "D"):
                raise ValueError("classification must be A|B|C|D")
            q = q.where(Lead.classification == classification)
        if status:
            q = q.where(Lead.status == status)
        q = q.order_by(Lead.created_at.desc()).limit(limit)
        rows = list((await db.execute(q)).scalars().all())
        return {
            "total": len(rows),
            "leads": [_lead_summary(r) for r in rows],
        }


# ════════════════════════════════════════════════════════════
# 2. get_lead
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def get_lead(lead_id: str, purpose: str = "") -> dict[str, Any]:
    """Get a single lead's full details including inquiry text + AI analysis.

    `purpose` is REQUIRED — the AI assistant must explain why it's reading this
    lead (e.g. 'draft a reply to this customer', 'summarize for stakeholder'),
    written into audit trail per v3 P0-3 secret-boundary rules.
    """
    if not purpose or len(purpose) < 5:
        raise ValueError("purpose required (min 5 chars) — explain why reading this lead")
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        lead = await db.get(Lead, uuid.UUID(lead_id))
        if not lead or lead.tenant_id != api_key.tenant_id:
            raise PermissionError("lead not found or not in tenant")
        # 审计读·走现有 AI_ASSET_SNIPPET_READ 通道（同性质·purpose-required）
        from app.services import audit_service
        from app.services.audit_service import AuditAction
        await audit_service.audit(
            db,
            action=AuditAction.AI_ASSET_SNIPPET_READ,
            tenant_id=lead.tenant_id,
            actor_user_id=api_key.created_by_user_id,
            actor_kind="api_key",
            target_kind="lead",
            target_id=lead.id,
            purpose=purpose,
            metadata={"factory_slug": lead.factory_slug, "via": "mcp.get_lead"},
        )
        return _lead_full(lead)


# ════════════════════════════════════════════════════════════
# 3. create_lead
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def create_lead(
    factory_slug: str,
    source: str,
    inquiry_text: str,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    contact_country: str | None = None,
    contact_company: str | None = None,
) -> dict[str, Any]:
    """Create a lead (inquiry) and run the 6-factor classification algorithm.

    `source`: 'website-form' | 'email-inbox' | 'whatsapp' | 'linkedin' | 'manual'
    Returns: lead summary including classification (A/B/C/D) and score.
    """
    if not factory_slug or not inquiry_text:
        raise ValueError("factory_slug and inquiry_text required")
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        principal = await _api_key_to_principal(api_key, db)

        from app.services.crm import leads_service
        lead = await leads_service.create_lead(
            db,
            principal=principal,
            tenant_id=api_key.tenant_id,
            factory_slug=factory_slug,
            source=source,
            inquiry_text=inquiry_text,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            contact_country=contact_country,
            contact_company=contact_company,
        )
        await db.commit()
        return _lead_summary(lead)


# ════════════════════════════════════════════════════════════
# 4. search_leads
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def search_leads(
    query: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Full-text search on lead's inquiry_text + contact_name + contact_company.

    Uses PostgreSQL ILIKE for cross-language matching (Chinese, English, etc.).
    """
    if not query or len(query) < 2:
        raise ValueError("query min 2 chars")
    limit = min(limit, 100)
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        pattern = f"%{query}%"
        q = (
            select(Lead)
            .where(Lead.tenant_id == api_key.tenant_id)
            .where(
                Lead.inquiry_text.ilike(pattern)
                | Lead.contact_name.ilike(pattern)
                | Lead.contact_company.ilike(pattern)
                | Lead.contact_email.ilike(pattern)
            )
            .order_by(Lead.created_at.desc())
            .limit(limit)
        )
        rows = list((await db.execute(q)).scalars().all())
        return {
            "query": query,
            "total": len(rows),
            "leads": [_lead_summary(r) for r in rows],
        }


# ════════════════════════════════════════════════════════════
# 5. transition_lead
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def transition_lead(
    lead_id: str,
    new_status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move a lead through its workflow status.

    Allowed targets:
      new → contacted | qualified | unqualified | spam
      contacted → qualified | unqualified | nurturing
      qualified → converted (use convert_lead instead for converted) | lost
      ...
    """
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        lead = await db.get(Lead, uuid.UUID(lead_id))
        if not lead or lead.tenant_id != api_key.tenant_id:
            raise PermissionError("lead not found")

        principal = await _api_key_to_principal(api_key, db)
        from app.services.crm import leads_service
        try:
            # v3 P1.3 phase 5+ (2026-05-14) SonarQube 真 bug 修复：
            # transition_status signature 是 (db, *, principal, lead_id, new_status, note, lost_reason, ...)
            # 之前传 lead=lead + reason=reason 都不匹配 · 任何调用 100% TypeError
            lead = await leads_service.transition_status(
                db,
                principal=principal,
                lead_id=lead.id,
                new_status=new_status,
                note=reason,
            )
        except ValueError as e:
            raise ValueError(str(e)) from e
        await db.commit()
        return {
            "lead_id": str(lead.id),
            "new_status": lead.status,
        }


# ════════════════════════════════════════════════════════════
# 6. get_pipeline_forecast
# ════════════════════════════════════════════════════════════

@mcp.tool()
async def get_pipeline_forecast(
    factory_slug: str | None = None,
    days_ahead: int = 30,
) -> dict[str, Any]:
    """Forecast pipeline: open deals × stage probability × estimated value.

    Returns aggregated forecast plus per-stage breakdown for the next `days_ahead` days.
    Useful for "what's our Q2 outlook?" type questions.
    """
    if days_ahead < 1 or days_ahead > 365:
        days_ahead = 30
    cutoff = datetime.now(timezone.utc) + timedelta(days=days_ahead)

    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        q = select(Deal).where(
            Deal.tenant_id == api_key.tenant_id,
            Deal.status.in_(("open", "active")),
            Deal.expected_close_date <= cutoff,
        )
        if factory_slug:
            q = q.where(Deal.factory_slug == factory_slug)
        deals = list((await db.execute(q)).scalars().all())

        total_weighted_usd = 0.0
        total_value_usd = 0.0
        per_stage: dict[str, dict[str, float]] = {}

        for d in deals:
            v = float(getattr(d, "value_usd", 0) or 0)
            prob = float(getattr(d, "probability", 0) or 0) / 100
            stage = getattr(d, "stage", "unknown") or "unknown"
            weighted = v * prob

            total_value_usd += v
            total_weighted_usd += weighted

            row = per_stage.setdefault(
                stage, {"deals": 0, "value_usd": 0.0, "weighted_usd": 0.0}
            )
            row["deals"] += 1
            row["value_usd"] += v
            row["weighted_usd"] += weighted

        return {
            "factory_slug": factory_slug or "(all factories)",
            "days_ahead": days_ahead,
            "open_deals": len(deals),
            "total_value_usd": round(total_value_usd, 2),
            "weighted_forecast_usd": round(total_weighted_usd, 2),
            "by_stage": {
                k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                    for kk, vv in v.items()}
                for k, v in per_stage.items()
            },
        }


__all__ = [
    "create_lead",
    "get_lead",
    "get_pipeline_forecast",
    "list_leads",
    "search_leads",
    "transition_lead",
]
