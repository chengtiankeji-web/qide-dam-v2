from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

ACL_LITERALS = ("private", "project", "tenant", "public")


class AssetCreate(BaseModel):
    """Used for direct (small file) upload registration."""

    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    acl: str = Field(default="project")
    manual_tags: list[str] = Field(default_factory=list)
    custom_fields: dict = Field(default_factory=dict)


class AssetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    acl: str | None = None
    manual_tags: list[str] | None = None
    custom_fields: dict | None = None
    is_starred: bool | None = None
    # 2026-05-08 phase 1: rename + move 支持。folder_id=None 等于"放回根"。
    # 跨 tenant / 跨 project 移动暂不支持（phase 2 单独走 move endpoint）。
    folder_id: uuid.UUID | None = None


class AssetOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    folder_id: uuid.UUID | None = None  # 2026-05-10 phase 1.2: 暴露 folder_id 给前端
    name: str
    description: str | None
    sha256: str
    kind: str
    mime_type: str
    extension: str
    size_bytes: int
    storage_key: str
    public_url: str | None
    status: str
    source: str
    acl: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    page_count: int | None
    thumbnails: dict
    technical_metadata: dict
    auto_tags: list[str]
    manual_tags: list[str]
    ai_summary: str | None
    ai_alt_text: str | None
    current_version: int
    is_starred: bool
    custom_fields: dict
    # 2026-04-29 perf: 一次性 presigned URLs（list_assets 时填好 · 减少前端往返）
    thumb_urls: dict | None = None  # {sm, md, lg} → presigned R2 URLs · None=未签或非 image
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PresignedUploadIn(BaseModel):
    project_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    sha256: str | None = None
    acl: str = Field(default="project")
    manual_tags: list[str] = Field(default_factory=list)


class PresignedUploadOut(BaseModel):
    asset_id: uuid.UUID
    upload_url: str
    storage_key: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_in: int  # seconds


class BulkMoveIn(BaseModel):
    """v3 phase 1.2 (2026-05-10): 批量移动 assets 到指定 folder（同 project 内）。

    target_folder_id=None → 移到根（folder_id 设回 NULL）。
    跨 project 移动暂不支持，必须同 project。
    """
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    target_folder_id: uuid.UUID | None = None


class BulkMoveOut(BaseModel):
    moved: list[uuid.UUID] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)  # [{id, reason}]
