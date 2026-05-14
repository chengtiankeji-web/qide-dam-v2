"""测试 6 要素分级算法

跑：pytest tests/crm/test_classification.py -v

测试 ground truth = 真实询盘范例（来自 memory/context/cmh-contract-template.md
+ CMH 实际收到询盘 + 手工 label）
"""
from __future__ import annotations

from app.services.crm.classification import (
    ClassificationInput,
    classify_lead,
    detect_budget,
    detect_company_info,
    detect_specification,
)

# ════════════════════════════════════════════════════════════
# A 类·6 要素全·决策人·应 24h 内跟
# ════════════════════════════════════════════════════════════

CASE_A_GOLD = """
Hi, I'm Marcus Chen, Head of Procurement at Restoration Hardware.

We're sourcing custom upholstered sofas for our 2026 catalog launch.
Specifications: 3-seater, 220cm × 90cm × 85cm, premium velvet upholstery,
hardwood frame, charcoal grey color.

Initial order: 1500 pcs.
Budget: $480 - $620 per unit (USD).
Timeline: First shipment by mid-July, full delivery before Q4 2026.

Could you send a quote with FOB Shanghai pricing + production timeline?
Please attach material samples and a detailed spec sheet.

Best regards,
Marcus Chen
Head of Procurement | Restoration Hardware
marcus.chen@restorationhardware.com
+1 415-555-0192
"""

def test_class_a_gold_standard():
    """6 要素全 + 决策人 + 公司域名 = A 类"""
    result = classify_lead(ClassificationInput(
        inquiry_text=CASE_A_GOLD,
        contact_email="marcus.chen@restorationhardware.com",
        contact_company="Restoration Hardware",
        contact_role="Head of Procurement",
        has_attachments=False,
    ))
    assert result.has_quantity, f"未捕获数量·breakdown={result.breakdown['quantity']}"
    assert result.has_budget, f"未捕获预算·{result.breakdown['budget']}"
    assert result.has_timeline, f"未捕获时限·{result.breakdown['timeline']}"
    assert result.has_specification, f"未捕获规格·{result.breakdown['specification']}"
    assert result.has_decision_role, f"未捕获决策人·{result.breakdown['decision_role']}"
    assert result.has_company_info, f"未捕获公司·{result.breakdown['company_info']}"
    assert result.score == 6
    assert result.classification == "A"


# ════════════════════════════════════════════════════════════
# A 类·5 要素 + 决策人
# ════════════════════════════════════════════════════════════

def test_class_a_five_with_role():
    """5 要素 + 决策人 = A 类（哪怕缺预算）"""
    text = """
    Hi, we need 5000 pcs of LED strip lights.
    Spec: 5050 SMD, 12V, IP65 waterproof, 60 leds/m, warm white 2700K.
    Lead time: 30 days. Please send quote ASAP.

    Best,
    Jennifer Park
    Director of Sourcing
    """
    result = classify_lead(ClassificationInput(
        inquiry_text=text,
        contact_email="j.park@homedepot.com",
        contact_company="Home Depot",
        contact_role="Director of Sourcing",
    ))
    assert result.score >= 5
    assert result.has_decision_role
    assert result.classification == "A"


# ════════════════════════════════════════════════════════════
# B 类·3-4 要素
# ════════════════════════════════════════════════════════════

def test_class_b_three_factors():
    """3 要素 = B 类"""
    text = """
    Hi, looking for 200 units of stainless steel water bottles.
    Need by end of June. Send catalog please.
    """
    result = classify_lead(ClassificationInput(
        inquiry_text=text,
        contact_email="info@bottlestore.com",
        contact_company="BottleStore",
    ))
    # 期望：数量 + 时限 + 规格（"stainless steel"）+ 公司域名
    assert result.score >= 3, f"得分 {result.score} / breakdown={result.breakdown}"
    assert result.classification in ("A", "B")


# ════════════════════════════════════════════════════════════
# C 类·1-2 要素·进 nurture
# ════════════════════════════════════════════════════════════

def test_class_c_minimal():
    """只有 1-2 要素 = C 类·进邮件 nurture"""
    text = "Hi, can you tell me your products?"
    result = classify_lead(ClassificationInput(
        inquiry_text=text,
        contact_email="bob@gmail.com",
    ))
    assert result.score <= 2
    assert result.classification in ("C", "D")


# ════════════════════════════════════════════════════════════
# D 类·0 要素 / spam
# ════════════════════════════════════════════════════════════

def test_class_d_spam():
    """0 要素 = D 类·spam"""
    text = "hello"
    result = classify_lead(ClassificationInput(
        inquiry_text=text,
        contact_email="anonymous@gmail.com",
    ))
    assert result.score == 0
    assert result.classification == "D"


