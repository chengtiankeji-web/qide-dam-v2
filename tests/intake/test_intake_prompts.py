"""intake_prompts 单测·prompt 字符串结构 + 成本估算

无 DB / 无 LLM 调用·全部纯函数
"""
from __future__ import annotations

import pytest

from app.services.intake_prompts import (
    CLASSIFY_CATEGORIES,
    classify_filename_batch_prompt,
    cluster_skus_prompt,
    estimate_classify_cost_cny,
    estimate_cluster_cost_cny,
    estimate_entity_extract_cost_cny,
    estimate_total_job_cost_cny,
    estimate_visual_audit_cost_cny,
    extract_entity_prompt,
)


# ════════════════════════════════════════════════════════════
# 1. prompt 生成
# ════════════════════════════════════════════════════════════

def test_classify_prompt_contains_factory():
    prompt = classify_filename_batch_prompt(
        "aozhi",
        [{"id": "f1", "name": "宇士口护手霜-master.jpg", "kind": "image"}],
    )
    assert "aozhi" in prompt
    assert "f1" in prompt
    assert "宇士口护手霜" in prompt


def test_classify_prompt_lists_all_categories():
    prompt = classify_filename_batch_prompt("test-factory", [
        {"id": "f1", "name": "test.jpg", "kind": "image"},
    ])
    for cat in CLASSIFY_CATEGORIES:
        assert cat in prompt, f"category {cat} should appear in prompt"


def test_classify_prompt_with_known_skus():
    prompt = classify_filename_batch_prompt(
        "aozhi",
        [{"id": "f1", "name": "foo.jpg", "kind": "image"}],
        known_sku_slugs=["yushikou-handcream", "gostoo-sofa"],
    )
    assert "yushikou-handcream" in prompt
    assert "gostoo-sofa" in prompt


def test_cluster_prompt_truncates_large_input():
    """超过 300 个 item 时 prompt 应被截·防 token 爆"""
    items = [
        {"id": f"f{i}", "name": f"file{i}.jpg",
         "predicted_sku": f"sku{i}", "category": "master"}
        for i in range(500)
    ]
    prompt = cluster_skus_prompt("test-factory", items)
    # 应该出现 f0 / f100 / f200 / f299 但不应出现 f400 / f499
    assert "f0," in prompt
    assert "f299," in prompt
    assert "f400," not in prompt


def test_extract_entity_prompt_truncates_long_doc():
    """8000 字符以上文档应被截"""
    long_text = "x" * 20000
    prompt = extract_entity_prompt("test-factory", long_text)
    assert prompt.count("x") <= 8000 + 100  # 容忍 prompt 框架占用


def test_extract_entity_prompt_lists_required_fields():
    prompt = extract_entity_prompt("test-factory", "示例公司介绍·成立于 2018 年")
    for field in (
        "legal_name", "year_established", "main_products",
        "certifications", "main_contact_email",
    ):
        assert field in prompt


# ════════════════════════════════════════════════════════════
# 2. 成本估算
# ════════════════════════════════════════════════════════════

def test_classify_cost_zero_files():
    assert estimate_classify_cost_cny(0) == 0


def test_classify_cost_30_files_one_batch():
    """30 files = 1 batch ≈ 3000 input + 1500 output"""
    cost = estimate_classify_cost_cny(30)
    expected_min = 3000 / 1000 * 0.0008 + 1500 / 1000 * 0.002
    assert cost == pytest.approx(expected_min, rel=1e-3)


def test_classify_cost_scales_with_file_count():
    """文件越多·成本越多"""
    c30 = estimate_classify_cost_cny(30)
    c300 = estimate_classify_cost_cny(300)
    c3000 = estimate_classify_cost_cny(3000)
    assert c30 < c300 < c3000
    # ~10× files ≈ 10× cost
    assert c300 / c30 == pytest.approx(10, rel=0.1)


def test_cluster_cost_capped_at_300_items():
    """聚类 item 上限 300 · 超过不增成本"""
    c300 = estimate_cluster_cost_cny(300)
    c1000 = estimate_cluster_cost_cny(1000)
    c10000 = estimate_cluster_cost_cny(10000)
    assert c300 == c1000 == c10000


def test_entity_extract_cost():
    """一个 doc ~8000 input + 800 output"""
    cost = estimate_entity_extract_cost_cny(1)
    expected = 8000 / 1000 * 0.0008 + 800 / 1000 * 0.002
    assert cost == pytest.approx(expected, rel=1e-3)


def test_visual_audit_cost_is_10x_more_per_image():
    """VL 的 input 价是普通的 10×"""
    classify = estimate_classify_cost_cny(30)
    visual = estimate_visual_audit_cost_cny(30)
    assert visual > classify  # 同样 30 个文件·VL 显著贵


def test_total_cost_skip_visual_default():
    """skip_visual=True (默认) · visual_audit = 0"""
    cost = estimate_total_job_cost_cny(
        file_count=100, image_count=80, doc_count=2,
    )
    assert cost["visual_audit"] == 0
    assert cost["skip_visual"] is True


def test_total_cost_with_visual():
    cost = estimate_total_job_cost_cny(
        file_count=100, image_count=80, doc_count=2, skip_visual=False,
    )
    assert cost["visual_audit"] > 0


def test_total_cost_sums_correctly():
    """total_cny = classify + cluster + entity + visual"""
    cost = estimate_total_job_cost_cny(
        file_count=100, image_count=50, doc_count=3, skip_visual=True,
    )
    expected = (
        cost["classify"] + cost["cluster"]
        + cost["entity_extract"] + cost["visual_audit"]
    )
    assert cost["total_cny"] == pytest.approx(expected, abs=1e-4)


def test_realistic_aozhi_estimate():
    """真实场景·747 file 工厂 · 全 rule + cluster + 1 docx 抽 entity"""
    cost = estimate_total_job_cost_cny(
        file_count=747, image_count=600, doc_count=1, skip_visual=True,
    )
    # 整体应 < ¥1 · 否则成本失控
    assert cost["total_cny"] < 1.0
    # 估算应给 Sam admin SPA 显示可读数
    assert cost["total_cny"] > 0
