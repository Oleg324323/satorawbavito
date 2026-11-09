"""Final API role historical override reads, never live price or writer proof.

Existing disposable three-role grants fixture owns cleanup. Owner-only setup
creates revisions and removes the offer mapping; read API needs only the two
override SELECT grants, with existing session/account guard rights.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.wb_repricing_override_service import OverrideScope, SkuOverrideService
from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_notification_preferences_postgres as preferences
from tests import test_sku_override_schema as schema
from tests import test_wb_override_service as overrides

cluster = preferences.cluster
database = preferences.database
context = overrides.context


@pytest.fixture
def db(database):
    with database.owner.begin() as connection:
        schema.seed(connection)
    return database.owner, database.owner


def test_final_api_role_reads_unmapped_history_but_denies_replace_and_other_account(database, context):
    owner, _, principal, change = context
    binding = overrides.auth.binding()
    writer = SkuOverrideService(owner, max_request_bytes=8192)
    first = writer.replace(change, principal, binding, now=datetime(2026, 9, 9, 12, tzinfo=UTC))
    second_change = replace(change, command_id=str(uuid4()), expected_version=1,
                            values=replace(change.values, p_min_kopecks=2**80))
    second = writer.replace(second_change, principal, binding, now=datetime(2026, 9, 9, 13, tzinfo=UTC))
    with owner.begin() as connection:
        connection.execute(text("UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=:sku"),
                           {"sku": change.catalog_sku_id})
        connection.execute(text(
            "UPDATE iam_memberships SET role='custom',permissions='[\"settings:read\"]',"
            "scope_mode='selected',allowed_account_ids='[42]' WHERE membership_id=77"
        ))
        for table in (schema.V, schema.H):
            rights = connection.execute(text(
                "SELECT has_table_privilege(:r,:t,'SELECT'),has_table_privilege(:r,:t,'INSERT'),"
                "has_any_column_privilege(:r,:t,'UPDATE')"
            ), {"r": database.roles[0], "t": table}).one()
            assert tuple(rights) == (True, False, False)
        audit_rights = connection.execute(text(
            "SELECT has_table_privilege(:r,:t,'SELECT'),has_table_privilege(:r,:t,'INSERT'),"
            "has_any_column_privilege(:r,:t,'UPDATE')"
        ), {"r": database.roles[0], "t": schema.A}).one()
        assert tuple(audit_rights) == (False, False, False)
        for signature in schema.PURE:
            assert connection.scalar(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"),
                                     {"r": database.roles[0], "f": signature}) is False
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    service = SkuOverrideService(database.runtime, max_request_bytes=8192)
    scope = OverrideScope(7, 42, change.catalog_sku_id)
    assert service.get_current(scope, principal, binding) == second
    assert service.history(scope, principal, binding, limit=2) == (second, first)
    assert service.history(scope, principal, binding, limit=1, before_revision=2) == (first,)
    with pytest.raises(PublicationGuardError):
        service.replace(replace(second_change, command_id=str(uuid4()), expected_version=2),
                        principal, binding, now=datetime(2026, 9, 9, 14, tzinfo=UTC))
    # No-op writes still require table privileges; these cannot alter source rows.
    with pytest.raises(DBAPIError) as update_denied, database.runtime.begin() as connection:
        connection.execute(text("UPDATE wb_repricing_sku_override_heads SET version=version WHERE false"))
    assert update_denied.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as insert_denied, database.runtime.begin() as connection:
        connection.execute(text("INSERT INTO wb_repricing_sku_override_versions SELECT * FROM wb_repricing_sku_override_versions WHERE false"))
    assert insert_denied.value.orig.sqlstate == "42501"
    other_scope = OverrideScope(7, 43, change.catalog_sku_id)
    other_binding = replace(binding, marketplace_account_id=43, external_account_id="synthetic-43")
    with pytest.raises(PublicationGuardError):
        service.get_current(other_scope, principal, other_binding)
    with pytest.raises(PublicationGuardError):
        service.history(other_scope, principal, other_binding, limit=2)
    assert service.get_current(scope, principal, binding) == second
    with owner.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM wb_repricing_sku_override_audit WHERE catalog_sku_id=:sku"),
                                 {"sku": change.catalog_sku_id}) == 2
