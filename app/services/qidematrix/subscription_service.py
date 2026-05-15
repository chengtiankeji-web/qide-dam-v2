"""QideMatrix subscription / billing 业务

职责：
- upgrade_subscription · trial → standard / enterprise（生成 pending payment intent）
- record_billing_event · 不可篡改写入（支付成功 / 失败 / 退款 webhook 都走这里）
- get_current_subscription
- 配额检查（seat / storage / ai_calls）
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qidematrix import (
    QmBillingEvent,
    QmSubscription,
    QmUsageMeter,
    QmWorkspace,
    QmWorkspaceMember,
)
from app.schemas.qidematrix import (
    PLAN_CONFIG,
    BillingCycle,
    Plan,
    PaymentProvider,
    SubscriptionUpgradeIn,
)


class SubscriptionError(Exception):
    pass


class InvalidPlanError(SubscriptionError):
    pass


# ─── Current subscription ────────────────────────────────────────────

async def get_current_subscription(
    db: AsyncSession, *, workspace_id: uuid.UUID
) -> QmSubscription | None:
    """活跃订阅（status in active/trial/past_due）· 最新一条"""
    rows = (
        await db.execute(
            select(QmSubscription)
            .where(
                QmSubscription.workspace_id == workspace_id,
                QmSubscription.status.in_(("active", "trial", "past_due")),
            )
            .order_by(QmSubscription.created_at.desc())
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


# ─── Upgrade / Downgrade ─────────────────────────────────────────────

async def create_upgrade_pending(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    payload: SubscriptionUpgradeIn,
) -> tuple[QmSubscription, dict]:
    """创建升档"待支付"订阅 + 返回 payment 信息（client 端拉起微信/Stripe）

    返 (pending_sub, payment_info)
      payment_info: {
        provider: 'wechat',
        amount_cny_cents: int,
        order_id: str,
        prepay_id: str,  # 仅 wechat
        ...
      }
    """
    if payload.target_plan == "trial":
        raise InvalidPlanError("cannot 'upgrade' to trial · use create_workspace flow")

    plan_cfg = PLAN_CONFIG[payload.target_plan]
    price_key = "yearly_price_cny_cents" if payload.billing_cycle == "yearly" else "monthly_price_cny_cents"
    price = plan_cfg[price_key]
    if price <= 0:
        raise InvalidPlanError(f"plan {payload.target_plan} has no paid price in PLAN_CONFIG")

    workspace = (
        await db.execute(
            select(QmWorkspace).where(
                QmWorkspace.id == workspace_id,
                QmWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not workspace:
        raise SubscriptionError("workspace not found")

    now = datetime.now(UTC)
    period_days = 365 if payload.billing_cycle == "yearly" else 30
    period_end = now + timedelta(days=period_days)

    pending_sub = QmSubscription(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        plan=payload.target_plan,
        status="active",  # 先建 active · webhook 回来扣 / 续期改 status
        billing_cycle=payload.billing_cycle,
        price_cny_cents=price,
        started_at=now,
        current_period_start=now,
        current_period_end=period_end,
        cancel_at_period_end=False,
        payment_provider=None,  # 等支付 callback 后填
        extra_metadata={"pending_upgrade": True, "actor_user_id": str(actor_user_id)},
        created_at=now,
        updated_at=now,
    )
    db.add(pending_sub)

    # 配额 立即升档（让用户感知）· 真支付 webhook 没回前给 grace period
    workspace.plan = payload.target_plan
    workspace.plan_seats = plan_cfg["seats"]
    workspace.plan_storage_gb = plan_cfg["storage_gb"]
    workspace.plan_ai_calls_monthly = plan_cfg["ai_calls_monthly"]
    workspace.updated_at = now

    await db.flush()

    # payment_info 此版返 stub · v0.2 接微信支付时填真值
    payment_info = {
        "subscription_id": str(pending_sub.id),
        "amount_cny_cents": price,
        "amount_cny": price / 100,
        "billing_cycle": payload.billing_cycle,
        "wechat_pay_intent": None,  # TODO: 真接 WeChat Pay V3 createOrder
        "stripe_checkout_url": None,
    }
    return pending_sub, payment_info


# ─── Billing event 记录（webhook 调） ─────────────────────────────────

async def record_billing_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    subscription_id: uuid.UUID | None,
    event_type: str,
    amount_cny_cents: int | None = None,
    payment_provider: str | None = None,
    payment_provider_event_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> QmBillingEvent:
    """记一条计费审计 · 不可改 / 不可删（trigger 保护）"""
    now = datetime.now(UTC)

    # 幂等：如果 payment_provider_event_id 已存在 直接返既有的（不抛）
    if payment_provider_event_id and payment_provider:
        existing = (
            await db.execute(
                select(QmBillingEvent).where(
                    QmBillingEvent.payment_provider == payment_provider,
                    QmBillingEvent.payment_provider_event_id == payment_provider_event_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    event = QmBillingEvent(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        event_type=event_type,
        amount_cny_cents=amount_cny_cents,
        payment_provider=payment_provider,
        payment_provider_event_id=payment_provider_event_id,
        actor_user_id=actor_user_id,
        payload=payload or {},
        created_at=now,
    )
    db.add(event)
    await db.flush()
    return event


async def mark_subscription_paid(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    payment_provider: PaymentProvider,
    payment_provider_subscription_id: str,
    payment_provider_event_id: str,
    amount_cny_cents: int,
) -> QmSubscription:
    """支付成功回调 · 把 sub 从 pending 转 active · 记 billing_event"""
    sub = (
        await db.execute(
            select(QmSubscription).where(QmSubscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if not sub:
        raise SubscriptionError("subscription not found")

    sub.status = "active"
    sub.payment_provider = payment_provider
    sub.payment_provider_subscription_id = payment_provider_subscription_id
    sub.updated_at = datetime.now(UTC)

    await record_billing_event(
        db,
        workspace_id=sub.workspace_id,
        subscription_id=sub.id,
        event_type="payment_succeeded",
        amount_cny_cents=amount_cny_cents,
        payment_provider=payment_provider,
        payment_provider_event_id=payment_provider_event_id,
        payload={"sub_id": str(sub.id)},
    )

    await db.flush()
    return sub


async def cancel_subscription(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    at_period_end: bool = True,
) -> QmSubscription:
    """取消订阅 · 默认到期不续 · at_period_end=False 立即停"""
    sub = (
        await db.execute(
            select(QmSubscription).where(QmSubscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if not sub:
        raise SubscriptionError("subscription not found")

    now = datetime.now(UTC)
    sub.cancel_at_period_end = at_period_end
    if not at_period_end:
        sub.status = "cancelled"
        sub.cancelled_at = now
        # 降到 trial 配额
        workspace = (
            await db.execute(
                select(QmWorkspace).where(QmWorkspace.id == sub.workspace_id)
            )
        ).scalar_one_or_none()
        if workspace:
            trial = PLAN_CONFIG["trial"]
            workspace.plan = "trial"
            workspace.plan_seats = trial["seats"]
            workspace.plan_storage_gb = trial["storage_gb"]
            workspace.plan_ai_calls_monthly = trial["ai_calls_monthly"]
            workspace.updated_at = now
    sub.updated_at = now

    await record_billing_event(
        db,
        workspace_id=sub.workspace_id,
        subscription_id=sub.id,
        event_type="cancel_requested" if at_period_end else "cancel_immediate",
        actor_user_id=actor_user_id,
        payload={"at_period_end": at_period_end},
    )

    await db.flush()
    return sub


# ─── Usage / Quota check ─────────────────────────────────────────────

async def get_or_create_usage_meter(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    period_month: date | None = None,
) -> QmUsageMeter:
    """拿当前月 usage record · 没有就建"""
    if period_month is None:
        today = date.today()
        period_month = today.replace(day=1)

    meter = (
        await db.execute(
            select(QmUsageMeter).where(
                QmUsageMeter.workspace_id == workspace_id,
                QmUsageMeter.period_month == period_month,
            )
        )
    ).scalar_one_or_none()
    if meter:
        return meter

    meter = QmUsageMeter(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        period_month=period_month,
        updated_at=datetime.now(UTC),
    )
    db.add(meter)
    await db.flush()
    return meter


async def bump_usage(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    ai_calls: int = 0,
    ai_tokens_input: int = 0,
    ai_tokens_output: int = 0,
    ai_cost_cny_cents: int = 0,
    storage_delta_bytes: int = 0,
    workflow_runs: int = 0,
) -> None:
    """累加用量 · 任何 LLM 调用 / 资产上传 / workflow 跑后调"""
    meter = await get_or_create_usage_meter(db, workspace_id=workspace_id)
    meter.ai_calls_total += ai_calls
    meter.ai_tokens_input += ai_tokens_input
    meter.ai_tokens_output += ai_tokens_output
    meter.ai_cost_cny_cents += ai_cost_cny_cents
    meter.storage_bytes = max(0, meter.storage_bytes + storage_delta_bytes)
    meter.workflow_runs += workflow_runs
    meter.updated_at = datetime.now(UTC)
    await db.flush()


async def check_ai_quota(
    db: AsyncSession, *, workspace_id: uuid.UUID
) -> tuple[bool, int, int]:
    """返回 (允许吗, 已用, 配额)

    enterprise plan ai_calls_monthly=-1 不限 · 永远 allow
    """
    workspace = (
        await db.execute(
            select(QmWorkspace).where(QmWorkspace.id == workspace_id)
        )
    ).scalar_one_or_none()
    if not workspace:
        return False, 0, 0

    quota = workspace.plan_ai_calls_monthly
    if quota < 0:  # unlimited
        return True, 0, -1

    meter = await get_or_create_usage_meter(db, workspace_id=workspace_id)
    return meter.ai_calls_total < quota, meter.ai_calls_total, quota


async def get_usage_summary(
    db: AsyncSession, *, workspace_id: uuid.UUID
) -> dict:
    """给 dashboard 用 · 当前周期 vs 配额 · 含百分比"""
    workspace = (
        await db.execute(
            select(QmWorkspace).where(QmWorkspace.id == workspace_id)
        )
    ).scalar_one_or_none()
    if not workspace:
        raise SubscriptionError("workspace not found")

    meter = await get_or_create_usage_meter(db, workspace_id=workspace_id)

    seat_count = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == workspace_id
            )
        )
    ).scalars().all()

    ai_quota = workspace.plan_ai_calls_monthly
    storage_gb_used = meter.storage_bytes / (1024**3)
    storage_quota = workspace.plan_storage_gb

    return {
        "workspace_id": workspace.id,
        "plan": workspace.plan,
        "period_month": meter.period_month,
        "ai_calls_used": meter.ai_calls_total,
        "ai_calls_quota": ai_quota,
        "ai_calls_pct": (
            0.0 if ai_quota < 0 or ai_quota == 0
            else round(meter.ai_calls_total / ai_quota * 100, 1)
        ),
        "storage_used_gb": round(storage_gb_used, 3),
        "storage_quota_gb": storage_quota,
        "storage_pct": (
            0.0 if storage_quota == 0
            else round(storage_gb_used / storage_quota * 100, 1)
        ),
        "seats_used": len(seat_count),
        "seats_quota": workspace.plan_seats,
    }
