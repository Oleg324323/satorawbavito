from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from alembic import op
from sqlalchemy import MetaData, Table, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.infra.models import Base
from app.platform.economics import orm as _economics_models  # noqa: F401
from tests.test_empty_database_migrations import cluster, database  # noqa: F401

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260902_0055_dated_economics.py"
)
TABLES = {
    "organization_economics_versions",
    "catalog_economics_override_versions",
}


def test_dated_economics_migration_creates_forced_rls_ledgers(monkeypatch) -> None:
    created: list[str] = []
    statements: list[str] = []
    monkeypatch.setattr(
        op, "create_table", lambda name, *_args, **_kwargs: created.append(name)
    )
    monkeypatch.setattr(op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        op, "execute", lambda statement: statements.append(str(statement))
    )

    migration = runpy.run_path(str(MIGRATION))
    migration["upgrade"]()

    assert migration["revision"] == "20260902_0055"
    assert migration["down_revision"] == "20260902_0054"
    assert set(created) == TABLES
    for table in TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements
        assert any(
            f"CREATE POLICY tenant_isolation_{table}" in statement
            for statement in statements
        )


def test_dated_economics_orm_keys_keep_tenant_ownership() -> None:
    organization = Base.metadata.tables["organization_economics_versions"]
    override = Base.metadata.tables["catalog_economics_override_versions"]

    assert (
        "organization_id",
        "supersedes_organization_economics_version_id",
    ) in {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in organization.foreign_key_constraints
    }
    override_foreign_keys = {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in override.foreign_key_constraints
    }
    assert ("organization_id", "catalog_sku_id") in override_foreign_keys
    assert (
        "organization_id",
        "supersedes_catalog_economics_override_version_id",
    ) in override_foreign_keys


def test_tax_evidence_migration_is_additive_without_confirming_old_rows(
    monkeypatch,
) -> None:
    columns: list[tuple[str, object]] = []
    constraints: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        op, "add_column", lambda table, column: columns.append((table, column))
    )
    monkeypatch.setattr(
        op,
        "create_check_constraint",
        lambda name, table, condition: constraints.append((name, table, condition)),
    )
    migration = runpy.run_path(
        str(MIGRATION.with_name("20260915_0087_tax_evidence.py"))
    )
    migration["upgrade"]()

    assert migration["down_revision"] == "20260911_0086"
    assert [(table, column.name) for table, column in columns] == [
        ("organization_economics_versions", "tax_value_state"),
        ("organization_economics_versions", "tax_evidence_status"),
    ]
    assert all(
        column.nullable and column.server_default is None for _, column in columns
    )
    assert len(constraints) == 1
    condition = constraints[0][2]
    assert "tax_value_state IS NULL AND tax_evidence_status IS NULL" in condition
    assert "tax_basis_points IS NOT NULL" in condition


def test_tax_evidence_downgrade_refuses_to_erase_confirmed_policy(monkeypatch) -> None:
    actions: list[tuple[str, str]] = []
    monkeypatch.setattr(
        op, "execute", lambda statement: actions.append(("sql", str(statement)))
    )
    monkeypatch.setattr(
        op,
        "drop_constraint",
        lambda name, *_args, **_kwargs: actions.append(("constraint", name)),
    )
    monkeypatch.setattr(
        op, "drop_column", lambda _table, column: actions.append(("column", column))
    )
    migration = runpy.run_path(
        str(MIGRATION.with_name("20260915_0087_tax_evidence.py"))
    )
    migration["downgrade"]()

    assert "DISABLE ROW LEVEL SECURITY" in actions[0][1]
    assert "tax_value_state IS NOT NULL" in actions[1][1]
    assert "RAISE EXCEPTION" in actions[1][1]
    assert "FORCE ROW LEVEL SECURITY" in actions[3][1]
    assert actions[-2:] == [
        ("column", "tax_evidence_status"),
        ("column", "tax_value_state"),
    ]


def test_tax_migration_keeps_legacy_projection_compatible_and_preserves_evidence(
    database,
) -> None:
    from datetime import datetime, timezone

    from app.platform.economics.policies import EconomicsService

    config, engine = database
    command.upgrade(config, "20260911_0086")
    legacy_table = Table(
        "organization_economics_versions", MetaData(), autoload_with=engine
    )
    legacy = dict(
        organization_id=1,
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1000,
        value_state="assumed",
        effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source="fixture",
        source_reference="legacy",
        evidence_status="undated",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lk_organizations (organization_id, slug, name) VALUES (1, 'synthetic-tax', 'Synthetic tax')"
            )
        )
        connection.execute(insert(legacy_table).values(**legacy))
        before = connection.execute(select(legacy_table)).mappings().one()
    command.upgrade(config, "20260915_0087")
    with engine.begin() as connection:
        assert connection.execute(select(legacy_table)).mappings().one() == before
        assert connection.execute(
            text(
                "SELECT tax_value_state, tax_evidence_status FROM organization_economics_versions"
            )
        ).one() == (None, None)
        # Old ORM projections still insert/read using only their known columns.
        connection.execute(
            insert(legacy_table).values(
                **(legacy | {"source_reference": "old-image-write"})
            )
        )
        assert len(connection.execute(select(legacy_table)).all()) == 2
    command.downgrade(config, "20260911_0086")
    command.upgrade(config, "20260915_0087")
    with Session(engine) as session:
        service = EconomicsService(session, 1)
        service.set_organization_policy(
            **(
                {k: v for k, v in legacy.items() if k != "organization_id"}
                | dict(
                    tax_basis_points=750,
                    tax_value_state="configured",
                    tax_evidence_status="dated",
                    effective_from=datetime(2026, 8, 31, 21, tzinfo=timezone.utc),
                    source_reference="owner-confirmation",
                )
            )
        )
    with pytest.raises(
        DBAPIError, match="cannot downgrade while confirmed tax evidence exists"
    ):
        command.downgrade(config, "20260911_0086")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "20260915_0087"
        )
        assert connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'organization_economics_versions'::regclass"
            )
        ).one() == (True, True)
        assert (
            connection.execute(
                select(legacy_table.c.tax_basis_points).where(
                    legacy_table.c.source_reference == "owner-confirmation"
                )
            ).scalar_one()
            == 750
        )
