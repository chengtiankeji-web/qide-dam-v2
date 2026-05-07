"""AuditService — single entry point for writing AuditEvent rows.

v3 P0-2. The DB enforces immutability via triggers in alembic 004; this
module enforces the *taxonomy* — making sure every write goes through one
consistent shape so admin UIs can rely on field presence.

Usage
-----
    from app.services.audit_service import audit, AuditAction

    await audit(
        session,
        action=AuditAction.VAULT_REVEALED,
        actor=principal,
        target_kind="vault_item",
        target_id=item.id,
        purpose="Looked up password to reset client account",
        request=request,  # optional FastAPI Request to capture ip + UA
        metadata={"vault_kind": "login"},
    )

The function is non-throwing by default — audit failures should not
break the user-facing operation. The exception is caught + logged, but
the original work proceeds. Critical-path operations (e.g. forced
revoke, security-incident) can pass `raise_on_failure=True` if they
want a hard fail.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.audit import AuditEvent

logger = get_logger("audit")


# ─── Action taxonomy (use the constants — never inline strings) ─────────


class AuditAction:
    """Stable namespaced action names. Add new ones here, never inline."""

    # Auth
    AUTH_LOGIN_SUCCESS = "auth.login_success"
    AUTH_LOGIN_FAIL = "auth.login_fail"
    AUTH_LOGOUT = "auth.logout"
    AUTH_TOKEN_REFRESHED = "auth.token_refreshed"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"
    AUTH_TOTP_ENABLED = "auth.totp_enabled"
    AUTH_TOTP_DISABLED = "auth.totp_disabled"

    # Membership / users
    MEMBER_INVITED = "member.invited"
    MEMBER_ACCEPTED = "member.accepted"
    MEMBER_REMOVED = "member.removed"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DELETED = "user.deleted"

    # API keys
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"

    # Asset
    ASSET_UPLOADED = "asset.uploaded"
    ASSET_PREVIEWED = "asset.previewed"
    ASSET_DOWNLOADED = "asset.downloaded"
    ASSET_UPDATED = "asset.updated"
    ASSET_DELETED = "asset.deleted"
    ASSET_PERMISSION_CHANGED = "asset.permission_changed"
    ASSET_SHARED = "asset.shared"

    # Vault
    VAULT_CREATED = "vault.created"
    VAULT_UPDATED = "vault.updated"
    VAULT_READ_REQUESTED = "vault.read_requested"
    VAULT_REVEALED = "vault.revealed"
    VAULT_COPIED = "vault.copied"
    VAULT_EXPORT_ATTEMPTED = "vault.export_attempted"
    VAULT_DECRYPT_FAILED = "vault.decrypt_failed"

    # AI
    AI_SEARCH_CALLED = "ai.search_called"
    AI_ASSET_SNIPPET_READ = "ai.asset_snippet_read"
    AI_ANSWER_DELIVERED = "ai.answer_delivered"
    AI_TOOL_DENIED = "ai.tool_denied"


# Actions that REQUIRE `purpose` to be set — enforced in audit().
_PURPOSE_REQUIRED_ACTIONS = frozenset(
    {
        AuditAction.VAULT_READ_REQUESTED,
        AuditAction.VAULT_REVEALED,
        AuditAction.VAULT_COPIED,
        AuditAction.VAULT_EXPORT_ATTEMPTED,
        AuditAction.AI_SEARCH_CALLED,
        AuditAction.AI_ASSET_SNIPPET_READ,
        AuditAction.AI_ANSWER_DELIVERED,
    }
)


# ─── The function ───────────────────────────────────────────────────────


async def audit(
    session: AsyncSession,
    *,
    action: str,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_kind: str = "user",
    target_kind: str | None = None,
    target_id: uuid.UUID | None = None,
    status: str = "success",
    purpose: str | None = None,
    request: Request | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
    raise_on_failure: bool = False,
) -> AuditEvent | None:
    """Write a single audit event.

    Returns the created AuditEvent on success, None on swallowed failure
    (default). Pass `raise_on_failure=True` to propagate exceptions.

    The session is flushed but NOT committed here — caller controls the
    transaction boundary. If the caller's main work later fails and rolls
    back, the audit row rolls back too (which is the right behaviour:
    we don't want phantom audit events for actions that didn't happen).
    """
    if action in _PURPOSE_REQUIRED_ACTIONS and not purpose:
        msg = f"audit action {action!r} requires `purpose` (caller must explain why)"
        if raise_on_failure:
            raise ValueError(msg)
        logger.warning(msg)
        # Continue anyway — the event still goes in, just with a clear
        # marker for compliance review.
        purpose = "[no-purpose-given]"

    if request is not None:
        # FastAPI Request → ip + user_agent if not explicitly passed
        if ip is None:
            ip = request.headers.get("x-forwarded-for") or request.client.host if request.client else None
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()
        if user_agent is None:
            user_agent = request.headers.get("user-agent")

    try:
        event = AuditEvent(
            tenant_id=tenant_id,
            project_id=project_id,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            status=status,
            purpose=purpose,
            ip=ip,
            user_agent=user_agent,
            extra_metadata=metadata or {},
        )
        session.add(event)
        await session.flush()
        return event
    except Exception as exc:  # noqa: BLE001
        logger.error("audit_write_failed", action=action, error=str(exc))
        if raise_on_failure:
            raise
        return None


__all__ = ["audit", "AuditAction"]
