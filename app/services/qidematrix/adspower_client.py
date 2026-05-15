"""AdsPower API 抽象层

AdsPower 是国内市占率第一的浏览器指纹隔离工具 · 提供本地 HTTP API（默认 50325 端口）·
我们的后端不直接调（生产服务器没装 AdsPower）· 而是给客户的本地 AdsPower
颁发"代办令牌" · 客户的桌面 client 用这个令牌调本地 AdsPower API。

本模块定义抽象接口 · 实现两种 backend：
  1. AdsPowerLocalClient · 直连本地 50325 端口（开发 / dogfooding 用）
  2. AdsPowerNoopClient · 测试用 · 不真调

后续如换 Multilogin / Dolphin Anty · 只需新增 client class · 接口不变。

═══════════════════════════════════════════════════════════════════════
AdsPower API 文档参考：
═══════════════════════════════════════════════════════════════════════
官方 API: https://localapi-doc-en.adspower.com/

关键端点：
  POST /api/v1/user/create         · 创建浏览器配置
  GET  /api/v1/user/list           · 列配置
  GET  /api/v1/browser/start       · 启动浏览器（返 WebDriver URL）
  GET  /api/v1/browser/stop        · 关闭浏览器
  GET  /api/v1/browser/active      · 当前活跃浏览器列表
  POST /api/v1/user/update         · 修改配置（含代理 IP 切换）
  POST /api/v1/user/delete         · 删除配置
"""
from __future__ import annotations

import abc
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class AdsPowerError(Exception):
    """AdsPower API 异常"""


class AdsPowerClient(abc.ABC):
    """AdsPower / 类似指纹浏览器的抽象接口

    后端调这些方法时不直接接触 AdsPower · 而是返"指令"给桌面 client 执行 ·
    保证生产服务器不需装 AdsPower · 客户桌面装即可。
    """

    @abc.abstractmethod
    async def create_profile(
        self,
        *,
        profile_name: str,
        country: str,
        timezone: str,
        proxy_config: dict | None = None,
        fingerprint_overrides: dict | None = None,
    ) -> dict:
        """创建新浏览器配置 · 返 {external_profile_id, fingerprint_summary}"""

    @abc.abstractmethod
    async def list_profiles(self, *, page: int = 1, page_size: int = 100) -> list[dict]:
        """列已有配置 · 返 [{external_profile_id, name, last_open_time, ...}]"""

    @abc.abstractmethod
    async def start_browser(self, *, external_profile_id: str) -> dict:
        """启动浏览器 · 返 {webdriver_url, debug_port} 供 Playwright 接入"""

    @abc.abstractmethod
    async def stop_browser(self, *, external_profile_id: str) -> None:
        """关闭浏览器"""

    @abc.abstractmethod
    async def update_proxy(
        self, *, external_profile_id: str, proxy_config: dict
    ) -> None:
        """切换代理 IP · 不重启浏览器"""

    @abc.abstractmethod
    async def delete_profile(self, *, external_profile_id: str) -> None:
        """删除配置（谨慎 · 不可恢复）"""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """AdsPower 本地服务是否可达"""


