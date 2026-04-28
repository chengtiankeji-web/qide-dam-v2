"""Assets — search / list / detail / presigned upload / confirm / delete.

Sprint 1: small + presigned upload paths. Multipart upload deferred to Sprint 2.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.asset import (
    AssetOut,
    AssetUpdate,
    PresignedUploadIn,
    PresignedUploadOut,
)
from app.schemas.common import PageOut
from app.services import asset_service, storage

router = APIRouter()


@router.post("/uploads/presign", response_model=PresignedUploadOut)
async def presign_upload(
    payload: PresignedUploadIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PresignedUploadOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        asset, url, headers = await asset_service.register_presigned_upload(
            db, tenant_id=p.tenant_id, payload=payload
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    return PresignedUploadOut(
        asset_id=asset.id,
        upload_url=url,
        storage_key=asset.storage_key,
        method="PUT",
        headers=headers,
        expires_in=900,
    )


@router.post("/{asset_id}/uploads/confirm", response_model=AssetOut)
async def confirm_upload(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        asset = await asset_service.confirm_upload(
            db, tenant_id=p.tenant_id, asset_id=asset_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return AssetOut.model_validate(asset)


@router.get("", response_model=PageOut[AssetOut])
async def list_assets(
    project_id: uuid.UUID | None = Query(None),
    kind: str | None = Query(None),
    status_: str | None = Query("ready", alias="status"),
    q: str | None = Query(None, description="full-text search across name/description/tags"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PageOut[AssetOut]:
    if project_id and not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    items, total = await asset_service.list_assets(
        db,
        tenant_id=p.tenant_id,
        project_id=project_id,
        kind=kind,
        status=status_,
        q=q,
        page=page,
        page_size=page_size,
    )
    return PageOut[AssetOut](
        items=[AssetOut.model_validate(a) for a in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        asset = await asset_service.get_asset(db, tenant_id=p.tenant_id, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    return AssetOut.model_validate(asset)


@router.patch("/{asset_id}", response_model=AssetOut)
async def update_asset(
    asset_id: uuid.UUID,
    payload: AssetUpdate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        asset = await asset_service.get_asset(db, tenant_id=p.tenant_id, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(asset, field, value)
    if asset.acl == "public" and not asset.public_url:
        asset.public_url = storage.public_url_for(asset.storage_key)
    await db.flush()
    return AssetOut.model_validate(asset)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        asset = await asset_service.get_asset(db, tenant_id=p.tenant_id, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    await asset_service.soft_delete_asset(db, tenant_id=p.tenant_id, asset_id=asset_id)


@router.get("/{asset_id}/download-url")
async def get_download_url(
    asset_id: uuid.UUID,
    expires_in: int = Query(3600, ge=60, le=86400),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        asset = await asset_service.get_asset(db, tenant_id=p.tenant_id, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    url = storage.presign_get(storage_key=asset.storage_key, expires_in=expires_in)
    return {"url": url, "expires_in": expires_in}
