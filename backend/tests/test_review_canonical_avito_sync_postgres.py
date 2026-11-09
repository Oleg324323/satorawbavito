"""Closed-root original-caller orchestration. Owned PostgreSQL slot only."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, text

from app.reviews import canonical_avito_sync as service
from app.reviews.canonical_avito_fetch import BoundedAvitoReviewSourceClient
from tests import test_review_avito_shadow_postgres as shadow
from tests.test_review_canonical_avito_fetch import response

cluster, stats_db, context = shadow.cluster, shadow.stats_db, shadow.context


def invoke(context, *, during_http=None, request_id=None, offset=0, loader=None):
    calls = []

    def send(request):
        calls.append(request)
        with context.owner.begin() as connection:
            connection.execute(
                text(
                    "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:id FOR UPDATE NOWAIT"
                ),
                {"id": context.account},
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM review_sync_runs_v2 WHERE marketplace_account_id=:id"
                    ),
                    {"id": context.account},
                ).scalar_one()
                == 1
            )
        return response() if during_http is None else during_http(request)

    receipt = service.sync_canonical_avito_page(
        context.runtime,
        actor=context.actor,
        settings=context.settings,
        marketplace_account_id=context.account,
        offset=offset,
        request_id=request_id or uuid4(),
        keyring_loader=loader or shadow.stats._keyring,
        client_factory=lambda resolved: BoundedAvitoReviewSourceClient(
            resolved, transport=httpx.MockTransport(send)
        ),
    )
    return receipt, calls


def test_reserve_before_one_get_no_root_over_http_publish_and_explicit_replay(context):
    key = uuid4()
    receipt, calls = invoke(context, request_id=key)
    assert receipt.observed_count == 1 and len(calls) == 1
    assert calls[0].url.path == "/ratings/v1/reviews"
    replay, repeated = invoke(context, request_id=key)
    assert replay == receipt and len(repeated) == 1
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_observations WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 1
        )


def test_same_request_id_changed_offset_conflicts_before_second_get(context):
    key = uuid4()
    invoke(context, request_id=key)

    def never(_):
        pytest.fail("checksum conflict performed GET")

    with pytest.raises(service.AvitoReviewSyncError, match="CONFLICT"):
        invoke(context, request_id=key, offset=50, during_http=never)


@pytest.mark.parametrize("change", ["permission", "roundtrip", "replacement"])
def test_changed_after_resolution_before_begin_never_fetches(
    context, monkeypatch, change
):
    original = service.begin_avito_review_shadow

    def changed(*args, **kwargs):
        if change == "permission":
            shadow.stats.change(
                context,
                "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
            )
        elif change == "replacement":
            shadow.stats.put_access(context)
        else:
            with context.owner.begin() as connection:
                shadow.roundtrip_binding(connection, context)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "begin_avito_review_shadow", changed)

    def never(_):
        pytest.fail("changed original authority performed GET")

    with pytest.raises(service.AvitoReviewSyncError):
        invoke(context, during_http=never)
    shadow.assert_no_published_facts(context)


@pytest.mark.parametrize(
    "change", ["permission", "session", "roundtrip", "replacement"]
)
def test_changed_during_http_does_not_publish_or_replace_authority(context, change):
    calls = []

    def during(_):
        calls.append(1)
        if change == "permission":
            shadow.stats.change(
                context,
                "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
            )
        elif change == "session":
            shadow.stats.change(
                context,
                "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
            )
        elif change == "replacement":
            shadow.stats.put_access(context)
        else:
            with context.owner.begin() as connection:
                shadow.roundtrip_binding(connection, context)
        return response()

    with pytest.raises(service.AvitoReviewSyncError) as error:
        invoke(context, during_http=during)
    assert calls == [1] and error.value.__context__ is None
    shadow.assert_no_published_facts(context)


def test_resolution_physical_commit_failure_never_fetches(context):
    commits = []

    def fail(_):
        commits.append(1)
        raise RuntimeError("synthetic-private")

    def never(_):
        pytest.fail("failed resolution commit performed GET")

    event.listen(context.runtime, "commit", fail)
    try:
        with pytest.raises(service.AvitoReviewSyncError) as error:
            invoke(context, during_http=never)
        assert error.value.__context__ is None and commits == [1]
    finally:
        event.remove(context.runtime, "commit", fail)
    shadow.assert_no_published_facts(context)


def test_malformed_response_keeps_running_intent_no_partial_facts(context):
    with pytest.raises(service.AvitoReviewSyncError):
        invoke(
            context,
            during_http=lambda _: response(b'{"reviews":[{"id":"missing-fields"}]}'),
        )
    shadow.assert_no_published_facts(context)
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT status FROM review_sync_runs_v2 WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == "running"
        )


def test_denied_member_never_loads_keyring(context):
    shadow.stats.change(
        context,
        "UPDATE iam_memberships SET permissions='[\"cabinet:read\"]'::json WHERE user_id=:user",
    )

    def never():
        pytest.fail("read-only member loaded keyring")

    with pytest.raises(service.AvitoReviewSyncError):
        invoke(context, loader=never)


def test_incarnation_change_inside_resolution_before_guard_never_fetches(
    context, monkeypatch
):
    original = service._resolve_fetch_in_session

    def changed(*args, **kwargs):
        resolved = original(*args, **kwargs)
        with context.owner.begin() as connection:
            shadow.roundtrip_binding(connection, context)
        return resolved

    monkeypatch.setattr(service, "_resolve_fetch_in_session", changed)

    def never(_):
        pytest.fail("resolution incarnation change reached HTTP")

    with pytest.raises(service.AvitoReviewSyncError):
        invoke(context, during_http=never)
    shadow.assert_no_published_facts(context)
