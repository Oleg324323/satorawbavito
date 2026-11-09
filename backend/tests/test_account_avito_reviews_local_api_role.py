"""Final API role metadata preview; synthetic encrypted seed, never Review writes."""

import pytest
from sqlalchemy import event, text

from app.avito.account_reviews import AccountReviewsError
from tests import test_account_avito_reviews_postgres as reviews
from tests import test_account_avito_stats_local_api_role as stats_role
from tests import test_account_avito_stats_postgres as stats
from tests import test_notification_preferences_postgres as preferences

cluster = preferences.cluster
database = preferences.database
stats_db = stats_role.stats_db
context = stats.context


def test_final_api_role_reviews_metadata_requires_current_authority_without_mutation(
    database, context
):
    # A real current DB membership supplies permission, not this actor snapshot.
    assert context.actor.permissions == frozenset()
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    instance, calls = reviews.service(context)
    factory = instance._client_factory

    def verified_factory(resolved):
        identity = resolved.binding.credential_identity
        assert (
            identity.credential_kind == "avito_oauth_access"
            and identity.generation == 1
        )
        assert resolved.binding.external_account_id == context.external
        return factory(resolved)

    instance._client_factory = verified_factory
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(database.runtime, "before_cursor_execute", capture)
    try:
        value = reviews.preview(instance, context)
        # Exact metadata-only shape excludes review/answer text and AI/send authority.
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
        assert calls == [1]
        with pytest.raises(AccountReviewsError) as denied:
            reviews.preview(instance, context, account=91101)
        assert denied.value.code == "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"
        assert denied.value.__context__ is None and calls == [1]
        stats.change(
            context,
            "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
        )
        with pytest.raises(AccountReviewsError) as revoked:
            reviews.preview(instance, context)
        assert revoked.value.code == "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"
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
