"""Multipart upload endpoints — for files >32 MiB.

Flow:
  POST   /v1/uploads/multipart/init                   -> returns upload_id + asset_id
  POST   /v1/uploads/multipart/{asset_id}/sign-part   -> presigned UploadPart URL
  POST   /v1/uploads/multipart/{asset_id}/complete    -> finalize + start processing
  DELETE /v1/uploads/multipart/{asset_id}             -> abort
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.asset import AssetOut
from app.schemas.upload import (
    MultipartAbortOut,
    MultipartCompleteIn,
    MultipartInitIn,
    MultipartInitOut,
    MultipartSignPartIn,
    MultipartSignPartOut,
)
from app.services import upload_service

router = APIRouter()


@router.post("/multipart/init", response_model=MultipartInitOut, status_code=201)
async def init_multipart(
    payload: MultipartInitIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> MultipartInitOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        asset, mp = await upload_service.init_multipart(
            db, tenant_id=p.tenant_id, payload=payload
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return MultipartInitOut(
        asset_id=asset.id, upload_id=mp.upload_id, storage_key=mp.storage_key
    )


@router.post("/multipart/{asset_id}/sign-part", response_model=MultipartSignPartOut)
async def sign_part(
    asset_id: uuid.UUID,
    payload: MultipartSignPartIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> MultipartSignPartOut:
    try:
        url = await upload_service.sign_part(
            db,
            tenant_id=p.tenant_id,
            asset_id=asset_id,
            part_number=payload.part_number,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return MultipartSignPartOut(upload_url=url, headers={}, expires_in=3600)


@router.post("/multipart/{asset_id}/complete", response_model=AssetOut)
async def complete_multipart(
    asset_id: uuid.UUID,
    payload: MultipartCompleteIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    parts = [
        {"PartNumber": p_.part_number, "ETag": p_.etag}
        for p_ in sorted(payload.parts, key=lambda x: x.part_number)
    ]
    try:
        asset = await upload_service.complete(
            db, tenant_id=p.tenant_id, asset_id=asset_id, parts=parts
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # After complete, kick the same Celery pipeline used by simple uploads
    try:
        from app.workers.tasks_pipeline import process_pipeline
        process_pipeline.delay(str(asset.id))
    except Exception:  # noqa: BLE001
        asset.status = "ready"
        await db.flush()
    return AssetOut.model_validate(asset)


@router.delete("/multipart/{asset_id}", response_model=MultipartAbortOut)
async def abort_multipart(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> MultipartAbortOut:
    try:
        await upload_service.abort(db, tenant_id=p.tenant_id, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return MultipartAbortOut(aborted=True)
