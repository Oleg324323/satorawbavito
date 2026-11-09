"""Owned PG only: read-only preview under fresh actual cabinet authority."""

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import event, text

from app.avito.account_orders import (
    AccountAvitoOrderStatusService,
    AccountOrderStatusError,
    OrderStatusPreviewPage,
    OrderStatusPreviewRow,
)
from tests import test_account_avito_stats_postgres as stats

cluster, stats_db, context = stats.cluster, stats.stats_db, stats.context


def service(context, *, during=None, keyring_loader=stats._keyring):
    calls = []

    class Client:
        def fetch_preview(self, *, date_from, page):
            with context.owner.begin() as connection:
                connection.execute(
                    text(
                        "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": context.account},
                )
            assert date_from == date(2026, 9, 1) and page == 1
            calls.append(1)
            if during:
                during()
            return OrderStatusPreviewPage(
                context.external,
                None,
                (
                    OrderStatusPreviewRow(
                        "000-order",
                        "ready_to_ship",
                        "ready_for_fulfillment",
                        "mapped",
                        "avito-order-status-v1",
                        None,
                        None,
                        "credential_scope",
                    ),
                ),
            )

    def client_factory(credential):
        assert credential.binding.owner.marketplace_account_id == context.account
        return Client()

    return AccountAvitoOrderStatusService(
        engine=context.runtime,
        keyring_loader=keyring_loader,
        client_factory=client_factory,
        enabled_for=lambda org, account: True,
    ), calls


def preview(instance, context, **changes):
    return instance.preview(
        changes.pop("actor", context.actor),
        marketplace_account_id=changes.pop("account", context.account),
        date_from=date(2026, 9, 1),
        page=1,
    )


def test_closed_roots_safe_partial_preview_without_canonical_writes(context):
    instance, calls = service(context)
    value = preview(instance, context)
    assert calls == [1] and value["marketplaceAccountId"] == context.account
    assert value["coverageState"] == "partial" and value["hasMore"] is None
    assert value["rows"][0]["rawStatus"] == "ready_to_ship"
    assert "total" not in value and "priceKopecks" not in str(value)
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM marketplace_orders WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 0
        )


def test_default_disabled_never_resolves_credentials(context):
    instance = AccountAvitoOrderStatusService(
        engine=context.runtime,
        keyring_loader=lambda: pytest.fail("key loaded"),
        client_factory=lambda credential: pytest.fail("provider made"),
    )
    with pytest.raises(AccountOrderStatusError, match="DISABLED"):
        preview(instance, context)


@pytest.mark.parametrize("case", ["session", "scope", "missing", "key"])
def test_initial_denial_without_provider_or_fallback(context, case):
    statements = {
        "session": "UPDATE lk_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=:login",
        "scope": "UPDATE iam_memberships SET allowed_account_ids='[]'::json WHERE user_id=:user",
        "missing": "DELETE FROM marketplace_account_credentials WHERE marketplace_account_id=:account",
    }

    def loader():
        if case == "key":
            raise RuntimeError("synthetic-key-error")
        return stats._keyring()

    if case in statements:
        stats.change(context, statements[case])
    instance, calls = service(context, keyring_loader=loader)
    with pytest.raises(AccountOrderStatusError) as error:
        preview(instance, context)
    assert calls == [] and error.value.__context__ is None


@pytest.mark.parametrize(
    "case", ["replacement", "incarnation", "permission", "session", "binding"]
)
def test_changed_authority_during_fetch_discards_preview(context, case):
    statements = {
        "incarnation": "UPDATE marketplace_accounts SET ingestion_binding_version=ingestion_binding_version+1 WHERE marketplace_account_id=:account",
        "permission": "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
        "session": "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
        "binding": "UPDATE marketplace_accounts SET external_account_id=external_account_id||'1' WHERE marketplace_account_id=:account",
    }

    def change():
        if case == "replacement":
            stats.put_access(context)
        else:
            stats.change(context, statements[case])

    instance, calls = service(context, during=change)
    with pytest.raises(AccountOrderStatusError) as error:
        preview(instance, context)
    assert calls == [1] and error.value.__context__ is None


@pytest.mark.parametrize("number", [1, 2])
def test_either_commit_failure_prevents_response(context, number):
    calls = []

    def commit(connection):
        calls.append(1)
        if len(calls) == number:
            raise RuntimeError("synthetic-commit-error")

    event.listen(context.runtime, "commit", commit)
    try:
        instance, provider = service(context)
        with pytest.raises(AccountOrderStatusError):
            preview(instance, context)
        assert len(provider) == number - 1
    finally:
        event.remove(context.runtime, "commit", commit)


@pytest.mark.parametrize("case", ["org", "account"])
def test_cross_scope_cannot_reuse_read_authority(context, case):
    instance, calls = service(context)
    with pytest.raises(AccountOrderStatusError):
        preview(
            instance,
            context,
            **(
                {"actor": replace(context.actor, organization_id=91002)}
                if case == "org"
                else {"account": 91101}
            ),
        )
    assert calls == []
