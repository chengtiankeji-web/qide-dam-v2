"""Settings — load from environment + .env via Pydantic Settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
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

    # --- Notifications ---
    WECOM_BOT_URL: str = ""

    # --- MCP ---
    MCP_HTTP_HOST: str = "0.0.0.0"
    MCP_HTTP_PORT: int = 8001
    MCP_API_KEY_HEADER: str = "X-DAM-API-Key"

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
