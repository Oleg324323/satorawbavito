"""Final API role + synthetic encrypted access credential, no HTTP/provider proof.

Reuses the final three-role allocator and the existing encrypted stats context
and fake client. No additional grants, refresh, credential mutation or dispatch.
"""

import pytest
from sqlalchemy import event, text

from app.avito.account_stats import AccountStatsError
from tests import test_account_avito_stats_postgres as stats
from tests import test_notification_preferences_postgres as preferences

cluster = preferences.cluster
database = preferences.database
context = stats.context
TOTALS = {"impressions": 100, "views": 0, "contactsMessenger": 2, "contacts": 3,
          "contactsShowPhone": 1, "contactsShowPhoneAndMessenger": 0, "favorites": 4,
          "spendKopecks": 12500, "orders": 1, "buyouts": 0}


@pytest.fixture
def stats_db(database):
    # Only the parent rows required by the reused stats context and denial case.
    with database.owner.begin() as connection:
        connection.execute(text("INSERT INTO lk_organizations(organization_id,slug,name) "
                                "VALUES(91001,'stats-api-role','Synthetic')"))
        connection.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,"
            "marketplace,external_account_id,status) VALUES(91101,91001,'avito','900001','connected')"))
    return database.owner, database.runtime


def test_final_api_role_reads_encrypted_account_stats_and_denies_other_account_without_writes(database, context):
    assert context.actor.permissions == frozenset()
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    instance, calls = stats.service(context)
    base_factory = instance._client_factory

    def complete_totals_factory(resolved):
        # The existing fake client proves roots are closed using owner NOWAIT,
        # and validates exact account/grouping. Supply all metrics explicitly.
        assert resolved.binding.credential_identity.credential_kind == "avito_oauth_access"
        client = base_factory(resolved)

        class CompleteTotalsClient:
            def fetch_stats(self, request):
                result = client.fetch_stats(request)
                return result.model_copy(update={"items": [result.items[0].model_copy(update=TOTALS)]})

        return CompleteTotalsClient()

    instance._client_factory = complete_totals_factory
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(database.runtime, "before_cursor_execute", capture)
    try:
        result = stats.fetch(instance, context)
        assert result["marketplaceAccountId"] == context.account
        assert result["externalAccountId"] == context.external
        assert result["provider"] == "avito" and result["status"] == "synced"
        assert result["rows"][0]["metrics"] == {key: str(value) for key, value in TOTALS.items()}
        assert result["daily"] == [] and len(calls) == 1
        with pytest.raises(AccountStatsError) as denied:
            stats.fetch(instance, context, account=91101)
        assert denied.value.code == "AVITO_ACCOUNT_STATS_ACCESS_DENIED"
        assert denied.value.__context__ is None and len(calls) == 1
    finally:
        event.remove(database.runtime, "before_cursor_execute", capture)
    # Locks are allowed; no credential refresh/write or producer mutation runs.
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE", "MERGE")) for sql in statements)
    assert "synthetic-stats-only" not in str(result)
    with database.owner.connect() as connection:
        row = connection.execute(text("SELECT generation,credential_kind FROM marketplace_account_credentials "
            "WHERE organization_id=91001 AND marketplace_account_id=:account"),
            {"account": context.account}).one()
    assert tuple(row) == (1, "avito_oauth_access")
