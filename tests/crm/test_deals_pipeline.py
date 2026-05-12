"""deals_service 集成测试 · 重点 pipeline 状态机 + forecast 聚合"""
from __future__ import annotations

import pytest

from app.services.crm.deals_service import (
    ALLOWED_STAGE_TRANSITIONS, DEFAULT_PROBABILITY,
)


def test_pipeline_state_machine_completeness():
    """所有 7 stage 都在 transitions table"""
    stages = {"prospect", "qualified", "proposal", "negotiation",
              "closed_won", "closed_lost", "on_hold"}
    assert stages == set(ALLOWED_STAGE_TRANSITIONS.keys())


def test_pipeline_state_machine_closed_won_is_terminal():
    """closed_won 是终态·不能再变"""
    assert ALLOWED_STAGE_TRANSITIONS["closed_won"] == set()


def test_pipeline_state_machine_closed_lost_can_reactivate():
    """closed_lost 可以 reactivate 回 prospect（极少 · 但允许）"""
    assert "prospect" in ALLOWED_STAGE_TRANSITIONS["closed_lost"]


def test_pipeline_state_machine_on_hold_can_revive():
    """on_hold 可以回到任何 open stage"""
    on_hold_targets = ALLOWED_STAGE_TRANSITIONS["on_hold"]
    assert "prospect" in on_hold_targets
    assert "qualified" in on_hold_targets
    assert "proposal" in on_hold_targets
    assert "negotiation" in on_hold_targets


def test_pipeline_default_probabilities_monotonic():
    """probability 沿 pipeline 递增（prospect < qualified < proposal < negotiation < closed_won）"""
    p_prospect = DEFAULT_PROBABILITY["prospect"]
    p_qualified = DEFAULT_PROBABILITY["qualified"]
    p_proposal = DEFAULT_PROBABILITY["proposal"]
    p_negotiation = DEFAULT_PROBABILITY["negotiation"]
    p_won = DEFAULT_PROBABILITY["closed_won"]
    p_lost = DEFAULT_PROBABILITY["closed_lost"]

    assert p_prospect < p_qualified < p_proposal < p_negotiation < p_won
    assert p_won == 100
    assert p_lost == 0


def test_pipeline_no_skipping_stages_normally():
    """prospect 不能直接到 closed_won（必须先经 qualified/proposal/negotiation）"""
    assert "closed_won" not in ALLOWED_STAGE_TRANSITIONS["prospect"]
    assert "negotiation" not in ALLOWED_STAGE_TRANSITIONS["prospect"]


def test_pipeline_can_always_close_lost_from_open():
    """任何 open stage 都能 closed_lost（业务现实：随时 lost）"""
    open_stages = ["prospect", "qualified", "proposal", "negotiation", "on_hold"]
    for s in open_stages:
        assert "closed_lost" in ALLOWED_STAGE_TRANSITIONS[s], \
            f"Stage {s} should allow closed_lost"


def test_pipeline_can_always_on_hold_from_open():
    """任何 open stage 都能 on_hold（业务现实：客户暂停）"""
    open_stages = ["prospect", "qualified", "proposal", "negotiation"]
    for s in open_stages:
        assert "on_hold" in ALLOWED_STAGE_TRANSITIONS[s]


# ════════════════════════════════════════════════════════════
# DB 集成（需 fixture）
# ════════════════════════════════════════════════════════════

pytestmark = pytest.mark.asyncio


@pytest.mark.skip(reason="Requires async db fixture")
async def test_create_deal_and_advance_to_won(db, principal):
    """完整流程：create deal → qualified → proposal → negotiation → won"""
    from app.services.crm import deals_service

    deal = await deals_service.create_deal(
        db, principal=principal, tenant_id=principal.tenant_id,
        factory_slug="test-factory", name="Test Deal",
        estimated_value_usd=10000, probability_pct=10,
    )
    assert deal.stage == "prospect"

    # qualified
    deal = await deals_service.transition_stage(
        db, principal=principal, deal_id=deal.id, new_stage="qualified",
    )
    assert deal.stage == "qualified"
    assert deal.probability_pct == 25  # 默认 qualified

    # proposal
    deal = await deals_service.transition_stage(
        db, principal=principal, deal_id=deal.id, new_stage="proposal",
    )
    assert deal.probability_pct == 50

    # negotiation
    deal = await deals_service.transition_stage(
        db, principal=principal, deal_id=deal.id, new_stage="negotiation",
    )
    assert deal.probability_pct == 75

    # won + actual amount
    deal = await deals_service.transition_stage(
        db, principal=principal, deal_id=deal.id,
        new_stage="closed_won", won_value_usd=12000,
    )
    assert deal.stage == "closed_won"
    assert float(deal.won_value_usd) == 12000
    assert deal.won_at is not None
    assert deal.probability_pct == 100


@pytest.mark.skip(reason="Requires async db fixture")
async def test_invalid_stage_transition_raises(db, principal):
    """非法转换 raises ValueError"""
    from app.services.crm import deals_service

    deal = await deals_service.create_deal(
        db, principal=principal, tenant_id=principal.tenant_id,
        factory_slug="test-factory", name="Test Invalid",
    )
    # prospect → closed_won 不允许（必须经 qualified/proposal/negotiation）
    with pytest.raises(ValueError, match="Cannot transition"):
        await deals_service.transition_stage(
            db, principal=principal, deal_id=deal.id, new_stage="closed_won",
        )


@pytest.mark.skip(reason="Requires async db fixture")
async def test_pipeline_forecast_aggregation(db, principal):
    """forecast 聚合各 stage 金额"""
    from app.services.crm import deals_service

    # 创 3 个 deals 不同 stage
    for value, stage_target in [
        (1000, "prospect"), (2000, "qualified"), (5000, "proposal"),
    ]:
        d = await deals_service.create_deal(
            db, principal=principal, tenant_id=principal.tenant_id,
            factory_slug="test-factory", name=f"Deal-{value}",
            estimated_value_usd=value,
        )
        if stage_target != "prospect":
            await deals_service.transition_stage(
                db, principal=principal, deal_id=d.id, new_stage=stage_target,
            )

    forecast = await deals_service.get_pipeline_forecast(
        db, tenant_id=principal.tenant_id,
    )
    by_stage = {s["stage"]: s for s in forecast["by_stage"]}
    assert "prospect" in by_stage
    assert "qualified" in by_stage
    assert "proposal" in by_stage
    assert by_stage["prospect"]["total_estimated_usd"] == 1000
    assert by_stage["qualified"]["total_estimated_usd"] == 2000
    assert by_stage["proposal"]["total_estimated_usd"] == 5000
