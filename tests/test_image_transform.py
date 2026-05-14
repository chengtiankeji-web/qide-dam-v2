"""image_transform_service 单测 · URL parser + cache key

不依赖 Pillow（仅测 parser）· render 测试留待 v4.1 接入 Pillow worker fixture
"""
from __future__ import annotations

import uuid

import pytest

from app.services.image_transform_service import (
    ALLOWED_CROP_MODES,
    ALLOWED_FORMATS,
    derived_storage_key,
    parse_transforms,
)

# ════════════════════════════════════════════════════════════
# parse_transforms · 合法 cases
# ════════════════════════════════════════════════════════════

def test_parse_basic_fill():
    t = parse_transforms("c_fill,w_400,h_400,q_85,f_webp", "jpg")
    # 注：format 来自 token f_webp · 不是 ext jpg
    assert t.crop_mode == "fill"
    assert t.width == 400
    assert t.height == 400
    assert t.quality == 85
    assert t.format == "webp"
    assert t.is_original is False


def test_parse_fit_with_width_only():
    t = parse_transforms("c_fit,w_1200", "jpg")
    assert t.crop_mode == "fit"
    assert t.width == 1200
    assert t.height is None
    assert t.format == "jpg"


def test_parse_original_passthrough():
    t = parse_transforms("original", "jpg")
    assert t.is_original is True


def test_parse_gravity():
    t = parse_transforms("c_fill,w_300,h_300,g_north", "jpg")
    assert t.gravity == "north"


def test_parse_quality_range_boundary():
    t = parse_transforms("c_fit,w_100,q_1", "jpg")
    assert t.quality == 1
    t = parse_transforms("c_fit,w_100,q_100", "jpg")
    assert t.quality == 100


def test_parse_crop_with_xy():
    t = parse_transforms("c_crop,x_100,y_50,w_300,h_300", "jpg")
    assert t.crop_mode == "crop"
    assert t.crop_x == 100
    assert t.crop_y == 50


# ════════════════════════════════════════════════════════════
# parse_transforms · 错误 cases
# ════════════════════════════════════════════════════════════

def test_parse_rejects_unknown_format_ext():
    with pytest.raises(ValueError, match="unsupported output format"):
        parse_transforms("c_fit,w_100", "bmp")


def test_parse_rejects_huge_width():
    with pytest.raises(ValueError, match="width out of range"):
        parse_transforms("c_fit,w_99999", "jpg")


def test_parse_rejects_zero_width():
    with pytest.raises(ValueError, match="width out of range"):
        parse_transforms("c_fit,w_0", "jpg")


def test_parse_rejects_quality_over_100():
    with pytest.raises(ValueError, match="quality out of range"):
        parse_transforms("c_fit,w_100,q_150", "jpg")


def test_parse_rejects_invalid_crop_mode():
    with pytest.raises(ValueError, match="invalid crop mode"):
        parse_transforms("c_squash,w_100", "jpg")


def test_parse_rejects_malformed_token():
    with pytest.raises(ValueError, match="malformed transform token"):
        parse_transforms("notatoken", "jpg")


def test_parse_rejects_invalid_gravity():
    with pytest.raises(ValueError, match="invalid gravity"):
        parse_transforms("c_fill,w_100,h_100,g_diagonal", "jpg")


def test_parse_crop_requires_xy_wh():
    with pytest.raises(ValueError, match="c_crop requires"):
        parse_transforms("c_crop", "jpg")


def test_parse_unknown_prefix():
    with pytest.raises(ValueError, match="unknown transform prefix"):
        parse_transforms("z_42", "jpg")


# ════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════

def test_allowed_formats_includes_modern():
    for fmt in ("webp", "avif"):
        assert fmt in ALLOWED_FORMATS


def test_allowed_crop_modes_4():
    assert ALLOWED_CROP_MODES == {"fill", "fit", "crop", "thumb"}


# ════════════════════════════════════════════════════════════
# derived_storage_key
# ════════════════════════════════════════════════════════════

def test_derived_key_contains_asset_id_prefix():
    aid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    t = parse_transforms("c_fit,w_100", "jpg")
    key = derived_storage_key(asset_id=aid, transform=t)
    assert key.startswith("derived/12/")
    assert str(aid) in key
    assert key.endswith(".jpg")


def test_derived_key_changes_with_transform():
    aid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    k1 = derived_storage_key(
        asset_id=aid,
        transform=parse_transforms("c_fit,w_100", "jpg"),
    )
    k2 = derived_storage_key(
        asset_id=aid,
        transform=parse_transforms("c_fit,w_200", "jpg"),
    )
    assert k1 != k2


def test_derived_key_stable_for_same_input():
    aid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    t = parse_transforms("c_fit,w_100,q_85", "jpg")
    k1 = derived_storage_key(asset_id=aid, transform=t)
    k2 = derived_storage_key(asset_id=aid, transform=t)
    assert k1 == k2


def test_derived_key_different_format():
    aid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    k_jpg = derived_storage_key(
        asset_id=aid,
        transform=parse_transforms("c_fit,w_100", "jpg"),
    )
    k_webp = derived_storage_key(
        asset_id=aid,
        transform=parse_transforms("c_fit,w_100,f_webp", "jpg"),
    )
    assert k_jpg != k_webp
    assert k_jpg.endswith(".jpg")
    assert k_webp.endswith(".webp")
