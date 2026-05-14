"""URL 动态图像变换 · /img/{asset_id}/{transforms}.{ext}

Cloudinary-style 公开 URL · 支持 fill / fit / crop / thumb + width / height / quality / format / gravity。

工作流：
  1. parse_transforms() 解析 URL
  2. 拉 asset metadata · 检 ACL（仅 public / sensitivity ≤ internal 匿名）
  3. 算 derived_storage_key · head_object check R2 缓存
  4. 缓存 hit → 308 redirect 到 CDN public URL
  5. miss → 拉原图 → render → put_object 到 R2 → 308 redirect

⚠️ confidential asset 需带 ?token=<HMAC 签名> · v4.1 已实装
⚠️ secret + vault_* 永远 403
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.asset import Asset
from app.services import image_transform_service as itx
from app.services import storage

logger = get_logger("img")

router = APIRouter()


# ════════════════════════════════════════════════════════════
# Signed URL minting (authenticated)
# ════════════════════════════════════════════════════════════

class SignedUrlIn(BaseModel):
    asset_id: uuid.UUID
    transforms: str           # 不含 .ext · 如 "c_fit,w_400"
    ext: str = "jpg"
    ttl_seconds: int = 3600


class SignedUrlOut(BaseModel):
    url: str
    expires_at: int


@router.post("/sign", response_model=SignedUrlOut)
async def mint_signed_url(
    payload: SignedUrlIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SignedUrlOut:
    """给 confidential / private asset 出一次性签名 URL · 1h 默认 TTL

    认证用户调本端点 → 返带 ?token= 的 URL · 前端把 URL 嵌 <img src=>
    """
    if payload.ttl_seconds < 30 or payload.ttl_seconds > 7 * 24 * 3600:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ttl_seconds out of range (30 .. 604800)",
        )

    # 校验 asset 存在 + 用户能访问
    asset = await db.get(Asset, payload.asset_id)
    if not asset or asset.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    if asset.kind in ("vault_login", "vault_identity", "vault_note"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "vault assets cannot be signed")
    sensitivity = getattr(asset, "sensitivity_level", "internal") or "internal"
    if sensitivity == "secret":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "secret-classified cannot be signed")

    # 提前校验 transforms 字符串合法
    try:
        itx.parse_transforms(payload.transforms, payload.ext)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    try:
        token = itx.sign_image_token(
            asset_id=str(payload.asset_id),
            transforms=payload.transforms,
            ttl_seconds=payload.ttl_seconds,
        )
    except RuntimeError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"image signing not configured: {e}",
        ) from e

    import time
    return SignedUrlOut(
        url=f"/img/{payload.asset_id}/{payload.transforms}.{payload.ext}?token={token}",
        expires_at=int(time.time()) + payload.ttl_seconds,
    )


@router.get("/{asset_id}/{transforms}.{ext}")
async def transform_image(
    asset_id: uuid.UUID,
    transforms: str,
    ext: str,
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """主入口·解析 + 缓存查 + 派生 + 返"""
    # 1) parse
    try:
        transform = itx.parse_transforms(transforms, ext)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # 2) load asset metadata
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    if asset.kind != "image":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"asset kind {asset.kind!r} not supported · only 'image'",
        )

    # 3) ACL check
    if asset.kind in ("vault_login", "vault_identity", "vault_note"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "vault assets cannot be transformed")
    sensitivity = getattr(asset, "sensitivity_level", "internal") or "internal"
    if sensitivity == "secret":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "secret-classified assets are not transformable")

    # v4.1 · HMAC token 真校验
    requires_token = (
        sensitivity == "confidential"
        or asset.acl not in ("public", "tenant", "project")
    )
    if requires_token:
        if not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "asset requires ?token=<signed-token>",
            )
        try:
            itx.verify_image_token(
                token=token,
                asset_id=str(asset_id),
                transforms=transforms,
            )
        except RuntimeError as exc:
            # 服务端密钥未配 · 500
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"image signing not configured: {exc}",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"invalid token: {exc}",
            ) from exc

    # 4) cache key + R2 hit check（off thread · S3 是同步 boto3）
    derived_key = itx.derived_storage_key(asset_id=asset_id, transform=transform)
    head = await asyncio.to_thread(storage.head_object, derived_key)
    if head is None:
        # 5) miss · 拉原图渲染入 R2
        try:
            source_bytes = await asyncio.to_thread(storage.get_object, asset.storage_key)
        except Exception as exc:
            logger.error(
                "img.fetch_source_failed",
                asset_id=str(asset_id), storage_key=asset.storage_key, error=str(exc),
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "failed to fetch source asset from storage",
            ) from exc

        try:
            rendered = await asyncio.to_thread(itx.render, source_bytes=source_bytes, transform=transform)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "img.render_failed",
                asset_id=str(asset_id), transforms=transforms, error=str(exc),
            )
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "image transformation failed",
            ) from exc

        content_type = itx.content_type_for(transform.format)
        try:
            await asyncio.to_thread(
                storage.put_object,
                storage_key=derived_key,
                body=rendered,
                content_type=content_type,
            )
        except Exception as exc:  # noqa: BLE001
            # 即使写 R2 失败也返渲染后的字节·下次再尝试缓存
            logger.warning(
                "img.cache_write_failed",
                derived_key=derived_key, error=str(exc),
            )
            return Response(
                content=rendered,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Qide-Img-Cache": "MISS_NOWRITE",
                },
            )

        logger.info(
            "img.cached",
            asset_id=str(asset_id), derived_key=derived_key, bytes=len(rendered),
        )

    # 6) redirect to CDN public URL (优先) · 否则 presigned
    public = storage.public_url_for(derived_key)
    if public:
        return RedirectResponse(
            url=public,
            status_code=308,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Qide-Img-Cache": "HIT" if head is not None else "MISS_WROTE",
            },
        )
    signed = await asyncio.to_thread(
        storage.presign_get, storage_key=derived_key, expires_in=3600,
    )
    return RedirectResponse(
        url=signed,
        status_code=302,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Qide-Img-Cache": "HIT_SIGNED" if head is not None else "MISS_WROTE",
        },
    )
