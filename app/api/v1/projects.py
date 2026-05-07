"""Projects — scoped under a tenant."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.project import Project
from app.schemas.project import ProjectCreate, ProjectOut

router = APIRouter()


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    project = Project(
        id=uuid.uuid4(),
        tenant_id=p.tenant_id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        storage_prefix=payload.slug,
        default_acl=payload.default_acl,
    )
    db.add(project)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"slug exists: {e.orig}") from e
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    tenant_id: uuid.UUID | None = Query(None,
        description="platform_admin 才能传 · 跨 tenant 查；否则忽略，查自己 tenant"),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    # 决定真正过滤的 tenant_id：
    # · platform_admin 传了 ?tenant_id=xxx → 用 xxx（跨租户能力）
    # · 其他情况都强制用自己的 p.tenant_id（防止越权）
    target_tenant_id = (
        tenant_id if (tenant_id is not None and p.is_platform_admin) else p.tenant_id
    )
    stmt = select(Project).where(
        Project.tenant_id == target_tenant_id,
        Project.deleted_at.is_(None),
    ).order_by(Project.slug)
    rows = (await db.execute(stmt)).scalars().all()
    if p.is_platform_admin or "*" in p.project_access:
        return [ProjectOut.model_validate(r) for r in rows]
    allowed = set(p.project_access)
    return [ProjectOut.model_validate(r) for r in rows if str(r.id) in allowed]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    project = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.tenant_id == p.tenant_id)
        )
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return ProjectOut.model_validate(project)
