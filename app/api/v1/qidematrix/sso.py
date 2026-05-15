"""qingxuan → QideMatrix SSO bridge REST API · /v1/qm/sso/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix import (
    SsoBridgeCreateIn,
    SsoBridgeCreateOut,
    SsoBridgeRedeemIn,
)
from app.services.qidematrix import sso_service

router = APIRouter(prefix="/qm/sso", tags=["qidematrix-sso"])


# 前端的 redirect URL · 后续生产环境改成 qidematrix.com / app.qidematrix.com
_FRONTEND_SSO_LANDING = (
    getattr(settings, "QM_FRONTEND_URL", None)
    or "https://qingxuantech.work/sso"
)


def _err(code: int, detail: str):
    return HTTPException(status_code=code, detail=detail)


@router.post("/create", response_model=SsoBridgeCreateOut)
async def create_bridge(
    payload: SsoBridgeCreateIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """qingxuan 前端调此 · 给当前已登录用户生成 30s 一次性 bridge token

    流程：
      1. qingxuan 用户已用 JWT cookie 登录到 QideDAM
      2. 点"进入工作台" → 前端 POST 此端点（自动带 JWT）
      3. 后端校验 user · 生成 token
      4. 前端拿 redirect_url · window.location.href = redirect_url
      5. QideMatrix 前端读 URL ?token=xxx · 调 redeem 换 JWT
    """
    if not p.user_id:
        raise _err(401, "user identity required")

    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        session = await sso_service.create_sso_bridge(
            db,
            user_id=p.user_id,
            target_workspace_id=payload.target_workspace_id,
            ip=ip,
            user_agent=user_agent,
        )
    except sso_service.SsoError as e:
        raise _err(403, str(e)) from None

    await db.commit()

    redirect_url = f"{_FRONTEND_SSO_LANDING}?token={session.bridge_token}"
    return SsoBridgeCreateOut(
        bridge_token=session.bridge_token,
        expires_at=session.expires_at,
        redirect_url=redirect_url,
    )


@router.post("/redeem")
async def redeem_bridge(
    payload: SsoBridgeRedeemIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """QideMatrix 前端调此 · 用 bridge token 换 JWT + 当前 workspace

    无需 JWT · 这是登录入口
    """
    redeem_ip = request.client.host if request.client else None
    try:
        user, workspace, jwt = await sso_service.redeem_sso_bridge(
            db, bridge_token=payload.bridge_token, redeem_ip=redeem_ip
        )
    except sso_service.SsoError as e:
        raise _err(401, str(e)) from None

    await db.commit()
    return {
        "access_token": jwt,
        "token_type": "bearer",
        "user_id": str(user.id),
        "user_email": user.email,
        "workspace_id": str(workspace.id) if workspace else None,
        "workspace_slug": workspace.slug if workspace else None,
        "workspace_display_name": workspace.display_name if workspace else None,
    }
