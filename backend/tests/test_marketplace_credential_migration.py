from __future__ import annotations

import runpy
from pathlib import Path

from sqlalchemy import BigInteger, LargeBinary, SmallInteger

from app.infra.models import Base
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountIngestionTokenRow,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "20260908_0061_marketplace_credentials.py"


def _column_names(table_name: str) -> set[str]:
    return set(Base.metadata.tables[table_name].c.keys())


def test_revision_extends_the_only_0060_head() -> None:
    assert MIGRATION.exists()
    migration = runpy.run_path(str(MIGRATION))
    assert migration["revision"] == "20260908_0061"
    assert migration["down_revision"] == "20260905_0060"


def test_credential_model_has_exact_account_owned_envelope() -> None:
    assert MarketplaceAccountCredentialRow.__tablename__ == "marketplace_account_credentials"
    table = Base.metadata.tables[MarketplaceAccountCredentialRow.__tablename__]
    assert _column_names(table.name) == {
        "credential_id",
        "organization_id",
        "marketplace_account_id",
        "provider",
        "credential_kind",
        "algorithm",
        "key_version",
        "aad_version",
        "payload_schema_version",
        "nonce",
        "ciphertext",
        "generation",
        "expires_at",
        "revoked_at",
        "revocation_reason_code",
        "created_at",
        "updated_at",
    }
    assert isinstance(table.c.organization_id.type, BigInteger)
    assert isinstance(table.c.marketplace_account_id.type, BigInteger)
    assert isinstance(table.c.generation.type, BigInteger)
    assert isinstance(table.c.aad_version.type, SmallInteger)
    assert isinstance(table.c.payload_schema_version.type, SmallInteger)
    assert isinstance(table.c.nonce.type, LargeBinary)
    assert isinstance(table.c.ciphertext.type, LargeBinary)
    assert ("organization_id", "marketplace_account_id", "provider") in {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in table.foreign_key_constraints
    }
    constraint_names = {constraint.name for constraint in table.constraints if constraint.name}
    assert {
        "fk_marketplace_account_credentials_org_account_provider",
        "ck_marketplace_account_credentials_algorithm",
        "ck_marketplace_account_credentials_aad_version",
        "ck_marketplace_account_credentials_schema",
        "ck_marketplace_account_credentials_nonce",
        "ck_marketplace_account_credentials_ciphertext",
        "ck_marketplace_account_credentials_generation",
        "ck_marketplace_account_credentials_expiry",
        "ck_marketplace_account_credentials_revocation",
    } <= constraint_names
    indexes = {index.name: index for index in table.indexes}
    active = indexes["uq_marketplace_account_credentials_active_kind"]
    assert active.unique is True
    assert tuple(column.name for column in active.columns) == (
        "organization_id",
        "marketplace_account_id",
        "credential_kind",
    )
    assert "revoked_at IS NULL" in str(active.dialect_options["postgresql"]["where"])
    assert "ix_marketplace_account_credentials_key_version" in indexes


def test_ingestion_token_model_is_one_way_and_account_owned() -> None:
    assert MarketplaceAccountIngestionTokenRow.__tablename__ == "marketplace_account_ingestion_tokens"
    table = Base.metadata.tables[MarketplaceAccountIngestionTokenRow.__tablename__]
    assert _column_names(table.name) == {
        "token_id",
        "organization_id",
        "marketplace_account_id",
        "provider",
        "verifier",
        "scope",
        "issued_at",
        "expires_at",
        "revoked_at",
        "revocation_reason_code",
        "last_used_at",
        "binding_schema_version",
        "binding_external_account_id",
        "binding_credential_ref",
        "binding_version",
    }
    assert "token" not in table.c
    assert "bearer" not in table.c
    assert isinstance(table.c.verifier.type, LargeBinary)
    assert ("organization_id", "marketplace_account_id", "provider") in {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in table.foreign_key_constraints
    }
    assert {constraint.name for constraint in table.constraints if constraint.name} >= {
        "fk_marketplace_account_ingestion_tokens_org_account_provider",
        "ck_marketplace_account_ingestion_tokens_provider",
        "ck_marketplace_account_ingestion_tokens_scope",
        "ck_marketplace_account_ingestion_tokens_verifier",
        "ck_marketplace_account_ingestion_tokens_expiry",
        "ck_marketplace_account_ingestion_tokens_revocation",
    }


def test_parent_has_provider_bound_composite_unique() -> None:
    table = Base.metadata.tables["marketplace_accounts"]
    assert (
        "organization_id",
        "marketplace_account_id",
        "marketplace",
    ) in {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_runtime_role_gate_includes_both_new_forced_rls_tables() -> None:
    role_sql = (ROOT / "ops" / "runtime-db-role.sql").read_text()
    assert "('marketplace_account_credentials')" in role_sql
    assert "('marketplace_account_ingestion_tokens')" in role_sql
    assert "relrowsecurity" in role_sql
    assert "relforcerowsecurity" in role_sql


def test_migration_is_additive_and_downgrade_never_restores_plaintext() -> None:
    source = MIGRATION.read_text()
    for legacy_name in (
        "lk_user_wb_tokens",
        "lk_user_avito_credentials",
        "wb_token",
        "client_secret",
        "cached_access_token",
    ):
        assert legacy_name not in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
