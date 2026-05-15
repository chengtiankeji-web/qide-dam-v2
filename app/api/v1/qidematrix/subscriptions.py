"""QideMatrix subscriptions REST API · /v1/qm/subscriptions/*"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix import (
    PLAN_CONFIG,
    SubscriptionOut,
    SubscriptionUpgradeIn,
    UsageSummaryOut,
)
from app.services.qidematrix import (
    subscription_service as sub_svc,
    workspace_service as ws_svc,
)

router = APIRouter(prefix="/qm/subscriptions", tags=["qidematrix-subscriptions"])


def _err(code: int, detail: str):
    return HTTPException(status_code=code, detail=detail)


@router.get("/plans")
async def list_plans():
    """公开端点 · 给营销页 / 注册页用 · 不需登录"""
    plans_view = []
    for plan_name, cfg in PLAN_CONFIG.items():
        plans_view.append({
            "plan": plan_name,
            "seats": cfg["seats"],
            "storage_gb": cfg["storage_gb"],
            "ai_calls_monthly": cfg["ai_calls_monthly"],
            "monthly_price_cny": cfg["monthly_price_cny_cents"] / 100,
            "yearly_price_cny": cfg["yearly_price_cny_cents"] / 100,
            "trial_days": cfg.get("trial_days"),
        })
    return {"plans": plans_view}


@router.get(
    "/workspace/{workspace_id}/current",
    response_model=SubscriptionOut | None,
)
async def get_current(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """当前活跃订阅"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.get_workspace_for_user(
            db, workspace_id=workspace_id, user_id=p.user_id
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None

    sub = await sub_svc.get_current_subscription(db, workspace_id=workspace_id)
    if not sub:
        return None
    return SubscriptionOut.model_validate(sub)


@router.post("/workspace/{workspace_id}/upgrade")
async def upgrade(
    workspace_id: uuid.UUID,
    payload: SubscriptionUpgradeIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """升档 · 仅 owner 能调 · 返 payment info 给前端拉起支付"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.get_workspace_for_user(
            db,
            workspace_id=workspace_id,
            user_id=p.user_id,
            require_role=("owner",),
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None

    try:
        sub, payment_info = await sub_svc.create_upgrade_pending(
            db,
            workspace_id=workspace_id,
            actor_user_id=p.user_id,
            payload=payload,
        )
    except sub_svc.InvalidPlanError as e:
        raise _err(400, str(e)) from None
    except sub_svc.SubscriptionError as e:
        raise _err(404, str(e)) from None

    await db.commit()
    return {
        "subscription": SubscriptionOut.model_validate(sub).model_dump(mode="json"),
        "payment": payment_info,
    }


@router.post("/workspace/{workspace_id}/cancel")
async def cancel(
    workspace_id: uuid.UUID,
    at_period_end: bool = True,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """取消订阅 · 仅 owner · 默认到期不续 · at_period_end=False 立即停"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.get_workspace_for_user(
            db,
            workspace_id=workspace_id,
            user_id=p.user_id,
            require_role=("owner",),
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None

    sub = await sub_svc.get_current_subscription(db, workspace_id=workspace_id)
    if not sub:
        raise _err(404, "no active subscription")

    try:
        sub = await sub_svc.cancel_subscription(
            db,
            subscription_id=sub.id,
            actor_user_id=p.user_id,
            at_period_end=at_period_end,
        )
    except sub_svc.SubscriptionError as e:
        raise _err(400, str(e)) from None

    await db.commit()
    return SubscriptionOut.model_validate(sub)


@router.get(
    "/workspace/{workspace_id}/usage",
    response_model=UsageSummaryOut,
)
async def get_usage(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """用量 dashboard · 任意 workspace 成员可看"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.get_workspace_for_user(
            db, workspace_id=workspace_id, user_id=p.user_id
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None

    summary = await sub_svc.get_usage_summary(db, workspace_id=workspace_id)
    return UsageSummaryOut(**summary)
