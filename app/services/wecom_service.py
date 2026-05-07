"""WeCom (企业微信) integration · v0.1
- access_token cache (Redis · 7200 sec validity)
- send_text / send_news (file card with link)
- callback decryption (WIP · Phase B 拿到 token+aeskey 后启用)

References:
- access_token: https://developer.work.weixin.qq.com/document/path/91039
- send message: https://developer.work.weixin.qq.com/document/path/90236
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

WECOM_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_CACHE_KEY = "wecom:access_token"
CONTACTS_CACHE_KEY = "wecom:contacts:v1"
CONTACTS_CACHE_TTL = 600  # 10 min

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def get_access_token() -> str:
    """Cache access_token in Redis (~2h TTL) and refresh on demand."""
    if not settings.WECOM_CORPID or not settings.WECOM_CORPSECRET:
        raise RuntimeError("WECOM_CORPID / WECOM_CORPSECRET 未配置 · 不能调企微 API")

    r = _get_redis()
    cached = await r.get(TOKEN_CACHE_KEY)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=15) as cli:
        resp = await cli.get(f"{WECOM_BASE}/gettoken", params={
            "corpid": settings.WECOM_CORPID,
            "corpsecret": settings.WECOM_CORPSECRET,
        })
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    token = data["access_token"]
    expires_in = data.get("expires_in", 7200)
    # Save with 60 sec safety margin
    await r.setex(TOKEN_CACHE_KEY, expires_in - 60, token)
    logger.info("wecom.token.refreshed", expires_in=expires_in)
    return token


async def send_text(touser: str, content: str) -> dict[str, Any]:
    """Send a text message to a single userid (or '@all' for all)."""
    token = await get_access_token()
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": int(settings.WECOM_AGENTID) if settings.WECOM_AGENTID else 0,
        "text": {"content": content},
        "safe": 0,
    }
    async with httpx.AsyncClient(timeout=15) as cli:
        resp = await cli.post(f"{WECOM_BASE}/message/send", params={"access_token": token}, json=payload)
    data = resp.json()
    if data.get("errcode") != 0:
        logger.warning("wecom.send_text.fail", to=touser, resp=data)
    return data


async def send_news(touser: str, articles: list[dict]) -> dict[str, Any]:
    """Send a news (rich card) message. articles=[{title, description, url, picurl}]"""
    token = await get_access_token()
    payload = {
        "touser": touser,
        "msgtype": "news",
        "agentid": int(settings.WECOM_AGENTID) if settings.WECOM_AGENTID else 0,
        "news": {"articles": articles[:8]},  # cap at 8 per API spec
        "safe": 0,
    }
    async with httpx.AsyncClient(timeout=15) as cli:
        resp = await cli.post(f"{WECOM_BASE}/message/send", params={"access_token": token}, json=payload)
    data = resp.json()
    if data.get("errcode") != 0:
        logger.warning("wecom.send_news.fail", to=touser, resp=data)
    return data


async def send_file_link(
    touser: str, *, filename: str, size_bytes: int, url: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Convenience helper · send a file as a clickable news card."""
    sz_mb = size_bytes / 1024 / 1024
    sz_str = f"{sz_mb:.1f} MB" if sz_mb >= 1 else f"{size_bytes / 1024:.0f} KB"
    desc = description or f"{sz_str} · 24 小时有效 · 来自 QideDAM"
    return await send_news(touser, [{
        "title": filename,
        "description": desc,
        "url": url,
        "picurl": "https://dam.qidelinktech.com/favicon.ico",  # placeholder · TODO custom icon
    }])


# ───────── 通讯录 ─────────

async def list_users(department_id: int = 1, fetch_child: int = 1) -> list[dict]:
    """List users in a department (default = 全企业 root department=1)."""
    r = _get_redis()
    cached = await r.get(CONTACTS_CACHE_KEY)
    if cached:
        return json.loads(cached)

    token = await get_access_token()
    async with httpx.AsyncClient(timeout=15) as cli:
        resp = await cli.get(f"{WECOM_BASE}/user/list", params={
            "access_token": token,
            "department_id": department_id,
            "fetch_child": fetch_child,
        })
    data = resp.json()
    if data.get("errcode") != 0:
        logger.warning("wecom.list_users.fail", resp=data)
        return []
    users = data.get("userlist", [])
    await r.setex(CONTACTS_CACHE_KEY, CONTACTS_CACHE_TTL, json.dumps(users, ensure_ascii=False))
    return users


async def resolve_user_by_name(hint: str) -> dict | None:
    """Fuzzy match a person by name hint. e.g. '刘总' / '张志刚' / 'Sam'.

    Strategy:
    1. exact name match
    2. substring match (Sam → 李佳佳 (Sam))
    3. surname + 称谓 match ('刘总' → 找姓刘的）
    """
    users = await list_users()
    if not users:
        return None

    h = hint.strip().lower()

    # 1. Exact match
    for u in users:
        if u.get("name", "").lower() == h or u.get("alias", "").lower() == h:
            return u

    # 2. Substring (英文别名常见)
    for u in users:
        nm = u.get("name", "")
        alias = u.get("alias", "") or ""
        if h in nm.lower() or h in alias.lower():
            return u

    # 3. 姓 + 称谓 解析 ("刘总" → 找姓刘 的高级别)
    suffixes = ("总", "总监", "经理", "老师", "工")
    for s in suffixes:
        if hint.endswith(s):
            surname = hint[:-len(s)]
            if surname:
                # 找以这个姓开头的
                cands = [u for u in users if u.get("name", "").startswith(surname)]
                if cands:
                    # 优先选 position 含相关词的
                    for u in cands:
                        pos = (u.get("position") or "").lower()
                        if any(k in pos for k in ["ceo", "cto", "cfo", "总", "vp", "总监"]):
                            return u
                    return cands[0]

    return None
