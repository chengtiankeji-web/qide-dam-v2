"""Collections — albums / curated sets."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.models.collection import Collection
from app.schemas.asset import AssetOut
from app.schemas.collection import (
    CollectionAssetIn,
    CollectionCreate,
    CollectionOut,
    CollectionUpdate,
)
from app.schemas.common import PageOut
from app.services import collection_service

router = APIRouter()


@router.post("", response_model=CollectionOut, status_code=201)
async def create_collection(
    payload: CollectionCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> CollectionOut:
    if payload.project_id and not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        coll = await collection_service.create_collection(
            db,
            tenant_id=p.tenant_id,
            owner_user_id=p.user_id,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            project_id=payload.project_id,
            cover_asset_id=payload.cover_asset_id,
            acl=payload.acl,
            is_smart=payload.is_smart,
            smart_query=payload.smart_query,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return CollectionOut.model_validate(coll)


@router.get("", response_model=list[CollectionOut])
async def list_collections(
    project_id: uuid.UUID | None = Query(None),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[CollectionOut]:
    rows = await collection_service.list_collections(
        db, tenant_id=p.tenant_id, project_id=project_id
    )
    return [CollectionOut.model_validate(c) for c in rows]


@router.get("/{collection_id}", response_model=CollectionOut)
async def get_collection(
    collection_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> CollectionOut:
    coll = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.tenant_id == p.tenant_id,
                Collection.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not coll:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return CollectionOut.model_validate(coll)


@router.patch("/{collection_id}", response_model=CollectionOut)
async def update_collection(
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> CollectionOut:
    coll = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id, Collection.tenant_id == p.tenant_id
            )
        )
    ).scalar_one_or_none()
    if not coll:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(coll, field, value)
    await db.flush()
    return CollectionOut.model_validate(coll)


@router.post("/{collection_id}/assets", status_code=204)
async def add_assets(
    collection_id: uuid.UUID,
    items: list[CollectionAssetIn],
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    coll = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id, Collection.tenant_id == p.tenant_id
            )
        )
    ).scalar_one_or_none()
    if not coll:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    await collection_service.add_assets(
        db, collection_id=collection_id, items=[i.model_dump() for i in items]
    )


@router.delete("/{collection_id}/assets/{asset_id}", status_code=204)
async def remove_asset(
    collection_id: uuid.UUID,
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    await collection_service.remove_asset(
        db, collection_id=collection_id, asset_id=asset_id
    )


@router.get("/{collection_id}/assets", response_model=PageOut[AssetOut])
async def list_assets_in_collection(
    collection_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PageOut[AssetOut]:
    asset_ids, total = await collection_service.list_assets_in_collection(
        db, collection_id=collection_id, page=page, page_size=page_size
    )
    if not asset_ids:
        return PageOut[AssetOut](items=[], total=total, page=page, page_size=page_size)
    rows = (
        await db.execute(
            select(Asset).where(
                Asset.id.in_(asset_ids), Asset.tenant_id == p.tenant_id
            )
        )
    ).scalars().all()
    by_id = {a.id: a for a in rows}
    ordered = [by_id[i] for i in asset_ids if i in by_id]
    return PageOut[AssetOut](
        items=[AssetOut.model_validate(a) for a in ordered],
        total=total, page=page, page_size=page_size,
    )
