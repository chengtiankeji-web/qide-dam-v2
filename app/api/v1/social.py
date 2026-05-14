"""Social Matrix v2 · REST API · /v1/social/*

端点：
  POST   /v1/social/oauth/{platform}/authorize     · 返 url + state（前端跳转）
  GET    /v1/social/oauth/{platform}/callback      · 接 code + state · 入凭证
  POST   /v1/social/credentials/{id}/revoke        · 撤销凭证
  GET    /v1/social/credentials                    · 列凭证（不含 plaintext）

  GET    /v1/social/accounts                       · 工厂账号列表
  POST   /v1/social/accounts                       · 创建账号（OAuth callback 后通常自动创建）
  PATCH  /v1/social/accounts/{id}                  · 改 display_name / status
  DELETE /v1/social/accounts/{id}                  · 断开账号

  GET    /v1/social/posts                          · 帖子列表（支持 status / account_id / factory_slug 筛）
  POST   /v1/social/posts                          · 创草稿
  POST   /v1/social/posts/{id}/approve             · 状态机 → approved
  POST   /v1/social/posts/{id}/schedule            · 入 scheduled
  POST   /v1/social/posts/{id}/publish-now         · 立即发布（同步等结果·15s 超时）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.social import SocialAccount, SocialCredential, SocialPost
from app.services import social_credential_service, social_oauth_service

router = APIRouter()


# ════════════════════════════════════════════════════════════
# Schemas（行内·与 schemas/intake.py 同风格）
# ════════════════════════════════════════════════════════════

class AuthorizeIn(BaseModel):
    factory_slug: str = Field(..., max_length=64)
    project_id: uuid.UUID
    extra_scopes: list[str] | None = None


class AuthorizeOut(BaseModel):
    authorize_url: str
    state: str


class CredentialSummaryOut(BaseModel):
    id: str
    platform: str
    credential_type: str
    expires_at: str | None = None
    refresh_failed_at: str | None = None
    scopes: str | None = None
    created_at: str


class SocialAccountCreate(BaseModel):
    project_id: uuid.UUID
    factory_slug: str = Field(..., max_length=64)
    platform: str
    platform_account_id: str
    display_name: str | None = None
    profile_url: str | None = None
    avatar_url: str | None = None
    credential_id: uuid.UUID | None = None


class SocialAccountUpdate(BaseModel):
    display_name: str | None = None
    status: str | None = None
    credential_id: uuid.UUID | None = None


class SocialAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    factory_slug: str
    platform: str
    platform_account_id: str
    display_name: str | None = None
    profile_url: str | None = None
    avatar_url: str | None = None
    credential_id: uuid.UUID | None = None
    status: str
    last_post_at: datetime | None = None
    warning_count: int
    metrics: dict[str, Any] | None = None
    created_at: datetime


class SocialPostCreate(BaseModel):
    project_id: uuid.UUID
    account_id: uuid.UUID
    factory_slug: str = Field(..., max_length=64)
    content_text: str = Field(..., min_length=1, max_length=10000)
    content_language: str = "en"
    asset_ids: list[str] | None = None
    link_url: str | None = None
    scheduled_at: datetime | None = None


class SocialPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    factory_slug: str
    content_text: str
    content_language: str | None = None
    status: str
    scheduled_at: datetime | None = None
    published_at: datetime | None = None
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    metrics_likes: int = 0
    metrics_comments: int = 0
    metrics_shares: int = 0
    metrics_impressions: int = 0
    error_message: str | None = None
    created_at: datetime


# ════════════════════════════════════════════════════════════
# OAuth
# ════════════════════════════════════════════════════════════

@router.post("/oauth/{platform}/authorize", response_model=AuthorizeOut)
async def social_oauth_authorize(
    platform: str,
    payload: AuthorizeIn,
    p: Principal = Depends(get_current_principal),
) -> AuthorizeOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        url, state = social_oauth_service.build_authorize_url(
            platform=platform,
            factory_slug=payload.factory_slug,
            tenant_id=p.tenant_id,
            project_id=payload.project_id,
            initiated_by_user_id=p.user_id,
            extra_scopes=payload.extra_scopes,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return AuthorizeOut(authorize_url=url, state=state)


@router.get("/oauth/{platform}/callback")
async def social_oauth_callback(
    platform: str,
    code: str = Query(...),
    state: str = Query(...),
    request: Request = None,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    parts = state.split(":")
    code_verifier = parts[4] if len(parts) >= 5 else None
    try:
        result = await social_oauth_service.handle_callback(
            db,
            principal=p,
            platform=platform,
            code=code,
            state=state,
            code_verifier=code_verifier,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return result


# ════════════════════════════════════════════════════════════
# Credentials
# ════════════════════════════════════════════════════════════

@router.get("/credentials", response_model=list[CredentialSummaryOut])
async def list_credentials(
    platform: str | None = Query(None, max_length=32),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[CredentialSummaryOut]:
    items = await social_credential_service.list_credentials_summary(
        db, tenant_id=p.tenant_id, platform=platform,
    )
    return [CredentialSummaryOut(**it) for it in items]


@router.post("/credentials/{credential_id}/revoke")
async def revoke_credential_endpoint(
    credential_id: uuid.UUID,
    reason: str = Query(..., max_length=256),
    request: Request = None,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    cred = await db.get(SocialCredential, credential_id)
    if not cred or cred.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found")
    await social_credential_service.revoke_credential(
        db, principal=p, credential_id=credential_id, reason=reason, request=request,
    )
    return {"status": "revoked"}


# ════════════════════════════════════════════════════════════
# Accounts
# ════════════════════════════════════════════════════════════

@router.get("/accounts", response_model=list[SocialAccountOut])
async def list_accounts(
    project_id: uuid.UUID | None = Query(None),
    factory_slug: str | None = Query(None, max_length=64),
    platform: str | None = Query(None, max_length=32),
    status_filter: str | None = Query(None, alias="status", max_length=32),
    limit: int = Query(100, ge=1, le=500),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[SocialAccountOut]:
    q = select(SocialAccount).where(SocialAccount.tenant_id == p.tenant_id)
    if project_id is not None:
        if not p.can_access_project(project_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
        q = q.where(SocialAccount.project_id == project_id)
    if factory_slug:
        q = q.where(SocialAccount.factory_slug == factory_slug)
    if platform:
        q = q.where(SocialAccount.platform == platform)
    if status_filter:
        q = q.where(SocialAccount.status == status_filter)
    q = q.order_by(SocialAccount.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return [SocialAccountOut.model_validate(r) for r in result.scalars().all()]


@router.post("/accounts", response_model=SocialAccountOut, status_code=201)
async def create_account(
    payload: SocialAccountCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SocialAccountOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    acct = SocialAccount(
        tenant_id=p.tenant_id,
        project_id=payload.project_id,
        factory_slug=payload.factory_slug,
        platform=payload.platform,
        platform_account_id=payload.platform_account_id,
        display_name=payload.display_name,
        profile_url=payload.profile_url,
        avatar_url=payload.avatar_url,
        credential_id=payload.credential_id,
        status="active" if payload.credential_id else "pending_oauth",
    )
    db.add(acct)
    await db.flush()
    return SocialAccountOut.model_validate(acct)


@router.patch("/accounts/{account_id}", response_model=SocialAccountOut)
async def update_account(
    account_id: uuid.UUID,
    payload: SocialAccountUpdate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SocialAccountOut:
    acct = await db.get(SocialAccount, account_id)
    if not acct or acct.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    if not p.can_access_project(acct.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    if payload.display_name is not None:
        acct.display_name = payload.display_name
    if payload.status is not None:
        if payload.status not in (
            "active", "expired", "suspended", "disconnected", "pending_oauth",
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid status")
        acct.status = payload.status
    if payload.credential_id is not None:
        acct.credential_id = payload.credential_id
    acct.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return SocialAccountOut.model_validate(acct)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(
    account_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    acct = await db.get(SocialAccount, account_id)
    if not acct or acct.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    if not p.can_access_project(acct.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    await db.delete(acct)
    await db.flush()


# ════════════════════════════════════════════════════════════
# Posts
# ════════════════════════════════════════════════════════════

@router.get("/posts", response_model=list[SocialPostOut])
async def list_posts(
    project_id: uuid.UUID | None = Query(None),
    account_id: uuid.UUID | None = Query(None),
    factory_slug: str | None = Query(None, max_length=64),
    status_filter: str | None = Query(None, alias="status", max_length=32),
    limit: int = Query(100, ge=1, le=500),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[SocialPostOut]:
    q = select(SocialPost).where(SocialPost.tenant_id == p.tenant_id)
    if project_id is not None:
        if not p.can_access_project(project_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
        q = q.where(SocialPost.project_id == project_id)
    if account_id:
        q = q.where(SocialPost.account_id == account_id)
    if factory_slug:
        q = q.where(SocialPost.factory_slug == factory_slug)
    if status_filter:
        q = q.where(SocialPost.status == status_filter)
    q = q.order_by(SocialPost.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return [SocialPostOut.model_validate(r) for r in result.scalars().all()]


@router.post("/posts", response_model=SocialPostOut, status_code=201)
async def create_post(
    payload: SocialPostCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SocialPostOut:
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    acct = await db.get(SocialAccount, payload.account_id)
    if not acct or acct.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")

    initial_status = "scheduled" if payload.scheduled_at else "draft"
    post = SocialPost(
        tenant_id=p.tenant_id,
        project_id=payload.project_id,
        account_id=payload.account_id,
        factory_slug=payload.factory_slug,
        content_text=payload.content_text,
        content_language=payload.content_language,
        asset_ids=payload.asset_ids or [],
        link_url=payload.link_url,
        status=initial_status,
        scheduled_at=payload.scheduled_at,
        created_by_user_id=p.user_id,
    )
    db.add(post)
    await db.flush()
    return SocialPostOut.model_validate(post)


@router.post("/posts/{post_id}/approve", response_model=SocialPostOut)
async def approve_post(
    post_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SocialPostOut:
    post = await db.get(SocialPost, post_id)
    if not post or post.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
    if post.status not in ("draft", "pending_approval"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"can only approve draft / pending · current={post.status}",
        )
    post.status = "approved"
    post.approved_by_user_id = p.user_id
    post.approved_at = datetime.now(timezone.utc)
    await db.flush()
    return SocialPostOut.model_validate(post)


@router.post("/posts/{post_id}/publish-now", response_model=SocialPostOut)
async def publish_post_now(
    post_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SocialPostOut:
    post = await db.get(SocialPost, post_id)
    if not post or post.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
    if post.status not in ("approved", "draft", "scheduled"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"can not publish from status {post.status}",
        )

    post.status = "publishing"
    await db.flush()

    # 入 Celery 异步发（v4.1 实装）
    try:
        from app.workers.tasks_social import publish_social_post
        publish_social_post.delay(str(post.id))
    except Exception:
        # 同步 fallback：仅记错·状态待人工 retry
        post.status = "draft"
        post.error_message = "celery_unavailable"
        await db.flush()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "celery worker not available · publish queued aborted",
        )

    return SocialPostOut.model_validate(post)
