"""Pydantic schemas for /v1/vault routes.

The encrypted-payload bytes never travel over the wire as bytes — they're
hex-encoded in JSON. The plaintext payload shapes (login / identity /
note) are documented here so clients know what fields to pass.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

VaultKind = Literal["login", "identity", "note", "totp"]


# ─── Plaintext payload shapes ──────────────────────────────────────────
# These are what the client sends as `payload` and gets back on reveal.
# They become JSON inside the encrypted blob — never stored plaintext.


class LoginPayload(BaseModel):
    """Vault entry for username/password (with optional TOTP secret)."""

    model_config = ConfigDict(extra="allow")

    username: str
    password: str
    # Optional: TOTP shared secret (base32) so the Vault can also be a 2FA
    # store for non-Vault accounts.
    totp_secret: str | None = None
    notes: str | None = None
    # The domain a user might search by ("sothebys.com"). Stored both as
    # plaintext inside the encrypted payload AND as an HMAC-hash on the
    # row level so the user can search.
    domain: str | None = None
    url: str | None = None


class IdentityPayload(BaseModel):
    """Vault entry for an ID document — passport, ID card, driver's licence."""

    model_config = ConfigDict(extra="allow")

    full_name: str
    document_type: Literal["passport", "national_id", "driver_licence", "other"] = "passport"
    document_number: str
    issuing_country: str | None = None
    issued_on: str | None = None  # ISO date
    expires_on: str | None = None
    notes: str | None = None


class NotePayload(BaseModel):
    """Free-form secure note — API keys, private procedures, etc."""

    model_config = ConfigDict(extra="allow")

    body: str
    notes: str | None = None


# ─── Request / response wrappers ───────────────────────────────────────


class VaultItemCreate(BaseModel):
    """POST /v1/vault — body."""

    project_id: UUID
    vault_kind: VaultKind
    title: Annotated[str, Field(min_length=1, max_length=255)]
    payload: dict[str, Any]
    labels: list[str] | None = None

    @field_validator("payload")
    @classmethod
    def _payload_not_empty(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("payload must not be empty")
        return v


class VaultItemUpdate(BaseModel):
    """PATCH /v1/vault/{id} — partial update; payload is full-replace if given."""

    title: str | None = Field(default=None, max_length=255)
    payload: dict[str, Any] | None = None
    labels: list[str] | None = None


class VaultItemSummary(BaseModel):
    """List view — never includes payload bytes or any decrypted data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID
    tenant_id: UUID
    vault_kind: str
    title: str
    labels: list[str] | None = None
    has_domain_hash: bool
    schema_version: int
    created_at: datetime
    updated_at: datetime


class VaultItemReveal(BaseModel):
    """Response of POST /v1/vault/{id}/reveal — decrypted payload + audit info."""

    id: UUID
    vault_kind: str
    title: str
    payload: dict[str, Any]
    revealed_at: datetime
    purpose: str


class VaultDomainSearchRequest(BaseModel):
    """POST /v1/vault/search/domain — find logins by domain HMAC.

    Client sends raw domain; server normalises + hashes + queries.
    """

    domain: Annotated[str, Field(min_length=2, max_length=255)]


__all__ = [
    "VaultKind",
    "LoginPayload",
    "IdentityPayload",
    "NotePayload",
    "VaultItemCreate",
    "VaultItemUpdate",
    "VaultItemSummary",
    "VaultItemReveal",
    "VaultDomainSearchRequest",
]
