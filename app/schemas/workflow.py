from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class WorkflowStepIn(BaseModel):
    order_no: int = Field(ge=1)
    approver_user_id: uuid.UUID | None = None
    role: str | None = None


class WorkflowStepOut(BaseModel):
    id: uuid.UUID
    order_no: int
    approver_user_id: uuid.UUID | None
    role: str | None
    status: str
    decided_at: str | None
    comment: str | None

    model_config = {"from_attributes": True}


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    project_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    steps: list[WorkflowStepIn] = Field(min_length=1)


class WorkflowOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    name: str
    description: str | None
    status: str
    steps: list[WorkflowStepOut]
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowDecideIn(BaseModel):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    comment: str | None = None
