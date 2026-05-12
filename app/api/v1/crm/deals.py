"""/v1/crm/deals · 商机 REST API"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.crm.deal import (
    DealCreate, DealOut, DealListOut, DealStageTransitionIn, PipelineForecastOut,
)
from app.services.crm import deals_service

router = APIRouter()


@router.post("", response_model=DealOut, status_code=http_status.HTTP_201_CREATED)
async def create_deal(
    payload: DealCreate,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    deal = await deals_service.create_deal(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        factory_slug=payload.factory_slug,
        name=payload.name,
        account_id=payload.account_id,
        primary_contact_id=payload.primary_contact_id,
        lead_id=payload.lead_id,
        estimated_value_usd=payload.estimated_value_usd,
        probability_pct=payload.probability_pct,
        expected_close_date=payload.expected_close_date,
        related_sku_slugs=payload.related_sku_slugs,
        owner_user_id=payload.owner_user_id,
    )
    await db.commit()
    return DealOut.model_validate(deal)


@router.get("", response_model=DealListOut)
async def list_deals(
    factory_slug: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    owner_user_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> DealListOut:
    rows = await deals_service.list_deals(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        factory_slug=factory_slug,
        stage=stage,
        owner_user_id=owner_user_id,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )
    return DealListOut(
        items=[DealOut.model_validate(r) for r in rows],
        total=len(rows), limit=limit, offset=offset,
    )


@router.get("/{deal_id}", response_model=DealOut)
async def get_deal(
    deal_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    from app.models.crm.deal import Deal
    deal = await db.get(Deal, deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")
    if not principal.is_platform_admin and deal.tenant_id != principal.tenant_id:
        raise HTTPException(403, "Forbidden")
    return DealOut.model_validate(deal)


@router.post("/{deal_id}/transition", response_model=DealOut)
async def transition_stage(
    deal_id: uuid.UUID,
    payload: DealStageTransitionIn,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    try:
        deal = await deals_service.transition_stage(
            db,
            principal=principal,
            deal_id=deal_id,
            new_stage=payload.new_stage,
            won_value_usd=payload.won_value_usd,
            lost_reason=payload.lost_reason,
            lost_competitor=payload.lost_competitor,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return DealOut.model_validate(deal)


@router.get("/forecast/pipeline", response_model=PipelineForecastOut)
async def get_pipeline_forecast(
    factory_slug: Optional[str] = Query(None),
    owner_user_id: Optional[uuid.UUID] = Query(None),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PipelineForecastOut:
    """漏斗 forecast·按 stage 聚合金额"""
    result = await deals_service.get_pipeline_forecast(
        db,
        tenant_id=principal.tenant_id,
        factory_slug=factory_slug,
        owner_user_id=owner_user_id,
    )
    return PipelineForecastOut(**result)
