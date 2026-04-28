"""Tenants — admin-only CRUD."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal, require_platform_admin
from app.db.session import get_db
from app.models.tenant import Tenant
from app.schemas.tenant import TenantCreate, TenantOut

router = APIRouter()


@router.post("", response_model=TenantOut, status_code=201,
             dependencies=[Depends(require_platform_admin)])
async def create_tenant(
    payload: TenantCreate, db: AsyncSession = Depends(get_db)
) -> TenantOut:
    tenant = Tenant(
        id=uuid.uuid4(),
        slug=payload.slug,
        name=payload.name,
        display_name=payload.display_name,
        legal_entity_type=payload.legal_entity_type,
        credit_code=payload.credit_code,
        storage_prefix=payload.slug,
    )
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"tenant exists: {e.orig}") from e
    return TenantOut.model_validate(tenant)


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[TenantOut]:
    if p.is_platform_admin:
        stmt = select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.slug)
    else:
        stmt = select(Tenant).where(Tenant.id == p.tenant_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [TenantOut.model_validate(t) for t in rows]


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> TenantOut:
    if not p.is_platform_admin and p.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return TenantOut.model_validate(tenant)
