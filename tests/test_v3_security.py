"""Unit tests for v3 P0 security primitives.

Pure Python — no DB, no async — covering the parts that can be verified
in isolation:

  - Vault encrypt/decrypt round-trip
  - Vault tamper detection (changing ciphertext / AAD breaks decrypt)
  - Domain HMAC stability + normalisation
  - AuditAction taxonomy enumerable

Database-touching tests (audit immutability trigger, RLS, token_version
flow) live in tests/test_db_security.py and require Postgres.
"""
from __future__ import annotations

import os

# IMPORTANT: configure crypto env BEFORE importing the service module.
# 64-char hex == 32 bytes == 256 bits.
os.environ["VAULT_KEK_HEX"] = "a" * 64  # AES-256 key (deterministic, test-only)
os.environ["VAULT_HMAC_HEX"] = "b" * 64  # HMAC-SHA256 key
os.environ["VAULT_KEK_ACTIVE_VERSION"] = "1"
os.environ["APP_ENV"] = "development"  # bypass prod placeholder check
os.environ["SECRET_KEY"] = "x"

import pytest  # noqa: E402

from app.services import vault_service  # noqa: E402
from app.services.audit_service import AuditAction  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
#  Vault encryption
# ──────────────────────────────────────────────────────────────────────

SAMPLE_TENANT = "11111111-1111-1111-1111-111111111111"
SAMPLE_ASSET = "22222222-2222-2222-2222-222222222222"


def test_vault_round_trip_login() -> None:
    payload = {
        "username": "lijiajia",
        "password": "Pa$$w0rd!",
        "domain": "sothebys.com",
    }
    enc = vault_service.encrypt_payload(
        payload=payload,
        tenant_id=SAMPLE_TENANT,
        asset_id=SAMPLE_ASSET,
        vault_kind="login",
    )
    out = vault_service.decrypt_payload(
        encrypted_payload=enc["encrypted_payload"],
        nonce=enc["nonce"],
        wrapped_dek=enc["wrapped_dek"],
        aad=enc["aad"],
        kek_ref=enc["kek_ref"],
    )
    assert out == payload


def test_vault_ciphertext_tamper_fails() -> None:
    """Flipping a single bit in the ciphertext must trigger an auth-tag
    failure on decrypt — that's the whole point of GCM."""
    enc = vault_service.encrypt_payload(
        payload={"a": 1},
        tenant_id=SAMPLE_TENANT,
        asset_id=SAMPLE_ASSET,
        vault_kind="note",
    )
    bad = bytearray(enc["encrypted_payload"])
    bad[0] ^= 0x01  # flip one bit
    with pytest.raises(Exception):  # cryptography.exceptions.InvalidTag in real env
        vault_service.decrypt_payload(
            encrypted_payload=bytes(bad),
            nonce=enc["nonce"],
            wrapped_dek=enc["wrapped_dek"],
            aad=enc["aad"],
            kek_ref=enc["kek_ref"],
        )


def test_vault_aad_swap_fails() -> None:
    """Swapping AAD between rows must break decrypt — protects against
    an attacker copying ciphertext from one user's row to another."""
    enc_a = vault_service.encrypt_payload(
        payload={"a": 1}, tenant_id=SAMPLE_TENANT, asset_id=SAMPLE_ASSET, vault_kind="note"
    )
    other_tenant = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(Exception):
        vault_service.decrypt_payload(
            encrypted_payload=enc_a["encrypted_payload"],
            nonce=enc_a["nonce"],
            wrapped_dek=enc_a["wrapped_dek"],
            aad={"t": other_tenant, "a": SAMPLE_ASSET, "k": "note", "v": 1},  # swapped tenant
            kek_ref=enc_a["kek_ref"],
        )


def test_vault_returns_kek_ref_for_active_version() -> None:
    enc = vault_service.encrypt_payload(
        payload={"x": 1}, tenant_id=SAMPLE_TENANT, asset_id=SAMPLE_ASSET, vault_kind="note"
    )
    assert enc["kek_ref"] == "env:1"


# ──────────────────────────────────────────────────────────────────────
#  Domain HMAC
# ──────────────────────────────────────────────────────────────────────


def test_domain_hash_stable_and_normalised() -> None:
    a = vault_service.domain_hash("https://www.Sothebys.com/auctions/")
    b = vault_service.domain_hash("sothebys.com")
    c = vault_service.domain_hash("SOTHEBYS.COM")
    assert a == b == c
    assert len(a) == 32  # SHA-256 digest


def test_domain_hash_distinguishes_different_domains() -> None:
    a = vault_service.domain_hash("sothebys.com")
    b = vault_service.domain_hash("christies.com")
    assert a != b


def test_normalise_domain_examples() -> None:
    assert vault_service.normalise_domain(" HTTPS://Foo.bar/baz ") == "foo.bar"
    assert vault_service.normalise_domain("www.example.com") == "example.com"
    assert vault_service.normalise_domain("plain.com") == "plain.com"


# ──────────────────────────────────────────────────────────────────────
#  AuditAction taxonomy
# ──────────────────────────────────────────────────────────────────────


def test_audit_actions_have_namespace_dot_verb_format() -> None:
    """Every AuditAction constant must look like 'namespace.verb' so the
    /v1/audit prefix-filter works ('vault.' catches all vault.* events)."""
    constants = {
        k: v
        for k, v in vars(AuditAction).items()
        if not k.startswith("_") and isinstance(v, str)
    }
    assert constants, "AuditAction class is empty"
    for name, value in constants.items():
        assert "." in value, f"AuditAction.{name} = {value!r} missing namespace dot"
        ns, _, verb = value.partition(".")
        assert ns and verb, f"AuditAction.{name} = {value!r} malformed"


def test_audit_action_categories_present() -> None:
    """Sanity: the 5 mandated categories from Cursor §02-9.1 all exist."""
    constants = {
        v
        for k, v in vars(AuditAction).items()
        if not k.startswith("_") and isinstance(v, str)
    }
    namespaces = {v.split(".")[0] for v in constants}
    for required in {"auth", "member", "asset", "vault", "ai"}:
        assert required in namespaces, f"missing audit namespace: {required}"
