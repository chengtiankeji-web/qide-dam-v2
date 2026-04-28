from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ShareLinkCreate(BaseModel):
    asset_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)
    expires_at: datetime | None = None
    max_downloads: int | None = Field(default=None, ge=1)
    note: str | None = None


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
