"""6 要素分级算法·扩展测试·覆盖边界 + 真实询盘"""
from __future__ import annotations

from app.services.crm.classification import (
    ClassificationInput,
    classify_lead,
    detect_budget,
    detect_company_info,
    detect_decision_role,
    detect_quantity,
    detect_timeline,
)

# ════════════════════════════════════════════════════════════
# 真实询盘片段·从 CMH / 客户实际收到的模式
# ════════════════════════════════════════════════════════════

REAL_CASE_DRIP_NURTURE = """
Hello! I saw your factory page on ChinaMakersHub. We are looking into
custom furniture for our retail chain. Could you send me your catalog?
- Sarah from Mumbai
"""

REAL_CASE_PRICE_FISHING = """
Dear Sir,
We are a trading company in Egypt. Please send us your best price
for sofa. Thanks.
"""

REAL_CASE_SERIOUS_BUYER = """
Hi team,

I'm reaching out from Bedrock Hotels NYC. We are renovating 12 boutique
hotels (240 rooms total) and need 240 sets of king-size bed frames +
nightstands.

Specs: solid oak frame, 200cm × 200cm × 90cm headboard, brushed brass
accents. Material: FSC certified wood, low VOC finish.

Timeline: 50% delivered by end of August, remainder by Q4 2026.
Budget: $850-$1200 per set (USD, FOB Shanghai).

We have an inspection team in Shenzhen for QC.

Please quote with payment terms (we typically do 30% TT + 70% on inspection)
and lead time estimate.

Best,
Jeffrey Liu
VP of Procurement, Bedrock Hospitality Group
jeffrey.liu@bedrockhotels.com
"""


def test_drip_nurture_case():
    """轻信号 · 应进 C 类 nurture"""
    result = classify_lead(ClassificationInput(
        inquiry_text=REAL_CASE_DRIP_NURTURE,
        # 注：contact_name / contact_country 仅 leads 表字段·不进 algorithm
        # 算法只看：inquiry_text / contact_email / contact_company / contact_role
    ))
    # 个人邮箱 · 没具体规格 / 数量 / 预算 / 时限
    assert result.score <= 2
    assert result.classification in ("C", "D")


def test_price_fishing_case():
    """泛 price fishing · 应进 C 类"""
    result = classify_lead(ClassificationInput(
        inquiry_text=REAL_CASE_PRICE_FISHING,
        contact_company="Trading Company Egypt",
    ))
    # 提了 sofa（弱 spec）+ 提了公司 = 2 分
    assert result.classification in ("B", "C")


def test_serious_buyer_full_a():
    """完整 A 类买家 · 6 要素齐全"""
    result = classify_lead(ClassificationInput(
        inquiry_text=REAL_CASE_SERIOUS_BUYER,
        contact_email="jeffrey.liu@bedrockhotels.com",
        contact_company="Bedrock Hospitality Group",
        contact_role="VP of Procurement",
    ))
    assert result.has_quantity   # "240 sets"
    assert result.has_budget     # "$850-$1200"
    assert result.has_timeline   # "by end of August" / "Q4 2026"
    assert result.has_specification  # "solid oak / FSC / etc."
    assert result.has_decision_role  # "VP of Procurement"
    assert result.has_company_info   # domain email + company field
    assert result.score == 6
    assert result.classification == "A"


# ════════════════════════════════════════════════════════════
# Edge cases
# ════════════════════════════════════════════════════════════

def test_empty_text():
    """空询盘 · score=0"""
    result = classify_lead(ClassificationInput(inquiry_text=""))
    assert result.score == 0
    assert result.classification == "D"


def test_chinese_only_decision_role():
    """中文决策人角色识别"""
    factor = detect_decision_role(
        "您好",
        contact_role="采购总监",
    )
    assert factor.detected


def test_chinese_only_company():
    """中文公司识别"""
    factor = detect_company_info(
        "我们是顺德博能茶业有限公司·想采购陈皮",
        contact_email="info@example.com",
    )
    assert factor.detected


def test_quantity_with_chinese_units():
    """中文量词识别"""
    factor = detect_quantity("我们需要 500 套餐桌椅")
    assert factor.detected


def test_budget_with_chinese_currency():
    """人民币预算识别"""
    factor = detect_budget("预算 200000 元")
    assert factor.detected


def test_timeline_chinese_month():
    """中文月份+前/底/内"""
    factor = detect_timeline("8 月底前发货")
    assert factor.detected


def test_breakdown_has_evidence_for_a_class():
    """A 类 · 每要素都有 evidence"""
    result = classify_lead(ClassificationInput(
        inquiry_text=REAL_CASE_SERIOUS_BUYER,
        contact_email="jeffrey.liu@bedrockhotels.com",
        contact_company="Bedrock Hospitality Group",
        contact_role="VP of Procurement",
    ))
    db_dict = result.to_db_dict()
    breakdown = db_dict["six_factor_breakdown"]
    for _factor_name, factor_data in breakdown.items():
        if factor_data["detected"]:
            assert len(factor_data["evidence"]) > 0


def test_override_path_dataclass_consistency():
    """LeadOut → DB dict 字段 round-trip"""
    result = classify_lead(ClassificationInput(
        inquiry_text=REAL_CASE_SERIOUS_BUYER,
        contact_email="jeffrey.liu@bedrockhotels.com",
        contact_company="Bedrock",
        contact_role="VP of Procurement",
    ))
    db_dict = result.to_db_dict()
    # 必须含 6 个布尔 + score + classification + breakdown
    for k in ["has_quantity", "has_budget", "has_timeline",
              "has_specification", "has_decision_role", "has_company_info"]:
        assert k in db_dict
        assert isinstance(db_dict[k], bool)
    assert db_dict["six_factor_score"] == 6
    assert db_dict["classification"] == "A"
    assert "six_factor_breakdown" in db_dict
