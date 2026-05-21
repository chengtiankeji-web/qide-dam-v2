"""Reddit API 客户端 · OAuth2 application-only flow

文档：https://www.reddit.com/dev/api
免费层：60 请求/分钟 · 用 OAuth token 才能拿到这个 quota（不登 OAuth 只能 10/min）

环境变量需要：
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  REDDIT_USER_AGENT   · 推荐格式: "QideMatrix/0.1 (by /u/your_reddit_username)"

注册方式：
  1. 用 Reddit 账号登录 https://www.reddit.com/prefs/apps
  2. 拉到底点"create app" → 选 "script" 类型
  3. name=QideMatrix · redirect=http://localhost:8000 · 拿 client_id（14 char）+ secret
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class RedditError(Exception):
    pass


@dataclass
class RedditPost:
    """简化的 Reddit 帖子结构"""
    external_id: str
    url: str
    title: str
    body: str
    author: str
    score: int
    num_comments: int
    posted_at_ts: int  # Unix timestamp
    top_comments: list[dict]  # [{author, body, score}]


class RedditClient:
    """Reddit API 异步客户端 · application-only OAuth2"""

    AUTH_URL = "https://www.reddit.com/api/v1/access_token"
    BASE_URL = "https://oauth.reddit.com"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.client_id = client_id or getattr(settings, "REDDIT_CLIENT_ID", "")
        self.client_secret = client_secret or getattr(settings, "REDDIT_CLIENT_SECRET", "")
        self.user_agent = user_agent or getattr(
            settings,
            "REDDIT_USER_AGENT",
            "QideMatrix/0.1 (B2B topic monitor)",
        )

        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._http: httpx.AsyncClient | None = None

    def _ensure_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise RedditError(
                "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set · "
                "register app at reddit.com/prefs/apps"
            )

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _get_token(self) -> str:
        """OAuth2 client_credentials flow · 自动缓存 + 续期"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        self._ensure_credentials()
        client = await self._client()
        resp = await client.post(
            self.AUTH_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            headers={"User-Agent": self.user_agent},
        )
        if resp.status_code != 200:
            raise RedditError(f"Auth failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = now + data.get("expires_in", 3600)
        return self._access_token

    async def _api_get(self, path: str, **params) -> dict:
        token = await self._get_token()
        client = await self._client()
        resp = await client.get(
            f"{self.BASE_URL}{path}",
            params=params,
            headers={
                "Authorization": f"bearer {token}",
                "User-Agent": self.user_agent,
            },
        )
        if resp.status_code == 429:
            # Reddit rate limit · respect Retry-After
            wait = int(resp.headers.get("Retry-After", "60"))
            logger.warning("reddit.rate_limited", wait_seconds=wait)
            await asyncio.sleep(min(wait, 60))
            return await self._api_get(path, **params)
        if resp.status_code != 200:
            raise RedditError(f"GET {path} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def fetch_top_posts(
        self,
        subreddit: str,
        *,
        limit: int = 20,
        time_filter: str = "day",  # hour / day / week / month / year / all
    ) -> list[RedditPost]:
        """抓 subreddit 的 top 帖子

        time_filter='day' = 过去 24h · 适合每日监测
        """
        data = await self._api_get(
            f"/r/{subreddit}/top",
            limit=limit,
            t=time_filter,
        )
        children = data.get("data", {}).get("children", [])
        posts: list[RedditPost] = []
        for child in children:
            d = child.get("data", {})
            posts.append(
                RedditPost(
                    external_id=d.get("id", ""),
                    url=f"https://www.reddit.com{d.get('permalink', '')}",
                    title=d.get("title", "")[:500],
                    body=(d.get("selftext") or "")[:5000],  # cap at 5k chars
                    author=d.get("author", "deleted"),
                    score=int(d.get("score", 0)),
                    num_comments=int(d.get("num_comments", 0)),
                    posted_at_ts=int(d.get("created_utc", 0)),
                    top_comments=[],  # filled by fetch_comments
                )
            )
        return posts

    async def fetch_comments(
        self, subreddit: str, post_id: str, *, limit: int = 10
    ) -> list[dict]:
        """抓某帖子 top N 评论"""
        data = await self._api_get(
            f"/r/{subreddit}/comments/{post_id}",
            limit=limit,
            sort="top",
            depth=1,
        )
        # response is [listing(post), listing(comments)]
        if not isinstance(data, list) or len(data) < 2:
            return []
        comments_listing = data[1].get("data", {}).get("children", [])
        out: list[dict] = []
        for child in comments_listing[:limit]:
            kind = child.get("kind")
            if kind != "t1":  # only real comments
                continue
            d = child.get("data", {})
            out.append({
                "author": d.get("author", "deleted"),
                "body": (d.get("body") or "")[:2000],
                "score": int(d.get("score", 0)),
            })
        return out

    async def fetch_subreddit_full(
        self,
        subreddit: str,
        *,
        posts_limit: int = 20,
        comments_limit: int = 10,
        time_filter: str = "day",
    ) -> list[RedditPost]:
        """一次性抓 subreddit 帖子 + 每帖的 top 评论

        关键设计：post 抓回来后用 asyncio.gather 并发拉评论 ·
                  但限制并发数 = 5 防 Reddit 限频
        """
        posts = await self.fetch_top_posts(
            subreddit, limit=posts_limit, time_filter=time_filter
        )
        sem = asyncio.Semaphore(5)

        async def fetch_one_comments(post: RedditPost) -> None:
            async with sem:
                try:
                    post.top_comments = await self.fetch_comments(
                        subreddit, post.external_id, limit=comments_limit
                    )
                except RedditError as e:
                    logger.warning(
                        "reddit.comments.failed",
                        post_id=post.external_id, error=str(e)[:100],
                    )
                    post.top_comments = []

        await asyncio.gather(*[fetch_one_comments(p) for p in posts])
        return posts

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ─── 工厂方法 ────────────────────────────────────────────────────────

_default_client: RedditClient | None = None


def get_reddit_client() -> RedditClient:
    global _default_client
    if _default_client is None:
        _default_client = RedditClient()
    return _default_client
