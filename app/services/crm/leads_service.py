"""leads_service · 询盘 CRUD + 状态机 + 6 要素分类

业务逻辑层·封装：
1. create_from_inbox()    从 social_inbox / email / form 创建 lead
2. classify()             跑 6 要素算法 + 写回
3. assign()               分派给 BD
4. transition_status()    状态机（new → contacted → qualified → ...）
5. convert_to_deal()      lead → deal 流转
6. mark_lost / spam()     失败 / 屏蔽

每个状态变更都写 audit_events（复用现有 audit_service）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.lead import Lead
from app.models.crm.deal import Deal
from app.services import audit_service
from app.services.audit_service import AuditAction
from app.services.crm.classification import (
    ClassificationInput,
    classify_lead as classify_algorithm,
)

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# 1. 创建 lead
# ════════════════════════════════════════════════════════════

async def create_lead(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    factory_slug: str,
    source: str,
    inquiry_text: str,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_company: Optional[str] = None,
    contact_country: Optional[str] = None,
    contact_role: Optional[str] = None,
    inquiry_attachments: Optional[list[dict]] = None,
    inquiry_language: Optional[str] = None,
    # 来源关联（可选）
    source_inbox_id: Optional[uuid.UUID] = None,
    source_share_link_id: Optional[uuid.UUID] = None,
    source_campaign: Optional[str] = None,
    source_url: Optional[str] = None,
    # 关联（可选）
    contact_id: Optional[uuid.UUID] = None,
    account_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
) -> Lead:
    """创建新询盘 + 自动跑 6 要素分级 + 写 audit"""
    # 1. 跑分类算法
    classification_input = ClassificationInput(
        inquiry_text=inquiry_text,
        contact_email=contact_email,
        contact_company=contact_company,
        contact_role=contact_role,
        contact_phone=contact_phone,
        source=source,
        has_attachments=bool(inquiry_attachments),
    )
    cls_result = classify_algorithm(classification_input)

    # 2. 创建 Lead
    lead = Lead(
        tenant_id=tenant_id,
        factory_slug=factory_slug,
        project_id=project_id,
        source=source,
        source_inbox_id=source_inbox_id,
        source_share_link_id=source_share_link_id,
        source_campaign=source_campaign,
        source_url=source_url,
        contact_id=contact_id,
        account_id=account_id,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        contact_company=contact_company,
        contact_country=contact_country,
        contact_role=contact_role,
        inquiry_text=inquiry_text,
        inquiry_attachments=inquiry_attachments,
        inquiry_language=inquiry_language,
        status="new",
        **cls_result.to_db_dict(),
    )
    db.add(lead)
    await db.flush()  # 拿 lead.id

    # 3. 审计
    await audit_service.log(
        db,
        principal=principal,
        action=AuditAction.LEAD_CREATED,
        target_kind="lead",
        target_id=lead.id,
        project_id=project_id,
        payload={
            "factory_slug": factory_slug,
            "source": source,
            "classification": cls_result.classification,
            "score": cls_result.score,
        },
    )

    logger.info(
        "lead.created",
        lead_id=str(lead.id),
        factory=factory_slug,
        source=source,
        classification=cls_result.classification,
        score=cls_result.score,
    )

    # 异步 dispatch AI enrichment（intent / 翻译 / suggested_reply）
    # · 失败不 raise · 不阻塞 lead 创建
    try:
        from app.services.crm import ai_enrichment_service
        await ai_enrichment_service.enrich_lead_async_dispatch(lead.id)
    except Exception:  # noqa: BLE001
        pass  # 永远不让 AI dispatch 影响 lead 创建

    return lead


# ════════════════════════════════════════════════════════════
# 2. 重新分类（手工触发 · BD 改 lead 内容后重跑）
# ════════════════════════════════════════════════════════════

async def reclassify(
    db: AsyncSession,
    *,
    principal: Principal,
    lead_id: uuid.UUID,
) -> Lead:
    """重新跑 6 要素算法（如 inquiry_text 被更新）"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    cls_result = classify_algorithm(ClassificationInput(
        inquiry_text=lead.inquiry_text,
        contact_email=lead.contact_email,
        contact_company=lead.contact_company,
        contact_role=lead.contact_role,
        contact_phone=lead.contact_phone,
        source=lead.source,
        has_attachments=bool(lead.inquiry_attachments),
    ))

    old_classification = lead.classification
    for k, v in cls_result.to_db_dict().items():
        setattr(lead, k, v)
    lead.classification_overridden = False  # 重置

    await db.flush()

    if old_classification != cls_result.classification:
        await audit_service.log(
            db,
            principal=principal,
            action=AuditAction.LEAD_RECLASSIFIED,
            target_kind="lead",
            target_id=lead.id,
            project_id=lead.project_id,
            payload={
                "from": old_classification,
                "to": cls_result.classification,
                "score": cls_result.score,
            },
        )
    return lead


