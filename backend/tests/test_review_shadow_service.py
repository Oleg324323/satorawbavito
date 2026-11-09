"""Guarded WB received-DTO composition with actual owned PostgreSQL/credentials."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.control_plane.auth import ActorContext
from app.platform.integrations import credential_store as store
from app.security.marketplace_credentials import CredentialKeyring
from app.wb_api.feedbacks_runtime import WbFeedbackRow
from tests import test_review_facts_repository as repository_tests
from tests import test_review_publication_repository as guarded_tests
from tests.test_marketplace_credential_fetch_postgres import wait_blocked
from tests.test_orders_schema_candidate import scope

cluster = repository_tests.cluster
db = repository_tests.db
principal = guarded_tests.principal
COVERAGE = {
    "from": None,
    "to": None,
    "streams": [{"name": "unanswered", "terminalReached": False}],
    "pagesObserved": 1,
    "providerEndReached": False,
}


def api():
    return importlib.import_module("app.reviews.shadow_service")


@pytest.fixture
def context(db, principal, monkeypatch):
    with db[0].begin() as c:
        c.execute(
            text(
                "UPDATE marketplace_accounts SET status='connected',external_account_id='synthetic-c',"
                "credential_ref=NULL WHERE marketplace_account_id=91103"
            )
        )
        c.execute(
            text(
                'UPDATE iam_memberships SET permissions=\'["reviews:write","cabinet:read"]\','
                "allowed_account_ids='[91103]' WHERE membership_id=:id"
            ),
            {"id": principal.membership_id},
        )
    factory = sessionmaker(bind=db[1], expire_on_commit=False)
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        store,
        "_load_keyring",
        lambda: CredentialKeyring(current_key_version=1, keys={1: b"t" * 32}),
    )
    owner = store.MarketplaceAccountCredentialOwner(91001, 91103, "wb")
    store.put_marketplace_credential(
        owner, "wb_api", {"token": "SYNTHETIC_REVIEW_SHADOW_TOKEN"}
    )
    fetched = store.resolve_marketplace_credential_for_fetch(owner, "wb_api")
    actor = ActorContext(
        actor_id=principal.user_id,
        user_id=principal.user_id,
        organization_id=91001,
        permission_profile="custom",
        permissions=frozenset(),
        session_id=principal.session_id,
    )
    return actor, fetched, owner


def row(key, body="received\0text"):
    return WbFeedbackRow(
        feedback_id=key,
        nm_id=101,
        imt_id=None,
        brand_name="Synthetic",
        product_name="Synthetic",
        created_date=repository_tests.NOW,
        text=body,
        pros="",
        cons="",
        answer_text=None,
        is_answered=False,
        rating=5,
        raw_payload={"synthetic": True},
    )


def start(db, context, source=None):
    actor, fetched, _ = context
    return api().begin_review_shadow(
        db[1],
        actor=actor,
        binding=fetched.binding,
        source_run_id=source or uuid4().hex,
        request_checksum="a" * 64,
    )


def current(db, key):
    with db[1].begin() as c:
        scope(c)
        return repository_tests.repository(
            c, account=91103, provider="wb", external="synthetic-c"
        ).get_fact(key)


def test_same_received_dto_is_published_once_after_committed_reservation(db, context):
    source, key = uuid4().hex, uuid4().hex + "\0key"
    calls = []

    def provider(secret):
        calls.append(1)
        assert secret == {"token": "SYNTHETIC_REVIEW_SHADOW_TOKEN"}
        with db[0].connect() as c:
            assert (
                c.execute(
                    text(
                        "SELECT status FROM review_sync_runs_v2 WHERE source_run_id_utf8=:id"
                    ),
                    {"id": source.encode("utf-8")},
                ).scalar_one()
                == "running"
            )
        return (row(key),)

    ticket = start(db, context, source)
    received = provider(context[1].secret.reveal())
    before = received[0].raw_payload.copy()
    receipt = api().publish_received_review_rows(
        db[1], ticket=ticket, rows=received, coverage=COVERAGE
    )
    again = api().publish_received_review_rows(
        db[1], ticket=ticket, rows=received, coverage=COVERAGE
    )
    assert again == receipt
    assert receipt.observed_count == 1
    assert current(db, key).text == "received\0text"
    assert current(db, key).revision == 1
    assert calls == [1]
    assert received[0].raw_payload == before
    with db[0].connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM review_sync_runs_v2 WHERE sync_run_id=:id"),
                {"id": receipt.sync_run_id},
            ).scalar_one()
            == "partial"
        )


def test_earlier_reserved_late_response_cannot_replace_current_unknown_time(
    db, context
):
    key = uuid4().hex
    earlier, later = start(db, context), start(db, context)
    api().publish_received_review_rows(
        db[1], ticket=later, rows=(row(key, "later"),), coverage=COVERAGE
    )
    api().publish_received_review_rows(
        db[1], ticket=earlier, rows=(row(key, "earlier"),), coverage=COVERAGE
    )
    assert current(db, key).text == "later"


def test_changed_same_run_response_conflicts_and_keeps_original(db, context):
    key = uuid4().hex
    ticket = start(db, context)
    api().publish_received_review_rows(
        db[1], ticket=ticket, rows=(row(key, "original"),), coverage=COVERAGE
    )
    with pytest.raises(api().ReviewShadowError, match="^REVIEW_SHADOW_CONFLICT$"):
        api().publish_received_review_rows(
            db[1], ticket=ticket, rows=(row(key, "changed"),), coverage=COVERAGE
        )
    assert current(db, key).text == "original"


def test_sessionless_scheduler_actor_is_denied_before_reserving(db, context):
    actor, fetched, _ = context
    with pytest.raises(api().ReviewShadowError, match="^REVIEW_SHADOW_DENIED$"):
        api().begin_review_shadow(
            db[1],
            actor=replace(actor, session_id=None),
            binding=fetched.binding,
            source_run_id=uuid4().hex,
            request_checksum="a" * 64,
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ("session", "REVIEW_SHADOW_DENIED"),
        ("membership", "REVIEW_SHADOW_DENIED"),
        ("scope", "REVIEW_SHADOW_DENIED"),
        ("account", "REVIEW_SHADOW_AUTHORITY_CHANGED"),
        ("credential", "REVIEW_SHADOW_AUTHORITY_CHANGED"),
    ],
)
def test_revocation_between_fetch_and_publication_leaves_only_running_reservation(
    db, context, change, code
):
    ticket, key = start(db, context), uuid4().hex
    actor, _, owner = context
    if change == "credential":
        store.revoke_marketplace_credential(owner, "wb_api", "operator_revoked")
    else:
        with db[0].begin() as c:
            if change == "session":
                c.execute(
                    text(
                        "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                    ),
                    {"id": actor.session_id},
                )
            elif change == "membership":
                c.execute(
                    text(
                        "UPDATE iam_memberships SET is_active=false WHERE user_id=:id"
                    ),
                    {"id": actor.user_id},
                )
            elif change == "scope":
                c.execute(
                    text(
                        "UPDATE iam_memberships SET allowed_account_ids='[91101]' WHERE user_id=:id"
                    ),
                    {"id": actor.user_id},
                )
            else:
                c.execute(
                    text(
                        "UPDATE marketplace_accounts SET external_account_id='changed' WHERE marketplace_account_id=91103"
                    )
                )
    with pytest.raises(api().ReviewShadowError, match="^" + code + "$"):
        api().publish_received_review_rows(
            db[1], ticket=ticket, rows=(row(key),), coverage=COVERAGE
        )
    with db[0].connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM review_sync_runs_v2 WHERE sync_run_id=:id"),
                {"id": ticket.run.sync_run_id},
            ).scalar_one()
            == "running"
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM review_facts WHERE external_review_id_utf8=:id"
                ),
                {"id": key.encode("utf-8")},
            ).scalar_one()
            == 0
        )


def test_partial_snapshot_keeps_absent_review_and_duplicate_ids_publish_nothing(
    db, context
):
    first, absent = uuid4().hex, uuid4().hex
    api().publish_received_review_rows(
        db[1],
        ticket=start(db, context),
        rows=(row(first), row(absent)),
        coverage=COVERAGE,
    )
    api().publish_received_review_rows(
        db[1],
        ticket=start(db, context),
        rows=(row(first, "changed"),),
        coverage=COVERAGE,
    )
    assert current(db, absent).text == "received\0text"
    key = uuid4().hex
    with pytest.raises(api().ReviewShadowError, match="^REVIEW_SHADOW_INVALID$"):
        api().publish_received_review_rows(
            db[1],
            ticket=start(db, context),
            rows=(row(key), row(key)),
            coverage=COVERAGE,
        )
    assert current(db, key) is None


def test_stale_actor_admin_claim_cannot_override_live_membership(db, context):
    actor, fetched, _ = context
    with db[0].begin() as c:
        c.execute(
            text("UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"),
            {"id": actor.user_id},
        )
    with pytest.raises(api().ReviewShadowError, match="^REVIEW_SHADOW_DENIED$"):
        api().begin_review_shadow(
            db[1],
            actor=replace(
                actor,
                permission_profile="admin",
                permissions=frozenset({"reviews:write"}),
            ),
            binding=fetched.binding,
            source_run_id=uuid4().hex,
            request_checksum="a" * 64,
        )


def test_physical_commit_error_is_sanitized_and_rolls_back_publication(db, context):
    ticket, key = start(db, context), uuid4().hex

    def fail_commit(connection):
        raise OperationalError(
            "synthetic-private-sql", {}, RuntimeError("synthetic-private-payload")
        )

    event.listen(db[1], "commit", fail_commit)
    try:
        with pytest.raises(
            api().ReviewShadowError, match="^REVIEW_SHADOW_STORAGE_UNAVAILABLE$"
        ):
            api().publish_received_review_rows(
                db[1], ticket=ticket, rows=(row(key),), coverage=COVERAGE
            )
    finally:
        event.remove(db[1], "commit", fail_commit)
    assert current(db, key) is None


@pytest.mark.parametrize("publisher_first", [False, True])
def test_actual_session_revocation_lock_winners_fence_review_publication(
    db, context, monkeypatch, publisher_first
):
    service = api()
    ticket, key = start(db, context), uuid4().hex
    started, written, release, revoke_started = Event(), Event(), Event(), Event()
    pids = {}
    original_guard = service.acquire_publication_guard
    original_ingest = service.ReviewFactsRepository.ingest

    def observed_guard(session, **kwargs):
        pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
        started.set()
        return original_guard(session, **kwargs)

    def paused_ingest(repo, *args, **kwargs):
        result = original_ingest(repo, *args, **kwargs)
        written.set()
        assert release.wait(8)
        return result

    monkeypatch.setattr(service, "acquire_publication_guard", observed_guard)
    if publisher_first:
        monkeypatch.setattr(service.ReviewFactsRepository, "ingest", paused_ingest)

    def publish():
        return service.publish_received_review_rows(
            db[1], ticket=ticket, rows=(row(key),), coverage=COVERAGE
        )

    def revoke():
        with Session(db[0]) as session, session.begin():
            pids["revoker"] = session.scalar(text("SELECT pg_backend_pid()"))
            revoke_started.set()
            session.execute(
                text(
                    "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                ),
                {"id": context[0].session_id},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            if publisher_first:
                future = pool.submit(publish)
                assert written.wait(8)
                revoker = pool.submit(revoke)
                assert revoke_started.wait(5)
                with db[0].connect() as observer:
                    wait_blocked(observer, pids["revoker"], pids["publisher"])
                assert not revoker.done()
                release.set()
                assert future.result(timeout=8).observed_count == 1
                revoker.result(timeout=8)
                assert current(db, key).text == "received\0text"
            else:
                with Session(db[0]) as session, session.begin():
                    blocker = session.scalar(text("SELECT pg_backend_pid()"))
                    session.execute(
                        text(
                            "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                        ),
                        {"id": context[0].session_id},
                    )
                    future = pool.submit(publish)
                    assert started.wait(5)
                    with db[0].connect() as observer:
                        wait_blocked(observer, pids["publisher"], blocker)
                    assert not future.done()
                with pytest.raises(
                    service.ReviewShadowError, match="^REVIEW_SHADOW_DENIED$"
                ):
                    future.result(timeout=8)
                assert current(db, key) is None
        finally:
            release.set()


@pytest.mark.parametrize(
    "change,code",
    [
        ("request", "REVIEW_SHADOW_CONFLICT"),
        ("credential", "REVIEW_SHADOW_AUTHORITY_CHANGED"),
        ("row", "REVIEW_SHADOW_INVALID"),
        ("list", "REVIEW_SHADOW_INVALID"),
    ],
)
def test_bad_ticket_or_received_payload_never_creates_a_fact(db, context, change, code):
    ticket, key = start(db, context), uuid4().hex
    received = (row(key),)
    if change == "request":
        ticket = replace(ticket, request_checksum="b" * 64)
    elif change == "credential":
        ticket = replace(
            ticket, authority=replace(ticket.authority, credential_id=uuid4())
        )
    elif change == "row":
        received = (
            replace(received[0], text={"private": "synthetic-customer-content"}),
        )
    else:
        received = list(received)
    with pytest.raises(api().ReviewShadowError, match="^" + code + "$") as failure:
        api().publish_received_review_rows(
            db[1], ticket=ticket, rows=received, coverage=COVERAGE
        )
    assert str(failure.value) == code
    assert current(db, key) is None


def test_connection_bound_entry_does_not_commit_callers_root(db, context):
    actor, fetched, _ = context
    with db[1].begin() as c:
        with pytest.raises(api().ReviewShadowError, match="^REVIEW_SHADOW_INVALID$"):
            api().begin_review_shadow(
                c,
                actor=actor,
                binding=fetched.binding,
                source_run_id=uuid4().hex,
                request_checksum="a" * 64,
            )
        assert c.in_transaction()
        assert c.exec_driver_sql("SELECT 1").scalar_one() == 1
