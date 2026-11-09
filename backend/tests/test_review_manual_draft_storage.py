"""Requires T1 manual-mode SQL amendment and an explicitly allocated PG slot."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.reviews.local_repository import ReviewLocalError
from tests import test_review_local_service as local

cluster = local.cluster
db = local.db
principal = local.principal
actor = local.actor


def test_first_manual_draft_replay_edit_approval_and_competing_first(db, actor):
    local.policy(db, actor)
    review = local.source(db)
    request = local.prepared(db, actor, review, mode="manual", value="  Спасибо!\0 é 😀\n")
    competing = deepcopy(request)
    competing["localCommandId"] = str(uuid4())
    competing["input"]["draftId"] = str(uuid4())
    competing["input"]["generation"]["generationId"] = str(uuid4())
    first = local.execute(db, actor, request)
    assert first["draftRevision"] == first["headVersion"] == 1
    assert local.execute(db, actor, request) == first
    current = local.context(db, actor, review)
    assert current["draft"]["text"] == request["input"]["text"]
    assert current["draft"]["generation"]["mode"] == "manual"
    assert current["decision"] is None
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_CONFLICT"):
        local.execute(db, actor, competing)
    local.execute(db, actor, local.decision(db, actor, review))
    edit = local.prepared(db, actor, review, mode="manual_edit", value="Уточнённый ответ")
    assert local.execute(db, actor, edit)["draftRevision"] == 2
    assert local.context(db, actor, review)["decision"] is None
    assert local.execute(db, actor, request) == first


@pytest.mark.parametrize("change", ["session", "permission", "scope"])
def test_manual_receipt_replay_rechecks_live_authority(db, actor, change):
    local.policy(db, actor)
    review = local.source(db)
    request = local.prepared(db, actor, review, mode="manual", value="Спасибо!")
    local.execute(db, actor, request)
    with db[0].begin() as connection:
        if change == "session":
            connection.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"),
                               {"id": actor.session_id})
        elif change == "permission":
            connection.execute(text("UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"),
                               {"id": actor.user_id})
        else:
            connection.execute(text("UPDATE iam_memberships SET allowed_account_ids='[91102]' WHERE user_id=:id"),
                               {"id": actor.user_id})
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_DENIED"):
        local.execute(db, actor, request)


def test_manual_prepared_source_change_conflicts(db, actor):
    local.policy(db, actor)
    review = local.source(db)
    request = local.prepared(db, actor, review, mode="manual", value="Спасибо!")
    local.source(db, key=review["external_review_id"], body="Changed synthetic source", expected=1)
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_SOURCE_CHANGED"):
        local.execute(db, actor, request)
