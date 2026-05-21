"""S1 · Onboarding service · 处理 CMH 入驻申请 + 发起 S2 诊断

主要功能：
1. submit_onboarding  · POST /v1/qm/onboardings 入口
2. publish onboarding.submitted + onboarding.completed 事件
3. mark_diagnostic_id / advance_stage 给下游服务回写
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix.pipeline import QmDiagnostic, QmOnboarding
from app.services.qidematrix import pipeline_service

logger = get_logger("qm.onboarding")


async def submit_onboarding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: dict,
    actor_id: uuid.UUID | None = None,
    actor_kind: str = "external",
) -> QmOnboarding:
    """S1 入驻申请 · 持久化 + 触发 onboarding.submitted + onboarding.completed 事件

    payload 字段来自 schemas.qidematrix.pipeline.OnboardingSubmitIn

    流程：
      1. 检查 source + source_ref 防重提（同表单不可二次提交）
      2. INSERT qm_onboardings 行
      3. publish 2 个事件：
         · onboarding.submitted (S1 · 进列队)
         · onboarding.completed (S1 done · 触发 S2 诊断 + S3 DAM workspace)
      4. 返回 onboarding 对象（API 立即返回 · S2 异步跑）
    """
    source = payload.get("source", "cmh_factory_apply")
    source_ref = payload.get("source_ref")

    if source_ref:
        existing = await db.execute(
            select(QmOnboarding).where(
                QmOnboarding.source == source,
                QmOnboarding.source_ref == source_ref,
            )
        )
        existing_ob = existing.scalar_one_or_none()
        if existing_ob:
            logger.info(
                "qm.onboarding.duplicate_submit",
                source=source,
                source_ref=source_ref,
                existing_id=str(existing_ob.id),
            )
            return existing_ob

    now = datetime.now(UTC)
    onboarding = QmOnboarding(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        factory_name=payload["factory_name"],
        contact_name=payload["contact_name"],
        contact_email=payload["contact_email"],
        contact_phone=payload.get("contact_phone"),
        contact_wechat=payload.get("contact_wechat"),
        website_url=payload.get("website_url"),
        business_license_number=payload.get("business_license_number"),
        company_description=payload.get("company_description"),
        product_categories=payload.get("product_categories") or [],
        target_markets=payload.get("target_markets") or [],
        export_stage=payload.get("export_stage"),
        existing_social_urls=payload.get("existing_social_urls") or [],
        monthly_budget=payload.get("monthly_budget"),
        desired_services=payload.get("desired_services") or [],
        top_skus=payload.get("top_skus"),
        biggest_pain_point=payload.get("biggest_pain_point"),
        source=source,
        source_ref=source_ref,
        asset_ids=payload.get("asset_ids") or [],
        current_stage="S1",
        stage_status="submitted",
        created_at=now,
        updated_at=now,
    )
    db.add(onboarding)
    await db.flush()

    common_payload = {
        "onboarding_id": str(onboarding.id),
        "factory_name": onboarding.factory_name,
        "contact_email": onboarding.contact_email,
        "product_categories": onboarding.product_categories or [],
        "target_markets": onboarding.target_markets or [],
        "source": onboarding.source,
    }

    await pipeline_service.publish(
        db,
        tenant_id=tenant_id,
        event_type="onboarding.submitted",
        actor_kind=actor_kind,
        actor_id=actor_id,
        subject_kind="onboarding",
        subject_id=onboarding.id,
        payload=common_payload,
    )

    await pipeline_service.publish(
        db,
        tenant_id=tenant_id,
        event_type="onboarding.completed",
        actor_kind=actor_kind,
        actor_id=actor_id,
        subject_kind="onboarding",
        subject_id=onboarding.id,
        payload=common_payload,
    )

    logger.info(
        "qm.onboarding.submitted",
        onboarding_id=str(onboarding.id),
        factory_name=onboarding.factory_name,
        tenant_id=str(tenant_id),
    )

    return onboarding


async def get_onboarding(
    db: AsyncSession, *, onboarding_id: uuid.UUID
) -> QmOnboarding | None:
    result = await db.execute(
        select(QmOnboarding).where(QmOnboarding.id == onboarding_id)
    )
    return result.scalar_one_or_none()


async def list_onboardings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    stage_status: str | None = None,
    current_stage: str | None = None,
    operator_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QmOnboarding]:
    stmt = select(QmOnboarding).order_by(QmOnboarding.created_at.desc())
    if tenant_id:
        stmt = stmt.where(QmOnboarding.tenant_id == tenant_id)
    if stage_status:
        stmt = stmt.where(QmOnboarding.stage_status == stage_status)
    if current_stage:
        stmt = stmt.where(QmOnboarding.current_stage == current_stage)
    if operator_id:
        stmt = stmt.where(QmOnboarding.assigned_operator_id == operator_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def assign_operator(
    db: AsyncSession,
    *,
    onboarding_id: uuid.UUID,
    operator_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> QmOnboarding:
    """S4 · 运营点 "接单" → 启动 S4 工作流"""
    ob = await get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise ValueError(f"onboarding {onboarding_id} not found")

    ob.assigned_operator_id = operator_id
    ob.current_stage = "S4"
    ob.stage_status = "processing"
    ob.updated_at = datetime.now(UTC)

    await pipeline_service.publish(
        db,
        tenant_id=ob.tenant_id,
        workspace_id=ob.workspace_id,
        event_type="social.matrix_requested",
        actor_kind="user",
        actor_id=actor_id,
        subject_kind="onboarding",
        subject_id=ob.id,
        payload={
            "onboarding_id": str(ob.id),
            "factory_name": ob.factory_name,
            "operator_id": str(operator_id),
        },
    )

    return ob


async def attach_workspace(
    db: AsyncSession,
    *,
    onboarding_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> QmOnboarding:
    """S3 完成后 · DAM workspace 关联到 onboarding"""
    ob = await get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise ValueError(f"onboarding {onboarding_id} not found")
    ob.workspace_id = workspace_id
    ob.updated_at = datetime.now(UTC)
    return ob


async def attach_diagnostic(
    db: AsyncSession,
    *,
    onboarding_id: uuid.UUID,
    diagnostic_id: uuid.UUID,
) -> QmOnboarding:
    """S2 诊断完成后 · 回填 onboarding.diagnostic_id"""
    ob = await get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise ValueError(f"onboarding {onboarding_id} not found")
    ob.diagnostic_id = diagnostic_id
    # S2 done · 推到 S3
    if ob.current_stage in ("S1", "S2"):
        ob.current_stage = "S3"
        ob.stage_status = "processing"
    ob.updated_at = datetime.now(UTC)
    return ob


async def mark_ready(
    db: AsyncSession,
    *,
    onboarding_id: uuid.UUID,
) -> QmOnboarding:
    """S3 完成 → 等运营接单（S4 进 ready 队列）"""
    ob = await get_onboarding(db, onboarding_id=onboarding_id)
    if not ob:
        raise ValueError(f"onboarding {onboarding_id} not found")
    ob.current_stage = "S4"
    ob.stage_status = "ready"
    ob.updated_at = datetime.now(UTC)

    await pipeline_service.publish(
        db,
        tenant_id=ob.tenant_id,
        workspace_id=ob.workspace_id,
        event_type="dam.workspace_ready",
        subject_kind="onboarding",
        subject_id=ob.id,
        payload={
            "onboarding_id": str(ob.id),
            "workspace_id": str(ob.workspace_id) if ob.workspace_id else None,
        },
    )
    return ob
