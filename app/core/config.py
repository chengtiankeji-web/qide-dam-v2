"""Settings — load from environment + .env via Pydantic Settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Core ---
    APP_NAME: str = "QideDAM"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev-secret-not-for-prod"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080  # 7 days

    # --- DB ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://qidedam:qidedam@localhost:5432/qidedam"
    )
    DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://qidedam:qidedam@localhost:5432/qidedam"
    )

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- Object storage ---
    S3_ENDPOINT: str = "http://localhost:9000"
    S3_REGION: str = "auto"
    S3_BUCKET: str = "qidedam-dev"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_USE_SSL: bool = False
    S3_PUBLIC_BASE_URL: str = "http://localhost:9000/qidedam-dev"

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # --- Multi-tenant defaults ---
    DEFAULT_TENANT_SLUG: str = "qide"
    DEFAULT_PROJECT_SLUG: str = "core"

    # --- AI ---
    DASHSCOPE_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # --- Notifications · WeCom (企业微信内部应用) ---
    WECOM_BOT_URL: str = ""               # 旧 · 群机器人 webhook（保留兼容）
    WECOM_CORPID: str = ""                 # 企业 ID · ww...
    WECOM_CORPSECRET: str = ""             # 应用 Secret · 44 位
    WECOM_AGENTID: str = ""                # 应用 ID · 7 位数字
    WECOM_CALLBACK_TOKEN: str = ""         # 回调 token（Phase B 拿）
    WECOM_CALLBACK_AESKEY: str = ""        # 43 位 EncodingAESKey（Phase B 拿）

    # --- MCP ---
    MCP_HTTP_HOST: str = "0.0.0.0"
    MCP_HTTP_PORT: int = 8001
    MCP_API_KEY_HEADER: str = "X-DAM-API-Key"

    # --- Vault (v3 P0-1) ---
    # Master KEK for AES-256-GCM envelope encryption of Vault payloads.
    # Sprint 1 ships server-side encryption only — i.e. the server can
    # decrypt for an authorised user. Sprint 2 (P1-1) will layer
    # client-side XChaCha20-Poly1305 on top so the server cannot decrypt.
    #
    # Format: 64-char hex (= 32 raw bytes = 256 bits). Generate with
    #   python -c "import secrets; print(secrets.token_hex(32))"
    # Rotate by appending a new key with a higher version (later P0
    # iteration); current production key picked by VAULT_KEK_ACTIVE_VERSION.
    #
    # In dev a deterministic placeholder is used; production .env MUST
    # override or every Vault item becomes unrecoverable on restart.
    VAULT_KEK_HEX: str = "0" * 64
    VAULT_KEK_ACTIVE_VERSION: int = 1
    # HMAC key for indexable hashes (Vault domain hash, search tokens, etc.).
    # Same generation pattern as VAULT_KEK_HEX.
    VAULT_HMAC_HEX: str = "1" * 64

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
