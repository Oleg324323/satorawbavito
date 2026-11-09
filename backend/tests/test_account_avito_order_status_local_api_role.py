"""Final API role preview: synthetic encrypted seed/client, not bearer proof."""

import pytest
from sqlalchemy import event, text

from app.avito.account_orders import AccountOrderStatusError
from tests import test_account_avito_order_status_postgres as orders
from tests import test_account_avito_stats_local_api_role as stats_role
from tests import test_account_avito_stats_postgres as stats
from tests import test_notification_preferences_postgres as preferences

cluster = preferences.cluster
database = preferences.database
stats_db = stats_role.stats_db
context = stats.context


def test_final_api_role_previews_with_current_encrypted_authority_without_mutation(
    database, context
):
    assert context.actor.permissions == frozenset()
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    instance, calls = orders.service(context)
    factory = instance._client_factory

    def verified_factory(resolved):
        identity = resolved.binding.credential_identity
        assert identity.credential_kind == "avito_oauth_access"
        assert identity.generation == 1
        assert resolved.binding.external_account_id == context.external
        return factory(resolved)

    instance._client_factory = verified_factory
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(database.runtime, "before_cursor_execute", capture)
    try:
        value = orders.preview(instance, context)
        assert value == {
            "marketplaceAccountId": context.account,
            "provider": "avito",
            "externalAccountId": context.external,
            "dateFrom": "2026-09-01",
            "page": "1",
            "limit": 20,
            "coverageState": "partial",
            "hasMore": None,
            "rows": [
                {
                    "orderId": "000-order",
                    "rawStatus": "ready_to_ship",
                    "canonicalStatus": "ready_for_fulfillment",
                    "mappingState": "mapped",
                    "mappingVersion": "avito-order-status-v1",
                    "createdAt": None,
                    "updatedAt": None,
                    "accountEvidence": "credential_scope",
                }
            ],
        }
        assert calls == [1]
        with pytest.raises(AccountOrderStatusError) as denied:
            orders.preview(instance, context, account=91101)
        assert denied.value.code == "AVITO_ORDER_STATUS_ACCESS_DENIED"
        assert denied.value.__context__ is None and calls == [1]
        # The HTTP actor remains identical; fresh DB authority must still win.
        stats.change(
            context,
            "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
        )
        with pytest.raises(AccountOrderStatusError) as revoked:
            orders.preview(instance, context)
        assert revoked.value.code == "AVITO_ORDER_STATUS_ACCESS_DENIED"
        assert revoked.value.__context__ is None and calls == [1]
    finally:
        event.remove(database.runtime, "before_cursor_execute", capture)
    assert not any(
        sql.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE", "MERGE"))
        for sql in statements
    )
    assert "synthetic-stats-only" not in str(value)
    with database.owner.connect() as connection:
        credential = connection.execute(
            text(
                "SELECT generation,credential_kind FROM marketplace_account_credentials "
                "WHERE organization_id=91001 AND marketplace_account_id=:account"
            ),
            {"account": context.account},
        ).one()
        assert tuple(credential) == (1, "avito_oauth_access")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM marketplace_orders WHERE marketplace_account_id=:account"
                ),
                {"account": context.account},
            )
            == 0
        )
