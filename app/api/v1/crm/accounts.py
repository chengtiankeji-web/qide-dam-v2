"""/v1/crm/accounts · 公司 REST API"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.crm.account import (
    AccountCreate, AccountOut, AccountListOut, AccountMergeIn,
)
from app.services.crm import accounts_service

router = APIRouter()


@router.post("", response_model=AccountOut, status_code=http_status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AccountOut:
    account = await accounts_service.create_account(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        display_name=payload.display_name,
        legal_name=payload.legal_name,
        country=payload.country,
        country_code=payload.country_code,
        industry=payload.industry,
        website=payload.website,
        employee_count=payload.employee_count,
        annual_revenue_usd=payload.annual_revenue_usd,
        primary_email=payload.primary_email,
        primary_phone=payload.primary_phone,
        source=payload.source,
        tags=payload.tags,
    )
    await db.commit()
    return AccountOut.model_validate(account)


@router.get("", response_model=AccountListOut)
async def list_accounts(
    country: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=128),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AccountListOut:
    rows = await accounts_service.list_accounts(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        country=country,
        industry=industry,
        search=search,
        limit=limit,
        offset=offset,
    )
    return AccountListOut(
        items=[AccountOut.model_validate(r) for r in rows],
        total=len(rows), limit=limit, offset=offset,
    )


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AccountOut:
    from app.models.crm.account import Account
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if not principal.is_platform_admin and account.tenant_id != principal.tenant_id:
        raise HTTPException(403, "Forbidden")
    return AccountOut.model_validate(account)


@router.post("/merge", response_model=AccountOut)
async def merge_accounts(
    payload: AccountMergeIn,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AccountOut:
    """合并 2 个重复公司·把 merge_id 的 contacts/deals/leads 转给 keep_id"""
    try:
        kept = await accounts_service.merge_accounts(
            db, principal=principal,
            keep_id=payload.keep_id, merge_id=payload.merge_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return AccountOut.model_validate(kept)
