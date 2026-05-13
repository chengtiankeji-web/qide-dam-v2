"""ShareLinks — public read-only links to assets / collections.

v3 P1.3 (2026-05-13) 改造：
  - D6 audit 全覆盖：CREATE / REVOKE 都写 audit
  - D10 expires_in ergonomic 字段（schema 层）
  - D11 GET /p/share/{token} 路径处理无密码 link 邮件直接点击场景
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.models.share_link import ShareLink
from app.schemas.share_link import (
    ShareLinkCreate,
    ShareLinkOut,
    ShareLinkResolveIn,
)
from app.services import audit_service, share_link_service, storage
from app.services.audit_service import AuditAction

router = APIRouter()


@router.post("", response_model=ShareLinkOut, status_code=201)
async def create_share_link(
    payload: ShareLinkCreate,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ShareLinkOut:
    try:
        sl = await share_link_service.create_link(
            db,
            tenant_id=p.tenant_id,
            asset_id=payload.asset_id,
            collection_id=payload.collection_id,
            created_by_user_id=p.user_id,
            password=payload.password,
            expires_at=payload.expires_at,
            max_downloads=payload.max_downloads,
            note=payload.note,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # v3 P1.3 D6 audit
    await audit_service.audit(
        db,
        action=AuditAction.SHARE_LINK_CREATED,
        tenant_id=p.tenant_id,
        project_id=None,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="share_link",
        target_id=sl.id,
        request=request,
        metadata={
            "asset_id": str(payload.asset_id) if payload.asset_id else None,
            "collection_id": str(payload.collection_id) if payload.collection_id else None,
            "expires_at": sl.expires_at.isoformat() if sl.expires_at else None,
            "max_downloads": payload.max_downloads,
            "has_password": payload.password is not None,
            "note": payload.note,
        },
    )
    return ShareLinkOut.model_validate(sl)


@router.get("", response_model=list[ShareLinkOut])
async def list_share_links(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ShareLinkOut]:
    rows = (
        await db.execute(
            select(ShareLink).where(ShareLink.tenant_id == p.tenant_id)
            .order_by(ShareLink.created_at.desc())
        )
    ).scalars().all()
    return [ShareLinkOut.model_validate(r) for r in rows]


@router.delete("/{share_link_id}", status_code=204)
async def revoke_link(
    share_link_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    sl = (
        await db.execute(
            select(ShareLink).where(
                ShareLink.id == share_link_id, ShareLink.tenant_id == p.tenant_id
            )
        )
    ).scalar_one_or_none()
    if not sl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    sl.is_active = False
    await db.flush()

    # v3 P1.3 D6 audit
    await audit_service.audit(
        db,
        action=AuditAction.SHARE_LINK_REVOKED,
        tenant_id=p.tenant_id,
        project_id=None,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="share_link",
        target_id=sl.id,
        request=request,
        metadata={
            "token_prefix": sl.token[:6] + "...",
            "asset_id": str(sl.asset_id) if sl.asset_id else None,
            "collection_id": str(sl.collection_id) if sl.collection_id else None,
            "download_count_at_revoke": sl.download_count,
        },
    )


# ─── public resolution (no auth) ────────────────────────────────────

public_router = APIRouter()


@public_router.post("/share/{token}/resolve")
async def resolve_share_link_post(
    token: str,
    payload: ShareLinkResolveIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """POST: JSON 协议 · 客户端 SPA 用 · password 走 body"""
    try:
        sl = await share_link_service.resolve_link(db, token=token, password=payload.password)
    except ValueError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    if sl.asset_id:
        asset = (
            await db.execute(select(Asset).where(Asset.id == sl.asset_id))
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "asset gone")
        url = storage.presign_get(storage_key=asset.storage_key, expires_in=3600)
        return {"kind": "asset", "asset": {
            "id": str(asset.id), "name": asset.name, "kind": asset.kind,
            "thumbnails": asset.thumbnails,
        }, "download_url": url, "expires_in": 3600}

    return {"kind": "collection", "collection_id": str(sl.collection_id)}


@public_router.get("/share/{token}")
async def resolve_share_link_get(
    token: str,
    password: str | None = Query(None,
        description="可选 password · 推荐放 body 通过 POST · GET 仅给 password-less link"),
    db: AsyncSession = Depends(get_db),
):
    """v3 P1.3 D11 新增：GET 直接访问 · 让邮件正文中的 share 链接可点击。

    流程：
      - 无密码 link · 浏览器 GET 命中 → 302 redirect 到 R2 signed URL（直接下载）
      - 有密码 link 无 password param · 返回 JSON 提示用密码 POST （兼容 SPA）
      - 有密码 link 带 ?password= · resolve → 302 redirect 到 R2 URL
    """
    try:
        sl = await share_link_service.resolve_link(db, token=token, password=password)
    except ValueError as e:
        # 有密码 link 没传 password
        if "password required" in str(e).lower():
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "password_required",
                    "message": "link requires password · 用 POST /p/share/{token}/resolve "
                               "with body {\"password\": \"...\"} · "
                               "or append ?password=xxx to this URL（不推荐 · 会留 URL log）",
                },
            ) from e
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    if sl.asset_id:
        asset = (
            await db.execute(select(Asset).where(Asset.id == sl.asset_id))
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "asset gone")
        url = storage.presign_get(storage_key=asset.storage_key, expires_in=3600)
        # 302 redirect 让浏览器直接拉文件
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

    return {"kind": "collection", "collection_id": str(sl.collection_id)}
