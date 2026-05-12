"""leads_service 集成测试 · 测真实 service 调用（不走 HTTP · 直接 DB）

注意：跑这测试需要 pytest-asyncio + 测试 DB
本测试设计为可与真实 db 跑·conftest 注入 db fixture

跑：pytest tests/crm/test_leads_service.py -v
"""
from __future__ import annotations

import uuid

import pytest

from app.models.crm.lead import Lead
from app.services.crm import leads_service
from app.services.crm.classification import ClassificationInput, classify_lead


pytestmark = pytest.mark.asyncio


# ════════════════════════════════════════════════════════════
# 单元测试·不需 DB（纯算法）
# ════════════════════════════════════════════════════════════

def test_classification_returns_expected_db_fields():
    """to_db_dict 返完整 leads 字段"""
    result = classify_lead(ClassificationInput(
        inquiry_text="We need 1000 pcs of widgets. Budget $5000. By next month.",
        contact_email="buyer@acme.com",
        contact_company="ACME Corp",
        contact_role="Procurement Manager",
    ))
    db_dict = result.to_db_dict()
    assert "has_quantity" in db_dict
    assert "has_budget" in db_dict
    assert "has_timeline" in db_dict
    assert "has_specification" in db_dict
    assert "has_decision_role" in db_dict
    assert "has_company_info" in db_dict
    assert "six_factor_score" in db_dict
    assert "classification" in db_dict
    assert "six_factor_breakdown" in db_dict
    # 6 要素全 hit
    assert result.score >= 4
    assert result.classification in ("A", "B")


def test_classification_consistent_repeat_call():
    """重复调用 · 同输入 → 同输出"""
    inp = ClassificationInput(
        inquiry_text="Need 500 pcs ASAP, budget $2000",
        contact_email="x@y.com", contact_company="Y Co",
    )
    r1 = classify_lead(inp)
    r2 = classify_lead(inp)
    assert r1.score == r2.score
    assert r1.classification == r2.classification


def test_state_machine_allowed_transitions():
    """leads_service.ALLOWED_TRANSITIONS 完整性"""
    from app.services.crm.leads_service import ALLOWED_TRANSITIONS
    # new 必须能转到所有合理目标
    assert "contacted" in ALLOWED_TRANSITIONS["new"]
    assert "qualified" in ALLOWED_TRANSITIONS["new"]
    assert "spam" in ALLOWED_TRANSITIONS["new"]
    # converted/lost/spam 必须是死路或仅到 archived
    assert ALLOWED_TRANSITIONS["converted"] == {"archived"}
    assert ALLOWED_TRANSITIONS["lost"] == {"archived"}
    assert ALLOWED_TRANSITIONS["archived"] == set()


def test_state_machine_rejects_invalid_transition():
    """不允许跳过状态机（e.g. new → lost 直接走 lost 而非 contacted → lost）"""
    from app.services.crm.leads_service import ALLOWED_TRANSITIONS
    # new → lost? 看实际允不允许（"new" 状态机不直接到 lost · 要经 contacted）
    assert "lost" not in ALLOWED_TRANSITIONS["new"]
    # 但 contacted → lost 允许
    assert "lost" in ALLOWED_TRANSITIONS["contacted"]


# ════════════════════════════════════════════════════════════
# DB 集成测试（需 conftest async db fixture）
# 暂留 stub · conftest.py 完整接通后可激活
# ════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="Requires async db fixture · 接 conftest 后激活")
async def test_create_lead_inserts_with_classification(db, principal):
    """create_lead 完整流程：写 lead + 跑 6 要素 + audit"""
    lead = await leads_service.create_lead(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        factory_slug="test-factory",
        source="linkedin",
        inquiry_text="Need 1000 pcs widgets, budget $5000, by month end, "
                     "specs in attachment. We are ACME from US.",
        contact_email="buyer@acme.com",
        contact_company="ACME Corp",
        contact_role="VP Sourcing",
    )
    assert lead.id is not None
    assert lead.classification in ("A", "B")
    assert lead.six_factor_score >= 4
    assert lead.has_quantity
    assert lead.has_decision_role
    assert lead.status == "new"


@pytest.mark.skip(reason="Requires async db fixture")
async def test_lead_to_deal_conversion(db, principal):
    """lead → deal 流转"""
    # 先创 qualified lead
    lead = await leads_service.create_lead(
        db, principal=principal, tenant_id=principal.tenant_id,
        factory_slug="test-factory", source="linkedin",
        inquiry_text="High quality serious inquiry with all 6 factors",
        contact_email="ceo@bigco.com", contact_company="BigCo Ltd",
        contact_role="CEO",
    )
    await leads_service.transition_status(
        db, principal=principal, lead_id=lead.id, new_status="qualified",
    )

    # 转 deal
    lead_after, deal = await leads_service.convert_to_deal(
        db, principal=principal, lead_id=lead.id,
        deal_name="BigCo - Test Factory",
        estimated_value_usd=50000,
        probability_pct=60,
    )
    assert lead_after.status == "converted"
    assert lead_after.converted_to_deal_id == deal.id
    assert deal.stage == "prospect"
    assert float(deal.estimated_value_usd) == 50000
    assert float(deal.weighted_value_usd) == 30000  # 50000 × 60%


@pytest.mark.skip(reason="Requires async db fixture")
async def test_override_classification_preserves_audit(db, principal):
    """手工 override 写 audit"""
    lead = await leads_service.create_lead(
        db, principal=principal, tenant_id=principal.tenant_id,
        factory_slug="test-factory", source="email",
        inquiry_text="vague short message",
    )
    assert lead.classification in ("C", "D")
    # BD 手工调到 B（认为客户实际意图强）
    overridden = await leads_service.override_classification(
        db, principal=principal, lead_id=lead.id,
        new_classification="B", reason="Met buyer at trade show, qualified",
    )
    assert overridden.classification == "B"
    assert overridden.classification_overridden is True
    assert overridden.classification_overridden_by == principal.user_id


@pytest.mark.skip(reason="Requires async db fixture")
async def test_assign_lead_logs_audit(db, principal, another_user):
    """assign 写 audit + 更新字段"""
    lead = await leads_service.create_lead(
        db, principal=principal, tenant_id=principal.tenant_id,
        factory_slug="test-factory", source="linkedin",
        inquiry_text="Test inquiry",
    )
    assigned = await leads_service.assign(
        db, principal=principal, lead_id=lead.id,
        assignee_user_id=another_user.id,
    )
    assert assigned.assigned_user_id == another_user.id
    assert assigned.assigned_at is not None
