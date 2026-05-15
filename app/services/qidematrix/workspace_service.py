"""QideMatrix workspace 业务逻辑

职责：
- create_workspace · 含 owner member 一并落 · 默认 trial plan · 14 天试用
- list_workspaces_for_user · 我是哪些 workspace 的成员
- update_workspace / soft_delete_workspace
- get_workspace_for_user 鉴权辅助 · 不在成员里直接 403
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qidematrix import (
    QmInvitation,
    QmSubscription,
    QmWorkspace,
    QmWorkspaceMember,
)
from app.models.user import User
from app.schemas.qidematrix import (
    PLAN_CONFIG,
    InvitationCreateIn,
    WorkspaceCreateIn,
    WorkspaceUpdateIn,
)


class WorkspaceError(Exception):
    """Workspace 业务异常 · API 层转 4xx"""


class WorkspaceSlugTaken(WorkspaceError):
    pass


class WorkspaceNotFound(WorkspaceError):
    pass


class WorkspacePermissionDenied(WorkspaceError):
    pass


class SeatLimitReached(WorkspaceError):
    pass


# ─── Create ───────────────────────────────────────────────────────────

async def create_workspace(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: WorkspaceCreateIn,
) -> QmWorkspace:
    """创建 workspace · 落初始 owner member + trial subscription

    Side effects:
      - 1 行 qm_workspaces (plan='trial', 14 天试用)
      - 1 行 qm_workspace_members (role='owner')
      - 1 行 qm_subscriptions (plan='trial', status='trial')
    """
    # 1. 防 slug 重复
    existing = (
        await db.execute(
            select(QmWorkspace).where(QmWorkspace.slug == payload.slug)
        )
    ).scalar_one_or_none()
    if existing:
        raise WorkspaceSlugTaken(f"slug {payload.slug!r} already exists")

    now = datetime.now(UTC)
    trial_config = PLAN_CONFIG["trial"]
    trial_ends = now + timedelta(days=trial_config["trial_days"])

    # 2. workspace
    workspace = QmWorkspace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        slug=payload.slug,
        display_name=payload.display_name,
        owner_user_id=owner_user_id,
        plan="trial",
        plan_seats=trial_config["seats"],
        plan_storage_gb=trial_config["storage_gb"],
        plan_ai_calls_monthly=trial_config["ai_calls_monthly"],
        trial_ends_at=trial_ends,
        industry=payload.industry,
        locale=payload.locale,
        created_at=now,
        updated_at=now,
        extra_metadata={},
    )
    db.add(workspace)
    await db.flush()  # 拿 workspace.id

    # 3. owner member
    member = QmWorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=owner_user_id,
        role="owner",
        invited_by=None,
        joined_at=now,
        extra_metadata={},
    )
    db.add(member)

    # 4. trial subscription
    sub = QmSubscription(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        plan="trial",
        status="trial",
        billing_cycle="monthly",
        price_cny_cents=0,
        started_at=now,
        current_period_start=now,
        current_period_end=trial_ends,
        cancel_at_period_end=False,
        payment_provider=None,
        extra_metadata={"trial_init": True},
        created_at=now,
        updated_at=now,
    )
    db.add(sub)

    await db.flush()
    return workspace


# ─── Read ─────────────────────────────────────────────────────────────

async def list_workspaces_for_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[QmWorkspace]:
    """我是哪些 workspace 的成员（不分 role · owner/admin/member/viewer 都列出）"""
    stmt = (
        select(QmWorkspace)
        .join(QmWorkspaceMember, QmWorkspaceMember.workspace_id == QmWorkspace.id)
        .where(
            QmWorkspaceMember.user_id == user_id,
            QmWorkspace.deleted_at.is_(None),
        )
        .order_by(QmWorkspace.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def get_workspace_for_user(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    require_role: tuple[str, ...] = ("owner", "admin", "member", "viewer"),
) -> tuple[QmWorkspace, QmWorkspaceMember]:
    """拿 workspace + 该用户在 workspace 里的 member record · 鉴权辅助

    抛：
      - WorkspaceNotFound · 不存在 / 软删
      - WorkspacePermissionDenied · 不在成员里 / role 不在 require_role
    """
    workspace = (
        await db.execute(
            select(QmWorkspace).where(
                QmWorkspace.id == workspace_id,
                QmWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not workspace:
        raise WorkspaceNotFound(f"workspace {workspace_id} not found")

    member = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == workspace_id,
                QmWorkspaceMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise WorkspacePermissionDenied("not a member")

    if member.role not in require_role:
        raise WorkspacePermissionDenied(
            f"role {member.role} not in required {require_role}"
        )

    return workspace, member


async def get_workspace_by_slug(
    db: AsyncSession, *, slug: str
) -> QmWorkspace | None:
    return (
        await db.execute(
            select(QmWorkspace).where(
                QmWorkspace.slug == slug,
                QmWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


# ─── Update / Delete ──────────────────────────────────────────────────

async def update_workspace(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: WorkspaceUpdateIn,
) -> QmWorkspace:
    """只 owner / admin 能改 workspace 设置"""
    workspace, _ = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        require_role=("owner", "admin"),
    )
    now = datetime.now(UTC)
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(workspace, key, value)
    workspace.updated_at = now
    await db.flush()
    return workspace


async def soft_delete_workspace(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """仅 owner 能删 · soft delete · 数据保留"""
    workspace, _ = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        require_role=("owner",),
    )
    now = datetime.now(UTC)
    workspace.deleted_at = now
    workspace.updated_at = now
    await db.flush()


# ─── Invitation ───────────────────────────────────────────────────────

async def create_invitation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    inviter_user_id: uuid.UUID,
    payload: InvitationCreateIn,
) -> QmInvitation:
    """邀请新成员 · 仅 owner / admin"""
    workspace, _ = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=inviter_user_id,
        require_role=("owner", "admin"),
    )

    # 检查 seat 限额
    seat_count = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == workspace_id
            )
        )
    ).scalars().all()
    if len(seat_count) >= workspace.plan_seats:
        raise SeatLimitReached(
            f"plan {workspace.plan} allows {workspace.plan_seats} seats · "
            f"already used {len(seat_count)}"
        )

    now = datetime.now(UTC)
    expires = now + timedelta(hours=payload.expires_in_hours)

    invitation = QmInvitation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        email=payload.email.lower(),
        role=payload.role,
        invited_by_user_id=inviter_user_id,
        token=secrets.token_urlsafe(32),  # 43 chars · 唯一
        expires_at=expires,
        extra_metadata={},
        created_at=now,
    )
    db.add(invitation)
    await db.flush()
    return invitation


async def accept_invitation(
    db: AsyncSession,
    *,
    token: str,
    user_id: uuid.UUID,
    user_email: str,
) -> tuple[QmWorkspace, QmWorkspaceMember]:
    """用户点邮件链接 · 用 token 接受邀请 · 落 member 行"""
    now = datetime.now(UTC)

    invitation = (
        await db.execute(
            select(QmInvitation).where(QmInvitation.token == token)
        )
    ).scalar_one_or_none()
    if not invitation:
        raise WorkspaceNotFound("invitation not found")
    if invitation.accepted_at is not None:
        raise WorkspaceError("invitation already accepted")
    if invitation.revoked_at is not None:
        raise WorkspaceError("invitation revoked")
    if invitation.expires_at < now:
        raise WorkspaceError("invitation expired")

    # 检查 email 匹配（防止别人偷 token 用自己账号接受）
    if invitation.email.lower() != user_email.lower():
        raise WorkspacePermissionDenied(
            "invitation email does not match logged-in user"
        )

    # workspace 是否还活
    workspace = (
        await db.execute(
            select(QmWorkspace).where(
                QmWorkspace.id == invitation.workspace_id,
                QmWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not workspace:
        raise WorkspaceNotFound("workspace gone")

    # 已经是 member 了的话 update 邀请状态即可
    existing_member = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == invitation.workspace_id,
                QmWorkspaceMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if existing_member:
        invitation.accepted_at = now
        invitation.accepted_by_user_id = user_id
        await db.flush()
        return workspace, existing_member

    # 再检查 seat 限额（创建邀请到接受之间可能其他人也加入了）
    members_count = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == invitation.workspace_id
            )
        )
    ).scalars().all()
    if len(members_count) >= workspace.plan_seats:
        raise SeatLimitReached(
            f"workspace {workspace.slug} has hit seat limit"
        )

    member = QmWorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=invitation.workspace_id,
        user_id=user_id,
        role=invitation.role,
        invited_by=invitation.invited_by_user_id,
        joined_at=now,
        extra_metadata={"via_invitation": str(invitation.id)},
    )
    db.add(member)
    invitation.accepted_at = now
    invitation.accepted_by_user_id = user_id

    await db.flush()
    return workspace, member


async def revoke_invitation(
    db: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """撤回邀请 · 仅 owner / admin"""
    invitation = (
        await db.execute(
            select(QmInvitation).where(QmInvitation.id == invitation_id)
        )
    ).scalar_one_or_none()
    if not invitation:
        raise WorkspaceNotFound("invitation not found")

    # 鉴权
    await get_workspace_for_user(
        db,
        workspace_id=invitation.workspace_id,
        user_id=user_id,
        require_role=("owner", "admin"),
    )

    if invitation.accepted_at is not None:
        raise WorkspaceError("cannot revoke accepted invitation")

    invitation.revoked_at = datetime.now(UTC)
    await db.flush()


# ─── Member ───────────────────────────────────────────────────────────

async def list_members(
    db: AsyncSession, *, workspace_id: uuid.UUID, requester_user_id: uuid.UUID
) -> list[QmWorkspaceMember]:
    """列成员 · 任何 workspace 成员都能看（包括 viewer）"""
    await get_workspace_for_user(
        db, workspace_id=workspace_id, user_id=requester_user_id
    )
    rows = (
        await db.execute(
            select(QmWorkspaceMember)
            .where(QmWorkspaceMember.workspace_id == workspace_id)
            .order_by(QmWorkspaceMember.joined_at)
        )
    ).scalars().all()
    return list(rows)


async def update_member_role(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_user_id: uuid.UUID,
    new_role: str,
    requester_user_id: uuid.UUID,
) -> QmWorkspaceMember:
    """改成员角色 · 仅 owner 能改 · owner 不能被改"""
    workspace, requester_member = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner",),
    )

    target = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == workspace_id,
                QmWorkspaceMember.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if not target:
        raise WorkspaceNotFound("target member not found")
    if target.role == "owner":
        raise WorkspaceError("cannot change owner role · transfer ownership first")
    if new_role == "owner":
        raise WorkspaceError("use transfer_ownership() to change owner")

    target.role = new_role
    await db.flush()
    return target


async def remove_member(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_user_id: uuid.UUID,
    requester_user_id: uuid.UUID,
) -> None:
    """移除成员 · owner / admin 能移除 member/viewer · admin 不能移除 admin/owner"""
    workspace, requester_member = await get_workspace_for_user(
        db,
        workspace_id=workspace_id,
        user_id=requester_user_id,
        require_role=("owner", "admin"),
    )

    target = (
        await db.execute(
            select(QmWorkspaceMember).where(
                QmWorkspaceMember.workspace_id == workspace_id,
                QmWorkspaceMember.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if not target:
        raise WorkspaceNotFound("target member not found")
    if target.role == "owner":
        raise WorkspaceError("cannot remove owner · transfer first")
    if requester_member.role == "admin" and target.role in ("admin", "owner"):
        raise WorkspacePermissionDenied("admin cannot remove admin/owner")
    if requester_user_id == target_user_id:
        raise WorkspaceError("use leave_workspace() to leave by yourself")

    await db.delete(target)
    await db.flush()
