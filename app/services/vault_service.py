"""VaultService — server-side AES-256-GCM envelope encryption.

v3 P0-1 (Sprint 1): server holds the master KEK in env. A per-item DEK is
generated, used to encrypt the JSON payload, then itself wrapped by the
KEK. On reveal the server unwraps the DEK and decrypts the payload, then
returns plaintext to an authorised user (with strong audit + purpose).

v3.2 P1-1 (Sprint 2 / "true zero-knowledge") will replace the server-side
KEK wrap with a client-side wrap derived from the user's password +
workspace salt via Argon2id. The schema below already accommodates either
mode — only the KEK ref string changes.

This module is deliberately stdlib-only (cryptography pkg is already a
transitive dep via PyJWT/passlib so no new dependency is added).

Design notes
------------
- AES-256-GCM provides both confidentiality and integrity in one pass;
  we don't need a separate HMAC.
- AAD (additional authenticated data) is bound to (tenant_id, asset_id,
  vault_kind, schema_version) so an attacker who swaps ciphertext rows
  in the DB triggers an auth-tag failure on decrypt.
- The DEK never touches the disk — it lives in memory only during
  create / reveal flows.
- HMAC-SHA256 is used for the searchable `domain_hash` so users can find
  "the password for sothebys.com" without leaking the whole domain
  inventory if the DB is dumped.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# ─── Constants ──────────────────────────────────────────────────────────
NONCE_BYTES = 12  # AES-GCM standard nonce length (96 bits)
DEK_BYTES = 32    # 256-bit DEK


# ─── Master key loading (idempotent) ────────────────────────────────────
def _load_kek_bytes() -> bytes:
    """Decode the configured master KEK from hex.

    Raises a clear error if the env var is the dev placeholder so misconfig
    can't silently make every Vault item undecryptable in prod.
    """
    hex_value = settings.VAULT_KEK_HEX
    if not hex_value or len(hex_value) != 64:
        raise RuntimeError(
            "VAULT_KEK_HEX must be a 64-char hex string (32 bytes / 256 bits). "
            "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    if hex_value == "0" * 64 and settings.is_production:
        raise RuntimeError(
            "VAULT_KEK_HEX is the dev placeholder in a production environment. "
            "Refusing to start — set a real key in .env.production."
        )
    return bytes.fromhex(hex_value)


def _load_hmac_bytes() -> bytes:
    hex_value = settings.VAULT_HMAC_HEX
    if not hex_value or len(hex_value) != 64:
        raise RuntimeError(
            "VAULT_HMAC_HEX must be a 64-char hex string (32 bytes)."
        )
    if hex_value == "1" * 64 and settings.is_production:
        raise RuntimeError(
            "VAULT_HMAC_HEX is the dev placeholder in production. "
            "Set a real value in .env.production."
        )
    return bytes.fromhex(hex_value)


# Lazy: module-level cache so we don't decode hex on every call.
_kek_cache: bytes | None = None
_hmac_cache: bytes | None = None


def _kek() -> bytes:
    global _kek_cache
    if _kek_cache is None:
        _kek_cache = _load_kek_bytes()
    return _kek_cache


def _hmac_key() -> bytes:
    global _hmac_cache
    if _hmac_cache is None:
        _hmac_cache = _load_hmac_bytes()
    return _hmac_cache


def _kek_ref() -> str:
    """Identifier of the active KEK, written into vault_key_material.kek_ref."""
    return f"env:{settings.VAULT_KEK_ACTIVE_VERSION}"


# ─── Envelope encryption primitives ─────────────────────────────────────


def _make_aad(*, tenant_id: str, asset_id: str, vault_kind: str, schema_version: int = 1) -> bytes:
    """Stable bytes for AAD — order matters, must match on encrypt/decrypt."""
    return json.dumps(
        {"t": tenant_id, "a": asset_id, "k": vault_kind, "v": schema_version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_payload(
    *,
    payload: dict[str, Any],
    tenant_id: str,
    asset_id: str,
    vault_kind: str,
    schema_version: int = 1,
) -> dict[str, bytes | dict[str, Any] | int | str]:
    """Encrypt a Vault payload and wrap the per-item DEK with the master KEK.

    Returns a dict with everything the caller needs to write to the DB:
      - encrypted_payload (bytes)  : AES-GCM ciphertext over the JSON
      - nonce (bytes)              : 12-byte random nonce
      - aad (dict)                 : the AAD as a JSON dict (also stored)
      - wrapped_dek (bytes)        : DEK wrapped by KEK
      - kek_ref (str)              : "env:1" etc.
      - schema_version (int)
    """
    dek = secrets.token_bytes(DEK_BYTES)
    payload_nonce = os.urandom(NONCE_BYTES)
    aad_bytes = _make_aad(
        tenant_id=tenant_id,
        asset_id=asset_id,
        vault_kind=vault_kind,
        schema_version=schema_version,
    )

    # 1) Encrypt the actual payload with the per-item DEK.
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(dek).encrypt(payload_nonce, payload_bytes, aad_bytes)

    # 2) Wrap the DEK with the master KEK. We embed a separate nonce inside
    #    the wrapped bytes so each wrap uses fresh randomness even if the
    #    same DEK were ever to be re-wrapped.
    wrap_nonce = os.urandom(NONCE_BYTES)
    wrapped = AESGCM(_kek()).encrypt(wrap_nonce, dek, aad_bytes)
    wrapped_dek = wrap_nonce + wrapped  # nonce ‖ ciphertext+tag

    # Zero out the DEK reference (best-effort; CPython doesn't guarantee
    # zeroing memory but at least we drop the local reference).
    del dek

    return {
        "encrypted_payload": ciphertext,
        "nonce": payload_nonce,
        "aad": json.loads(aad_bytes),
        "wrapped_dek": wrapped_dek,
        "kek_ref": _kek_ref(),
        "schema_version": schema_version,
    }


def decrypt_payload(
    *,
    encrypted_payload: bytes,
    nonce: bytes,
    wrapped_dek: bytes,
    aad: dict[str, Any],
    kek_ref: str,
) -> dict[str, Any]:
    """Reverse of encrypt_payload. Caller must check authorisation BEFORE
    invoking this — there is no permission check here.

    Raises cryptography.exceptions.InvalidTag on tampered ciphertext / wrong
    KEK / wrong AAD. Callers should map this to a 403/500 audit event.
    """
    if not kek_ref.startswith("env:"):
        # Future: dispatch to KMS or client-side handlers based on prefix.
        raise NotImplementedError(
            f"KEK reference {kek_ref!r} not handled by server-side reveal. "
            "(Sprint 2 client-side decrypt happens in browser, not here.)"
        )

    aad_bytes = json.dumps(aad, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # 1) Unwrap the DEK.
    wrap_nonce = wrapped_dek[:NONCE_BYTES]
    wrap_ct = wrapped_dek[NONCE_BYTES:]
    dek = AESGCM(_kek()).decrypt(wrap_nonce, wrap_ct, aad_bytes)

    # 2) Decrypt the payload.
    plaintext = AESGCM(dek).decrypt(nonce, encrypted_payload, aad_bytes)
    del dek

    return json.loads(plaintext.decode("utf-8"))


# ─── Domain hashing for searchable Login items ──────────────────────────


def normalise_domain(raw: str) -> str:
    """Canonicalise a domain so search hits work across slight variations.

    Lowercase, trim whitespace, strip protocol + path + leading 'www.'.
    """
    s = raw.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    if "/" in s:
        s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def domain_hash(raw_domain: str) -> bytes:
    """HMAC-SHA256(VAULT_HMAC_HEX, normalised_domain). 32 bytes."""
    return hmac.new(_hmac_key(), normalise_domain(raw_domain).encode("utf-8"), hashlib.sha256).digest()


__all__ = [
    "encrypt_payload",
    "decrypt_payload",
    "domain_hash",
    "normalise_domain",
]
