"""社媒矩阵 REST API · /v1/qm/social/*

模块：
  · accounts     · 平台账号档案 CRUD + 隔离绑定
  · browsers     · 浏览器环境（AdsPower 等）
  · proxies      · 代理 IP 池
  · posts        · 跨平台内容池
  · schedules    · 发布调度
  · health       · 健康度事件流
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.services.qidematrix import (
    social_matrix_service as sm,
    workspace_service as ws_svc,
)

router = APIRouter(prefix="/qm/social", tags=["qidematrix-social"])


def _err(code: int, detail: str):
    return HTTPException(status_code=code, detail=detail)


# ─── Schemas (inline · 简短 · 后续拆出去到 schemas/qidematrix/social.py) ──

class SocialAccountCreateIn(BaseModel):
    workspace_id: uuid.UUID
    platform: str = Field(min_length=3, max_length=30)
    account_handle: str = Field(min_length=1, max_length=200)
    purpose: str = "main"
    display_name: str | None = None
    persona: dict | None = None
    geo_country: str | None = Field(None, min_length=2, max_length=2)
    geo_timezone: str | None = None


class SocialAccountOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    platform: str
    purpose: str
    account_handle: str
    display_name: str | None
    status: str
    health_score: int
    browser_profile_id: uuid.UUID | None
    proxy_pool_id: uuid.UUID | None
    geo_country: str | None
    geo_timezone: str | None
    daily_post_limit: int | None
    daily_follow_limit: int | None
    daily_like_limit: int | None
    last_login_at: datetime | None
    last_warning_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class BindIsolationIn(BaseModel):
    browser_profile_id: uuid.UUID | None = None
    proxy_pool_id: uuid.UUID | None = None
    geo_country: str | None = None
    geo_timezone: str | None = None


class BrowserProfileCreateIn(BaseModel):
    workspace_id: uuid.UUID
    profile_name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=2)
    timezone: str
    provider: str = "adspower"
    proxy_config: dict | None = None
    fingerprint_overrides: dict | None = None


class BrowserProfileOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    provider: str
    external_profile_id: str
    profile_name: str
    status: str
    fingerprint_summary: dict
    open_count: int
    last_opened_at: datetime | None
    last_closed_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class ProxyRegisterIn(BaseModel):
    workspace_id: uuid.UUID
    provider: str
    proxy_type: str
    country: str = Field(min_length=2, max_length=2)
    region: str | None = None
    city: str | None = None
    host: str | None = None
    port: int | None = None
    credentials_vault_id: uuid.UUID | None = None
    monthly_quota_gb: int | None = None


class ProxyOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    provider: str
    proxy_type: str
    country: str
    region: str | None
    city: str | None
    host: str | None
    port: int | None
    monthly_quota_gb: int | None
    used_gb_this_month: float
    status: str
    consecutive_failures: int
    avg_latency_ms: int | None
    last_health_check_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class PostCreateIn(BaseModel):
    workspace_id: uuid.UUID
    original_title: str | None = None
    original_body: str | None = None
    original_media_asset_ids: list[uuid.UUID] | None = None
    platform_variants: dict | None = None
    approval_required: bool = False
    content_type: str | None = None
    target_industry: str | None = None
    ai_generated: bool = False
    ai_use_case: str | None = None


class PostOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    original_title: str | None
    original_body: str | None
    original_media_asset_ids: list[uuid.UUID]
    platform_variants: dict
    status: str
    approval_required: bool
    approved_at: datetime | None
    content_type: str | None
    ai_generated: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ScheduleCreateIn(BaseModel):
    workspace_id: uuid.UUID
    post_id: uuid.UUID
    social_account_id: uuid.UUID
    scheduled_at: datetime
    jitter_seconds: int = Field(default=0, ge=0, le=3600)


class ScheduleOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    post_id: uuid.UUID
    social_account_id: uuid.UUID
    scheduled_at: datetime
    actual_published_at: datetime | None
    jitter_seconds: int
    status: str
    platform_post_id: str | None
    platform_post_url: str | None
    error_message: str | None
    retry_count: int
    max_retries: int
    created_at: datetime

    class Config:
        from_attributes = True


class HealthEventOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    social_account_id: uuid.UUID
    event_type: str
    severity: str
    description: str | None
    triggered_by: str | None
    health_score_before: int | None
    health_score_after: int | None
    occurred_at: datetime

    class Config:
        from_attributes = True


# ─── Endpoints · Social Accounts ─────────────────────────────────────

@router.post("/accounts", response_model=SocialAccountOut, status_code=201)
async def create_account(
    payload: SocialAccountCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        account = await sm.create_social_account(
            db,
            workspace_id=payload.workspace_id,
            requester_user_id=p.user_id,
            platform=payload.platform,
            account_handle=payload.account_handle,
            purpose=payload.purpose,
            display_name=payload.display_name,
            persona=payload.persona,
            geo_country=payload.geo_country,
            geo_timezone=payload.geo_timezone,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    except sm.SocialMatrixError as e:
        raise _err(400, str(e)) from None
    await db.commit()
    return SocialAccountOut.model_validate(account)


@router.get("/accounts", response_model=list[SocialAccountOut])
async def list_accounts(
    workspace_id: uuid.UUID = Query(...),
    platform: str | None = Query(None),
    status: str | None = Query(None),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        rows = await sm.list_social_accounts(
            db,
            workspace_id=workspace_id,
            requester_user_id=p.user_id,
            platform=platform,
            status=status,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    return [SocialAccountOut.model_validate(r) for r in rows]


@router.post("/accounts/{account_id}/bind", response_model=SocialAccountOut)
async def bind_isolation(
    account_id: uuid.UUID,
    payload: BindIsolationIn,
    workspace_id: uuid.UUID = Query(...),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """绑定隔离三要素 · 三要素齐自动 active"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        account = await sm.bind_account_isolation(
            db,
            workspace_id=workspace_id,
            requester_user_id=p.user_id,
            social_account_id=account_id,
            browser_profile_id=payload.browser_profile_id,
            proxy_pool_id=payload.proxy_pool_id,
            geo_country=payload.geo_country,
            geo_timezone=payload.geo_timezone,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    except sm.SocialMatrixError as e:
        raise _err(400, str(e)) from None
    await db.commit()
    return SocialAccountOut.model_validate(account)


