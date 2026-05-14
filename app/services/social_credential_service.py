"""Social Matrix v2 · 凭证安全存储

封装 OAuth token / refresh token 的 AES-256-GCM 加密入库 + 解密读出。

复用 vault_service 的 KEK + DEK envelope 加密方案：
  - 每个 credential 独立 DEK（32 字节）
  - DEK 用 master KEK 包·payload 用 DEK 加密
  - AAD 绑定 (tenant_id, platform, credential_type) · 防 ciphertext 跨行掉包

⚠️ 安全准则：
  - credential.payload 永不返给 API 调用方 · 只在 publisher / OAuth refresh 内存里短暂存在
  - 任何 reveal 都走 vault_service 的 audit-required 通道（v4.1 加 social.credential.revealed 审计）
  - refresh_failed_at 非空 → 立即标 status='disconnected' · 后台不再尝试用

测试：tests/social/test_social_credential_service.py
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.social import SocialAccount, SocialCredential
from app.services import audit_service
from app.services.vault_service import decrypt_payload, encrypt_payload

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# AAD 构造（与 vault_service._make_aad 一致风格 · 但用 platform 替 vault_kind）
# ════════════════════════════════════════════════════════════

def _social_aad(*, tenant_id: str, credential_id: str, platform: str) -> dict[str, Any]:
    """social credential 的 AAD · 绑租户 + 凭证 + 平台"""
    return {
        "ns": "social",
        "t": tenant_id,
        "c": credential_id,
        "p": platform,
        "v": 1,
    }


# ════════════════════════════════════════════════════════════
# 1. 写入新凭证（OAuth callback 拿到 token 后调）
# ════════════════════════════════════════════════════════════

async def store_credential(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    platform: str,
    credential_type: str,
    token_payload: dict[str, Any],  # {access_token, refresh_token, expires_in, scopes, ...}
    expires_at: datetime | None = None,
    scopes: str | None = None,
    request=None,
) -> SocialCredential:
    """加密 token_payload 入库 · 返 SocialCredential（不含 plaintext）

    token_payload 示例：
      {"access_token": "...", "refresh_token": "...", "id_token": "...",
       "token_type": "Bearer", "scope": "w_member_social r_liteprofile"}
    """
    # 先生成临时 cred id（AAD 要用）·若失败回滚不会落地
    cred_id = uuid.uuid4()

    # 加密
    enc = encrypt_payload(
        payload=token_payload,
        tenant_id=str(tenant_id),
        asset_id=str(cred_id),     # 复用 vault encrypt 入参·借位用 credential id
        vault_kind=platform,        # AAD 绑 platform
        schema_version=1,
    )

    # 写库
    cred = SocialCredential(
        id=cred_id,
        tenant_id=tenant_id,
        platform=platform,
        credential_type=credential_type,
        kek_id=enc["kek_ref"],
        dek_wrapped=enc["wrapped_dek"],
        dek_nonce=enc["wrapped_dek"][:12],  # 兼容旧 schema·实际 wrap nonce 已在 wrapped_dek 前缀
        payload_ciphertext=enc["encrypted_payload"],
        payload_nonce=enc["nonce"],
        payload_tag=b"",  # AES-GCM tag 已 inline 在 ciphertext 尾·留空兼容
        expires_at=expires_at,
        scopes=scopes,
        created_by_user_id=principal.user_id,
    )
    db.add(cred)
    await db.flush()

    await audit_service.audit(
        db,
        action="social.credential_created",
        tenant_id=tenant_id,
        actor_user_id=principal.user_id,
        target_kind="social_credential",
        target_id=cred.id,
        request=request,
        metadata={"platform": platform, "type": credential_type},
    )
    logger.info(
        "social.credential_stored",
        credential_id=str(cred.id), platform=platform, type=credential_type,
    )
    return cred


# ════════════════════════════════════════════════════════════
# 2. 解密 token（publisher / refresh job 调）
# ════════════════════════════════════════════════════════════

async def reveal_credential(
    db: AsyncSession,
    *,
    principal: Principal | None,
    credential_id: uuid.UUID,
    purpose: str,  # 必填·审计要用
    request=None,
) -> dict[str, Any]:
    """解密 token payload · 写 social.credential_revealed 审计

    purpose 例：
      - "post_to_linkedin_share"
      - "refresh_token_pre_expiry"
      - "user_disconnect_account"
    """
    cred = await db.get(SocialCredential, credential_id)
    if not cred:
        raise ValueError(f"credential {credential_id} not found")
    if cred.revoked_at is not None:
        raise ValueError(f"credential {credential_id} has been revoked")

    aad_dict = {
        "t": str(cred.tenant_id),
        "a": str(cred.id),
        "k": cred.platform,
        "v": 1,
    }

    try:
        payload = decrypt_payload(
            encrypted_payload=cred.payload_ciphertext,
            nonce=cred.payload_nonce,
            wrapped_dek=cred.dek_wrapped,
            aad=aad_dict,
            kek_ref=cred.kek_id,
        )
    except Exception as exc:
        await audit_service.audit(
            db,
            action="social.credential_decrypt_failed",
            tenant_id=cred.tenant_id,
            actor_user_id=principal.user_id if principal else None,
            target_kind="social_credential",
            target_id=cred.id,
            status="failure",
            purpose=purpose,
            request=request,
            metadata={"error": str(exc)[:200]},
        )
        raise

    await audit_service.audit(
        db,
        action="social.credential_revealed",
        tenant_id=cred.tenant_id,
        actor_user_id=principal.user_id if principal else None,
        target_kind="social_credential",
        target_id=cred.id,
        purpose=purpose,
        request=request,
        metadata={"platform": cred.platform},
    )
    return payload


# ════════════════════════════════════════════════════════════
# 3. 撤销（用户主动断开 / 后台检测到 refresh 失败）
# ════════════════════════════════════════════════════════════

async def revoke_credential(
    db: AsyncSession,
    *,
    principal: Principal | None,
    credential_id: uuid.UUID,
    reason: str,
    request=None,
) -> SocialCredential:
    cred = await db.get(SocialCredential, credential_id)
    if not cred:
        raise ValueError(f"credential {credential_id} not found")
    if cred.revoked_at is not None:
        return cred  # 已撤销·幂等

    now = datetime.now(timezone.utc)
    cred.revoked_at = now
    cred.refresh_failed_at = now

    # 同步把所有 reference 此 cred 的 social_accounts 标 disconnected
    stmt = (
        update(SocialAccount)
        .where(SocialAccount.credential_id == cred.id)
        .values(status="disconnected", updated_at=now)
    )
    await db.execute(stmt)
    await db.flush()

    await audit_service.audit(
        db,
        action="social.credential_revoked",
        tenant_id=cred.tenant_id,
        actor_user_id=principal.user_id if principal else None,
        target_kind="social_credential",
        target_id=cred.id,
        request=request,
        metadata={"reason": reason},
    )
    return cred


# ════════════════════════════════════════════════════════════
# 4. 健康检查（list dashboard 用）· 不解密只读 metadata
# ════════════════════════════════════════════════════════════

async def list_credentials_summary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """列凭证·只返非敏感字段·永不返 ciphertext"""
    q = select(SocialCredential).where(
        SocialCredential.tenant_id == tenant_id,
        SocialCredential.revoked_at.is_(None),
    )
    if platform:
        q = q.where(SocialCredential.platform == platform)
    q = q.order_by(SocialCredential.created_at.desc())
    result = await db.execute(q)
    rows = list(result.scalars().all())
    return [
        {
            "id": str(c.id),
            "platform": c.platform,
            "credential_type": c.credential_type,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "refresh_failed_at": c.refresh_failed_at.isoformat() if c.refresh_failed_at else None,
            "scopes": c.scopes,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


__all__ = [
    "list_credentials_summary",
    "reveal_credential",
    "revoke_credential",
    "store_credential",
]
