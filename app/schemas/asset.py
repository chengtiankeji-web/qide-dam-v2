from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

ACL_LITERALS = ("private", "project", "tenant", "public")

# v3 P1.3 (2026-05-13): sha256 严格化 · 必须 64 hex chars · 拒绝 None / 空串
# 兼容 watcher / admin SPA / MCP 客户端：先 hash 本地 · 再 presign 调用
SHA256_PATTERN = r"^[0-9a-f]{64}$"


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
    """v3 P1.3 (2026-05-13): sha256 严格化 · 必填 64 hex.

    历史背景：v3 P1.1 (2026-05-09) 加 sha256 dedup 但仍兼容 None ·
    Cowork watcher V2 在 5-13 audit 中重传 ~27 个 dup 因为 V1 刚传的
    sha256 还没在 list 中可见（status=processing 窗口）· dedup 看不见。

    现在：
      - sha256 必填（schema 层硬拒）· 任何客户端必须本地先 hash 再调
      - 加上 alembic 010 partial unique index · DB 层兜底 race
      - dedup_strategy 控制 dup 时行为：reject (legacy 409) / link (返既有 id)
    """

    project_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    # P0 D1 修复：sha256 改 required + 严格 64 hex 校验
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=SHA256_PATTERN,
        description="SHA-256 of file content · 64 lowercase hex chars · "
                    "client MUST compute before calling presign",
    )
    acl: str = Field(default="project")
    manual_tags: list[str] = Field(default_factory=list)


class PresignedUploadOut(BaseModel):
    """v3 P1.3 (2026-05-13): 新增 deduplicated 字段表达 link 语义。

    - deduplicated=False（默认）·     client 必须 PUT 到 upload_url · 然后 confirm
    - deduplicated=True (link 命中)· client 拿到既有 asset_id · 跳过 PUT
                                     · 跳过 confirm（既有已 confirm）
                                     · 直接当作"已上传"处理
    upload_url / storage_key / expires_in 在 deduplicated=True 时为 None / "" / 0
    headers 空 dict
    """

    asset_id: uuid.UUID
    upload_url: str | None = None
    storage_key: str | None = None
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_in: int = 0  # seconds
    # P0 D1 新字段：是否走的 dedup 路径
    deduplicated: bool = False
    existing_status: str | None = None  # "ready" / "processing" / "uploading"


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


# ─── v3 P1.3 新增：dedup by sha256 端点用 ────────────────────────────

class DedupBySha256In(BaseModel):
    """POST /v1/assets/_dedup_by_sha256 - 一次性清理同 project 同 sha256 重复。

    跑完返回每个 sha256 哪些 asset_id 被保留 / 哪些被 archive。
    可重复跑（幂等）· 修完后跑 0 dup 是预期。
    """
    project_id: uuid.UUID | None = None  # None = 全租户扫描
    dry_run: bool = Field(default=True, description="True = 只报告不动数据 · False = 真 archive")


class DedupBySha256Out(BaseModel):
    project_id: uuid.UUID | None
    dry_run: bool
    dup_groups: int  # 多少组 (project_id, sha256) 有 >1 行
    archived_count: int  # 实际 / 拟 archive 的行数
    sample: list[dict] = Field(default_factory=list)  # 前 20 组样本：{sha256, kept_id, archived_ids[]}
