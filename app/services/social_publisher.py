"""Social Matrix v2 · 多平台发帖统一接口

5 个平台的 client class · 统一签名：
    PublisherClient.publish(account, content, asset_urls, link_url) -> PublishResult

返回 PublishResult 标准化结构供调用方 (REST / Celery) 写回 social_posts。

平台覆盖：
  - LinkedInClient        · Pages + Personal · /v2/posts (UGC API)
  - MetaPageClient        · FB Page · /v22.0/{page-id}/feed
  - InstagramBusinessClient · IG Business · 双步：container → publish
  - TikTokBusinessClient  · /v2/content/post/inbox/video/init/
  - YouTubeClient         · video.insert (resumable upload)
  - XClient               · /2/tweets

⚠️ v4.0 占位实现：返 fake PublishResult · 真 HTTP 调用在 v4.1 接入
⚠️ token refresh：每个 client 进 publish 时先 check expires_at · 过期前 5 分钟自动 refresh

测试：tests/social/test_social_publisher.py
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.logging import get_logger
from app.models.social import SocialAccount

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# Result schema
# ════════════════════════════════════════════════════════════

@dataclass
class PublishResult:
    success: bool
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_response: dict[str, Any] | None = None
    published_at: datetime | None = None


@dataclass
class PublishRequest:
    account: SocialAccount
    token: dict[str, Any]      # 解密后的 token payload
    content_text: str
    asset_urls: list[str]      # R2 signed URLs · 或 absolute https
    asset_mime_types: list[str]
    link_url: str | None = None
    language: str = "en"


# ════════════════════════════════════════════════════════════
# Base client
# ════════════════════════════════════════════════════════════

class PublisherClient(ABC):
    """所有平台 client 共享接口"""

    platform: str = ""
    timeout_seconds: float = 30.0

    @abstractmethod
    async def publish(self, req: PublishRequest) -> PublishResult:
        ...

    async def _post_json(
        self,
        url: str,
        *,
        token: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """通用 POST JSON · 带 Authorization Bearer header"""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            return await client.post(url, json=payload, headers=headers)


# ════════════════════════════════════════════════════════════
# 1. LinkedIn (Pages + Personal)
# ════════════════════════════════════════════════════════════

class LinkedInClient(PublisherClient):
    platform = "linkedin"
    base_url = "https://api.linkedin.com"

    async def publish(self, req: PublishRequest) -> PublishResult:
        # v4.0 占位
        if not req.token.get("access_token"):
            return PublishResult(
                success=False,
                error_code="missing_token",
                error_message="LinkedIn access_token absent",
            )

        # v4.1 真实装：
        # POST /v2/posts with author URN + commentary + media
        # 多图：先 POST /rest/images?action=initializeUpload 上传 · 再引用 URN
        # urn 格式：urn:li:share:{shareId}

        logger.info(
            "publish.linkedin.placeholder",
            account_id=str(req.account.id),
            chars=len(req.content_text),
            assets=len(req.asset_urls),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="LinkedIn publish v4.1 待接入·v4.0 placeholder",
        )


# ════════════════════════════════════════════════════════════
# 2. Meta (Facebook Page)
# ════════════════════════════════════════════════════════════

class MetaPageClient(PublisherClient):
    platform = "meta-page"
    base_url = "https://graph.facebook.com/v22.0"

    async def publish(self, req: PublishRequest) -> PublishResult:
        if not req.token.get("page_access_token"):
            return PublishResult(
                success=False,
                error_code="missing_page_token",
                error_message="Meta page_access_token required (not user token)",
            )

        # v4.1：POST /{page-id}/feed with message + link
        # 多图：先 /{page-id}/photos 上传拿 photo_id · feed 引用 attached_media

        logger.info(
            "publish.meta.placeholder",
            account_id=str(req.account.id), chars=len(req.content_text),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="Meta publish v4.1 待接入",
        )


# ════════════════════════════════════════════════════════════
# 3. Instagram Business
# ════════════════════════════════════════════════════════════

class InstagramBusinessClient(PublisherClient):
    platform = "instagram-business"
    base_url = "https://graph.facebook.com/v22.0"

    async def publish(self, req: PublishRequest) -> PublishResult:
        if not req.asset_urls:
            return PublishResult(
                success=False,
                error_code="missing_media",
                error_message="Instagram requires at least 1 image / video",
            )

        # v4.1：双步
        # 1) POST /{ig-user-id}/media with image_url + caption  → 拿 creation_id
        # 2) POST /{ig-user-id}/media_publish with creation_id  → 真发布

        logger.info(
            "publish.instagram.placeholder",
            account_id=str(req.account.id), assets=len(req.asset_urls),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="Instagram publish v4.1 待接入",
        )


# ════════════════════════════════════════════════════════════
# 4. TikTok Business
# ════════════════════════════════════════════════════════════

class TikTokBusinessClient(PublisherClient):
    platform = "tiktok-business"
    base_url = "https://business-api.tiktok.com"

    async def publish(self, req: PublishRequest) -> PublishResult:
        # 仅支持视频
        if not req.asset_urls or "video" not in (req.asset_mime_types[0] if req.asset_mime_types else ""):
            return PublishResult(
                success=False,
                error_code="missing_video",
                error_message="TikTok requires 1 video asset",
            )

        # v4.1：POST /v2/content/post/inbox/video/init/
        # 分片上传 · 然后 POST publish

        logger.info(
            "publish.tiktok.placeholder",
            account_id=str(req.account.id),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="TikTok publish v4.1 待接入",
        )


# ════════════════════════════════════════════════════════════
# 5. X (Twitter)
# ════════════════════════════════════════════════════════════

class XClient(PublisherClient):
    platform = "x"
    base_url = "https://api.twitter.com"

    async def publish(self, req: PublishRequest) -> PublishResult:
        if len(req.content_text) > 280 and not req.account.metrics.get("x_premium", False):
            return PublishResult(
                success=False,
                error_code="too_long",
                error_message=f"X free tier max 280 chars · got {len(req.content_text)}",
            )

        # v4.1：POST /2/tweets with text + media_ids
        # 媒体先走 /1.1/media/upload.json (旧 API · 仍是 ulpoad 唯一路径)

        logger.info(
            "publish.x.placeholder",
            account_id=str(req.account.id), chars=len(req.content_text),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="X publish v4.1 待接入",
        )


# ════════════════════════════════════════════════════════════
# 6. YouTube (video upload)
# ════════════════════════════════════════════════════════════

class YouTubeClient(PublisherClient):
    platform = "youtube"
    base_url = "https://www.googleapis.com"

    async def publish(self, req: PublishRequest) -> PublishResult:
        if not req.asset_urls:
            return PublishResult(
                success=False,
                error_code="missing_video",
                error_message="YouTube requires 1 video asset",
            )

        # v4.1：resumable upload 协议
        # 1) POST /upload/youtube/v3/videos?uploadType=resumable → 拿 upload URL
        # 2) PUT 数据到 upload URL（支持 chunked）

        logger.info(
            "publish.youtube.placeholder",
            account_id=str(req.account.id),
        )
        return PublishResult(
            success=False,
            error_code="not_implemented",
            error_message="YouTube publish v4.1 待接入",
        )


# ════════════════════════════════════════════════════════════
# 7. Factory · 按 platform 派遣 client
# ════════════════════════════════════════════════════════════

_REGISTRY: dict[str, type[PublisherClient]] = {
    "linkedin": LinkedInClient,
    "linkedin-personal": LinkedInClient,
    "meta-page": MetaPageClient,
    "instagram-business": InstagramBusinessClient,
    "tiktok-business": TikTokBusinessClient,
    "x": XClient,
    "youtube": YouTubeClient,
}


def get_publisher(platform: str) -> PublisherClient:
    cls = _REGISTRY.get(platform)
    if cls is None:
        raise ValueError(f"unsupported platform: {platform!r}")
    return cls()


async def publish_post(
    *,
    account: SocialAccount,
    token: dict[str, Any],
    content_text: str,
    asset_urls: list[str],
    asset_mime_types: list[str] | None = None,
    link_url: str | None = None,
    language: str = "en",
) -> PublishResult:
    """统一发帖入口·调对应 platform 的 client"""
    client = get_publisher(account.platform)
    req = PublishRequest(
        account=account,
        token=token,
        content_text=content_text,
        asset_urls=asset_urls,
        asset_mime_types=asset_mime_types or [],
        link_url=link_url,
        language=language,
    )
    try:
        result = await client.publish(req)
        if result.success and result.published_at is None:
            result.published_at = datetime.now(timezone.utc)
        return result
    except httpx.HTTPError as exc:
        logger.error(
            "publish.http_error",
            platform=account.platform, account_id=str(account.id), error=str(exc),
        )
        return PublishResult(
            success=False,
            error_code="http_error",
            error_message=str(exc)[:500],
        )


__all__ = [
    "InstagramBusinessClient",
    "LinkedInClient",
    "MetaPageClient",
    "PublishRequest",
    "PublishResult",
    "PublisherClient",
    "TikTokBusinessClient",
    "XClient",
    "YouTubeClient",
    "get_publisher",
    "publish_post",
]
