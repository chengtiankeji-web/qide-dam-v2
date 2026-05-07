"""Vault — encrypted high-sensitivity entries (passwords, IDs, notes).

v3 P0-1. Backed by alembic 004_v3_security migration.

Design summary
--------------
Each Vault entry is *also* an Asset row (so the unified search / ACL /
audit / collections infra applies uniformly), but its payload bytes
live in `vault_items.encrypted_payload` rather than object storage.

  ┌──────────────┐      ┌────────────────┐      ┌───────────────────┐
  │   assets     │←────│   vault_items   │────→│ vault_key_material│
  │ kind=vault_* │ 1:1 │ encrypted_payload│ 1:1 │   wrapped_dek     │
  │ sensitivity  │      │  + nonce + aad  │      │  + kek_ref + ver  │
  │  = secret    │      └────────────────┘      └───────────────────┘
  └──────────────┘

Encryption choice (Sprint 1 / "server-side"):
  - AES-256-GCM (authenticated encryption from the standard library)
  - Per-item DEK (32 random bytes, never written to disk)
  - DEK wrapped with the master KEK loaded from VAULT_KEK_HEX env var
  - AAD bound to (tenant_id, asset_id, vault_kind) so payloads cannot
    be swapped between rows without auth-tag failure

Sprint 2 (v3.2 / P1-1) will add a client-side wrap on top of this so
the server can no longer read the DEK in plaintext (true zero-knowledge).
The schema below already supports both: the server treats `wrapped_dek`
as opaque bytes either way.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.asset import Asset


VAULT_KINDS = ("login", "identity", "note", "totp")


class VaultItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted-payload mate to an asset row of kind vault_*.

    The asset_id back-reference is unique — a Vault entry is exactly one
    asset, never shared. Searchable fields (`title`, `domain_hash`,
    `labels`) are stored plaintext-but-narrow; everything sensitive is
    inside `encrypted_payload`.
    """

    __tablename__ = "vault_items"
    __table_args__ = (
        Index("ix_vault_items_tenant_id", "tenant_id"),
        Index("ix_vault_items_domain_hash", "domain_hash"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    vault_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Cipher payload ---
    # AES-256-GCM ciphertext bytes; includes the GCM auth tag.
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # AAD typically: {"t": tenant_id, "a": asset_id, "k": vault_kind, "v": schema}
    aad: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # --- Searchable narrow surfaces ---
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # HMAC-SHA256(VAULT_HMAC_HEX, normalised_domain) for vault_login items
    # so users can search by domain without leaking the domain list.
    domain_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    labels: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(64)), nullable=True
    )

    # --- Relationships ---
    asset: Mapped[Asset] = relationship()
    key_material: Mapped[VaultKeyMaterial] = relationship(
        back_populates="vault_item",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<VaultItem asset={self.asset_id} kind={self.vault_kind}>"


class VaultKeyMaterial(UUIDPrimaryKeyMixin, Base):
    """Wrapped Data Encryption Key for a single vault_item.

    The bytes in `wrapped_dek` are opaque to the server when
    Sprint-2 client-side encryption is enabled (server can't unwrap).
    For Sprint 1 the server's own KEK wraps the DEK and the server
    can unwrap during reveal. Either way the column shape is the same.
    """

    __tablename__ = "vault_key_material"

    vault_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vault_items.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # e.g. "env:1" → ENV-loaded KEK version 1 (Sprint 1 default)
    #      "kms:arn:aws:..." → AWS KMS key id (Enterprise upgrade)
    #      "client:user-derived" → client-side, server cannot unwrap
    kek_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # --- Relationships ---
    vault_item: Mapped[VaultItem] = relationship(back_populates="key_material")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<VaultKeyMaterial item={self.vault_item_id} ref={self.kek_ref}>"
