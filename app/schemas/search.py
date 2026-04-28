from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.asset import AssetOut


class VectorSearchIn(BaseModel):
    """Find assets similar to a given asset OR text query.

    One of `asset_id` (visual similarity) / `text` (semantic) must be set.
    """
    asset_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=2048)
    project_id: uuid.UUID | None = None
    kind: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    min_similarity: float = Field(default=0.0, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    asset: AssetOut
    similarity: float


class VectorSearchOut(BaseModel):
    query_kind: str  # "asset" | "text"
    items: list[SearchHit]
