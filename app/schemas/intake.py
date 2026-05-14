"""Smart Intake v4 · Pydantic schemas

与 app/models/intake.py + app/api/v1/intake.py 配套
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ════════════════════════════════════════════════════════════
# Job
# ════════════════════════════════════════════════════════════

class IntakeJobCreate(BaseModel):
    project_id: uuid.UUID
    factory_slug: str = Field(..., min_length=1, max_length=64)
    source_path: str = Field(..., min_length=1)
    options: dict[str, Any] | None = None


class IntakeJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    factory_slug: str
    source_path: str
    status: str

    total_files: int
    classified_count: int
    flagged_count: int
    duplicate_count: int
    clusters_count: int
    pushed_count: int
    push_error_count: int

    llm_cost_cny: float
    llm_tokens_input: int
    llm_tokens_output: int

    options: dict[str, Any] | None = None
    entity_yml: dict[str, Any] | None = None

    created_at: datetime
    updated_at: datetime
    scan_completed_at: datetime | None = None
    review_at: datetime | None = None
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    failed_reason: str | None = None


class IntakeJobTransition(BaseModel):
    """状态机切换的 payload · POST /v1/intake/jobs/{id}/transition"""
    new_status: str = Field(
        ...,
        pattern="^(approved|rejected|cancelled)$",
    )
    reason: str | None = Field(None, max_length=512)


# ════════════════════════════════════════════════════════════
# Item
# ════════════════════════════════════════════════════════════

class IntakeItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    source_path: str
    filename: str
    size_bytes: int
    sha256: str
    mime_type: str | None = None
    kind: str | None = None

    predicted_category: str | None = None
    predicted_sku_slug: str | None = None
    predicted_subdir: str | None = None
    predicted_target_filename: str | None = None
    predicted_tags: list[str] | None = None
    confidence: float = 0.0
    flagged_reason: str | None = None

    cluster_id: uuid.UUID | None = None

    visual_verified: bool = False
    visual_dominant_colors: dict[str, Any] | None = None

    user_decision: str | None = None
    user_override: dict[str, Any] | None = None
    user_decision_at: datetime | None = None

    pushed_asset_id: uuid.UUID | None = None
    push_error: str | None = None
    pushed_at: datetime | None = None

    created_at: datetime


class IntakeItemOverride(BaseModel):
    """单文件 override · BD 改 subdir / filename / tags / sku / category"""
    predicted_category: str | None = None
    predicted_sku_slug: str | None = None
    predicted_subdir: str | None = None
    predicted_target_filename: str | None = None
    predicted_tags: list[str] | None = None


class BulkDecisionIn(BaseModel):
    """批量 approve / reject · POST /v1/intake/jobs/{id}/items/_bulk/decide"""
    item_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    decision: str = Field(..., pattern="^(approve|reject)$")


class BulkDecisionOut(BaseModel):
    affected: int


# ════════════════════════════════════════════════════════════
# Cluster
# ════════════════════════════════════════════════════════════

class IntakeClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    sku_slug: str
    sku_name_cn: str | None = None
    sku_name_en: str | None = None
    subcategory: str | None = None
    item_count: int
    representative_item_id: uuid.UUID | None = None
    category_breakdown: dict[str, Any] | None = None
    user_confirmed: bool = False
    user_renamed_slug: str | None = None
    created_at: datetime


class ClusterRenameIn(BaseModel):
    new_slug: str = Field(..., min_length=1, max_length=128, pattern="^[a-z0-9-]+$")


# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

class IntakeJobSummary(BaseModel):
    by_category: dict[str, int]
    by_decision: dict[str, int]
    cluster_count: int
    flagged_count: int


__all__ = [
    "BulkDecisionIn",
    "BulkDecisionOut",
    "ClusterRenameIn",
    "IntakeClusterOut",
    "IntakeItemOut",
    "IntakeItemOverride",
    "IntakeJobCreate",
    "IntakeJobOut",
    "IntakeJobSummary",
    "IntakeJobTransition",
]
