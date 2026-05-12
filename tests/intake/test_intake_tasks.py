"""tasks_intake 单测·rule classifier + sku slug 抽取 + ext kind 映射

不依赖 Celery / DB / R2 · 仅测纯函数
"""
from __future__ import annotations

import pytest

from app.workers.tasks_intake import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    _extract_sku_slug,
    _kind_from_ext,
    _rule_classify,
)


# ════════════════════════════════════════════════════════════
# kind from ext
# ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ext,expected", [
    (".jpg", "image"), (".JPEG", "image"), (".png", "image"),
    (".mp4", "video"), (".mov", "video"),
    (".mp3", "audio"),
    (".pdf", "document"), (".docx", "document"),
    (".zip", "archive"),
    (".xyz", "other"), ("", "other"),
])
def test_kind_from_ext(ext, expected):
    assert _kind_from_ext(ext) == expected


def test_image_exts_cover_common_formats():
    for e in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        assert e in IMAGE_EXTS


# ════════════════════════════════════════════════════════════
# rule classifier
# ════════════════════════════════════════════════════════════

def test_classify_license_high_confidence():
    cat, conf, _ = _rule_classify("营业执照-2024.pdf", "document")
    assert cat == "license"
    assert conf >= 0.8


def test_classify_brand_logo():
    cat, conf, _ = _rule_classify("brand-logo-final.png", "image")
    assert cat == "brand-logo"


def test_classify_catalog_pdf():
    cat, _, _ = _rule_classify("Product-Catalog-2024.pdf", "document")
    assert cat == "catalog"


def test_classify_packaging():
    cat, _, _ = _rule_classify("外箱包装图-01.jpg", "image")
    assert cat == "packaging"


def test_classify_factory():
    cat, _, _ = _rule_classify("生产车间-workshop.jpg", "image")
    assert cat == "factory"


def test_classify_detail():
    cat, _, _ = _rule_classify("detail-closeup-material.jpg", "image")
    assert cat == "detail"


def test_classify_lifestyle():
    cat, _, _ = _rule_classify("model-lifestyle-scene.jpg", "image")
    assert cat == "lifestyle"


def test_classify_unknown_image_to_master():
    """无关键词的 image 默认 master · confidence 低 + flagged"""
    cat, conf, flagged = _rule_classify("DSC_0001.jpg", "image")
    assert cat == "master"
    assert conf < 0.6
    assert flagged == "low_confidence"


def test_classify_video_always_high_confidence():
    cat, conf, flagged = _rule_classify("anything.mp4", "video")
    assert cat == "video"
    assert conf >= 0.9
    assert flagged is None


def test_classify_audio_flagged():
    """音频不在主流程·flagged 让人看一眼"""
    cat, _, flagged = _rule_classify("music.mp3", "audio")
    assert flagged == "unknown_format"


# ════════════════════════════════════════════════════════════
# SKU slug extraction
# ════════════════════════════════════════════════════════════

def test_sku_slug_from_simple_filename():
    slug = _extract_sku_slug("yushikou-handcream-master-01.jpg")
    assert slug == "yushikou"


def test_sku_slug_strips_version_suffix():
    slug = _extract_sku_slug("modern-sofa-v2.png")
    # 去掉 v2 后取 modern
    assert slug == "modern"


def test_sku_slug_skips_pure_digits():
    """全数字片段不应被选为 sku · 选第一个字母片段"""
    slug = _extract_sku_slug("001-002-yushikou.jpg")
    assert slug == "yushikou"


def test_sku_slug_handles_chinese():
    """中文片段·当前 fallback _safe_slug 会 strip 掉中文·返默认 unnamed"""
    slug = _extract_sku_slug("宇士口护手霜.png")
    assert slug in ("unnamed", "")  # v4.1 接入拼音转换前的预期行为


def test_sku_slug_truncates_long():
    long_name = "a" * 200 + ".jpg"
    slug = _extract_sku_slug(long_name)
    assert slug is not None
    assert len(slug) <= 128
