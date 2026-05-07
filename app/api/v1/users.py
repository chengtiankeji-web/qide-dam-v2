"""User CRUD endpoints — list / invite / update / reset-password / disable.

Permission model:
- platform_admin   : can list/create/update/delete users in any tenant
- tenant_admin     : can list/create/update/delete users in their own tenant
- member / viewer  : 403 (no user management)

Self-service:
- /v1/users/me            : GET self profile (richer than /v1/auth/me)
- /v1/users/me/password   : POST change own password (verifies current_password)
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import (
    PasswordChange,
    PasswordReset,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter()


def _is_admin(p: Principal) -> bool:
    return p.is_platform_admin or p.role == "tenant_admin"


# ─────────────────────────  self-service  ─────────────────────────

@router.get("/me", response_model=UserOut)
async def get_me(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    if not p.user_id:
        raise HTTPException(401, "API-key auth has no user profile")
    u = (await db.execute(select(User).where(User.id == p.user_id))).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "user not found")
    return UserOut.model_validate(u)


@router.post("/me/password", status_code=204)
async def change_my_password(
    payload: PasswordChange,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not p.user_id:
        raise HTTPException(401, "API-key auth cannot change password")
    u = (await db.execute(select(User).where(User.id == p.user_id))).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "user not found")
    if not verify_password(payload.current_password, u.password_hash):
        raise HTTPException(401, "current_password incorrect")
    u.password_hash = hash_password(payload.new_password)
    await db.commit()


# ─────────────────────────  admin: list/CRUD  ─────────────────────

@router.get("", response_model=list[UserOut])
async def list_users(
    tenant_id: uuid.UUID | None = None,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[UserOut]:
    if not _is_admin(p):
        raise HTTPException(403, "admin only")

    stmt = select(User).where(User.deleted_at.is_(None))
    if p.is_platform_admin:
        # can scope by tenant_id (optional)
        if tenant_id is not None:
            stmt = stmt.where(User.tenant_id == tenant_id)
    else:
        # tenant_admin: scoped to own tenant
        stmt = stmt.where(User.tenant_id == p.tenant_id)

    stmt = stmt.order_by(User.created_at.desc())
    users = (await db.execute(stmt)).scalars().all()
    return [UserOut.model_validate(u) for u in users]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    if not _is_admin(p):
        raise HTTPException(403, "admin only")

    # 决定 target tenant
    target_tenant_id = payload.tenant_id or p.tenant_id
    if not p.is_platform_admin and target_tenant_id != p.tenant_id:
        raise HTTPException(403, "tenant_admin cannot create user in other tenant")

    # 防重 (tenant_id, email) 唯一约束
    existing = (
        await db.execute(
            select(User).where(
                User.tenant_id == target_tenant_id,
                User.email == payload.email,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "user with this email already exists in target tenant")

    # 普通 admin 不能造 platform_admin
    if payload.role == "platform_admin" and not p.is_platform_admin:
        raise HTTPException(403, "only platform_admin can grant platform_admin role")

    u = User(
        tenant_id=target_tenant_id,
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_platform_admin=(payload.role == "platform_admin"),
        is_active=True,
        project_access=payload.project_access,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return UserOut.model_validate(u)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    if not _is_admin(p):
        raise HTTPException(403, "admin only")

    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not u or u.deleted_at is not None:
        raise HTTPException(404, "user not found")

    if not p.is_platform_admin and u.tenant_id != p.tenant_id:
        raise HTTPException(403, "cannot modify user in other tenant")

    if payload.full_name is not None:
        u.full_name = payload.full_name
    if payload.role is not None:
        if payload.role == "platform_admin" and not p.is_platform_admin:
            raise HTTPException(403, "only platform_admin can grant platform_admin role")
        u.role = payload.role
        u.is_platform_admin = (payload.role == "platform_admin")
    if payload.is_active is not None:
        u.is_active = payload.is_active
    if payload.project_access is not None:
        u.project_access = payload.project_access

    await db.commit()
    await db.refresh(u)
    return UserOut.model_validate(u)


@router.post("/{user_id}/reset-password", status_code=204)
async def admin_reset_password(
    user_id: uuid.UUID,
    payload: PasswordReset,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not _is_admin(p):
        raise HTTPException(403, "admin only")
    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not u or u.deleted_at is not None:
        raise HTTPException(404, "user not found")
    if not p.is_platform_admin and u.tenant_id != p.tenant_id:
        raise HTTPException(403, "cannot reset password in other tenant")
    u.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not _is_admin(p):
        raise HTTPException(403, "admin only")
    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not u or u.deleted_at is not None:
        raise HTTPException(404, "user not found")
    if not p.is_platform_admin and u.tenant_id != p.tenant_id:
        raise HTTPException(403, "cannot delete user in other tenant")
    if u.id == p.user_id:
        raise HTTPException(400, "cannot delete yourself")
    # soft delete
    from datetime import datetime, timezone
    u.deleted_at = datetime.now(timezone.utc)
    u.is_active = False
    await db.commit()
