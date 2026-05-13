"""Projects — scoped under a tenant.

v3 P1.3 (2026-05-13): 完整 CRUD + audit 全覆盖
  - POST  /v1/projects        · create (admin only) + audit project.created
  - GET   /v1/projects        · list (filtered by tenant)
  - GET   /v1/projects/{id}   · detail
  - PATCH /v1/projects/{id}   · update name/description/default_acl/is_active
                                + audit project.updated
                                · slug + storage_prefix 不可改（baked into R2 path）
  - DELETE /v1/projects/{id}  · soft delete (deleted_at=NOW)
                                + audit project.deleted
                                · 默认拒删非空 project（需要 ?force=true）
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.models.project import Project
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.services import audit_service
from app.services.audit_service import AuditAction

router = APIRouter()


def _require_admin(p: Principal) -> None:
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "project mutation requires tenant_admin / platform_admin",
        )


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectCreate,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    _require_admin(p)
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

    # v3 P1.3 audit
    await audit_service.audit(
        db,
        action=AuditAction.PROJECT_CREATED,
        tenant_id=p.tenant_id,
        project_id=project.id,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="project",
        target_id=project.id,
        request=request,
        metadata={"slug": project.slug, "name": project.name},
    )
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
    # platform_admin 跨 tenant 时不限制
    where_clauses = [Project.id == project_id]
    if not p.is_platform_admin:
        where_clauses.append(Project.tenant_id == p.tenant_id)
    project = (
        await db.execute(select(Project).where(*where_clauses))
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """v3 P1.3 (2026-05-13) D5: PATCH project · 改 name/description/default_acl/is_active。

    **不能改 slug / storage_prefix**（已固化在 R2 path · 改了所有 download URL 失效）。
    历史背景：Sam 2026-05-13 改 internal → qidematrix-sam 需要 SSH + SQL ·
    这条端点上线后改名直接 admin SPA 一键搞定。
    """
    _require_admin(p)
    if not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    where_clauses = [Project.id == project_id, Project.deleted_at.is_(None)]
    if not p.is_platform_admin:
        where_clauses.append(Project.tenant_id == p.tenant_id)
    project = (
        await db.execute(select(Project).where(*where_clauses))
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    data = payload.model_dump(exclude_unset=True)
    changes: dict = {}
    for field, value in data.items():
        old_value = getattr(project, field, None)
        if old_value != value:
            changes[field] = {"old": old_value, "new": value}
            setattr(project, field, value)

    if not changes:
        return ProjectOut.model_validate(project)

    await db.flush()

    # v3 P1.3 audit
    await audit_service.audit(
        db,
        action=AuditAction.PROJECT_UPDATED,
        tenant_id=project.tenant_id,
        project_id=project.id,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="project",
        target_id=project.id,
        request=request,
        metadata={"changes": changes, "slug": project.slug},
    )
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: uuid.UUID,
    request: Request,
    force: bool = Query(
        False,
        description="True = 即使 project 下还有 active asset 也 soft-delete · "
                    "False (default) = 项目非空时拒绝（防误删）",
    ),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    """v3 P1.3 (2026-05-13) D5: Soft delete project。

    Asset 不会被自动连带删 · 仅 project.deleted_at=NOW · 之后 list_projects 不再展示。
    要真清 asset 用 /v1/assets/_trash/empty。
    """
    from datetime import UTC, datetime
    _require_admin(p)
    if not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    where_clauses = [Project.id == project_id, Project.deleted_at.is_(None)]
    if not p.is_platform_admin:
        where_clauses.append(Project.tenant_id == p.tenant_id)
    project = (
        await db.execute(select(Project).where(*where_clauses))
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    # 检查 project 下是否还有 active asset
    asset_count = (
        await db.execute(
            select(func.count(Asset.id)).where(
                Asset.project_id == project_id,
                Asset.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    if asset_count > 0 and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"project still has {asset_count} active assets · "
            "use ?force=true to soft-delete anyway · "
            "or empty trash + clear assets first",
        )

    project.deleted_at = datetime.now(UTC)
    await db.flush()

    # v3 P1.3 audit
    await audit_service.audit(
        db,
        action=AuditAction.PROJECT_DELETED,
        tenant_id=project.tenant_id,
        project_id=project.id,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="project",
        target_id=project.id,
        request=request,
        metadata={
            "slug": project.slug,
            "name": project.name,
            "force": force,
            "active_asset_count_at_delete": asset_count,
        },
    )
