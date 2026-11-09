"""Final API role local Review lifecycle, not bearer HTTP or provider proof.

Uses existing disposable final-grants fixture. Owner only seeds synthetic policy
and source. No added grants, permission cache bypass, or existing application DB.
Requires migration 0083 and T1's local Review grants; run with allocated PG slot.
"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.reviews.local_repository import ReviewLocalError
from app.reviews.local_service import read_local_review_context
from tests import test_notification_preferences_postgres as preferences
from tests import test_review_local_history as history
from tests import test_review_local_service as local
from tests import test_review_local_storage_schema as storage

cluster = preferences.cluster
database = preferences.database
principal = local.principal
actor = local.actor


@pytest.fixture
def db(database):
    with database.owner.begin() as connection:
        storage.seed(connection)
    return database.owner, database.owner


def test_final_api_role_manual_edit_approve_history_replay_and_live_denial(database, db, actor):
    # Existing fixtures grant the real DB member reviews:read/write/approve.
    # The ActorContext permissions cache remains empty throughout this proof.
    assert actor.permissions == frozenset()
    local.policy(db, actor)
    review = local.source(db)
    api = database.owner, database.runtime
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    with database.owner.connect() as connection:
        denied = connection.execute(text(
            "SELECT has_table_privilege(:r,'review_send_commands','INSERT'),"
            "has_table_privilege(:r,'review_send_enqueue_intents','INSERT'),"
            "has_table_privilege(:r,'review_send_command_authorities','INSERT'),"
            "has_table_privilege(:r,'review_send_attempts','INSERT')"
        ), {"r": database.roles[0]}).one()
    assert tuple(denied) == (False, False, False, False)

    # Every helper opens a fresh service Session on the API engine.
    initial = local.context(api, actor, review)
    assert initial["policy"] is not None
    assert initial["draft"] is initial["workflowHead"] is None
    manual = local.prepared(api, actor, review, mode="manual", value="  Спасибо!\0 é 😀\n")
    first = local.execute(api, actor, manual)
    assert first["draftRevision"] == first["headVersion"] == 1
    saved = local.context(api, actor, review)
    assert saved["draft"]["text"] == manual["input"]["text"]
    assert saved["draft"]["generation"]["mode"] == "manual"
    assert saved["decision"] is None
    edit = local.prepared(api, actor, review, mode="manual_edit", value="Уточнённый ручной ответ")
    second = local.execute(api, actor, edit)
    assert second["draftRevision"] == second["headVersion"] == 2
    approval = local.decision(api, actor, review)
    approved = local.execute(api, actor, approval)
    assert approved["headVersion"] == 3
    current = local.context(api, actor, review)
    assert current["decision"]["decisionKind"] == "approved"
    assert current["decision"]["draftId"] == second["draftId"]
    page = history.history(api, actor, review)
    assert page["throughVersion"] == 3
    assert [event["eventKind"] for event in page["events"]] == [
        "draft.published", "draft.published", "decision.approved"]
    assert local.execute(api, actor, manual) == first
    assert history.history(api, actor, review) == page

    # Independent owner readback proves immutable audit/receipts survived commit.
    with database.owner.connect() as connection:
        params = {"rid": UUID(review["review_id"])}
        assert connection.scalar(text(
            "SELECT count(*) FROM review_draft_revisions WHERE review_id=:rid"), params) == 2
        assert connection.scalar(text(
            "SELECT count(*) FROM review_local_command_receipts WHERE review_id=:rid"), params) == 3
        assert connection.scalar(text(
            "SELECT count(*) FROM review_local_audit WHERE review_id=:rid"), params) == 3

    with pytest.raises(ReviewLocalError, match="^REVIEW_LOCAL_DENIED$"):
        read_local_review_context(database.runtime, actor=actor, settings=local.SETTINGS,
            marketplace_account_id=91102, marketplace="avito")
    with database.owner.begin() as connection:
        connection.execute(text("UPDATE iam_memberships SET is_active=false WHERE user_id=:u"),
                           {"u": actor.user_id})
    for action in (lambda: local.context(api, actor, review),
                   lambda: local.execute(api, actor, manual),
                   lambda: history.history(api, actor, review)):
        with pytest.raises(ReviewLocalError, match="^REVIEW_LOCAL_DENIED$"):
            action()