# ════════════════════════════════════════════════════════════
# 3. 手工 override 分类（BD 强改 · 算法不准时）
# ════════════════════════════════════════════════════════════

async def override_classification(
    db: AsyncSession,
    *,
    principal: Principal,
    lead_id: uuid.UUID,
    new_classification: str,  # A/B/C/D
    reason: str,
) -> Lead:
    """BD 手工改分类·记录 override + 写 audit"""
    if new_classification not in ("A", "B", "C", "D"):
        raise ValueError(f"Invalid classification: {new_classification}")

    lead = await db.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    old = lead.classification
    lead.classification = new_classification
    lead.classification_overridden = True
    lead.classification_overridden_by = principal.user_id

    await audit_service.log(
        db,
        principal=principal,
        action=AuditAction.LEAD_CLASSIFICATION_OVERRIDDEN,
        target_kind="lead",
        target_id=lead.id,
        project_id=lead.project_id,
        payload={"from": old, "to": new_classification, "reason": reason},
    )
    await db.flush()
    return lead


# ════════════════════════════════════════════════════════════
# 4. 分派给 BD
# ════════════════════════════════════════════════════════════

async def assign(
    db: AsyncSession,
    *,
    principal: Principal,
    lead_id: uuid.UUID,
    assignee_user_id: uuid.UUID,
) -> Lead:
    """分配 lead 给指定 BD"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    old_assignee = lead.assigned_user_id
    lead.assigned_user_id = assignee_user_id
    lead.assigned_at = datetime.now(timezone.utc)

    await audit_service.log(
        db,
        principal=principal,
        action=AuditAction.LEAD_ASSIGNED,
        target_kind="lead",
        target_id=lead.id,
        project_id=lead.project_id,
        payload={
            "from": str(old_assignee) if old_assignee else None,
            "to": str(assignee_user_id),
        },
    )
    await db.flush()
    return lead


# ════════════════════════════════════════════════════════════
# 5. 状态机
# ════════════════════════════════════════════════════════════

# 合法状态转换
ALLOWED_TRANSITIONS = {
    "new":         {"contacted", "qualified", "unqualified", "spam", "archived"},
    "contacted":   {"qualified", "unqualified", "nurturing", "lost", "archived"},
    "qualified":   {"converted", "nurturing", "lost", "archived"},
    "unqualified": {"qualified", "nurturing", "archived"},
    "nurturing":   {"contacted", "qualified", "lost", "archived"},
    "converted":   {"archived"},  # 已转 deal · 不能再退
    "lost":        {"archived"},
    "spam":        {"archived"},
    "archived":    set(),  # 终态
}


async def transition_status(
    db: AsyncSession,
    *,
    principal: Principal,
    lead_id: uuid.UUID,
    new_status: str,
    note: Optional[str] = None,
    lost_reason: Optional[str] = None,
    lost_competitor: Optional[str] = None,
) -> Lead:
    """状态机·校验合法转换 + 写 audit"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    current = lead.status
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition from '{current}' to '{new_status}'·"
            f"allowed: {sorted(allowed)}"
        )

    now = datetime.now(timezone.utc)
    lead.status = new_status

    # 状态特定字段
    if new_status == "contacted" and not lead.first_contact_at:
        lead.first_contact_at = now
        lead.first_contact_by = principal.user_id
    elif new_status == "qualified" and not lead.qualified_at:
        lead.qualified_at = now
    elif new_status == "lost":
        lead.lost_at = now
        if lost_reason:
            lead.lost_reason = lost_reason
        if lost_competitor:
            lead.lost_competitor = lost_competitor

    lead.last_activity_at = now

    await audit_service.log(
        db,
        principal=principal,
        action=AuditAction.LEAD_STATUS_CHANGED,
        target_kind="lead",
        target_id=lead.id,
        project_id=lead.project_id,
        payload={
            "from": current,
            "to": new_status,
            "note": note,
            "lost_reason": lost_reason,
            "lost_competitor": lost_competitor,
        },
    )
    await db.flush()
    return lead


