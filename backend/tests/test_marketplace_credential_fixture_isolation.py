"""Credential tests own a bounded SQLite schema, not every imported ORM table."""

from sqlalchemy import Column, Integer, Table, inspect, select
from sqlalchemy.dialects.postgresql import JSONB

from app.cabinet.orm import LkAuditEventRow
from app.infra.models import Base
from app.platform.integrations.credential_store import (
    put_marketplace_credential,
    resolve_marketplace_credential,
)
from tests import test_marketplace_credential_store as store_tests
from tests.test_marketplace_credential_store import CANARY, _wb_owner

store_db = store_tests.store_db
EXPECTED_TABLES = {
    "lk_audit_events",
    "lk_organizations",
    "lk_users",
    "marketplace_account_credentials",
    "marketplace_accounts",
}


def test_store_fixture_supports_credential_round_trip_and_audit(store_db) -> None:
    factory, _keyring = store_db

    put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})

    assert resolve_marketplace_credential(_wb_owner(), "wb_api").reveal() == {"token": CANARY}
    with factory() as session:
        assert session.scalars(select(LkAuditEventRow.action)).all() == [
            "integration.marketplace_credential.put"
        ]


def test_store_fixture_ignores_unrelated_postgresql_only_metadata(request) -> None:
    foreign = Table(
        "credential_fixture_unrelated_pg_only",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("coverage", JSONB),
    )
    try:
        factory, _keyring = request.getfixturevalue("store_db")
        put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})

        assert resolve_marketplace_credential(_wb_owner(), "wb_api").reveal() == {"token": CANARY}
        with factory() as session:
            assert session.scalars(select(LkAuditEventRow.action)).all() == [
                "integration.marketplace_credential.put"
            ]
        assert set(inspect(factory.kw["bind"]).get_table_names()) == EXPECTED_TABLES
        assert foreign.name not in inspect(factory.kw["bind"]).get_table_names()
        assert Base.metadata.tables[foreign.name] is foreign
        assert isinstance(foreign.c.coverage.type, JSONB)
    finally:
        Base.metadata.remove(foreign)
