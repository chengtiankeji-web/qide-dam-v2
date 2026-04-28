"""ShareLinks — public read-only links to assets / collections."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.models.share_link import ShareLink
from app.schemas.share_link import (
    ShareLinkCreate,
    ShareLinkOut,
    ShareLinkResolveIn,
)
from app.services import share_link_service, storage

router = APIRouter()


@router.post("", response_model=ShareLinkOut, status_code=201)
async def create_share_link(
    payload: ShareLinkCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ShareLinkOut:
    try:
        sl = await share_link_service.create_link(
            db,
            tenant_id=p.tenant_id,
            asset_id=payload.asset_id,
            collection_id=payload.collection_id,
            created_by_user_id=p.user_id,
            password=payload.password,
            expires_at=payload.expires_at,
            max_downloads=payload.max_downloads,
            note=payload.note,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return ShareLinkOut.model_validate(sl)


@router.get("", response_model=list[ShareLinkOut])
async def list_share_links(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ShareLinkOut]:
    rows = (
        await db.execute(
            select(ShareLink).where(ShareLink.tenant_id == p.tenant_id)
            .order_by(ShareLink.created_at.desc())
        )
    ).scalars().all()
    return [ShareLinkOut.model_validate(r) for r in rows]


@router.delete("/{share_link_id}", status_code=204)
async def revoke_link(
    share_link_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    sl = (
        await db.execute(
            select(ShareLink).where(
                ShareLink.id == share_link_id, ShareLink.tenant_id == p.tenant_id
            )
        )
    ).scalar_one_or_none()
    if not sl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    sl.is_active = False
    await db.flush()


# ----- public resolution (no auth) -----

public_router = APIRouter()


@public_router.post("/share/{token}/resolve")
async def resolve_share_link(
    token: str,
    payload: ShareLinkResolveIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        sl = await share_link_service.resolve_link(db, token=token, password=payload.password)
    except ValueError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    if sl.asset_id:
        asset = (
            await db.execute(select(Asset).where(Asset.id == sl.asset_id))
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "asset gone")
        url = storage.presign_get(storage_key=asset.storage_key, expires_in=3600)
        return {"kind": "asset", "asset": {
            "id": str(asset.id), "name": asset.name, "kind": asset.kind,
            "thumbnails": asset.thumbnails,
        }, "download_url": url, "expires_in": 3600}

    return {"kind": "collection", "collection_id": str(sl.collection_id)}
