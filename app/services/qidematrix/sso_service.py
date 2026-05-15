"""qingxuant → QideMatrix SSO bridge service

工作流：
  1. 用户在 qingxuan 已登录（cookie/JWT）
  2. qingxuan 前端点"进入工作台" → 调 POST /v1/qidematrix/sso/create
  3. 后端校验当前 user · 生成 30 秒过期一次性 bridge_token · 落 qm_sso_sessions
  4. 返 redirect_url = https://app.qidematrix.com/sso?token=xxx
  5. 浏览器跳过去 · QideMatrix 前端用 token 调 POST /v1/qidematrix/sso/redeem
  6. 后端校验 token 没过期 / 没用过 · 把 used_at 标 NOW · 返该用户 JWT + 默认 workspace
  7. QideMatrix 前端拿 JWT 进系统 · qingxuan 双登录态痛点解决
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.models.qidematrix import QmSsoSession, QmWorkspace, QmWorkspaceMember
from app.models.user import User


BRIDGE_TOKEN_TTL_SECONDS = 30


class SsoError(Exception):
    pass


async def create_sso_bridge(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    target_workspace_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> QmSsoSession:
    """qingxuan 调此 · 给当前已登录用户生成 30s 一次性 bridge token"""
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=BRIDGE_TOKEN_TTL_SECONDS)

    # 如指定 target workspace · 校验用户是成员（防止偷渡进非己 workspace）
    if target_workspace_id:
        is_member = (
            await db.execute(
                select(QmWorkspaceMember).where(
                    QmWorkspaceMember.workspace_id == target_workspace_id,
                    QmWorkspaceMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not is_member:
            raise SsoError("user not member of target workspace")

    session = QmSsoSession(
        id=uuid.uuid4(),
        user_id=user_id,
        bridge_token=secrets.token_urlsafe(32),
        target_workspace_id=target_workspace_id,
        issued_at=now,
        expires_at=expires,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(session)
    await db.flush()
    return session


async def redeem_sso_bridge(
    db: AsyncSession,
    *,
    bridge_token: str,
    redeem_ip: str | None = None,
) -> tuple[User, QmWorkspace | None, str]:
    """QideMatrix 前端调此 · 校验 token + 返回 user + 默认 workspace + JWT

    返 (user, target_workspace, jwt_token)
    """
    now = datetime.now(UTC)

    session = (
        await db.execute(
            select(QmSsoSession).where(QmSsoSession.bridge_token == bridge_token)
        )
    ).scalar_one_or_none()
    if not session:
        raise SsoError("invalid bridge token")
    if session.used_at is not None:
        raise SsoError("bridge token already used")
    if session.expires_at < now:
        raise SsoError("bridge token expired")

    # 拿 user
    user = (
        await db.execute(select(User).where(User.id == session.user_id))
    ).scalar_one_or_none()
    if not user:
        raise SsoError("user gone")

    # 拿 target workspace（如指定）or 用户第一个 workspace
    workspace: QmWorkspace | None = None
    if session.target_workspace_id:
        workspace = (
            await db.execute(
                select(QmWorkspace).where(
                    QmWorkspace.id == session.target_workspace_id,
                    QmWorkspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    else:
        # 拿 user 所属第一个 workspace
        rows = (
            await db.execute(
                select(QmWorkspace)
                .join(
                    QmWorkspaceMember,
                    QmWorkspaceMember.workspace_id == QmWorkspace.id,
                )
                .where(
                    QmWorkspaceMember.user_id == user.id,
                    QmWorkspace.deleted_at.is_(None),
                )
                .order_by(QmWorkspace.created_at)
                .limit(1)
            )
        ).scalars().all()
        workspace = rows[0] if rows else None

    # 标 used
    session.used_at = now
    await db.flush()

    # 颁发 JWT · 复用 QideDAM 的 token signing
    jwt = create_access_token(
        subject=str(user.id),
        extra_claims={
            "via": "sso_bridge",
            "workspace_id": str(workspace.id) if workspace else None,
        },
    )

    return user, workspace, jwt
