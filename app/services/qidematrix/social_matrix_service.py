"""QideMatrix 社媒矩阵业务逻辑

职责：
- 账号 CRUD + 隔离三要素绑定（浏览器 + 代理 + 地理）
- 浏览器环境管理（封装 AdsPower client）
- 代理 IP 池管理 + 健康检查
- 内容池 + 跨平台改写 + 发布调度
- 健康度事件记录（不可改）
- 风控配额检查 (每日发帖 / 关注 / 点赞 上限)
"""
from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qidematrix import (
    QmAccountHealthEvent,
    QmBrowserProfile,
    QmPostSchedule,
    QmProxyPool,
    QmSocialAccount,
    QmSocialPost,
)
from app.services.qidematrix.adspower_client import (
    AdsPowerClient,
    AdsPowerError,
    get_adspower_client,
)
from app.services.qidematrix.workspace_service import (
    WorkspaceNotFound,
    WorkspacePermissionDenied,
    get_workspace_for_user,
)


class SocialMatrixError(Exception):
    pass


# ─── Platform 默认配额（与 alembic 015 种子数据保持一致） ───────────────

DEFAULT_PLATFORM_QUOTAS: dict[str, dict[str, int]] = {
    "linkedin_company":   {"posts": 3, "follows": 0, "likes": 50},
    "linkedin_personal":  {"posts": 2, "follows": 100, "likes": 100},
    "tiktok_business":    {"posts": 5, "follows": 50, "likes": 200},
    "tiktok_creator":     {"posts": 10, "follows": 200, "likes": 500},
    "instagram_business": {"posts": 5, "follows": 50, "likes": 150},
    "instagram_creator":  {"posts": 8, "follows": 100, "likes": 300},
    "facebook_page":      {"posts": 10, "follows": 0, "likes": 100},
    "x_twitter":          {"posts": 20, "follows": 50, "likes": 500},
    "youtube_channel":    {"posts": 2, "follows": 50, "likes": 100},
}


# ─── Social Account CRUD ─────────────────────────────────────────────

async def create_social_account(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    platform: str,
    account_handle: str,
    purpose: str = "main",
    display_name: str | None = None,
    persona: dict | None = None,
    geo_country: str | None = None,
    geo_timezone: str | None = None,
) -> QmSocialAccount:
    """创建社媒账号档案 · 必须 admin / owner

    隔离三要素（browser_profile + proxy_pool + geo）后续单独绑定
    """
    await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin"),
    )

    now = datetime.now(UTC)
    quotas = DEFAULT_PLATFORM_QUOTAS.get(platform, {"posts": 5, "follows": 50, "likes": 100})

    account = QmSocialAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        platform=platform,
        purpose=purpose,
        account_handle=account_handle,
        display_name=display_name,
        persona=persona or {},
        geo_country=geo_country,
        geo_timezone=geo_timezone,
        status="pending_setup",  # 三要素全绑齐才能 active
        health_score=100,
        daily_post_limit=quotas["posts"],
        daily_follow_limit=quotas["follows"],
        daily_like_limit=quotas["likes"],
        extra_metadata={},
        created_at=now,
        updated_at=now,
    )
    db.add(account)
    await db.flush()
    return account


async def list_social_accounts(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    platform: str | None = None,
    status: str | None = None,
) -> list[QmSocialAccount]:
    await get_workspace_for_user(
        db, workspace_id=workspace_id, user_id=requester_user_id
    )
    stmt = (
        select(QmSocialAccount)
        .where(
            QmSocialAccount.workspace_id == workspace_id,
            QmSocialAccount.deleted_at.is_(None),
        )
        .order_by(QmSocialAccount.created_at.desc())
    )
    if platform:
        stmt = stmt.where(QmSocialAccount.platform == platform)
    if status:
        stmt = stmt.where(QmSocialAccount.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


# ─── Browser Profile 绑定 ────────────────────────────────────────────

async def create_browser_profile(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    profile_name: str,
    country: str,
    timezone: str,
    proxy_config: dict | None = None,
    provider: str = "adspower",
    fingerprint_overrides: dict | None = None,
) -> QmBrowserProfile:
    """创建浏览器配置 · 调 AdsPower API 实际生成 + 落本地记录"""
    await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin"),
    )

    client: AdsPowerClient = get_adspower_client()
    try:
        result = await client.create_profile(
            profile_name=profile_name,
            country=country,
            timezone=timezone,
            proxy_config=proxy_config,
            fingerprint_overrides=fingerprint_overrides,
        )
    except AdsPowerError as e:
        raise SocialMatrixError(f"AdsPower create_profile failed: {e}") from None

    now = datetime.now(UTC)
    profile = QmBrowserProfile(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        provider=provider,
        external_profile_id=result["external_profile_id"],
        profile_name=profile_name,
        fingerprint_summary=result.get("fingerprint_summary", {}),
        status="idle",
        open_count=0,
        extra_metadata={},
        created_at=now,
        updated_at=now,
    )
    db.add(profile)
    await db.flush()
    return profile


