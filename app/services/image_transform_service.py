"""URL-based image transforms · Cloudinary-style API

URL 形式：
  GET /img/{asset_id}/{transforms}.{ext}

例子：
  /img/abc-123/c_fill,w_400,h_400,q_85,f_webp.jpg     · 400×400 填充裁切·webp 输出
  /img/abc-123/c_fit,w_1200,q_80.jpg                  · 1200 宽限制·保比·jpg q80
  /img/abc-123/c_crop,x_100,y_50,w_300,h_300.png      · 自定义裁切
  /img/abc-123/h_100,g_face.jpg                       · 100 高·人脸居中
  /img/abc-123/original.jpg                           · 原图（pass-through）

支持的变换：
  c_fill   · cover (裁剪填满)
  c_fit    · contain (保比缩放·留白)
  c_crop   · 自定义裁切（必带 x,y,w,h）
  c_thumb  · 缩略图（按短边裁正方再缩放）

  w_<num>  · 宽
  h_<num>  · 高
  q_<num>  · JPEG/WebP 质量 0-100（默认 85）
  f_<fmt>  · 输出格式 jpg/png/webp/avif
  g_<pos>  · 裁剪重力 center/face/north/south/east/west/north_east/...

⚠️ 缓存：
  - 解析后的 (asset_id, transforms) → R2 缓存 key  derived/{sha256_first_8}/{asset_id}/{transforms_hash}.{ext}
  - 第一次 miss → Pillow 生成 → 写 R2 → 返
  - 之后命中 → 308 redirect 到 R2 cdn url

⚠️ 安全：
  - 仅 public ACL / sensitivity ≤ internal 的 asset 才能匿名访问
  - confidential 必带 short-lived token query param
  - secret + vault_* 永远 403
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# Transform parser
# ════════════════════════════════════════════════════════════

ALLOWED_FORMATS = {"jpg", "jpeg", "png", "webp", "avif", "gif"}
ALLOWED_CROP_MODES = {"fill", "fit", "crop", "thumb"}
ALLOWED_GRAVITY = {
    "center", "face", "north", "south", "east", "west",
    "north_east", "north_west", "south_east", "south_west",
}
MAX_DIM = 4096  # 防 OOM 攻击
MIN_DIM = 1


@dataclass
class ImageTransform:
    """已解析的变换参数·所有字段可选（除 asset_id）"""
    crop_mode: str = "fit"
    width: Optional[int] = None
    height: Optional[int] = None
    crop_x: Optional[int] = None  # c_crop 专用
    crop_y: Optional[int] = None
    quality: int = 85
    format: str = "jpg"
    gravity: str = "center"
    is_original: bool = False  # /original.jpg

    def cache_key_suffix(self) -> str:
        """稳定的 cache key · 防同义变换出多个缓存"""
        if self.is_original:
            return "original"
        parts = [
            f"c_{self.crop_mode}",
            f"w_{self.width or 0}",
            f"h_{self.height or 0}",
            f"q_{self.quality}",
            f"g_{self.gravity}",
        ]
        if self.crop_mode == "crop":
            parts.extend([f"x_{self.crop_x or 0}", f"y_{self.crop_y or 0}"])
        return ",".join(parts) + f".{self.format}"


_TRANSFORM_TOKEN = re.compile(r"^([a-z])_([a-zA-Z0-9_]+)$")


def parse_transforms(token_str: str, ext: str) -> ImageTransform:
    """把 "c_fill,w_400,h_400,q_85,f_webp" + ext "jpg" → ImageTransform

    若 token_str == "original" → is_original=True · 直接 pass-through 原图
    """
    if not ext or ext.lower() not in ALLOWED_FORMATS:
        raise ValueError(
            f"unsupported output format: {ext!r} · allowed: {sorted(ALLOWED_FORMATS)}"
        )
    norm_ext = "jpg" if ext.lower() == "jpeg" else ext.lower()

    t = ImageTransform(format=norm_ext)

    if token_str.strip().lower() == "original":
        t.is_original = True
        return t

    for tok in token_str.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = _TRANSFORM_TOKEN.match(tok)
        if not m:
            raise ValueError(f"malformed transform token: {tok!r}")
        prefix, value = m.group(1), m.group(2)

        if prefix == "c":
            if value not in ALLOWED_CROP_MODES:
                raise ValueError(
                    f"invalid crop mode {value!r} · allowed: {sorted(ALLOWED_CROP_MODES)}"
                )
            t.crop_mode = value
        elif prefix == "w":
            n = int(value)
            if not (MIN_DIM <= n <= MAX_DIM):
                raise ValueError(f"width out of range: {n} (must be {MIN_DIM}-{MAX_DIM})")
            t.width = n
        elif prefix == "h":
            n = int(value)
            if not (MIN_DIM <= n <= MAX_DIM):
                raise ValueError(f"height out of range: {n} (must be {MIN_DIM}-{MAX_DIM})")
            t.height = n
        elif prefix == "x":
            t.crop_x = int(value)
        elif prefix == "y":
            t.crop_y = int(value)
        elif prefix == "q":
            n = int(value)
            if not (1 <= n <= 100):
                raise ValueError(f"quality out of range: {n}")
            t.quality = n
        elif prefix == "f":
            if value not in ALLOWED_FORMATS:
                raise ValueError(f"invalid format {value!r}")
            t.format = "jpg" if value == "jpeg" else value
        elif prefix == "g":
            if value not in ALLOWED_GRAVITY:
                raise ValueError(
                    f"invalid gravity {value!r} · allowed: {sorted(ALLOWED_GRAVITY)}"
                )
            t.gravity = value
        else:
            raise ValueError(f"unknown transform prefix: {prefix!r}")

    if t.crop_mode == "crop":
        if t.crop_x is None or t.crop_y is None or t.width is None or t.height is None:
            raise ValueError("c_crop requires x_, y_, w_, h_ parameters")

    return t


# ════════════════════════════════════════════════════════════
# Cache key
# ════════════════════════════════════════════════════════════

def derived_storage_key(*, asset_id: uuid.UUID, transform: ImageTransform) -> str:
    """R2 上的派生图 storage key

    形如 derived/{asset_uuid_prefix}/{asset_id}/{transforms}.{ext}
    """
    aid = str(asset_id)
    bucket_prefix = aid[:2]  # 散到 16 × 16 = 256 prefix（避免 R2 单 prefix 过多对象）
    suffix = transform.cache_key_suffix()
    # transforms 字符串本身可能很长·hash 一下保短
    suffix_hash = hashlib.sha256(suffix.encode()).hexdigest()[:16]
    return f"derived/{bucket_prefix}/{aid}/{suffix_hash}.{transform.format}"


# ════════════════════════════════════════════════════════════
# Pillow 渲染（同步·CPU-bound · 应放 thread pool 调）
# ════════════════════════════════════════════════════════════

def render(
    *,
    source_bytes: bytes,
    transform: ImageTransform,
) -> bytes:
    """用 Pillow 渲染派生图 · 返字节"""
    # Pillow 导入留在函数内 · 否则 mcp / migration 等不需要图像处理的入口也吃 Pillow 启动开销
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow not installed · `pip install Pillow`") from e

    src = Image.open(io.BytesIO(source_bytes))
    if src.mode == "RGBA" and transform.format in ("jpg", "jpeg"):
        # RGBA → RGB on white bg（避透明 → 黑底丑）
        bg = Image.new("RGB", src.size, (255, 255, 255))
        bg.paste(src, mask=src.split()[3])
        src = bg
    elif src.mode != "RGB" and transform.format in ("jpg", "jpeg"):
        src = src.convert("RGB")

    # 变换
    if transform.is_original:
        out = src
    elif transform.crop_mode == "fit":
        out = _resize_fit(src, transform.width, transform.height)
    elif transform.crop_mode == "fill":
        out = _resize_fill(src, transform.width, transform.height, transform.gravity)
    elif transform.crop_mode == "thumb":
        out = _resize_thumb(src, transform.width, transform.height)
    elif transform.crop_mode == "crop":
        out = src.crop(
            (
                transform.crop_x,
                transform.crop_y,
                transform.crop_x + transform.width,
                transform.crop_y + transform.height,
            )
        )
    else:
        out = src

    # 输出
    buf = io.BytesIO()
    fmt = "JPEG" if transform.format == "jpg" else transform.format.upper()
    save_kwargs: dict = {}
    if fmt in ("JPEG", "WEBP"):
        save_kwargs["quality"] = transform.quality
    if fmt == "WEBP":
        save_kwargs["method"] = 4
    out.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def _resize_fit(img, w: Optional[int], h: Optional[int]):
    if not w and not h:
        return img
    from PIL import Image
    src_w, src_h = img.size
    if w and h:
        target_w, target_h = w, h
        ratio = min(target_w / src_w, target_h / src_h)
    elif w:
        ratio = w / src_w
        target_w, target_h = w, int(src_h * ratio)
    else:
        ratio = h / src_h
        target_w, target_h = int(src_w * ratio), h
    new_w = max(1, int(src_w * ratio))
    new_h = max(1, int(src_h * ratio))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _resize_fill(img, w: Optional[int], h: Optional[int], gravity: str = "center"):
    """裁剪填满目标尺寸 · 保比"""
    from PIL import Image
    if not w or not h:
        return img
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # 居中裁
    left, top = _gravity_offset(new_w, new_h, w, h, gravity)
    return img.crop((left, top, left + w, top + h))


def _resize_thumb(img, w: Optional[int], h: Optional[int]):
    from PIL import Image
    target = w or h or 200
    src_w, src_h = img.size
    side = min(src_w, src_h)
    left = (src_w - side) // 2
    top = (src_h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    return img.resize((target, target), Image.LANCZOS)


def _gravity_offset(src_w: int, src_h: int, t_w: int, t_h: int, gravity: str) -> tuple[int, int]:
    """根据 gravity 字符串算裁剪起点"""
    max_left = max(0, src_w - t_w)
    max_top = max(0, src_h - t_h)
    if gravity in ("center", "face"):  # face 没装 detector 时退化到 center
        return max_left // 2, max_top // 2
    h_map = {"west": 0, "center": max_left // 2, "east": max_left}
    v_map = {"north": 0, "center": max_top // 2, "south": max_top}
    if "_" in gravity:
        v, h = gravity.split("_", 1)
    else:
        v = gravity if gravity in v_map else "center"
        h = gravity if gravity in h_map else "center"
    return h_map.get(h, max_left // 2), v_map.get(v, max_top // 2)


# ════════════════════════════════════════════════════════════
# Content-Type helper
# ════════════════════════════════════════════════════════════

def content_type_for(fmt: str) -> str:
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "avif": "image/avif",
        "gif": "image/gif",
    }.get(fmt.lower(), "application/octet-stream")


# ════════════════════════════════════════════════════════════
# HMAC-SHA256 URL signing · 给 confidential asset 用
# ════════════════════════════════════════════════════════════
#
# Token 格式（URL-safe base64 of compact JSON）：
#   {"a": "<asset_id>", "t": "<transforms>", "e": <expires_unix_ts>}
# 拼装：
#   token = base64url(payload).base64url(hmac_sha256(SECRET, payload))
# 验证：
#   1. 拆出 payload + sig
#   2. recompute hmac · constant-time compare
#   3. check 解码后的 a 匹配 URL · expires 未过
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


@dataclass
class SignedTokenPayload:
    asset_id: str
    transforms: str          # "c_fit,w_400" 等 · 防 token 重用到不同变换
    expires_at: int          # unix ts


def _get_signing_secret() -> bytes:
    """从 settings 读密钥·优先 IMG_SIGN_KEY_HEX · fallback VAULT_HMAC_HEX（已存在）"""
    from app.core.config import settings
    raw = getattr(settings, "IMG_SIGN_KEY_HEX", None) or getattr(
        settings, "VAULT_HMAC_HEX", None
    )
    if not raw or raw == "1" * 64 or raw == "0" * 64:
        raise RuntimeError(
            "IMG_SIGN_KEY_HEX (or VAULT_HMAC_HEX) not set or still default — "
            "cannot sign image URLs (生成: `openssl rand -hex 32`)"
        )
    return bytes.fromhex(raw)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_image_token(
    *,
    asset_id: str,
    transforms: str,
    ttl_seconds: int = 3600,
) -> str:
    """生成签名 token · 调用方拼到 URL ?token=<token>"""
    payload = {
        "a": asset_id,
        "t": transforms,
        "e": int(time.time()) + ttl_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_get_signing_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_image_token(
    *,
    token: str,
    asset_id: str,
    transforms: str,
) -> SignedTokenPayload:
    """验证 token · 校验失败抛 ValueError

    检查项：
      1. format <payload>.<sig>
      2. HMAC 一致（constant-time）
      3. payload.a == asset_id (防 token 跨 asset 复用)
      4. payload.t == transforms (防 token 跨变换复用)
      5. payload.e > now (未过期)
    """
    if not token or "." not in token:
        raise ValueError("malformed token")
    payload_b64, sig_b64 = token.split(".", 1)

    try:
        payload_bytes = _b64url_decode(payload_b64)
        sig_bytes = _b64url_decode(sig_b64)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"token decode failed: {e}") from e

    expected_sig = hmac.new(
        _get_signing_secret(), payload_bytes, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(sig_bytes, expected_sig):
        raise ValueError("invalid signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as e:
        raise ValueError(f"token payload not json: {e}") from e

    a = payload.get("a")
    t = payload.get("t")
    e = payload.get("e")
    if not (isinstance(a, str) and isinstance(t, str) and isinstance(e, int)):
        raise ValueError("token payload missing required fields")

    if a != asset_id:
        raise ValueError("token asset_id mismatch")
    if t != transforms:
        raise ValueError("token transforms mismatch")
    if e < int(time.time()):
        raise ValueError("token expired")

    return SignedTokenPayload(asset_id=a, transforms=t, expires_at=e)


__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_CROP_MODES",
    "ImageTransform",
    "SignedTokenPayload",
    "content_type_for",
    "derived_storage_key",
    "parse_transforms",
    "render",
    "sign_image_token",
    "verify_image_token",
]
