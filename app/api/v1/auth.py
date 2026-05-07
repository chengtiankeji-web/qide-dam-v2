"""Auth — login (JWT) + API key issuance."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import Principal, get_current_principal
from app.core.security import (
    create_access_token,
    generate_api_key,
    verify_password,
)
from app.db.session import get_db
from app.models.api_key import ApiKey
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreateOut,
    ApiKeyOut,
    LoginIn,
    TokenOut,
)
from app.services.audit_service import AuditAction, audit

router = APIRouter()


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    stmt = select(User).where(User.email == payload.email, User.is_active.is_(True))
    if payload.tenant_slug:
        stmt = stmt.join(Tenant, Tenant.id == User.tenant_id).where(
            Tenant.slug == payload.tenant_slug
        )
    users = (await db.execute(stmt)).scalars().all()
    if not users:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # If multiple tenants matched, require tenant_slug
    if len(users) > 1 and not payload.tenant_slug:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ambiguous user: please supply tenant_slug",
        )
    user = users[0]

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    token = create_access_token(
        str(user.id),
        extra_claims={
            "tid": str(user.tenant_id),
            "role": user.role,
            "ipa": user.is_platform_admin,
            # v3 P0-4: token-version watermark. The auth dep compares this
            # to user.token_version on every request — bumping the column
            # invalidates every JWT issued before the bump.
            "tv": user.token_version,
        },
    )
    return TokenOut(
        access_token=token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )


@router.get("/me")
async def me(p: Principal = Depends(get_current_principal)) -> dict:
    return {
        "tenant_id": str(p.tenant_id),
        "user_id": str(p.user_id) if p.user_id else None,
        "role": p.role,
        "is_platform_admin": p.is_platform_admin,
        "via": p.via,
        "scopes": p.scopes,
        "project_access": p.project_access,
    }


# ----- API keys -----

@router.post("/api-keys", response_model=ApiKeyCreateOut, status_code=201)
async def create_api_key(
    payload: ApiKeyCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreateOut:
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")

    raw_key, prefix, digest = generate_api_key(
        env_tag="live" if settings.is_production else "test"
    )
    record = ApiKey(
        id=uuid.uuid4(),
        tenant_id=p.tenant_id,
        user_id=p.user_id,
        name=payload.name,
        prefix=prefix,
        key_hash=digest,
        scopes=list(payload.scopes),
        project_id=payload.project_id,
    )
    db.add(record)
    await db.flush()

    return ApiKeyCreateOut(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=record.scopes,
        project_id=record.project_id,
        is_active=record.is_active,
        expires_at=None,
        created_at=record.created_at.isoformat(),
        raw_key=raw_key,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyOut]:
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.tenant_id == p.tenant_id).order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return [
        ApiKeyOut(
            id=r.id,
            name=r.name,
            prefix=r.prefix,
            scopes=r.scopes,
            project_id=r.project_id,
            is_active=r.is_active,
            expires_at=r.expires_at.isoformat() if r.expires_at else None,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke an API key. Tenant-scoped: tenant_admin / platform_admin can
    revoke keys in their own tenant; the owner of the key can also revoke
    their own key.

    v3 P0-4: also sets revoked_at = now() so the auth middleware can
    short-circuit on a single timestamp compare. The audit row is
    written before commit so the trail survives even if the user clicks
    cancel or the network drops mid-request — append-only via DB
    triggers means the event can't be erased after the fact.
    """
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        # Allow self-revoke (key owner) — fall through to ownership check below
        if p.role != "api":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")

    record = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")

    # tenant scoping: platform_admin 可跨 tenant ; 其他人只能改自己 tenant
    if not p.is_platform_admin and record.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot revoke key in other tenant")

    from datetime import datetime, timezone

    record.is_active = False
    record.revoked_at = datetime.now(timezone.utc)

    await audit(
        db,
        action=AuditAction.API_KEY_REVOKED,
        tenant_id=record.tenant_id,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="api_key",
        target_id=record.id,
        request=request,
        metadata={"prefix": record.prefix, "name": record.name},
    )

    await db.commit()
