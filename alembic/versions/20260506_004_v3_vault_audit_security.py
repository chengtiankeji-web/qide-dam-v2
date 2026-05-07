"""v3 P0 — Vault subsystem + immutable audit + sensitivity_level + token_version

Revision ID: 004_v3_security
Revises: 003_sprint4
Create Date: 2026-05-06

This is the v3 P0 migration. It adds, in additive-only fashion:

  1. assets.sensitivity_level    — public / internal / confidential / secret
                                     drives Vault encryption + AI access
  2. assets.kind                  — extended CHECK to allow vault_login /
                                     vault_identity / vault_note kinds
  3. vault_items                  — encrypted_payload (server-side AES-GCM)
                                     + aad + schema_version, 1:1 with asset
  4. vault_key_material           — wrapped_dek + kek_ref + key_version
                                     (per-asset envelope keys)
  5. audit_events                 — append-only event stream with rich
                                     actor/target/purpose/metadata fields
  6. PG triggers on audit_events  — RAISE EXCEPTION on UPDATE or DELETE
                                     so even a compromised DB role cannot
                                     rewrite the trail
  7. users.token_version          — bumped on revoke; JWT carries `tv` claim
                                     which auth middleware compares
  8. api_keys.revoked_at          — soft revoke flag for API keys
  9. assets.requires_purpose      — denormalised bool, true for sensitivity
                                     >= confidential. AI tools enforce this.

All operations are reversible and additive — no existing column is
dropped or its type narrowed, so this can roll out behind a feature
flag without breaking the live API.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "004_v3_security"
down_revision: Union[str, None] = "003_sprint4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Allowed sensitivity values, exact same order as ASSET_SENSITIVITIES in
# app.models.asset — keep these in sync.
SENSITIVITY_VALUES = ("public", "internal", "confidential", "secret")


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # 1. assets: add sensitivity_level + requires_purpose, extend `kind`
    # ─────────────────────────────────────────────────────────────────
    op.add_column(
        "assets",
        sa.Column(
            "sensitivity_level",
            sa.String(16),
            nullable=False,
            server_default="internal",
        ),
    )
    op.add_column(
        "assets",
        sa.Column(
            "requires_purpose",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_assets_sensitivity_valid",
        "assets",
        "sensitivity_level IN ('public','internal','confidential','secret')",
    )
    op.create_index(
        "ix_assets_sensitivity_level", "assets", ["sensitivity_level"]
    )

    # `kind` previously had no DB-level CHECK (just a Python tuple in the
    # ORM). Add one now that we have new vault kinds — including the
    # historical kinds + the three new vault types.
    op.create_check_constraint(
        "ck_assets_kind_valid",
        "assets",
        "kind IN ("
        "'image','video','audio','document','archive','model3d','other',"
        "'vault_login','vault_identity','vault_note'"
        ")",
    )

    # ─────────────────────────────────────────────────────────────────
    # 2. vault_items — server-stored encrypted payloads, 1:1 with asset
    # ─────────────────────────────────────────────────────────────────
    op.create_table(
        "vault_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vault_kind",
            sa.String(32),
            nullable=False,
        ),
        # Encrypted JSON blob: ciphertext bytes from AES-256-GCM.
        # Schema-versioned so future migrations can re-encrypt without
        # data-loss risk.
        sa.Column("encrypted_payload", sa.LargeBinary, nullable=False),
        # Nonce / IV reused alongside the ciphertext for decryption.
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        # Additional Authenticated Data — bound to (workspace_id, asset_id,
        # vault_kind) so an attacker swapping ciphertext between rows fails
        # auth tag verification.
        sa.Column("aad", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        # Searchable surfaces — only non-sensitive fields are stored
        # plaintext here. Domain hash is HMAC-SHA256(workspace_secret, domain).
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("domain_hash", sa.LargeBinary, nullable=True),
        sa.Column("labels", postgresql.ARRAY(sa.String(64)), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_check_constraint(
        "ck_vault_items_kind_valid",
        "vault_items",
        "vault_kind IN ('login','identity','note','totp')",
    )
    op.create_index("ix_vault_items_tenant_id", "vault_items", ["tenant_id"])
    op.create_index(
        "ix_vault_items_domain_hash", "vault_items", ["domain_hash"]
    )

    # ─────────────────────────────────────────────────────────────────
    # 3. vault_key_material — wrapped DEKs, one per vault_item
    # ─────────────────────────────────────────────────────────────────
    # Sprint 1 (now): server holds the master KEK in env, wraps a DEK
    #                 per vault_item; on read it unwraps + decrypts the
    #                 payload and returns plaintext to the authorised user.
    # Sprint 2 (P1-1): client generates DEK locally, server only stores
    #                  client-encrypted wrapped_dek and never sees the DEK
    #                  in plaintext (true zero-knowledge).
    # The schema below supports both stages — the wrapped_dek bytes are
    # opaque to the server in either case.
    op.create_table(
        "vault_key_material",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "vault_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vault_items.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Bytes of the DEK after being wrapped by the master KEK.
        sa.Column("wrapped_dek", sa.LargeBinary, nullable=False),
        # Identifier of the KEK used (e.g. "env:1" → ENV var, version 1;
        # later "kms:arn:..." for AWS KMS or "vault:transit/..." for HCV).
        sa.Column("kek_ref", sa.String(255), nullable=False),
        # Version of the KEK for rotation; bumped when re-wrapping.
        sa.Column("key_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ─────────────────────────────────────────────────────────────────
    # 4. audit_events — APPEND-ONLY event stream, immutable by trigger
    # ─────────────────────────────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # Actor — null is allowed for system-generated events.
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("actor_kind", sa.String(16), nullable=False, server_default="user"),
        # Action — namespaced as <category>.<verb> e.g. asset.uploaded,
        # vault.revealed, ai.search_called.
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(8),
            nullable=False,
            server_default="success",
        ),
        # `purpose` is required for any AI tool call or sensitive read;
        # nullable here so non-sensitive events don't have to fill it.
        sa.Column("purpose", sa.String(255), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        # Searchable JSONB for everything else — search-query hashes, file
        # sizes, edition numbers, anything contextually useful per event.
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_check_constraint(
        "ck_audit_events_status_valid",
        "audit_events",
        "status IN ('success','fail','denied')",
    )
    op.create_check_constraint(
        "ck_audit_events_actor_kind_valid",
        "audit_events",
        "actor_kind IN ('user','api_key','system','ai')",
    )
    op.create_index("ix_audit_events_tenant_created", "audit_events", ["tenant_id", "created_at"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_target", "audit_events", ["target_kind", "target_id"])

    # The crucial part: a trigger that makes audit_events truly
    # append-only at the database layer. Even a compromised application
    # role cannot rewrite history — only superuser DDL (drop trigger)
    # could bypass this, and that itself is auditable in pg_audit logs.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events is append-only — % blocked',
                TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION audit_events_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_delete
        BEFORE DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION audit_events_immutable();
        """
    )

    # ─────────────────────────────────────────────────────────────────
    # 5. users.token_version — JWT bump-to-revoke
    # ─────────────────────────────────────────────────────────────────
    # Bumping this column invalidates every JWT issued before the bump
    # (the auth middleware compares JWT.tv to user.token_version on every
    # request). Used when:
    #   - a user is removed from a tenant
    #   - an admin force-logs-out a compromised account
    #   - a user changes their password
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
    )

    # ─────────────────────────────────────────────────────────────────
    # 6. api_keys.revoked_at — soft revoke for API keys
    # ─────────────────────────────────────────────────────────────────
    op.add_column(
        "api_keys",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_api_keys_revoked_at", "api_keys", ["revoked_at"]
    )


def downgrade() -> None:
    # Reverse order so FKs unwind cleanly.

    # 6. api_keys.revoked_at
    op.drop_index("ix_api_keys_revoked_at", table_name="api_keys")
    op.drop_column("api_keys", "revoked_at")

    # 5. users.token_version
    op.drop_column("users", "token_version")

    # 4. audit_events — drop triggers first, then table
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events;")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable();")
    op.drop_index("ix_audit_events_target", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_created", table_name="audit_events")
    op.drop_table("audit_events")

    # 3. vault_key_material
    op.drop_table("vault_key_material")

    # 2. vault_items
    op.drop_index("ix_vault_items_domain_hash", table_name="vault_items")
    op.drop_index("ix_vault_items_tenant_id", table_name="vault_items")
    op.drop_table("vault_items")

    # 1. assets
    op.drop_constraint("ck_assets_kind_valid", "assets", type_="check")
    op.drop_index("ix_assets_sensitivity_level", table_name="assets")
    op.drop_constraint("ck_assets_sensitivity_valid", "assets", type_="check")
    op.drop_column("assets", "requires_purpose")
    op.drop_column("assets", "sensitivity_level")