class AdsPowerLocalClient(AdsPowerClient):
    """直连本地 AdsPower API · 仅用于内部 dogfooding / 开发测试

    生产环境**不**用这个 · 客户的桌面 client 调本地 AdsPower 即可。
    """

    def __init__(self, base_url: str = "http://local.adspower.net:50325") -> None:
        self.base_url = base_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        return self._http

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        client = await self._client()
        try:
            resp = await client.request(method, path, **kwargs)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise AdsPowerError(
                    f"AdsPower API error code={data.get('code')} msg={data.get('msg')}"
                )
            return data.get("data", {})
        except httpx.HTTPError as e:
            logger.warning("adspower.http_failed", path=path, error=str(e))
            raise AdsPowerError(f"HTTP {e}") from e

    async def create_profile(
        self,
        *,
        profile_name: str,
        country: str,
        timezone: str,
        proxy_config: dict | None = None,
        fingerprint_overrides: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "name": profile_name,
            "group_id": "0",
            "domain_name": "",
            "open_urls": [],
            "fingerprint_config": {
                "automatic_timezone": "0",
                "timezone": timezone,
                "language": ["en-US", "en"],
                "country": country,
                **(fingerprint_overrides or {}),
            },
        }
        if proxy_config:
            body["user_proxy_config"] = proxy_config

        data = await self._request("POST", "/api/v1/user/create", json=body)
        return {
            "external_profile_id": data.get("id"),
            "fingerprint_summary": {
                "country": country,
                "timezone": timezone,
                "ua_hint": data.get("ua"),
            },
        }

    async def list_profiles(self, *, page: int = 1, page_size: int = 100) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/v1/user/list",
            params={"page": page, "page_size": page_size},
        )
        return data.get("list", [])

    async def start_browser(self, *, external_profile_id: str) -> dict:
        data = await self._request(
            "GET", "/api/v1/browser/start", params={"user_id": external_profile_id}
        )
        return {
            "webdriver_url": data.get("ws", {}).get("selenium"),
            "debug_port": data.get("debug_port"),
            "puppeteer_url": data.get("ws", {}).get("puppeteer"),
        }

    async def stop_browser(self, *, external_profile_id: str) -> None:
        await self._request(
            "GET", "/api/v1/browser/stop", params={"user_id": external_profile_id}
        )

    async def update_proxy(
        self, *, external_profile_id: str, proxy_config: dict
    ) -> None:
        await self._request(
            "POST",
            "/api/v1/user/update",
            json={"user_id": external_profile_id, "user_proxy_config": proxy_config},
        )

    async def delete_profile(self, *, external_profile_id: str) -> None:
        await self._request(
            "POST", "/api/v1/user/delete", json={"user_ids": [external_profile_id]}
        )

    async def health_check(self) -> bool:
        try:
            client = await self._client()
            resp = await client.get("/status")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False


class AdsPowerNoopClient(AdsPowerClient):
    """No-op / Stub · 测试和 prod 不直连场景用 · 不真调 AdsPower

    返"假"成功 + 假 external_profile_id · 让上层业务流程跑通。
    """

    async def create_profile(
        self,
        *,
        profile_name: str,
        country: str,
        timezone: str,
        proxy_config: dict | None = None,
        fingerprint_overrides: dict | None = None,
    ) -> dict:
        import uuid
        fake_id = f"noop_{uuid.uuid4().hex[:12]}"
        logger.info(
            "adspower.noop.create_profile",
            name=profile_name, country=country, fake_id=fake_id,
        )
        return {
            "external_profile_id": fake_id,
            "fingerprint_summary": {
                "country": country,
                "timezone": timezone,
                "ua_hint": "Mozilla/5.0 (NoopFakeBrowser)",
            },
        }

    async def list_profiles(self, *, page: int = 1, page_size: int = 100) -> list[dict]:
        return []

    async def start_browser(self, *, external_profile_id: str) -> dict:
        logger.info("adspower.noop.start_browser", id=external_profile_id)
        return {
            "webdriver_url": f"noop://browser/{external_profile_id}",
            "debug_port": 0,
            "puppeteer_url": None,
        }

    async def stop_browser(self, *, external_profile_id: str) -> None:
        logger.info("adspower.noop.stop_browser", id=external_profile_id)

    async def update_proxy(
        self, *, external_profile_id: str, proxy_config: dict
    ) -> None:
        logger.info("adspower.noop.update_proxy", id=external_profile_id)

    async def delete_profile(self, *, external_profile_id: str) -> None:
        logger.info("adspower.noop.delete_profile", id=external_profile_id)

    async def health_check(self) -> bool:
        return True


# ─── 工厂方法 · 按 settings 选 backend ──────────────────────────────────

_default_client: AdsPowerClient | None = None


def get_adspower_client() -> AdsPowerClient:
    """单例 · 默认 Noop（生产用）· 开发期改 LocalClient

    将来支持 Multilogin / Dolphin · 在这里加 provider 选项即可。
    """
    global _default_client
    if _default_client is None:
        from app.core.config import settings
        provider = getattr(settings, "ADSPOWER_PROVIDER", "noop")
        if provider == "local":
            base = getattr(settings, "ADSPOWER_LOCAL_URL", "http://local.adspower.net:50325")
            _default_client = AdsPowerLocalClient(base_url=base)
        else:
            _default_client = AdsPowerNoopClient()
    return _default_client
