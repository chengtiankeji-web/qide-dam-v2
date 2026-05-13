from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    default_acl: str = Field(default="project")


class ProjectUpdate(BaseModel):
    """v3 P1.3 (2026-05-13): PATCH /v1/projects/{id}.

    可改字段：name / description / default_acl / is_active。
    **不能改** slug / storage_prefix —— 它们烤进 R2 storage_key 路径 ·
    改了会让所有现有 asset 的 download URL 失效 · 强拒（schema 没暴露）。

    改名（display name）通过本 schema 完成 · 不影响 R2 路径。
    """
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    default_acl: str | None = None
    is_active: bool | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    storage_prefix: str
    default_acl: str
    is_active: bool

    model_config = {"from_attributes": True}
