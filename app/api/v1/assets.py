"""Assets — search / list / detail / presigned upload / confirm / delete.

Sprint 1: small + presigned upload paths. Multipart upload deferred to Sprint 2.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
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
    status_: str | None = Query(None, alias="status",
        description="ready / processing / uploading / failed · 默认 None=显示全部"),
    q: str | None = Query(None, description="full-text search across name/description/tags"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PageOut[AssetOut]:
    if project_id and not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    # 2026-04-29 fix: platform_admin 传了 project_id 时，
    # 自动从 project 反查 tenant_id（这样跨 tenant 选项目能看到资产）
    effective_tenant_id = p.tenant_id
    if project_id and p.is_platform_admin:
        from app.models.project import Project as _P
        proj = (await db.execute(select(_P).where(_P.id == project_id))).scalar_one_or_none()
        if proj:
            effective_tenant_id = proj.tenant_id

    items, total = await asset_service.list_assets(
        db,
        tenant_id=effective_tenant_id,
        project_id=project_id,
        kind=kind,
        status=status_,
        q=q,
        page=page,
        page_size=page_size,
    )
    # 2026-04-29 perf: 列表里给 image kind 一次性签 thumb sm presigned URL
    # 客户端不再需要单独 round-trip 拿 download-url
    out_items = []
    for a in items:
        ao = AssetOut.model_validate(a)
        if a.kind == "image" and a.thumbnails:
            tu: dict = {}
            for variant in ("sm", "md", "lg"):
                key = a.thumbnails.get(variant) if isinstance(a.thumbnails, dict) else None
                if key:
                    try:
                        tu[variant] = storage.presign_get(storage_key=key, expires_in=3600)
                    except Exception:  # noqa: BLE001
                        pass
            if tu:
                ao.thumb_urls = tu
        out_items.append(ao)
    return PageOut[AssetOut](
        items=out_items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def _resolve_asset_tenant_id(
    db: AsyncSession, *, p: Principal, asset_id: uuid.UUID
) -> uuid.UUID:
    """For platform_admin, resolve the asset's tenant_id directly from the
    assets table (bypass JWT.tid). For other roles, use principal.tenant_id."""
    if not p.is_platform_admin:
        return p.tenant_id
    from app.models.asset import Asset as _A
    row = (await db.execute(select(_A).where(_A.id == asset_id))).scalar_one_or_none()
    return row.tenant_id if row else p.tenant_id


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
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
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
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
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    await asset_service.soft_delete_asset(db, tenant_id=effective_tid, asset_id=asset_id)


@router.get("/{asset_id}/download-url")
async def get_download_url(
    asset_id: uuid.UUID,
    expires_in: int = Query(3600, ge=60, le=86400),
    variant: str | None = Query(None, description="sm/md/lg · image kind 才有 · 否则原图"),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    # 2026-04-29 fix: variant=sm/md/lg 返回 thumbnail 缩略图 URL（如果存在）
    storage_key = asset.storage_key
    if variant and variant in ("sm", "md", "lg"):
        thumb_key = (asset.thumbnails or {}).get(variant)
        if thumb_key:
            storage_key = thumb_key
        # else: fallback to original

    url = storage.presign_get(storage_key=storage_key, expires_in=expires_in)
    return {"url": url, "expires_in": expires_in, "variant": variant or "original"}
