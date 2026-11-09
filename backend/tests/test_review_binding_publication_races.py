"""Synthetic account-row lock winners at guarded Review publication, not live I/O."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    acquire_publication_guard,
)
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.ingestion_contract import ReviewRepositoryError
from app.reviews.run_binding_storage import decode_review_run_binding
from tests import test_review_shadow_service as shadow
from tests.test_marketplace_credential_fetch_postgres import wait_blocked
from tests.test_orders_schema_candidate import scope

cluster = shadow.cluster
db = shadow.db
principal = shadow.principal
context = shadow.context


@pytest.mark.parametrize("publisher_first", [False, True])
@pytest.mark.parametrize(
    "external_id,credential_ref",
    [("synthetic-next-cabinet", None), ("synthetic-c", "synthetic-next-ref"), ("synthetic-c", "")],
    ids=["external-account-change", "credential-reference-change", "null-to-empty-reference"],
)
def test_account_binding_lock_winners_fence_review_publication(
    db, context, monkeypatch, publisher_first, external_id, credential_ref
):
    service = shadow.api()
    ticket, key = shadow.start(db, context), uuid4().hex
    # Read admission is real and explicit; the old ticket retains its original
    # paired binding. No provider call, new credential or token rotation is used.
    with db[0].begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET permissions="
                "'[\"reviews:write\",\"reviews:read\",\"cabinet:read\"]' "
                "WHERE organization_id=91001 AND membership_id=:id"
            ),
            {"id": ticket.principal.membership_id},
        )

    started, written, release, rebind_started = Event(), Event(), Event(), Event()
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
        assert release.wait(8), "Publisher release was not signalled"
        return result

    monkeypatch.setattr(service, "acquire_publication_guard", observed_guard)
    if publisher_first:
        monkeypatch.setattr(service.ReviewFactsRepository, "ingest", paused_ingest)

    def publish():
        return service.publish_received_review_rows(
            db[1], ticket=ticket, rows=(shadow.row(key, "synthetic-original-body"),),
            coverage=shadow.COVERAGE,
        )

    def update_binding(session, target_external, target_reference):
        changed = session.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id=:external, "
                "credential_ref=:reference WHERE organization_id=91001 "
                "AND marketplace_account_id=91103 AND marketplace='wb'"
            ),
            {"external": target_external, "reference": target_reference},
        ).rowcount
        assert changed == 1

    def rebind():
        with Session(db[0]) as session, session.begin():
            pids["rebinder"] = session.scalar(text("SELECT pg_backend_pid()"))
            rebind_started.set()
            update_binding(session, external_id, credential_ref)

    def read_under_fresh_binding():
        # Exceptions leave the entire guarded root. Callers assert outside it;
        # never catch a failed guarded read and commit that root.
        with Session(db[1]) as session, session.begin():
            guard = acquire_publication_guard(
                session,
                principal=ticket.principal,
                required_permissions=frozenset({"reviews:read"}),
                accounts=(ExpectedAccountBinding(
                    91103, "wb", external_id, credential_ref,
                ),),
                authorities=(),
            )
            guard.revalidate_before_write()
            repo = ReviewFactsRepository(
                session.connection(),
                ReviewOwner(91001, 91103, "wb", external_id, credential_ref),
                command_savepoints=False,
            )
            return repo.get_fact(key)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                if publisher_first:
                    future = pool.submit(publish)
                    assert written.wait(8), "Publication did not reach real ingest"
                    rebinder = pool.submit(rebind)
                    assert rebind_started.wait(5)
                    with db[0].connect() as observer:
                        wait_blocked(observer, pids["rebinder"], pids["publisher"])
                    assert not rebinder.done()
                    release.set()
                    receipt = future.result(timeout=8)
                    assert receipt.observed_count == 1
                    assert receipt.sync_run_id == ticket.run.sync_run_id
                    rebinder.result(timeout=8)
                else:
                    with Session(db[0]) as session, session.begin():
                        blocker = session.scalar(text("SELECT pg_backend_pid()"))
                        update_binding(session, external_id, credential_ref)
                        future = pool.submit(publish)
                        assert started.wait(5)
                        with db[0].connect() as observer:
                            wait_blocked(observer, pids["publisher"], blocker)
                        assert not future.done()
                    with pytest.raises(
                        service.ReviewShadowError,
                        match="^REVIEW_SHADOW_AUTHORITY_CHANGED$",
                    ):
                        future.result(timeout=8)
            finally:
                # Release the publisher before executor join, including assertion failures.
                release.set()

        # The rebinder really committed its new exact descriptor in either order.
        # Runtime RLS SELECT below observes metadata/counts only, not an old body
        # through an unsafe repository owner after the account changed.
        with db[1].begin() as connection:
            scope(connection)
            account = connection.execute(text(
                "SELECT external_account_id, credential_ref FROM marketplace_accounts "
                "WHERE organization_id=91001 AND marketplace_account_id=91103 AND marketplace='wb'"
            )).one()
            assert tuple(account) == (external_id, credential_ref)
            run = connection.execute(text(
                "SELECT * FROM review_sync_runs_v2 WHERE organization_id=91001 "
                "AND marketplace_account_id=91103 AND marketplace='wb' AND sync_run_id=:id"
            ), {"id": ticket.run.sync_run_id}).mappings().one()
            assert decode_review_run_binding(run) == ReviewBindingDescriptor(
                91001, 91103, "wb", "synthetic-c", None,
            )
            assert run["status"] == ("partial" if publisher_first else "running")
            assert run["observed_count"] == (1 if publisher_first else 0)
            for table, run_column in (
                ("review_observations", "source_run_id"),
                ("review_sync_run_items", "sync_run_id"),
            ):
                count = connection.execute(text(
                    f"SELECT count(*) FROM {table} WHERE organization_id=91001 "
                    f"AND marketplace_account_id=91103 AND marketplace='wb' AND {run_column}=:id"
                ), {"id": ticket.run.sync_run_id}).scalar_one()
                assert count == (1 if publisher_first else 0)

        if credential_ref == "":
            # Storage preserves NULL != empty; the platform guard deliberately
            # rejects empty references before a read can be authorized.
            with pytest.raises(PublicationGuardError, match="^publication_binding_changed$"):
                read_under_fresh_binding()
        elif publisher_first:
            # A valid new live read guard cannot authorize the committed old body.
            with pytest.raises(
                ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$",
            ):
                read_under_fresh_binding()
        else:
            assert read_under_fresh_binding() is None
    finally:
        release.set()
        with Session(db[0]) as session, session.begin():
            update_binding(session, ticket.account.external_account_id, ticket.account.credential_ref)