# ─── Endpoints · Browser Profiles ────────────────────────────────────

@router.post("/browsers", response_model=BrowserProfileOut, status_code=201)
async def create_browser(
    payload: BrowserProfileCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        profile = await sm.create_browser_profile(
            db,
            workspace_id=payload.workspace_id,
            requester_user_id=p.user_id,
            profile_name=payload.profile_name,
            country=payload.country,
            timezone=payload.timezone,
            proxy_config=payload.proxy_config,
            provider=payload.provider,
            fingerprint_overrides=payload.fingerprint_overrides,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    except sm.SocialMatrixError as e:
        raise _err(502, str(e)) from None  # AdsPower 调用失败
    await db.commit()
    return BrowserProfileOut.model_validate(profile)


# ─── Endpoints · Proxies ─────────────────────────────────────────────

@router.post("/proxies", response_model=ProxyOut, status_code=201)
async def register_proxy(
    payload: ProxyRegisterIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        proxy = await sm.register_proxy(
            db,
            workspace_id=payload.workspace_id,
            requester_user_id=p.user_id,
            provider=payload.provider,
            proxy_type=payload.proxy_type,
            country=payload.country,
            region=payload.region,
            city=payload.city,
            host=payload.host,
            port=payload.port,
            credentials_vault_id=payload.credentials_vault_id,
            monthly_quota_gb=payload.monthly_quota_gb,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    await db.commit()
    return ProxyOut.model_validate(proxy)


# ─── Endpoints · Posts ───────────────────────────────────────────────

@router.post("/posts", response_model=PostOut, status_code=201)
async def create_post(
    payload: PostCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        post = await sm.create_post(
            db,
            workspace_id=payload.workspace_id,
            requester_user_id=p.user_id,
            original_title=payload.original_title,
            original_body=payload.original_body,
            original_media_asset_ids=payload.original_media_asset_ids,
            platform_variants=payload.platform_variants,
            approval_required=payload.approval_required,
            content_type=payload.content_type,
            target_industry=payload.target_industry,
            ai_generated=payload.ai_generated,
            ai_use_case=payload.ai_use_case,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    await db.commit()
    return PostOut.model_validate(post)


# ─── Endpoints · Schedules ───────────────────────────────────────────

@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def schedule_post(
    payload: ScheduleCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        schedule = await sm.schedule_post(
            db,
            workspace_id=payload.workspace_id,
            requester_user_id=p.user_id,
            post_id=payload.post_id,
            social_account_id=payload.social_account_id,
            scheduled_at=payload.scheduled_at,
            jitter_seconds=payload.jitter_seconds,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    except sm.SocialMatrixError as e:
        raise _err(400, str(e)) from None
    await db.commit()
    return ScheduleOut.model_validate(schedule)


# ─── Endpoints · Health Events ───────────────────────────────────────

@router.get("/health", response_model=list[HealthEventOut])
async def list_health(
    workspace_id: uuid.UUID = Query(...),
    social_account_id: uuid.UUID | None = Query(None),
    severity_min: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        rows = await sm.list_health_events(
            db,
            workspace_id=workspace_id,
            requester_user_id=p.user_id,
            social_account_id=social_account_id,
            severity_min=severity_min,
            limit=limit,
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    return [HealthEventOut.model_validate(r) for r in rows]
