"""/v1/crm/leads · 询盘 REST API

端点：
  POST   /v1/crm/leads                     创建（手工 / 系统 webhook）
  GET    /v1/crm/leads                     列表（支持 classification/status/factory 等过滤）
  GET    /v1/crm/leads/{id}                详情
  PATCH  /v1/crm/leads/{id}                更新（不含状态机·走子端点）
  POST   /v1/crm/leads/{id}/reclassify     重跑 6 要素算法
  POST   /v1/crm/leads/{id}/classify/override  手工改分类
  POST   /v1/crm/leads/{id}/assign         分派 BD
  POST   /v1/crm/leads/{id}/transition     状态机转换
  POST   /v1/crm/leads/{id}/convert        lead → deal
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require_authenticated
from app.db.session import get_db
from app.schemas.crm.lead import (
    LeadCreate,
    LeadOut,
    LeadListOut,
    LeadAssignIn,
    LeadTransitionIn,
    LeadOverrideClassificationIn,
    LeadConvertIn,
    LeadDealOut,
)
from app.services.crm import leads_service

router = APIRouter()


# ════════════════════════════════════════════════════════════
# POST · 创建
# ════════════════════════════════════════════════════════════

@router.post("/", response_model=LeadOut, status_code=http_status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    """创建新询盘·自动跑 6 要素分级"""
    # 校验 tenant 范围（principal 必须在 payload.tenant_id 内或是 platform_admin）
    tenant_id = payload.tenant_id or principal.tenant_id
    if not principal.is_platform_admin and tenant_id != principal.tenant_id:
        raise HTTPException(403, "Cross-tenant lead creation requires platform_admin")

    lead = await leads_service.create_lead(
        db,
        principal=principal,
        tenant_id=tenant_id,
        factory_slug=payload.factory_slug,
        source=payload.source,
        inquiry_text=payload.inquiry_text,
        contact_name=payload.contact_name,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        contact_company=payload.contact_company,
        contact_country=payload.contact_country,
        contact_role=payload.contact_role,
        inquiry_attachments=payload.inquiry_attachments,
        inquiry_language=payload.inquiry_language,
        source_inbox_id=payload.source_inbox_id,
        source_share_link_id=payload.source_share_link_id,
        source_campaign=payload.source_campaign,
        source_url=payload.source_url,
        contact_id=payload.contact_id,
        account_id=payload.account_id,
        project_id=payload.project_id,
    )
    await db.commit()
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# GET · 列表
# ════════════════════════════════════════════════════════════

@router.get("/", response_model=LeadListOut)
async def list_leads(
    factory_slug: Optional[str] = Query(None),
    classification: Optional[str] = Query(None, pattern="^[ABCD]$"),
    status: Optional[str] = Query(None),
    assigned_user_id: Optional[uuid.UUID] = Query(None),
    source: Optional[str] = Query(None),
    tenant_id: Optional[uuid.UUID] = Query(None, description="platform_admin 跨 tenant"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at_desc",
                          pattern="^(created_at_desc|score_desc|last_activity_desc)$"),
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadListOut:
    """列询盘"""
    effective_tenant = tenant_id if (tenant_id and principal.is_platform_admin) else principal.tenant_id

    rows, total = await leads_service.list_leads(
        db,
        principal=principal,
        tenant_id=effective_tenant,
        factory_slug=factory_slug,
        classification=classification,
        status=status,
        assigned_user_id=assigned_user_id,
        source=source,
        limit=limit,
        offset=offset,
        order_by=order_by,
    )
    return LeadListOut(
        items=[LeadOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ════════════════════════════════════════════════════════════
# GET · 详情
# ════════════════════════════════════════════════════════════

@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    from app.models.crm.lead import Lead
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not principal.is_platform_admin and lead.tenant_id != principal.tenant_id:
        raise HTTPException(403, "Forbidden")
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# POST · 重分类
# ════════════════════════════════════════════════════════════

@router.post("/{lead_id}/reclassify", response_model=LeadOut)
async def reclassify_lead(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    lead = await leads_service.reclassify(db, principal=principal, lead_id=lead_id)
    await db.commit()
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# POST · 手工 override 分类
# ════════════════════════════════════════════════════════════

@router.post("/{lead_id}/classify/override", response_model=LeadOut)
async def override_classification(
    lead_id: uuid.UUID,
    payload: LeadOverrideClassificationIn,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    lead = await leads_service.override_classification(
        db,
        principal=principal,
        lead_id=lead_id,
        new_classification=payload.classification,
        reason=payload.reason,
    )
    await db.commit()
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# POST · 分派 BD
# ════════════════════════════════════════════════════════════

@router.post("/{lead_id}/assign", response_model=LeadOut)
async def assign_lead(
    lead_id: uuid.UUID,
    payload: LeadAssignIn,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    lead = await leads_service.assign(
        db, principal=principal, lead_id=lead_id, assignee_user_id=payload.assignee_user_id
    )
    await db.commit()
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# POST · 状态机
# ════════════════════════════════════════════════════════════

@router.post("/{lead_id}/transition", response_model=LeadOut)
async def transition_lead_status(
    lead_id: uuid.UUID,
    payload: LeadTransitionIn,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadOut:
    try:
        lead = await leads_service.transition_status(
            db,
            principal=principal,
            lead_id=lead_id,
            new_status=payload.new_status,
            note=payload.note,
            lost_reason=payload.lost_reason,
            lost_competitor=payload.lost_competitor,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return LeadOut.model_validate(lead)


# ════════════════════════════════════════════════════════════
# POST · lead → deal 转换
# ════════════════════════════════════════════════════════════

@router.post("/{lead_id}/convert", response_model=LeadDealOut, status_code=201)
async def convert_lead_to_deal(
    lead_id: uuid.UUID,
    payload: LeadConvertIn,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> LeadDealOut:
    try:
        lead, deal = await leads_service.convert_to_deal(
            db,
            principal=principal,
            lead_id=lead_id,
            deal_name=payload.deal_name,
            estimated_value_usd=payload.estimated_value_usd,
            probability_pct=payload.probability_pct,
            expected_close_date=payload.expected_close_date,
            related_sku_slugs=payload.related_sku_slugs,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return LeadDealOut(
        lead=LeadOut.model_validate(lead),
        deal_id=deal.id,
        deal_name=deal.name,
    )
