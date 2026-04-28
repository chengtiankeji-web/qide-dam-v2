from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folder import Folder


async def create_folder(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
) -> Folder:
    parent_path = "/"
    if parent_id:
        parent = (
            await db.execute(
                select(Folder).where(
                    Folder.id == parent_id,
                    Folder.project_id == project_id,
                    Folder.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not parent:
            raise ValueError("parent folder not found in project")
        parent_path = parent.path

    safe_name = name.replace("/", "_").strip()
    new_path = parent_path.rstrip("/") + "/" + safe_name + "/"

    folder = Folder(
        tenant_id=tenant_id,
        project_id=project_id,
        parent_id=parent_id,
        name=safe_name,
        path=new_path,
    )
    db.add(folder)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise ValueError(f"folder exists at {new_path}: {e}") from e
    return folder


async def list_folders(
    db: AsyncSession, *, tenant_id: uuid.UUID, project_id: uuid.UUID
) -> list[Folder]:
    stmt = select(Folder).where(
        Folder.tenant_id == tenant_id,
        Folder.project_id == project_id,
        Folder.deleted_at.is_(None),
    ).order_by(Folder.path)
    return list((await db.execute(stmt)).scalars().all())
