"""Folders — project-internal hierarchy."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.folder import FolderCreate, FolderOut
from app.services import folder_service

router = APIRouter()


@router.post("", response_model=FolderOut, status_code=201)
async def create_folder(
    payload: FolderCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> FolderOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        f = await folder_service.create_folder(
            db,
            tenant_id=p.tenant_id,
            project_id=payload.project_id,
            parent_id=payload.parent_id,
            name=payload.name,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return FolderOut.model_validate(f)


@router.get("", response_model=list[FolderOut])
async def list_folders(
    project_id: uuid.UUID = Query(...),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[FolderOut]:
    if not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    rows = await folder_service.list_folders(
        db, tenant_id=p.tenant_id, project_id=project_id
    )
    return [FolderOut.model_validate(f) for f in rows]
