"""Immutable workflow pagination on actual0071; no provider, source or receipt writes."""

import pytest
from sqlalchemy import event, text

from app.reviews.local_history import read_local_review_history
from app.reviews.local_repository import ReviewLocalError
from tests import test_review_local_service as local

cluster = local.cluster
db = local.db
principal = local.principal
actor = local.actor


def history(db, actor, review, **values):
    return read_local_review_history(db[1], actor=actor, settings=local.SETTINGS,
        marketplace_account_id=91101, marketplace="avito", review_id=review["review_id"], **values)


def setup(db, actor):
    local.policy(db, actor)
    review = local.source(db)
    local.execute(db, actor, local.prepared(db, actor, review))
    local.execute(db, actor, local.decision(db, actor, review))
    local.execute(db, actor, local.prepared(db, actor, review, mode="manual_edit", value="Private\0unexposed"))
    return review


def test_history_bound_does_not_move_when_new_actions_arrive(db, actor):
    review = setup(db, actor)
    first = history(db, actor, review, limit=1)
    assert first["throughVersion"] == 3 and first["nextAfterVersion"] == 1
    assert first["events"][0]["eventKind"] == "draft.published"
    local.execute(db, actor, local.decision(db, actor, review, kind="rejected"))
    second = history(db, actor, review, limit=1, head_id=first["headId"], through_version=3, after_version=1)
    final = history(db, actor, review, limit=1, head_id=first["headId"], through_version=3, after_version=2)
    assert second["throughVersion"] == final["throughVersion"] == 3
    assert second["events"][0]["eventKind"] == "decision.approved"
    assert final["events"][0]["eventKind"] == "draft.published" and final["nextAfterVersion"] is None
    assert history(db, actor, review, limit=1) == dict(first, throughVersion=4)
    assert history(db, actor, review, head_id=first["headId"], through_version=3, after_version=3)["events"] == []


def test_history_read_only_permission_no_domain_writes_or_private_payload(db, actor):
    review = setup(db, actor)
    with db[0].begin() as c:
        c.execute(text("UPDATE iam_memberships SET permissions='[\"reviews:read\"]' WHERE user_id=:id"), {"id": actor.user_id})
    statements = []
    def capture(c, cursor, sql, *args):
        statements.append(sql.upper().strip())
    event.listen(db[1], "before_cursor_execute", capture)
    try:
        page = history(db, actor, review)
    finally:
        event.remove(db[1], "before_cursor_execute", capture)
    assert len(page["events"]) == 3
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert not any("TEXT_UTF8" in sql or "REQUEST_PAYLOAD" in sql for sql in statements)
    assert "Private" not in str(page) and "credential" not in str(page)


@pytest.mark.parametrize("change", ["scope", "permission", "session", "binding"])
def test_history_revalidates_each_page(db, actor, change):
    review = setup(db, actor)
    first = history(db, actor, review, limit=1)
    with db[0].begin() as c:
        if change == "scope":
            c.execute(text("UPDATE iam_memberships SET allowed_account_ids='[91102]' WHERE user_id=:id"), {"id": actor.user_id})
        elif change == "permission":
            c.execute(text("UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"), {"id": actor.user_id})
        elif change == "session":
            c.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"), {"id": actor.session_id})
        else:
            c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='rebound-history' WHERE marketplace_account_id=91101")
    try:
        for after in (1, 3):
            # Even an otherwise empty terminal page must not bypass the binding fence.
            with pytest.raises(ReviewLocalError):
                history(db, actor, review, head_id=first["headId"], through_version=3, after_version=after)
    finally:
        if change == "binding":
            with db[0].begin() as c:
                c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101")


def test_empty_head_is_empty_not_fake_draft_history(db, actor):
    review = local.source(db)
    page = history(db, actor, review)
    assert (page["headId"], page["throughVersion"], page["events"], page["nextAfterVersion"]) == (None, 0, [], None)


@pytest.mark.parametrize("values", [{"after_version": 1}, {"through_version": 1}, {"limit": 0},
    {"limit": 201}, {"limit": True}, {"after_version": -1}, {"head_id": "invalid", "through_version": 1}])
def test_invalid_history_query_not_unbounded_or_silent_coercion(db, actor, values):
    review = local.source(db)
    with pytest.raises(ReviewLocalError):
        history(db, actor, review, **values)


def test_http_history_is_lossless_safe_and_fixed_bound(db, actor):
    review = setup(db, actor)
    with local.client(db, actor) as http:
        response = http.get("/api/v2/reviews/local/history", params={"marketplace_account_id": 91101,
            "marketplace": "avito", "review_id": review["review_id"], "limit": 1})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["throughVersion"] == "3" and value["events"][0]["aggregateVersion"] == "1"
        assert value["nextAfterVersion"] == "1" and response.headers["cache-control"] == "no-store"
        assert "Private" not in response.text and "credential" not in response.text
