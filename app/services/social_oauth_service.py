"""Social Matrix v2 · OAuth 2.0 流程封装

5 平台共用：authorize → user redirect to platform → callback → exchange code for token → store cred

平台差异封装在 _PLATFORM_CONFIG dict · 加新平台只改一个表

环境变量（5 个 Developer App 一组）：
  LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
  META_APP_ID / META_APP_SECRET
  TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET
  X_CLIENT_ID / X_CLIENT_SECRET
  GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET

State 验证：authorize 阶段写 redis · callback 阶段验·防 CSRF
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.deps import Principal
from app.core.logging import get_logger
from app.services.social_credential_service import store_credential

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# 平台配置 · OAuth 端点 + scope
# ════════════════════════════════════════════════════════════

_PLATFORM_CONFIG = {
    "linkedin": {
        "auth_url": "https://www.linkedin.com/oauth/v2/authorization",
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "default_scopes": ["openid", "profile", "w_member_social", "email"],
        "client_id_env": "LINKEDIN_CLIENT_ID",
        "client_secret_env": "LINKEDIN_CLIENT_SECRET",
        "response_type": "code",
    },
    "meta": {
        "auth_url": "https://www.facebook.com/v22.0/dialog/oauth",
        "token_url": "https://graph.facebook.com/v22.0/oauth/access_token",
        "default_scopes": [
            "pages_manage_posts",
            "pages_read_engagement",
            "pages_show_list",
            "business_management",
            "instagram_basic",
            "instagram_content_publish",
        ],
        "client_id_env": "META_APP_ID",
        "client_secret_env": "META_APP_SECRET",
        "response_type": "code",
    },
    "tiktok": {
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "default_scopes": ["user.info.basic", "video.publish"],
        "client_id_env": "TIKTOK_CLIENT_KEY",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
        "response_type": "code",
        "client_key_param": "client_key",  # TikTok 用 client_key 不 client_id
    },
    "x": {
        "auth_url": "https://twitter.com/i/oauth2/authorize",
        "token_url": "https://api.twitter.com/2/oauth2/token",
        "default_scopes": ["tweet.read", "tweet.write", "users.read", "offline.access"],
        "client_id_env": "X_CLIENT_ID",
        "client_secret_env": "X_CLIENT_SECRET",
        "response_type": "code",
        "requires_pkce": True,
    },
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "default_scopes": [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    },
}


def _get_redirect_uri(platform: str) -> str:
    """每个平台一个固定 callback URL · 入 Developer App 配置"""
    base = getattr(settings, "SOCIAL_OAUTH_REDIRECT_BASE", None) \
        or "https://dam-api.qidelinktech.com/v1/social/oauth"
    return f"{base.rstrip('/')}/{platform}/callback"


def _get_client_credentials(platform: str) -> tuple[str, str]:
    cfg = _PLATFORM_CONFIG.get(platform)
    if not cfg:
        raise ValueError(f"unsupported platform: {platform!r}")
    cid = getattr(settings, cfg["client_id_env"], None)
    csecret = getattr(settings, cfg["client_secret_env"], None)
    if not cid or not csecret:
        raise ValueError(
            f"missing env: {cfg['client_id_env']} / {cfg['client_secret_env']}"
        )
    return cid, csecret


# ════════════════════════════════════════════════════════════
# 1. 构造 authorize URL（user 浏览器跳转）
# ════════════════════════════════════════════════════════════

def build_authorize_url(
    *,
    platform: str,
    factory_slug: str,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    initiated_by_user_id: Optional[uuid.UUID] = None,
    extra_scopes: Optional[list[str]] = None,
) -> tuple[str, str]:
    """返 (url, state) · state 调用方应该写 Redis with TTL 10min 防 CSRF"""
    cfg = _PLATFORM_CONFIG.get(platform)
    if not cfg:
        raise ValueError(f"unsupported platform: {platform!r}")

    client_id, _ = _get_client_credentials(platform)
    state = secrets.token_urlsafe(32)

    scopes = list(cfg["default_scopes"])
    if extra_scopes:
        scopes.extend(extra_scopes)

    params: dict[str, Any] = {
        cfg.get("client_key_param", "client_id"): client_id,
        "response_type": cfg["response_type"],
        "redirect_uri": _get_redirect_uri(platform),
        "scope": " ".join(scopes),
        "state": state,
    }
    if "access_type" in cfg:
        params["access_type"] = cfg["access_type"]
    if "prompt" in cfg:
        params["prompt"] = cfg["prompt"]

    # PKCE for X
    code_verifier = None
    if cfg.get("requires_pkce"):
        code_verifier = secrets.token_urlsafe(64)
        import base64
        import hashlib
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode().rstrip("=")
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    url = f"{cfg['auth_url']}?{urlencode(params)}"

    # state 应该包含 factory_slug + tenant + project · 用 JSON+随机 token 联合
    # 简化：state 形式 "tenant:project:factory:random"
    composite_state = f"{tenant_id}:{project_id}:{factory_slug}:{state}"
    if code_verifier:
        composite_state += f":{code_verifier}"

    logger.info(
        "oauth.authorize_url_built",
        platform=platform, factory=factory_slug, tenant=str(tenant_id),
    )
    return url, composite_state


# ════════════════════════════════════════════════════════════
# 2. callback · 交换 code for token
# ════════════════════════════════════════════════════════════

async def exchange_code_for_token(
    *,
    platform: str,
    code: str,
    code_verifier: Optional[str] = None,
) -> dict[str, Any]:
    """callback 收到 code 后调·返 token_payload dict"""
    cfg = _PLATFORM_CONFIG.get(platform)
    if not cfg:
        raise ValueError(f"unsupported platform: {platform!r}")

    client_id, client_secret = _get_client_credentials(platform)

    payload = {
        cfg.get("client_key_param", "client_id"): client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": _get_redirect_uri(platform),
    }
    if code_verifier and cfg.get("requires_pkce"):
        payload["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            cfg["token_url"],
            data=payload,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            logger.error(
                "oauth.token_exchange_failed",
                platform=platform, status=resp.status_code, body=resp.text[:500],
            )
            raise ValueError(
                f"token exchange failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()


def parse_expires_at(token_response: dict[str, Any]) -> Optional[datetime]:
    """从 token response 解析 expires_at · platform 各家不一"""
    expires_in = token_response.get("expires_in")
    if expires_in:
        try:
            return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            pass
    # LinkedIn 还会同时返 refresh_token_expires_in
    return None


# ════════════════════════════════════════════════════════════
# 3. 完整 callback 处理（高层封装）
# ════════════════════════════════════════════════════════════

async def handle_callback(
    db,
    *,
    principal: Principal,
    platform: str,
    code: str,
    state: str,
    code_verifier: Optional[str] = None,
    request=None,
) -> dict[str, Any]:
    """callback 端点直接调这个·完成 token 存储

    返：{"credential_id": "...", "platform": "...", "scopes": "..."}
    """
    token_payload = await exchange_code_for_token(
        platform=platform, code=code, code_verifier=code_verifier,
    )

    # state 形式：tenant:project:factory:random[:pkce]
    parts = state.split(":")
    if len(parts) < 4:
        raise ValueError(f"malformed state: {state[:40]}...")
    tenant_id = uuid.UUID(parts[0])
    # project_id = uuid.UUID(parts[1])  # 暂存 caller 自己 use
    # factory_slug = parts[2]

    # 安全：principal.tenant_id 与 state 中 tenant_id 必须一致
    if principal.tenant_id != tenant_id:
        raise ValueError("tenant_id mismatch between principal and state")

    expires_at = parse_expires_at(token_payload)
    scopes = token_payload.get("scope") or token_payload.get("scopes") or ""

    cred = await store_credential(
        db,
        principal=principal,
        tenant_id=tenant_id,
        platform=platform,
        credential_type="oauth2",
        token_payload=token_payload,
        expires_at=expires_at,
        scopes=scopes if isinstance(scopes, str) else " ".join(scopes),
        request=request,
    )

    return {
        "credential_id": str(cred.id),
        "platform": platform,
        "scopes": scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


__all__ = [
    "build_authorize_url",
    "exchange_code_for_token",
    "handle_callback",
    "parse_expires_at",
]
