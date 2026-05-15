"""QideMatrix workspaces REST API · /v1/qm/workspaces/*"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.qidematrix import (
    InvitationAcceptIn,
    InvitationCreateIn,
    InvitationOut,
    MemberOut,
    MemberRoleUpdateIn,
    WorkspaceCreateIn,
    WorkspaceOut,
    WorkspaceUpdateIn,
)
from app.services import audit_service
from app.services.audit_service import AuditAction
from app.services.qidematrix import workspace_service as ws_svc

router = APIRouter(prefix="/qm/workspaces", tags=["qidematrix-workspaces"])


def _err(code: int, detail: str):
    return HTTPException(status_code=code, detail=detail)


def _ws_to_out(ws) -> WorkspaceOut:
    return WorkspaceOut.model_validate(ws)


# ─── Workspace CRUD ──────────────────────────────────────────────────

@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    payload: WorkspaceCreateIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """创建工作空间 · 当前 user 自动成为 owner"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        ws = await ws_svc.create_workspace(
            db,
            tenant_id=p.tenant_id,
            owner_user_id=p.user_id,
            payload=payload,
        )
    except ws_svc.WorkspaceSlugTaken:
        raise _err(409, f"slug '{payload.slug}' already taken") from None

    await audit_service.audit(
        db,
        action=AuditAction.ASSET_CREATED,  # 复用现有 action enum · 后续可加 qm.workspace.created
        tenant_id=p.tenant_id,
        project_id=None,
        actor_user_id=p.user_id,
        actor_kind="user",
        target_kind="qm_workspace",
        target_id=ws.id,
        request=request,
        metadata={"slug": ws.slug, "plan": ws.plan},
    )
    await db.commit()
    return _ws_to_out(ws)


@router.get("", response_model=list[WorkspaceOut])
async def list_my_workspaces(
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """列出当前 user 参与的所有 workspace"""
    if not p.user_id:
        raise _err(401, "user identity required")
    rows = await ws_svc.list_workspaces_for_user(db, user_id=p.user_id)
    return [_ws_to_out(w) for w in rows]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        ws, _ = await ws_svc.get_workspace_for_user(
            db, workspace_id=workspace_id, user_id=p.user_id
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "workspace not found") from None
    except ws_svc.WorkspacePermissionDenied:
        raise _err(403, "not a member") from None
    return _ws_to_out(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        ws = await ws_svc.update_workspace(
            db, workspace_id=workspace_id, user_id=p.user_id, payload=payload
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "workspace not found") from None
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e)) from None

    await db.commit()
    return _ws_to_out(ws)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """仅 owner 能删 · soft delete · 30 天内可申诉恢复"""
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.soft_delete_workspace(
            db, workspace_id=workspace_id, user_id=p.user_id
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "workspace not found") from None
    except ws_svc.WorkspacePermissionDenied:
        raise _err(403, "only owner can delete") from None
    await db.commit()


# ─── Members ─────────────────────────────────────────────────────────

@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        rows = await ws_svc.list_members(
            db, workspace_id=workspace_id, requester_user_id=p.user_id
        )
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    return [MemberOut.model_validate(m) for m in rows]


@router.patch(
    "/{workspace_id}/members/{user_id}/role", response_model=MemberOut
)
async def update_member_role(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MemberRoleUpdateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        m = await ws_svc.update_member_role(
            db,
            workspace_id=workspace_id,
            target_user_id=user_id,
            new_role=payload.role,
            requester_user_id=p.user_id,
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "member not found") from None
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e)) from None
    except ws_svc.WorkspaceError as e:
        raise _err(400, str(e)) from None
    await db.commit()
    return MemberOut.model_validate(m)


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.remove_member(
            db,
            workspace_id=workspace_id,
            target_user_id=user_id,
            requester_user_id=p.user_id,
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "member not found") from None
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e)) from None
    except ws_svc.WorkspaceError as e:
        raise _err(400, str(e)) from None
    await db.commit()


# ─── Invitations ─────────────────────────────────────────────────────

@router.post(
    "/{workspace_id}/invitations",
    response_model=InvitationOut,
    status_code=201,
)
async def create_invitation(
    workspace_id: uuid.UUID,
    payload: InvitationCreateIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        inv = await ws_svc.create_invitation(
            db,
            workspace_id=workspace_id,
            inviter_user_id=p.user_id,
            payload=payload,
        )
    except ws_svc.SeatLimitReached as e:
        raise _err(402, str(e)) from None  # 402 Payment Required = 升档
    except (ws_svc.WorkspaceNotFound, ws_svc.WorkspacePermissionDenied) as e:
        raise _err(403, str(e)) from None
    await db.commit()
    # 创建时返完整 token · 调用方拿去发邮件
    out = InvitationOut.model_validate(inv)
    out.token = inv.token
    return out


@router.post(
    "/invitations/accept",
    response_model=MemberOut,
)
async def accept_invitation(
    payload: InvitationAcceptIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """接受邀请 · 需当前 user 已登录 · email 必须匹配邀请"""
    if not p.user_id or not p.user_email:
        raise _err(401, "user identity required")
    try:
        _, member = await ws_svc.accept_invitation(
            db, token=payload.token, user_id=p.user_id, user_email=p.user_email
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "invitation not found") from None
    except ws_svc.SeatLimitReached as e:
        raise _err(402, str(e)) from None
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e)) from None
    except ws_svc.WorkspaceError as e:
        raise _err(400, str(e)) from None
    await db.commit()
    return MemberOut.model_validate(member)


@router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    try:
        await ws_svc.revoke_invitation(
            db, invitation_id=invitation_id, user_id=p.user_id
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "invitation not found") from None
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e)) from None
    except ws_svc.WorkspaceError as e:
        raise _err(400, str(e)) from None
    await db.commit()
