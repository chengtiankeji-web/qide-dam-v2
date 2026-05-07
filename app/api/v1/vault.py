"""/v1/vault — Vault CRUD + reveal flow.

v3 P0-1.

Five endpoints:

  POST   /v1/vault                       create entry (encrypts payload server-side)
  GET    /v1/vault                       list entries in current tenant (no payloads)
  GET    /v1/vault/{id}                  get summary + metadata (still no payload)
  POST   /v1/vault/{id}/reveal           decrypt & return payload (audited; purpose REQUIRED)
  PATCH  /v1/vault/{id}                  update title / labels / payload
  DELETE /v1/vault/{id}                  soft-delete entry
  POST   /v1/vault/search/domain         find logins by HMAC-hashed domain

Every reveal call writes a vault.revealed audit event with the user's
explicit `purpose` string — that's the GDPR-compliance + insider-abuse
defence layer. List + search calls are NOT audited at this granularity
(they leak only metadata, no plaintext).
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.asset import Asset
from app.models.vault import VaultItem, VaultKeyMaterial
from app.schemas.vault import (
    VaultDomainSearchRequest,
    VaultItemCreate,
    VaultItemReveal,
    VaultItemSummary,
    VaultItemUpdate,
)
from app.services import vault_service
from app.services.audit_service import AuditAction, audit

logger = get_logger("vault")
router = APIRouter(prefix="/vault", tags=["vault"])


# ─── helpers ────────────────────────────────────────────────────────────


async def _load_for_principal(
    db: AsyncSession, principal: Principal, item_id: uuid.UUID
) -> tuple[VaultItem, uuid.UUID | None]:
    """Fetch a vault item + its project_id (via parent Asset).

    Tenant boundary is enforced; 404 on miss / wrong tenant. project_id is
    returned alongside so audit rows for read/reveal/update/delete can be
    correctly scoped to the project — without it admin "show me last 7
    days for project X" queries miss the bulk of vault activity.
    """
    row = (
        await db.execute(
            select(VaultItem, Asset.project_id)
            .join(Asset, Asset.id == VaultItem.asset_id)
            .where(
                VaultItem.id == item_id,
                VaultItem.tenant_id == principal.tenant_id,
            )
        )
    ).one_or_none()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vault item not found")
    return row[0], row[1]


def _summary(item: VaultItem) -> VaultItemSummary:
    return VaultItemSummary(
        id=item.id,
        asset_id=item.asset_id,
        tenant_id=item.tenant_id,
        vault_kind=item.vault_kind,
        title=item.title,
        labels=item.labels,
        has_domain_hash=item.domain_hash is not None,
        schema_version=item.schema_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


# ─── endpoints ──────────────────────────────────────────────────────────


@router.post("", response_model=VaultItemSummary, status_code=status.HTTP_201_CREATED)
async def create_vault_item(
    body: VaultItemCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> VaultItemSummary:
    """Create a new vault entry. Payload is encrypted server-side; the
    caller never sees it again unless they call /reveal."""

    if not principal.can_access_project(body.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to that project")

    # 1) Create the parent Asset row (kind=vault_*, sensitivity=secret).
    asset_kind = f"vault_{body.vault_kind}"  # e.g. vault_login
    if asset_kind not in {"vault_login", "vault_identity", "vault_note"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported vault kind: {body.vault_kind}")

    asset = Asset(
        tenant_id=principal.tenant_id,
        project_id=body.project_id,
        owner_user_id=principal.user_id,
        name=body.title,
        sha256="",  # not applicable for Vault — payload is encrypted
        kind=asset_kind,
        mime_type="application/json+encrypted",
        extension="vault",
        size_bytes=0,
        storage_key=f"vault/{uuid.uuid4()}",  # placeholder — Vault doesn't use object storage
        storage_bucket="",
        sensitivity_level="secret",
        requires_purpose=True,
        acl="private",
    )
    db.add(asset)
    await db.flush()  # populate asset.id

    # 2) Encrypt payload + wrap DEK.
    enc = vault_service.encrypt_payload(
        payload=body.payload,
        tenant_id=str(principal.tenant_id),
        asset_id=str(asset.id),
        vault_kind=body.vault_kind,
    )

    # 3) Compute domain hash for vault_login if domain present.
    dh: bytes | None = None
    if body.vault_kind == "login":
        domain_raw = body.payload.get("domain") or body.payload.get("url")
        if isinstance(domain_raw, str) and domain_raw.strip():
            dh = vault_service.domain_hash(domain_raw)

    # 4) Insert vault_item + vault_key_material.
    vi = VaultItem(
        asset_id=asset.id,
        tenant_id=principal.tenant_id,
        vault_kind=body.vault_kind,
        encrypted_payload=enc["encrypted_payload"],
        nonce=enc["nonce"],
        aad=enc["aad"],
        schema_version=enc["schema_version"],
        title=body.title,
        domain_hash=dh,
        labels=body.labels,
    )
    db.add(vi)
    await db.flush()
    db.add(
        VaultKeyMaterial(
            vault_item_id=vi.id,
            wrapped_dek=enc["wrapped_dek"],
            kek_ref=enc["kek_ref"],
            key_version=1,
        )
    )

    await audit(
        db,
        action=AuditAction.VAULT_CREATED,
        tenant_id=principal.tenant_id,
        project_id=body.project_id,
        actor_user_id=principal.user_id,
        actor_kind="user" if principal.via == "jwt" else "api_key",
        target_kind="vault_item",
        target_id=vi.id,
        request=request,
        metadata={"vault_kind": body.vault_kind, "asset_id": str(asset.id)},
    )

    await db.commit()
    return _summary(vi)


@router.get("", response_model=list[VaultItemSummary])
async def list_vault_items(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
    vault_kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[VaultItemSummary]:
    """List vault entries for the current tenant. Never returns payload bytes."""
    stmt = select(VaultItem).where(VaultItem.tenant_id == principal.tenant_id)
    if vault_kind:
        stmt = stmt.where(VaultItem.vault_kind == vault_kind)
    stmt = stmt.order_by(VaultItem.updated_at.desc()).limit(limit).offset(offset)

    items = (await db.execute(stmt)).scalars().all()
    return [_summary(i) for i in items]


@router.get("/{item_id}", response_model=VaultItemSummary)
async def get_vault_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> VaultItemSummary:
    item, _project_id = await _load_for_principal(db, principal, item_id)
    return _summary(item)


@router.post("/{item_id}/reveal", response_model=VaultItemReveal)
async def reveal_vault_item(
    item_id: uuid.UUID,
    request: Request,
    purpose: Annotated[
        str,
        Query(
            min_length=4,
            max_length=255,
            description=(
                "WHY are you revealing this? Required by Vault's audit policy. "
                "E.g. 'Reset client account password', 'Fill OAuth redirect form'."
            ),
        ),
    ],
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> VaultItemReveal:
    """Decrypt and return the payload. Always audited with `purpose`.

    AI agents (api_key with actor_kind='api_key') are allowed to call
    this only if the key has the `vault:reveal` scope explicitly — see
    Sprint 1 doc note in CLAUDE.md. Default-deny.
    """
    item, project_id = await _load_for_principal(db, principal, item_id)

    # AI / api_key callers need explicit scope. Human JWT callers are
    # gated by tenant boundary only (further role gating is P1).
    if principal.via == "api_key" and "vault:reveal" not in (principal.scopes or []):
        await audit(
            db,
            action=AuditAction.AI_TOOL_DENIED,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            actor_user_id=principal.user_id,
            actor_kind="api_key",
            target_kind="vault_item",
            target_id=item.id,
            status="denied",
            purpose=purpose,
            request=request,
            metadata={"reason": "scope_missing", "scope_required": "vault:reveal"},
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API key missing scope vault:reveal — vault contents are AI-restricted",
        )

    # Pre-audit: register the intent before we decrypt. If decrypt fails
    # we'll write a second 'vault.decrypt_failed' event, so we always
    # know what was attempted.
    await audit(
        db,
        action=AuditAction.VAULT_READ_REQUESTED,
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_user_id=principal.user_id,
        actor_kind="user" if principal.via == "jwt" else "api_key",
        target_kind="vault_item",
        target_id=item.id,
        purpose=purpose,
        request=request,
        metadata={"vault_kind": item.vault_kind},
    )

    # Load key material and decrypt.
    km = (
        await db.execute(
            select(VaultKeyMaterial).where(VaultKeyMaterial.vault_item_id == item.id)
        )
    ).scalar_one_or_none()
    if not km:
        await audit(
            db,
            action=AuditAction.VAULT_DECRYPT_FAILED,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            actor_user_id=principal.user_id,
            target_kind="vault_item",
            target_id=item.id,
            status="fail",
            purpose=purpose,
            request=request,
            metadata={"reason": "no_key_material"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "key material missing")

    try:
        plaintext = vault_service.decrypt_payload(
            encrypted_payload=item.encrypted_payload,
            nonce=item.nonce,
            wrapped_dek=km.wrapped_dek,
            aad=item.aad,
            kek_ref=km.kek_ref,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("vault_decrypt_failed", item_id=str(item.id), error=str(exc))
        await audit(
            db,
            action=AuditAction.VAULT_DECRYPT_FAILED,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            actor_user_id=principal.user_id,
            target_kind="vault_item",
            target_id=item.id,
            status="fail",
            purpose=purpose,
            request=request,
            metadata={"reason": "crypto_error", "exc_type": type(exc).__name__},
        )
        await db.commit()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "decrypt failed")

    # Success audit — separate event so dashboards can count successful
    # reveals distinct from intent-only reads.
    revealed_event = await audit(
        db,
        action=AuditAction.VAULT_REVEALED,
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_user_id=principal.user_id,
        actor_kind="user" if principal.via == "jwt" else "api_key",
        target_kind="vault_item",
        target_id=item.id,
        purpose=purpose,
        request=request,
        metadata={"vault_kind": item.vault_kind},
    )
    await db.commit()

    return VaultItemReveal(
        id=item.id,
        vault_kind=item.vault_kind,
        title=item.title,
        payload=plaintext,
        revealed_at=(revealed_event.created_at if revealed_event else item.updated_at),
        purpose=purpose,
    )


@router.patch("/{item_id}", response_model=VaultItemSummary)
async def update_vault_item(
    item_id: uuid.UUID,
    body: VaultItemUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> VaultItemSummary:
    """Edit title, labels, and/or replace payload entirely.

    For payload changes we re-encrypt with a fresh DEK + nonce. The old
    DEK is discarded — there is no plaintext copy left anywhere.
    """
    item, project_id = await _load_for_principal(db, principal, item_id)

    if body.title is not None:
        item.title = body.title
    if body.labels is not None:
        item.labels = body.labels

    if body.payload is not None:
        enc = vault_service.encrypt_payload(
            payload=body.payload,
            tenant_id=str(principal.tenant_id),
            asset_id=str(item.asset_id),
            vault_kind=item.vault_kind,
            schema_version=item.schema_version,
        )
        item.encrypted_payload = enc["encrypted_payload"]
        item.nonce = enc["nonce"]
        item.aad = enc["aad"]

        # Replace key material row — atomic via cascade.
        km = (
            await db.execute(
                select(VaultKeyMaterial).where(VaultKeyMaterial.vault_item_id == item.id)
            )
        ).scalar_one_or_none()
        if km:
            km.wrapped_dek = enc["wrapped_dek"]
            km.kek_ref = enc["kek_ref"]
            km.key_version = km.key_version + 1

        # Refresh domain_hash for vault_login.
        if item.vault_kind == "login":
            domain_raw = body.payload.get("domain") or body.payload.get("url")
            item.domain_hash = (
                vault_service.domain_hash(domain_raw)
                if isinstance(domain_raw, str) and domain_raw.strip()
                else None
            )

    await audit(
        db,
        action=AuditAction.VAULT_UPDATED,
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_user_id=principal.user_id,
        actor_kind="user" if principal.via == "jwt" else "api_key",
        target_kind="vault_item",
        target_id=item.id,
        request=request,
        metadata={
            "vault_kind": item.vault_kind,
            "fields_changed": [
                k
                for k, v in {
                    "title": body.title,
                    "labels": body.labels,
                    "payload": body.payload,
                }.items()
                if v is not None
            ],
        },
    )

    await db.commit()
    await db.refresh(item)
    return _summary(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vault_item(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> None:
    item, project_id = await _load_for_principal(db, principal, item_id)

    # Hard-delete the vault row + key material (cascade) — there is no
    # legitimate reason to keep the cipher around. The audit row stays.
    await db.delete(item)

    await audit(
        db,
        action=AuditAction.VAULT_DELETED,
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_user_id=principal.user_id,
        actor_kind="user" if principal.via == "jwt" else "api_key",
        target_kind="vault_item",
        target_id=item.id,
        request=request,
        metadata={"vault_kind": item.vault_kind},
    )

    await db.commit()


@router.post("/search/domain", response_model=list[VaultItemSummary])
async def search_by_domain(
    body: VaultDomainSearchRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[VaultItemSummary]:
    """Find vault_login items where the stored domain_hash matches the
    HMAC of the provided domain. Listing-level call — does not reveal
    payloads, so no per-row audit (matches list_vault_items behaviour).
    """
    target_hash = vault_service.domain_hash(body.domain)
    stmt = (
        select(VaultItem)
        .where(
            and_(
                VaultItem.tenant_id == principal.tenant_id,
                VaultItem.vault_kind == "login",
                VaultItem.domain_hash == target_hash,
            )
        )
        .order_by(VaultItem.updated_at.desc())
    )
    items = (await db.execute(stmt)).scalars().all()
    return [_summary(i) for i in items]
