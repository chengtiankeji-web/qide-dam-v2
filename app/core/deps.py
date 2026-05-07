"""FastAPI dependencies — auth + tenant context.

Auth supports two flows:
1. Bearer JWT  →  decode, look up user, derive tenant_id from user record.
2. API Key (header `X-DAM-API-Key`) →  hash + look up api_keys row.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_access_token, hash_api_key
from app.db.session import get_db
from app.models.api_key import ApiKey
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    """Whoever is making this request — user or API key."""
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    role: str  # platform_admin | tenant_admin | member | viewer | api
    is_platform_admin: bool
    via: str  # "jwt" | "api_key"
    project_access: list  # [] = none; ["*"] = all; or list of project IDs as str
    scopes: list[str]  # only meaningful for api_key

    def can_access_project(self, project_id: uuid.UUID) -> bool:
        if self.is_platform_admin:
            return True
        access = self.project_access
        if "*" in access:
            return True
        return str(project_id) in access


async def get_current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    api_key_header: str | None = Header(None, alias=settings.MCP_API_KEY_HEADER),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    # ---- API Key path ----
    if api_key_header:
        digest = hash_api_key(api_key_header)
        # v3 P0-4: also reject keys that have been explicitly revoked.
        # is_active is the legacy flag; revoked_at is the new explicit
        # timestamp — we honour both so existing keys keep working while
        # the migration backfill runs.
        stmt = select(ApiKey).where(
            ApiKey.key_hash == digest,
            ApiKey.is_active.is_(True),
            ApiKey.revoked_at.is_(None),
        )
        api_key = (await db.execute(stmt)).scalar_one_or_none()
        if not api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        project_access: list = [str(api_key.project_id)] if api_key.project_id else ["*"]
        # 从签发该 key 的 user 表读真实 is_platform_admin（admin:* scope 仍可显式授权）
        owner_is_pa = False
        if api_key.user_id:
            owner = (
                await db.execute(select(User).where(User.id == api_key.user_id))
            ).scalar_one_or_none()
            if owner and owner.is_platform_admin:
                owner_is_pa = True
        return Principal(
            tenant_id=api_key.tenant_id,
            user_id=api_key.user_id,
            role="api",
            is_platform_admin=owner_is_pa,
            via="api_key",
            project_access=project_access,
            scopes=list(api_key.scopes or []),
        )

    # ---- JWT path ----
    if not creds or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing credentials")

    try:
        payload = decode_access_token(creds.credentials)
    except JWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}")

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing subject")

    user = (
        await db.execute(select(User).where(User.id == uuid.UUID(user_id_str)))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not active")

    # v3 P0-4: enforce token version. JWTs without a `tv` claim are
    # treated as version 1 (legacy compat) — once everyone re-signs in
    # this becomes a hard requirement.
    jwt_token_version = payload.get("tv", 1)
    if jwt_token_version != user.token_version:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "token revoked — please sign in again",
        )

    return Principal(
        tenant_id=user.tenant_id,
        user_id=user.id,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        via="jwt",
        project_access=list(user.project_access or []),
        scopes=[],
    )


def require_scope(scope: str):
    """Sub-dependency to require a specific API-key scope (no-op for JWT users)."""

    async def _checker(p: Principal = Depends(get_current_principal)) -> Principal:
        if p.via == "api_key" and scope not in p.scopes and "admin:*" not in p.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"scope {scope!r} required")
        return p

    return _checker


def require_platform_admin(p: Principal = Depends(get_current_principal)) -> Principal:
    if not p.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform admin only")
    return p
