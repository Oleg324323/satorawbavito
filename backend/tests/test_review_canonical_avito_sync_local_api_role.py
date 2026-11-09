"""Final API role canonical sync; synthetic single GET, no grants or bearer proof."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings
from app.reviews.canonical_avito_fetch import BoundedAvitoReviewSourceClient
from app.reviews.canonical_avito_sync import (
    AvitoReviewSyncError,
    sync_canonical_avito_page,
)
from tests import test_account_avito_stats_local_api_role as stats_role
from tests import test_account_avito_stats_postgres as stats
from tests import test_notification_preferences_postgres as preferences
from tests.test_review_canonical_avito_fetch import response

cluster = preferences.cluster
database = preferences.database
stats_db = stats_role.stats_db
context = stats.context


def test_final_api_role_canonical_sync_preserves_scope_one_get_and_live_authority(
    database, context
):
    # Reuse encrypted synthetic seed only; the shadow fixture's extra GRANT is
    # deliberately not reused. This role gets exactly the final ops ACL.
    stats.change(
        context,
        'UPDATE iam_memberships SET permissions=\'["reviews:write","reviews:read"]\'::json WHERE user_id=:user',
    )
    assert context.actor.permissions == frozenset()
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    settings = Settings(
        review_shadow_enabled=True,
        review_shadow_account_pairs=((91001, context.account), (91001, 91101)),
    )
    calls = []

    def send(request):
        # Retain only non-secret request metadata, never headers or Request repr.
        calls.append(
            (
                request.method,
                request.url.host,
                request.url.path,
                dict(request.url.params),
            )
        )
        with context.owner.begin() as connection:
            connection.execute(
                text(
                    "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:id FOR UPDATE NOWAIT"
                ),
                {"id": context.account},
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM review_sync_runs_v2 WHERE marketplace_account_id=:id AND status='running'"
                    ),
                    {"id": context.account},
                )
                == 1
            )
        return response()

    def client(resolved):
        assert resolved.binding.owner.marketplace_account_id == context.account
        assert resolved.binding.external_account_id == context.external
        assert (
            resolved.binding.credential_identity.credential_kind == "avito_oauth_access"
        )
        return BoundedAvitoReviewSourceClient(
            resolved, transport=httpx.MockTransport(send)
        )

    def invoke(account):
        return sync_canonical_avito_page(
            database.runtime,
            actor=context.actor,
            settings=settings,
            marketplace_account_id=account,
            offset=0,
            request_id=uuid4(),
            keyring_loader=stats._keyring,
            client_factory=client,
        )

    receipt = invoke(context.account)
    assert receipt.observed_count == 1
    assert calls == [
        ("GET", "api.avito.ru", "/ratings/v1/reviews", {"limit": "50", "offset": "0"})
    ]
    with database.owner.connect() as connection:
        run = connection.execute(
            text(
                "SELECT organization_id,marketplace_account_id,marketplace,status,completeness,observed_count "
                "FROM review_sync_runs_v2 WHERE sync_run_id=:id"
            ),
            {"id": receipt.sync_run_id},
        ).one()
        assert tuple(run) == (91001, context.account, "avito", "partial", "partial", 1)
        assert bytes(
            connection.scalar(
                text(
                    "SELECT text_utf8 FROM review_observations WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            )
        ) == b" exact\x00\xf0\x9f\x98\x80 "
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM review_facts WHERE organization_id=91001 AND marketplace_account_id=:id AND marketplace='avito'"
                ),
                {"id": context.account},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM review_observations WHERE organization_id=91001 AND marketplace_account_id=:id AND marketplace='avito'"
                ),
                {"id": context.account},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM review_sync_run_items WHERE sync_run_id=:id"
                ),
                {"id": receipt.sync_run_id},
            )
            == 1
        )
    with pytest.raises(AvitoReviewSyncError) as denied:
        invoke(91101)
    assert denied.value.__context__ is None and denied.value.__cause__ is None
    stats.change(
        context,
        "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
    )
    with pytest.raises(AvitoReviewSyncError) as revoked:
        invoke(context.account)
    assert revoked.value.__context__ is None and revoked.value.__cause__ is None
    assert len(calls) == 1
    assert "synthetic-stats-only" not in repr((receipt, denied.value, revoked.value))
    with database.owner.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM review_sync_runs_v2")) == 1
        assert connection.scalar(text("SELECT count(*) FROM review_send_commands")) == 0
        assert (
            connection.scalar(text("SELECT count(*) FROM review_send_enqueue_intents"))
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT generation FROM marketplace_account_credentials WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            )
            == 1
        )
