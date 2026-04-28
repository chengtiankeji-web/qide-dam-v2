from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CollectionCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    project_id: uuid.UUID | None = None
    cover_asset_id: uuid.UUID | None = None
    acl: str = "project"
    is_smart: bool = False
    smart_query: dict = Field(default_factory=dict)


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    cover_asset_id: uuid.UUID | None = None
    acl: str | None = None
    smart_query: dict | None = None


class CollectionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    slug: str
    name: str
    description: str | None
    cover_asset_id: uuid.UUID | None
    acl: str
    is_smart: bool
    smart_query: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CollectionAssetIn(BaseModel):
    asset_id: uuid.UUID
    sort_order: int = 0
    note: str | None = None
