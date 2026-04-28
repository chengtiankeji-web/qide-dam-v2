from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    default_acl: str = Field(default="project")


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
