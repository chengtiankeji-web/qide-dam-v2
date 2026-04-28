"""Workflows — multi-step approval over assets."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.workflow import Workflow
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowDecideIn,
    WorkflowOut,
)
from app.services import workflow_service

router = APIRouter()


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowCreate,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    wf = await workflow_service.create_workflow(
        db,
        tenant_id=p.tenant_id,
        initiator_user_id=p.user_id,
        name=payload.name,
        description=payload.description,
        project_id=payload.project_id,
        asset_id=payload.asset_id,
        steps=[s.model_dump() for s in payload.steps],
    )
    await db.refresh(wf, ["steps"])
    return WorkflowOut.model_validate(wf)


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    wf = (
        await db.execute(
            select(Workflow).where(
                Workflow.id == workflow_id, Workflow.tenant_id == p.tenant_id
            )
        )
    ).scalar_one_or_none()
    if not wf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    await db.refresh(wf, ["steps"])
    return WorkflowOut.model_validate(wf)


@router.post("/steps/{step_id}/decide", response_model=WorkflowOut)
async def decide(
    step_id: uuid.UUID,
    payload: WorkflowDecideIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    if not p.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "JWT user required to approve")
    try:
        wf = await workflow_service.decide_step(
            db,
            tenant_id=p.tenant_id,
            user_id=p.user_id,
            step_id=step_id,
            decision=payload.decision,
            comment=payload.comment,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await db.refresh(wf, ["steps"])
    return WorkflowOut.model_validate(wf)
