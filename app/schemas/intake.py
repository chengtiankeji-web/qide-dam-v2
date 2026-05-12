"""Smart Intake v4 · Pydantic schemas

与 app/models/intake.py + app/api/v1/intake.py 配套
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════
# Job
# ════════════════════════════════════════════════════════════

class IntakeJobCreate(BaseModel):
    project_id: uuid.UUID
    factory_slug: str = Field(..., min_length=1, max_length=64)
    source_path: str = Field(..., min_length=1)
    options: Optional[dict[str, Any]] = None


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

    options: Optional[dict[str, Any]] = None
    entity_yml: Optional[dict[str, Any]] = None

    created_at: datetime
    updated_at: datetime
    scan_completed_at: Optional[datetime] = None
    review_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None


class IntakeJobTransition(BaseModel):
    """状态机切换的 payload · POST /v1/intake/jobs/{id}/transition"""
    new_status: str = Field(
        ...,
        pattern="^(approved|rejected|cancelled)$",
    )
    reason: Optional[str] = Field(None, max_length=512)


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
    mime_type: Optional[str] = None
    kind: Optional[str] = None

    predicted_category: Optional[str] = None
    predicted_sku_slug: Optional[str] = None
    predicted_subdir: Optional[str] = None
    predicted_target_filename: Optional[str] = None
    predicted_tags: Optional[list[str]] = None
    confidence: float = 0.0
    flagged_reason: Optional[str] = None

    cluster_id: Optional[uuid.UUID] = None

    visual_verified: bool = False
    visual_dominant_colors: Optional[dict[str, Any]] = None

    user_decision: Optional[str] = None
    user_override: Optional[dict[str, Any]] = None
    user_decision_at: Optional[datetime] = None

    pushed_asset_id: Optional[uuid.UUID] = None
    push_error: Optional[str] = None
    pushed_at: Optional[datetime] = None

    created_at: datetime


class IntakeItemOverride(BaseModel):
    """单文件 override · BD 改 subdir / filename / tags / sku / category"""
    predicted_category: Optional[str] = None
    predicted_sku_slug: Optional[str] = None
    predicted_subdir: Optional[str] = None
    predicted_target_filename: Optional[str] = None
    predicted_tags: Optional[list[str]] = None


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
    sku_name_cn: Optional[str] = None
    sku_name_en: Optional[str] = None
    subcategory: Optional[str] = None
    item_count: int
    representative_item_id: Optional[uuid.UUID] = None
    category_breakdown: Optional[dict[str, Any]] = None
    user_confirmed: bool = False
    user_renamed_slug: Optional[str] = None
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
