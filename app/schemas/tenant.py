from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    legal_entity_type: str | None = None
    credit_code: str | None = None


class TenantOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    display_name: str
    legal_entity_type: str | None
    credit_code: str | None
    storage_prefix: str
    is_active: bool

    model_config = {"from_attributes": True}