# ════════════════════════════════════════════════════════════
# 6. lead → deal 流转
# ════════════════════════════════════════════════════════════

async def convert_to_deal(
    db: AsyncSession,
    *,
    principal: Principal,
    lead_id: uuid.UUID,
    deal_name: str,
    estimated_value_usd: Optional[float] = None,
    probability_pct: int = 50,
    expected_close_date: Optional[str] = None,
    related_sku_slugs: Optional[list[str]] = None,
) -> tuple[Lead, Deal]:
    """qualified lead → deal·创建 deal + 改 lead.status='converted'"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    if lead.status not in ("qualified", "contacted"):
        raise ValueError(
            f"Lead must be 'qualified' or 'contacted' to convert (current: {lead.status})"
        )

    # 创建 deal
    deal = Deal(
        tenant_id=lead.tenant_id,
        factory_slug=lead.factory_slug,
        name=deal_name,
        account_id=lead.account_id,
        primary_contact_id=lead.contact_id,
        lead_id=lead.id,
        stage="prospect",
        estimated_value_usd=estimated_value_usd,
        probability_pct=probability_pct,
        weighted_value_usd=(
            float(estimated_value_usd) * probability_pct / 100
            if estimated_value_usd else None
        ),
        related_sku_slugs=related_sku_slugs,
        owner_user_id=lead.assigned_user_id or principal.user_id,
        created_by_user_id=principal.user_id,
    )
    db.add(deal)
    await db.flush()

    # 更新 lead
    now = datetime.now(timezone.utc)
    lead.status = "converted"
    lead.converted_to_deal_id = deal.id
    lead.converted_at = now
    lead.last_activity_at = now

    # 审计
    await audit_service.log(
        db,
        principal=principal,
        action=AuditAction.LEAD_CONVERTED_TO_DEAL,
        target_kind="lead",
        target_id=lead.id,
        project_id=lead.project_id,
        payload={
            "deal_id": str(deal.id),
            "deal_name": deal_name,
            "estimated_value_usd": float(estimated_value_usd) if estimated_value_usd else None,
        },
    )

    logger.info(
        "lead.converted",
        lead_id=str(lead.id),
        deal_id=str(deal.id),
        value=estimated_value_usd,
    )
    return lead, deal


# ════════════════════════════════════════════════════════════
# 7. 查询
# ════════════════════════════════════════════════════════════

async def list_leads(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    factory_slug: Optional[str] = None,
    classification: Optional[str] = None,
    status: Optional[str] = None,
    assigned_user_id: Optional[uuid.UUID] = None,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_at_desc",
) -> tuple[list[Lead], int]:
    """列询盘·支持多筛选·返 (rows, total)"""
    q = select(Lead).where(Lead.tenant_id == tenant_id)

    if factory_slug:
        q = q.where(Lead.factory_slug == factory_slug)
    if classification:
        q = q.where(Lead.classification == classification)
    if status:
        q = q.where(Lead.status == status)
    if assigned_user_id:
        q = q.where(Lead.assigned_user_id == assigned_user_id)
    if source:
        q = q.where(Lead.source == source)

    # 排序
    if order_by == "created_at_desc":
        q = q.order_by(Lead.created_at.desc())
    elif order_by == "score_desc":
        q = q.order_by(Lead.six_factor_score.desc(), Lead.created_at.desc())
    elif order_by == "last_activity_desc":
        q = q.order_by(Lead.last_activity_at.desc().nullslast(), Lead.created_at.desc())

    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    rows = list(result.scalars().all())

    # TODO: 单独 count query
    return rows, len(rows)
