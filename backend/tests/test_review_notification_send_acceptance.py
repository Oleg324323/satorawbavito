"""Essential0074 acceptance: real services, runtime role and physical roots.

Reuse the existing local-service fixtures (upgrade to actual head), not a second
schema/bootstrap. All rows are synthetic. No provider, crypto or worker dispatch
is exercised: create/cancel and recipient writes are the bounded service paths.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.notification_service import NotificationServiceError, ReviewNotificationService
from app.platform.integrations.credential_store import make_executor_credential_resolver
from app.platform.integrations.review_job_authority import (
    ReviewJobCommands, ReviewJobExecutor, ReviewReconciliation,
)
from app.platform.integrations.review_job_contract import (
    ReviewAuthorityPolicy, ReviewExpectedState, ReviewJobError, ReviewJobLocator, ReviewSendIntent,
)
from app.platform.integrations.worker_identity import ExecutorRoleIdentity
from app.review_notifications_http import _actor, _wire, make_review_notifications_router
from app.reviews.local_repository import ReviewLocalError
from app.reviews.local_service import execute_local_review_with_notifications
from app.reviews.send_service import ReviewSendService
from app.reviews.storage_payloads import encode_review_send
from tests import test_review_local_service as local

cluster = local.cluster
db = local.db
principal = local.principal
actor = local.actor
ALLOWLIST = frozenset({(91001, 91101, "avito"), (91001, 91102, "avito"), (91002, 91201, "wb")})


def publish(db, actor, request):
    return execute_local_review_with_notifications(
        db[1], actor=actor, settings=local.SETTINGS, request=request,
    )


@pytest.fixture
def prepared_notification(db, actor):
    local.policy(db, actor)
    review = local.source(db)
    request = local.prepared(db, actor, review)
    publish(db, actor, request)
    with db[0].connect() as c:
        identifier = c.execute(text("SELECT event_id FROM notification_in_app_events "
            "WHERE organization_id=91001 AND marketplace_account_id=91101 AND source_review_id=:id"),
            {"id": UUID(review["review_id"])}).scalar_one()
    return SimpleNamespace(review=review, request=request, event_id=str(identifier))


@pytest.fixture
def notifications(db):
    return ReviewNotificationService(engine=db[1], allowlist=ALLOWLIST)


def visible(service, actor, ids):
    return service.read_visible(authenticated_actor=actor, marketplace_account_id=91101,
        marketplace="avito", event_ids=ids)


def mark(service, actor, ids, action="read"):
    return service.mark_visible(authenticated_actor=actor, marketplace_account_id=91101,
        marketplace="avito", event_ids=ids, action=action)


def receipt_rows(db, event_id):
    with db[0].connect() as c:
        return c.execute(text("SELECT recipient_membership_id,read_at,dismissed_at,version "
            "FROM notification_in_app_receipts WHERE organization_id=91001 AND "
            "marketplace_account_id=91101 AND event_id=:id ORDER BY recipient_membership_id"),
            {"id": UUID(event_id)}).all()


def fail_insert(table, *, ordinal=1):
    seen = 0

    def fail(c, cursor, statement, parameters, context, executemany):
        nonlocal seen
        compiled = getattr(context, "compiled", None)
        target = getattr(getattr(compiled, "statement", None), "table", None)
        if getattr(target, "name", None) == table and statement.lstrip().upper().startswith("INSERT"):
            seen += 1
            if seen == ordinal:
                raise OperationalError("synthetic-private-sql", {}, Exception("synthetic-private-diagnostic"))

    return fail


def test_notification_failure_rolls_back_source_draft_and_replay_does_not_duplicate_event(db, actor):
    """Breaking source+event atomicity would leave a draft after the failed insert."""
    local.policy(db, actor)
    review = local.source(db)
    request = local.prepared(db, actor, review)
    fail = fail_insert("notification_in_app_events")
    event.listen(db[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(ReviewLocalError, match="^REVIEW_LOCAL_STORAGE_UNAVAILABLE$"):
            publish(db, actor, request)
    finally:
        event.remove(db[1], "before_cursor_execute", fail)
    assert local.context(db, actor, review)["draft"] is None
    with db[0].connect() as c:
        assert c.execute(text("SELECT count(*) FROM notification_in_app_events WHERE source_review_id=:id"),
            {"id": UUID(review["review_id"])}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM review_local_command_receipts WHERE local_command_id=:id"),
            {"id": UUID(request["localCommandId"])}).scalar_one() == 0
    first = publish(db, actor, request)
    assert publish(db, actor, request).canonical_bytes == first.canonical_bytes
    with db[0].connect() as c:
        assert c.execute(text("SELECT count(*) FROM notification_in_app_events WHERE source_review_id=:id"),
            {"id": UUID(review["review_id"])}).scalar_one() == 1


@pytest.mark.parametrize("denial", ["other_org", "other_account", "revoked_login", "unselected_account"])
def test_recipient_reads_recheck_live_owner_and_access(db, actor, notifications, prepared_notification, denial):
    """Removing live authorization/account filters must expose the seeded event."""
    ids = [prepared_notification.event_id]
    assert visible(notifications, actor, ids)["items"][0]["receipt"] is None
    account, marketplace = 91101, "avito"
    if denial == "other_org":
        actor, account, marketplace = replace(actor, organization_id=91002), 91201, "wb"
    elif denial == "other_account":
        account = 91102
    else:
        with db[0].begin() as c:
            if denial == "revoked_login":
                c.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"),
                    {"id": actor.session_id})
            else:
                c.execute(text("UPDATE iam_memberships SET allowed_account_ids='[91102]' WHERE user_id=:id"),
                    {"id": actor.user_id})
    with pytest.raises(NotificationServiceError, match="^NOTIFICATION_DENIED$"):
        notifications.read_visible(authenticated_actor=actor, marketplace_account_id=account,
            marketplace=marketplace, event_ids=ids)
    assert receipt_rows(db, ids[0]) == []


def test_receipt_is_owned_by_live_member_and_http_cannot_choose_another_recipient(
    db, actor, notifications, prepared_notification,
):
    """A recipient body override must not write a different membership receipt."""
    identifier = prepared_notification.event_id
    app = FastAPI()
    app.include_router(make_review_notifications_router(
        service_dependency=lambda: notifications, max_request_bytes=4096, max_visible_ids=8,
    ))
    app.dependency_overrides[_actor] = lambda: actor
    body = {"schemaVersion": "review-notification-action-v1", "organizationId": 91001,
        "marketplaceAccountId": 91101, "marketplace": "avito", "eventIds": [identifier], "action": "read"}
    with TestClient(app) as http:
        denied = http.post("/api/v2/reviews/notifications/receipts", json={**body, "recipientMembershipId": 78})
        assert denied.status_code == 400
        assert receipt_rows(db, identifier) == []
        accepted = http.post("/api/v2/reviews/notifications/receipts", json=body)
        assert accepted.status_code == 200
        member = local.context(db, actor)["actorMembershipId"]
        assert accepted.json()["recipientMembershipId"] == member
        assert accepted.json()["items"][0]["value"]["recipientMembershipId"] == member
        assert accepted.json()["items"][0]["version"] == "1"
        assert accepted.headers["cache-control"] == "no-store"
    assert [row[0] for row in receipt_rows(db, identifier)] == [member]
    # Another real current member may see the event, never this user's receipt.
    # local78/member78 already belongs to the existing isolated fixture.
    login_id = uuid4().hex
    with db[0].begin() as c:
        c.execute(text("INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) "
            "VALUES(:id,'local78',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"),
            {"id": login_id})
    other = ActorContext("local78", "local78", 91001, "admin", frozenset(), session_id=login_id)
    assert visible(notifications, other, [identifier])["items"][0]["receipt"] is None
    assert mark(notifications, other, [identifier])["recipientMembershipId"] == 78
    assert [row[0] for row in receipt_rows(db, identifier)] == sorted([member, 78])


def test_second_receipt_failure_rolls_back_first_and_unknown_id_batch_writes_nothing(
    db, actor, notifications, prepared_notification,
):
    """No partial batch may survive a missing ID or later statement failure."""
    other_review = local.source(db)
    publish(db, actor, local.prepared(db, actor, other_review))
    with db[0].connect() as c:
        other = c.execute(text("SELECT event_id FROM notification_in_app_events WHERE source_review_id=:id"),
            {"id": UUID(other_review["review_id"])}).scalar_one()
    ids = [prepared_notification.event_id, str(other)]
    with pytest.raises(NotificationServiceError, match="^NOTIFICATION_NOT_FOUND$"):
        mark(notifications, actor, [ids[0], str(uuid4())])
    fail = fail_insert("notification_in_app_receipts", ordinal=2)
    event.listen(db[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(NotificationServiceError, match="^NOTIFICATION_UNAVAILABLE$"):
            mark(notifications, actor, ids)
    finally:
        event.remove(db[1], "before_cursor_execute", fail)
    assert receipt_rows(db, ids[0]) == receipt_rows(db, ids[1]) == []
    assert len(mark(notifications, actor, ids)["items"]) == 2


def concurrent_roots(engine, actions):
    """Two real Engine-bound roots; synchronize launch, never mock the CAS."""
    barrier, lock, pids = Barrier(2), Lock(), set()

    def observe(connection):
        pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        with lock:
            pids.add(pid)

    def run(action):
        barrier.wait(timeout=10)
        return action()

    event.listen(engine, "begin", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, actions))
    finally:
        event.remove(engine, "begin", observe)
    assert len(pids) == 2, "Concurrency acceptance requires two physical database sessions"
    return results


def test_concurrent_read_and_dismiss_merge_and_exact_replay_preserves_timestamps(
    db, actor, notifications, prepared_notification,
):
    """A whole-row overwrite would lose one independently committed viewing action."""
    ids = [prepared_notification.event_id]
    results = concurrent_roots(db[1], [
        lambda: mark(notifications, actor, ids, "read"),
        lambda: mark(notifications, actor, ids, "dismiss"),
    ])
    assert sorted(item["items"][0]["version"] for item in results) == [1, 2]
    before = receipt_rows(db, ids[0])
    assert len(before) == 1 and before[0][1] is not None and before[0][2] is not None
    assert before[0][3] == 2
    mark(notifications, actor, ids, "read")
    mark(notifications, actor, ids, "dismiss")
    assert receipt_rows(db, ids[0]) == before


def test_http_wire_keeps_golden_bigint_version_exact_without_changing_internal_bytes():
    """Converting to JS numeric JSON would lose the literal >2**53 version."""
    fixture = json.loads((Path(__file__).parent / "fixtures/reviews/send-in-app-storage-v1-golden.json").read_text())
    value = json.loads(fixture["vectors"]["notificationEvent"]["canonicalUtf8Json"])
    wire = json.loads(json.dumps(_wire({"items": [{"event": value}]})))
    assert wire["items"][0]["event"]["sourceVersion"] == "9223372036854775808"
    assert wire["items"][0]["event"]["eventId"] == "00000000-0000-4000-8000-000000000009"
    assert value["sourceVersion"] == 9223372036854775808


@pytest.fixture(scope="module")
def access_metadata(db):
    """Never decrypted: create/cancel requires actual credential metadata only."""
    with db[0].begin() as c:
        c.execute(text("INSERT INTO marketplace_account_credentials "
            "(credential_id,organization_id,marketplace_account_id,provider,credential_kind,payload_schema_version,"
            "algorithm,key_version,aad_version,nonce,ciphertext,generation,expires_at) VALUES "
            "(:id,91001,91101,'avito','avito_oauth_access',1,'AES-256-GCM',1,1,:nonce,:ciphertext,1,"
            "clock_timestamp()+interval '2 hours')"),
            {"id": uuid4(), "nonce": b"n" * 12, "ciphertext": b"c" * 32})


@pytest.fixture
def send_case(db, actor, prepared_notification, access_metadata):
    with db[0].begin() as c:
        c.execute(text("UPDATE iam_memberships SET permissions="
            "'[\"reviews:read\",\"reviews:write\",\"reviews:approve\",\"reviews:send\"]' WHERE user_id=:id"),
            {"id": actor.user_id})
    review = prepared_notification.review
    local.execute(db, actor, local.decision(db, actor, review))
    ctx = local.context(db, actor, review)
    draft, decision = ctx["draft"], ctx["decision"]
    payload = {"schemaVersion": "review-send-request-v1", "organizationId": 91001,
        "marketplaceAccountId": 91101, "marketplace": "avito", "externalReviewId": review["external_review_id"],
        "draftId": draft["draftId"], "draftRevision": draft["revision"], "decisionId": decision["decisionId"],
        "operationKind": "review.answer.create.v1", "bindingChecksum": draft["bindingChecksum"],
        "textChecksum": draft["textChecksum"]}
    encoded = encode_review_send(payload)
    locator = ReviewJobLocator(91001, 91101, "avito", uuid4())
    intent = ReviewSendIntent(locator, UUID(review["review_id"]), UUID(draft["draftId"]),
        draft["revision"], UUID(decision["decisionId"]), uuid4(), encoded.canonical_bytes,
        encoded.checksum, draft["bindingChecksum"], draft["textChecksum"])
    factory = lambda: Session(db[1], autoflush=False)

    def forbidden_io(*args, **kwargs):
        pytest.fail("Create/cancel must never resolve credentials or invoke a provider")

    # These unused executor dependencies are real types, not a fabricated handle.
    # No executor role exists or is claimed authenticated by these user-path tests.
    identity = ExecutorRoleIdentity("unused_review_executor", db[1].url.username)
    policy = ReviewAuthorityPolicy("synthetic-acceptance-v1", 1, 30)
    resolver = make_executor_credential_resolver(
        session_factory=forbidden_io, keyring_loader=forbidden_io, identity=identity,
    )
    service = ReviewSendService(commands=ReviewJobCommands(session_factory=factory),
        executor=ReviewJobExecutor(executor_session_factory=forbidden_io, identity=identity,
            policy=policy, credential_resolver=resolver),
        reconciliation=ReviewReconciliation(session_factory=forbidden_io, credential_resolver=forbidden_io))
    return SimpleNamespace(service=service, intent=intent, policy=policy,
        expiry=datetime.now(UTC) + timedelta(minutes=20))


def create_send(case, actor, intent=None):
    return case.service.create(authenticated_actor=actor, intent=intent or case.intent,
        policy=case.policy, authority_expires_at=case.expiry)


def send_counts(db, locator):
    with db[0].connect() as c:
        return tuple(c.execute(text(f"SELECT count(*) FROM {table} WHERE command_id=:id"),
            {"id": locator.command_id}).scalar_one() for table in (
                "review_send_commands", "review_send_command_authorities", "review_send_audit", "review_send_enqueue_intents",
            ))


def test_send_intent_audit_authority_enqueue_are_atomic_and_replay_cannot_duplicate(db, actor, send_case):
    """Failure after intent insert must not leave an executable orphan command."""
    fail = fail_insert("review_send_enqueue_intents")
    event.listen(db[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(ReviewJobError, match="^REVIEW_PERSISTENCE_FAILED$"):
            create_send(send_case, actor)
    finally:
        event.remove(db[1], "before_cursor_execute", fail)
    assert send_counts(db, send_case.intent.locator) == (0, 0, 0, 0)
    created = create_send(send_case, actor)
    assert create_send(send_case, actor) == created
    assert send_counts(db, send_case.intent.locator) == (1, 1, 1, 1)
    changed_payload = json.loads(send_case.intent.request_payload)
    changed_payload["externalReviewId"] = "different-synthetic-review"
    encoded = encode_review_send(changed_payload)
    reused_key = replace(send_case.intent, request_payload=encoded.canonical_bytes, request_checksum=encoded.checksum)
    with pytest.raises(ReviewJobError, match="^REVIEW_CONFLICT$"):
        create_send(send_case, actor, reused_key)
    with pytest.raises(ReviewJobError, match="^REVIEW_ACCESS_DENIED$"):
        create_send(send_case, replace(actor, organization_id=91002))
    wrong_owner = replace(send_case.intent, locator=replace(send_case.intent.locator, marketplace_account_id=91102))
    with pytest.raises(ReviewJobError):
        create_send(send_case, actor, wrong_owner)
    with db[0].begin() as c:
        c.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"),
            {"id": actor.session_id})
    with pytest.raises(ReviewJobError, match="^REVIEW_AUTHORITY_DENIED$"):
        create_send(send_case, actor)
    assert send_counts(db, send_case.intent.locator) == (1, 1, 1, 1)


def test_send_cancel_has_one_physical_cas_winner_and_one_audit(db, actor, send_case):
    """Two real sessions cannot both commit the same queued version transition."""
    create_send(send_case, actor)
    locator = send_case.intent.locator
    expected = ReviewExpectedState(locator, 1, "queued", None, None, None, None, None)

    def cancel():
        try:
            send_case.service.cancel(authenticated_actor=actor, locator=locator, expected=expected)
            return "committed"
        except ReviewJobError as error:
            return error.code

    assert sorted(concurrent_roots(db[1], [cancel, cancel])) == ["REVIEW_CONFLICT", "committed"]
    with db[0].connect() as c:
        assert c.execute(text("SELECT state,version FROM review_send_commands WHERE command_id=:id"),
            {"id": locator.command_id}).one() == ("cancelled", 2)
    assert send_counts(db, locator) == (1, 1, 2, 1)