async def bind_account_isolation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    social_account_id: uuid.UUID,
    browser_profile_id: uuid.UUID | None = None,
    proxy_pool_id: uuid.UUID | None = None,
    geo_country: str | None = None,
    geo_timezone: str | None = None,
) -> QmSocialAccount:
    """绑定账号的隔离三要素（浏览器 + 代理 + 地理）

    三要素齐 → status 从 pending_setup 翻 active
    """
    _, _ = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin"),
    )

    account = (
        await db.execute(
            select(QmSocialAccount).where(
                QmSocialAccount.id == social_account_id,
                QmSocialAccount.workspace_id == workspace_id,
                QmSocialAccount.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not account:
        raise SocialMatrixError("account not found")

    if browser_profile_id is not None:
        account.browser_profile_id = browser_profile_id
    if proxy_pool_id is not None:
        account.proxy_pool_id = proxy_pool_id
    if geo_country is not None:
        account.geo_country = geo_country
    if geo_timezone is not None:
        account.geo_timezone = geo_timezone

    # 三要素齐 → active
    if (
        account.browser_profile_id
        and account.proxy_pool_id
        and account.geo_country
        and account.geo_timezone
        and account.status == "pending_setup"
    ):
        account.status = "active"

    account.updated_at = datetime.now(UTC)
    await db.flush()
    return account


# ─── Proxy Pool ──────────────────────────────────────────────────────

async def register_proxy(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    provider: str,
    proxy_type: str,
    country: str,
    region: str | None = None,
    city: str | None = None,
    host: str | None = None,
    port: int | None = None,
    credentials_vault_id: uuid.UUID | None = None,
    monthly_quota_gb: int | None = None,
) -> QmProxyPool:
    """注册代理 IP · 凭证用 Vault 加密"""
    await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin"),
    )
    now = datetime.now(UTC)
    proxy = QmProxyPool(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        provider=provider,
        proxy_type=proxy_type,
        country=country,
        region=region,
        city=city,
        host=host,
        port=port,
        credentials_vault_id=credentials_vault_id,
        monthly_quota_gb=monthly_quota_gb,
        used_gb_this_month=0.0,
        status="available",
        consecutive_failures=0,
        extra_metadata={},
        created_at=now,
        updated_at=now,
    )
    db.add(proxy)
    await db.flush()
    return proxy


async def pick_available_proxy(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    country: str,
    proxy_type: str = "residential",
) -> QmProxyPool | None:
    """智能选 available 代理 · 优先：地理一致 + 失败次数少 + 流量充裕"""
    stmt = (
        select(QmProxyPool)
        .where(
            QmProxyPool.workspace_id == workspace_id,
            QmProxyPool.status == "available",
            QmProxyPool.country == country,
            QmProxyPool.proxy_type == proxy_type,
        )
        .order_by(
            QmProxyPool.consecutive_failures,
            QmProxyPool.used_gb_this_month,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ─── Posts + Schedules ────────────────────────────────────────────────

async def create_post(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    original_title: str | None,
    original_body: str | None,
    original_media_asset_ids: list[uuid.UUID] | None = None,
    platform_variants: dict | None = None,
    approval_required: bool = False,
    content_type: str | None = None,
    target_industry: str | None = None,
    ai_generated: bool = False,
    ai_use_case: str | None = None,
) -> QmSocialPost:
    """创建内容草稿 · 任意 workspace 成员可创建（owner/admin/member）"""
    _, member = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin", "member"),
    )
    now = datetime.now(UTC)
    post = QmSocialPost(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        original_title=original_title,
        original_body=original_body,
        original_media_asset_ids=original_media_asset_ids or [],
        platform_variants=platform_variants or {},
        status="pending_approval" if approval_required else "draft",
        approval_required=approval_required,
        content_type=content_type,
        target_industry=target_industry,
        ai_generated=ai_generated,
        ai_use_case=ai_use_case,
        created_by_user_id=requester_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(post)
    await db.flush()
    return post


async def schedule_post(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    post_id: uuid.UUID,
    social_account_id: uuid.UUID,
    scheduled_at: datetime,
    jitter_seconds: int = 0,
) -> QmPostSchedule:
    """调度发布 · jitter_seconds = 随机偏移上限 · 防 bot 模式

    例：scheduled_at = 10:00 · jitter_seconds = 1800 → 实际发布在 9:45-10:30 之间
    """
    await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin", "member"),
    )

    # 验 post 和 account 都属于 workspace
    post = (
        await db.execute(
            select(QmSocialPost).where(
                QmSocialPost.id == post_id,
                QmSocialPost.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if not post:
        raise SocialMatrixError("post not found")
    if post.approval_required and not post.approved_at:
        raise SocialMatrixError("post requires approval first")

    account = (
        await db.execute(
            select(QmSocialAccount).where(
                QmSocialAccount.id == social_account_id,
                QmSocialAccount.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if not account:
        raise SocialMatrixError("social account not found")
    if account.status != "active":
        raise SocialMatrixError(f"account status={account.status} · cannot schedule")

    # 检查当日发帖配额
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=UTC)
    today_count_q = (
        await db.execute(
            select(func.count(QmPostSchedule.id)).where(
                QmPostSchedule.social_account_id == social_account_id,
                QmPostSchedule.scheduled_at >= today_start,
                QmPostSchedule.status.in_(("pending", "publishing", "published")),
            )
        )
    )
    today_count = today_count_q.scalar() or 0
    if account.daily_post_limit and today_count >= account.daily_post_limit:
        raise SocialMatrixError(
            f"daily post limit {account.daily_post_limit} reached for this account"
        )

    now = datetime.now(UTC)
    schedule = QmPostSchedule(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        post_id=post_id,
        social_account_id=social_account_id,
        scheduled_at=scheduled_at,
        jitter_seconds=jitter_seconds,
        status="pending",
        retry_count=0,
        max_retries=3,
        extra_metadata={},
        created_at=now,
        updated_at=now,
    )
    db.add(schedule)
    await db.flush()
    return schedule


def calc_jittered_publish_time(
    scheduled_at: datetime, jitter_seconds: int
) -> datetime:
    """给 scheduled_at 加随机偏移 · 防 bot 检测的"准点发布"模式"""
    if jitter_seconds <= 0:
        return scheduled_at
    delta = random.randint(-jitter_seconds, jitter_seconds)
    return scheduled_at + timedelta(seconds=delta)


# ─── Health Events ───────────────────────────────────────────────────

async def record_health_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    social_account_id: uuid.UUID,
    event_type: str,
    severity: str = "info",
    description: str | None = None,
    proxy_pool_id: uuid.UUID | None = None,
    browser_profile_id: uuid.UUID | None = None,
    triggered_by: str | None = None,
    health_score_delta: int = 0,
    payload: dict | None = None,
) -> QmAccountHealthEvent:
    """记一条健康事件 · 自动同步调整 social_account.health_score

    event_type: login_success / login_failed / captcha_triggered / rate_limited /
                warning_received / suspended / restored / banned / unbanned /
                proxy_changed / browser_profile_swapped
    severity: info / warning / critical / fatal
    """
    now = datetime.now(UTC)

    # 拿 social_account 当前 health
    account = (
        await db.execute(
            select(QmSocialAccount).where(QmSocialAccount.id == social_account_id)
        )
    ).scalar_one_or_none()
    if not account:
        raise SocialMatrixError("account not found")

    health_before = account.health_score
    health_after = max(0, min(100, health_before + health_score_delta))

    event = QmAccountHealthEvent(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        social_account_id=social_account_id,
        event_type=event_type,
        severity=severity,
        description=description,
        proxy_pool_id=proxy_pool_id,
        browser_profile_id=browser_profile_id,
        triggered_by=triggered_by,
        health_score_before=health_before,
        health_score_after=health_after,
        payload=payload or {},
        occurred_at=now,
    )
    db.add(event)

    # 同步更 account · 严重事件触发状态自动降级
    account.health_score = health_after
    account.updated_at = now
    if event_type == "warning_received":
        account.last_warning_at = now
    if severity == "fatal":
        account.status = "banned"
    elif severity == "critical" and account.health_score < 30:
        account.status = "limited"

    await db.flush()
    return event


async def list_health_events(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    social_account_id: uuid.UUID | None = None,
    severity_min: str | None = None,
    limit: int = 100,
) -> list[QmAccountHealthEvent]:
    """看健康事件流 · 任意成员可看"""
    await get_workspace_for_user(
        db, workspace_id=workspace_id, user_id=requester_user_id
    )
    stmt = (
        select(QmAccountHealthEvent)
        .where(QmAccountHealthEvent.workspace_id == workspace_id)
        .order_by(QmAccountHealthEvent.occurred_at.desc())
        .limit(limit)
    )
    if social_account_id:
        stmt = stmt.where(QmAccountHealthEvent.social_account_id == social_account_id)
    if severity_min:
        order_map = {"info": 0, "warning": 1, "critical": 2, "fatal": 3}
        min_val = order_map.get(severity_min, 0)
        stmt = stmt.where(
            QmAccountHealthEvent.severity.in_(
                [k for k, v in order_map.items() if v >= min_val]
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)
