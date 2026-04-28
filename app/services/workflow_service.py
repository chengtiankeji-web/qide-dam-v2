from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import Workflow, WorkflowStep


async def create_workflow(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    initiator_user_id: uuid.UUID | None,
    name: str,
    description: str | None,
    project_id: uuid.UUID | None,
    asset_id: uuid.UUID | None,
    steps: list[dict],
) -> Workflow:
    wf = Workflow(
        tenant_id=tenant_id,
        initiator_user_id=initiator_user_id,
        name=name,
        description=description,
        project_id=project_id,
        asset_id=asset_id,
        status="pending" if steps else "draft",
    )
    db.add(wf)
    await db.flush()
    for step in steps:
        s = WorkflowStep(
            workflow_id=wf.id,
            order_no=int(step["order_no"]),
            approver_user_id=step.get("approver_user_id"),
            role=step.get("role"),
            status="pending",
        )
        db.add(s)
    await db.flush()
    return wf


async def decide_step(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    step_id: uuid.UUID,
    decision: str,
    comment: str | None,
) -> Workflow:
    step = (
        await db.execute(
            select(WorkflowStep).join(Workflow).where(
                WorkflowStep.id == step_id, Workflow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not step:
        raise ValueError("step not found")
    if step.status != "pending":
        raise ValueError(f"step already {step.status}")
    if step.approver_user_id and step.approver_user_id != user_id:
        raise ValueError("not the assigned approver")

    step.status = decision  # 'approved' | 'rejected'
    step.comment = comment
    step.decided_at = datetime.now(timezone.utc).isoformat()
    await db.flush()

    # If rejected, reject the whole workflow; otherwise check completion
    wf = (
        await db.execute(select(Workflow).where(Workflow.id == step.workflow_id))
    ).scalar_one()
    if decision == "rejected":
        wf.status = "rejected"
    else:
        all_steps = (
            await db.execute(
                select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)
            )
        ).scalars().all()
        if all(s.status == "approved" for s in all_steps):
            wf.status = "approved"
    await db.flush()
    return wf
