"""Synthetic owned PostgreSQL; no Review ingestion, answers or provider calls."""

from dataclasses import replace

import pytest
from sqlalchemy import event, text

from app.avito.account_reviews import (
    AccountAvitoReviewsService,
    AccountReviewsError,
    ReviewPreviewRating,
    ReviewPreviewRow,
    ReviewsPreviewPage,
)
from tests import test_account_avito_stats_postgres as stats

cluster, stats_db, context = stats.cluster, stats.stats_db, stats.context


def service(context, *, during=None, keyring_loader=stats._keyring):
    calls = []

    class Client:
        def fetch_preview(self, *, offset):
            with context.owner.begin() as connection:
                connection.execute(
                    text(
                        "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": context.account},
                )
            assert offset == 0
            calls.append(1)
            if during:
                during()
            return ReviewsPreviewPage(
                context.external,
                ReviewPreviewRating(None, None, None, None),
                None,
                (
                    ReviewPreviewRow(
                        "0001", None, None, None, False, None, None, None, None
                    ),
                ),
            )

    def factory(credential):
        assert credential.binding.owner.marketplace_account_id == context.account
        return Client()

    return AccountAvitoReviewsService(
        engine=context.runtime,
        keyring_loader=keyring_loader,
        client_factory=factory,
        enabled_for=lambda org, account: True,
    ), calls


def preview(instance, context, **changes):
    return instance.preview(
        changes.pop("actor", context.actor),
        marketplace_account_id=changes.pop("account", context.account),
        offset=0,
    )


def test_closed_roots_return_partial_metadata_with_original_scope(context):
    instance, calls = service(context)
    statements = []

    def record(connection, cursor, statement, parameters, execution, many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(context.runtime, "before_cursor_execute", record)
    try:
        value = preview(instance, context)
    finally:
        event.remove(context.runtime, "before_cursor_execute", record)
    assert statements and not set(statements) & {
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "MERGE",
    }
    assert calls == [1]
    assert value == {
        "marketplaceAccountId": context.account,
        "provider": "avito",
        "externalAccountId": context.external,
        "offset": "0",
        "limit": 50,
        "coverageState": "partial",
        "total": None,
        "rating": {
            "isEnabled": None,
            "score": None,
            "reviewsCount": None,
            "reviewsWithScoreCount": None,
        },
        "rows": [
            {
                "reviewId": "0001",
                "score": None,
                "stage": None,
                "usedInScore": None,
                "canAnswer": False,
                "createdAt": None,
                "itemId": None,
                "answerId": None,
                "answerStatus": None,
                "accountEvidence": "credential_scope",
            }
        ],
    }


def test_default_disabled_never_resolves_credentials(context):
    instance = AccountAvitoReviewsService(
        engine=context.runtime,
        keyring_loader=lambda: pytest.fail("key loaded"),
        client_factory=lambda credential: pytest.fail("HTTP made"),
    )
    with pytest.raises(AccountReviewsError, match="DISABLED"):
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
            raise RuntimeError("synthetic-private")
        return stats._keyring()

    if case in statements:
        stats.change(context, statements[case])
    instance, calls = service(context, keyring_loader=loader)
    with pytest.raises(AccountReviewsError) as error:
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
    with pytest.raises(AccountReviewsError) as error:
        preview(instance, context)
    assert calls == [1] and error.value.__context__ is None


@pytest.mark.parametrize("number", [1, 2])
def test_either_commit_failure_prevents_response(context, number):
    calls = []

    def commit(connection):
        calls.append(1)
        if len(calls) == number:
            raise RuntimeError("synthetic-private")

    event.listen(context.runtime, "commit", commit)
    try:
        instance, provider = service(context)
        with pytest.raises(AccountReviewsError):
            preview(instance, context)
        assert len(provider) == number - 1
    finally:
        event.remove(context.runtime, "commit", commit)


@pytest.mark.parametrize("case", ["org", "account"])
def test_cross_scope_cannot_reuse_read_authority(context, case):
    instance, calls = service(context)
    with pytest.raises(AccountReviewsError):
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
