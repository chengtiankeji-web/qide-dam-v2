from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, model_validator


class ShareLinkCreate(BaseModel):
    """v3 P1.3 (2026-05-13) D10: expires_in ergonomic field added.

    现在支持两种过期表达：
      - expires_at: ISO datetime（精确） · 既有行为
      - expires_in: int (seconds)（便利） · server 计算 expires_at = now + expires_in
    两个都传时 expires_at 优先。都不传 = 永不过期。

    今天 (2026-05-13) Sam 踩了这个坑：调用方传 expires_in=86400 被静默忽略 ·
    schema 根本不暴露该字段 · 留 expires_at=null → 永不过期 share-link 是高风险。
    """
    asset_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)
    expires_at: datetime | None = None
    expires_in: int | None = Field(
        default=None, ge=60, le=30 * 24 * 3600,
        description="便利字段：N 秒后过期 · 与 expires_at 互斥 · 1 分 ~ 30 天范围。"
                    "expires_at 优先（同时传时 expires_in 被忽略）",
    )
    max_downloads: int | None = Field(default=None, ge=1)
    note: str | None = None

    @model_validator(mode="after")
    def _materialize_expires(self) -> ShareLinkCreate:
        if self.expires_at is None and self.expires_in is not None:
            object.__setattr__(
                self,
                "expires_at",
                datetime.now(UTC) + timedelta(seconds=self.expires_in),
            )
        return self


class ShareLinkOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID | None
    collection_id: uuid.UUID | None
    token: str
    expires_at: datetime | None
    max_downloads: int | None
    download_count: int
    is_active: bool
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ShareLinkResolveIn(BaseModel):
    password: str | None = None