# ════════════════════════════════════════════════════════════
# 中文询盘
# ════════════════════════════════════════════════════════════

def test_chinese_inquiry_class_a():
    """中文 A 类询盘·6 要素齐"""
    text = """
    您好，我们是迪拜 Al Saud 建材集团采购总监李明。

    需要采购 800 套定制实木餐桌椅。
    规格：餐桌 1.8m × 1.0m × 0.75m，配 6 把餐椅。
    材质：橡木 + 真皮坐垫。
    预算：每套 $350-450（USD）。
    时限：8 月底前发货。

    请发 PI + FOB 上海报价。

    李明 / 采购总监
    Al Saud Building Materials Group
    li.ming@alsaud.ae
    """
    result = classify_lead(ClassificationInput(
        inquiry_text=text,
        contact_email="li.ming@alsaud.ae",
        contact_company="Al Saud Building Materials Group",
        contact_role="采购总监",
    ))
    # 中文 + 英文混合·6 要素都该 hit
    assert result.score >= 5
    assert result.has_decision_role
    assert result.classification == "A"


# ════════════════════════════════════════════════════════════
# 边界情况
# ════════════════════════════════════════════════════════════

def test_phone_number_not_budget():
    """电话号码 +86 18929299341 不应被误识别为预算"""
    text = "Call me at +86 18929299341 anytime"
    factor = detect_budget(text)
    assert not factor.detected, f"误判电话号码为预算·{factor.evidence}"


def test_personal_email_not_company_info():
    """gmail / yahoo 等个人邮箱不算公司信息（除非有公司名 field）"""
    text = "I'm interested in your products"
    factor = detect_company_info(
        text,
        contact_email="bob.smith@gmail.com",
    )
    assert not factor.detected


def test_personal_email_with_company_name_in_field():
    """个人邮箱但公司名 field 已填 = 算 company_info"""
    text = "I'm interested in your products"
    factor = detect_company_info(
        text,
        contact_email="bob@gmail.com",
        contact_company="Acme Corp",
    )
    assert factor.detected


def test_attachments_count_as_spec():
    """有附件且文本没规格 = 弱证据 spec"""
    factor = detect_specification("Please send your best price", has_attachments=True)
    assert factor.detected
    assert factor.confidence < 1.0


def test_attachments_dont_override_real_spec():
    """有附件也有文本规格 = 满分"""
    factor = detect_specification(
        "We need stainless steel grade 304, 2mm thickness",
        has_attachments=True,
    )
    assert factor.detected
    assert factor.confidence == 1.0


# ════════════════════════════════════════════════════════════
# Override 流程（BD 手工改）
# ════════════════════════════════════════════════════════════

def test_classification_overridden_field_exists():
    """算法不直接 override·写到 leads 表后 BD 手工改 + 标记 overridden=true"""
    result = classify_lead(ClassificationInput(
        inquiry_text="Test inquiry with full 6 factors",
        contact_email="test@company.com",
    ))
    db_dict = result.to_db_dict()
    # 算法不返 overridden（应 default false in DB）
    assert "classification_overridden" not in db_dict
    # BD service 层会改 leads.classification + classification_overridden=true


# ════════════════════════════════════════════════════════════
# Breakdown 详情（BD 看到为啥被分这类）
# ════════════════════════════════════════════════════════════

def test_breakdown_provides_evidence():
    """每要素 detected=true 时 evidence 不为空"""
    result = classify_lead(ClassificationInput(
        inquiry_text=CASE_A_GOLD,
        contact_email="marcus.chen@restorationhardware.com",
        contact_company="Restoration Hardware",
        contact_role="Head of Procurement",
    ))
    db_dict = result.to_db_dict()
    breakdown = db_dict["six_factor_breakdown"]
    for factor_name, factor_data in breakdown.items():
        if factor_data["detected"]:
            assert len(factor_data["evidence"]) > 0, \
                f"要素 {factor_name} detected=true 但 evidence 为空"


# ════════════════════════════════════════════════════════════
# 性能（无 LLM 调用·应 < 100ms / 询盘）
# ════════════════════════════════════════════════════════════

def test_performance_fast():
    import time
    start = time.time()
    for _ in range(100):
        classify_lead(ClassificationInput(
            inquiry_text=CASE_A_GOLD * 5,  # 长文本 5×
            contact_email="test@company.com",
            contact_company="Test Corp",
            contact_role="Director",
        ))
    elapsed = time.time() - start
    assert elapsed < 5.0, f"100 次询盘分类耗时 {elapsed:.2f}s 过慢"
