"""Final owned-PG worker composition; synthetic credentials/transport only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.control_plane.auth import ActorContext
from app.modules.wb_repricing_commands import UserApprovalCommands
from app.modules.wb_repricing_dispatch import ApplyOutcome, AttemptStatus
from app.modules.wb_repricing_postgres import ApprovalTransaction
from app.modules.wb_repricing_worker import (
    DurableApprovalWorker,
    OriginalPricePostResponse,
    ReceiptPublicationPending,
    WorkerDisposition,
)
from app.platform.integrations import credential_store
from app.platform.integrations.publication_guard import UserSessionPrincipal
from app.platform.integrations.repricer_job_contract import (
    RepricerApprovalBinding,
    RepricerAuthorityPolicy,
    RepricerJobError,
    RepricerReadbackRequired,
)
from app.platform.integrations.repricer_job_executor import (
    RepricerJobCommands,
    RepricerJobExecutor,
)
from app.platform.integrations.worker_identity import ExecutorRoleIdentity
from app.security.marketplace_credentials import CredentialKeyring
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script
from tests.test_repricer_approvals_schema import seed
from tests.test_wb_repricing_approval_commands import binding
from tests.test_wb_repricing_postgres_repository import request as approval_request
from tests.test_wb_repricing_postgres_repository import scope

cluster = candidate.cluster


@pytest.fixture(scope="module")
def worker_db(cluster):
    api_role = "t2_job_api_" + uuid4().hex
    worker_role = "t2_job_worker_" + uuid4().hex
    with candidate.disposable_database(cluster, (api_role, worker_role)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        api = create_engine(owner.url.set(username=api_role), hide_parameters=True)
        worker = create_engine(
            owner.url.set(username=worker_role), hide_parameters=True
        )
        try:
            with owner.begin() as connection:
                seed(connection)
            grants = runtime_script(owner, api_role)
            assert grants.returncode == 0, grants.stderr
            script = f"\\set executor_role {worker_role}\n\\set api_runtime_role {api_role}\n"
            script += (candidate.ROOT / "ops/repricer-executor-grants.sql").read_text()
            url = owner.url
            applied = candidate.subprocess.run(
                [
                    "psql",
                    "-X",
                    "-w",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    url.query["host"],
                    "-p",
                    str(url.query["port"]),
                    "-U",
                    url.username,
                    "-d",
                    url.database,
                ],
                input=script,
                env=candidate.isolated_environment(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert applied.returncode == 0, applied.stderr
            yield owner, api, worker, ExecutorRoleIdentity(worker_role, api_role)
        finally:
            worker.dispose()
            api.dispose()
            owner.dispose()


@pytest.fixture
def job(worker_db, monkeypatch, request):
    owner, api, worker, identity = worker_db
    worker_factory = sessionmaker(bind=worker)
    resolver_logins = []

    def observe_resolver_login(connection, _cursor, statement, _parameters, _context, _many):
        if "ciphertext" in statement and "marketplace_account_credentials" in statement:
            # Observe the physical DBAPI connection executing the encrypted-row
            # fetch itself, not an auxiliary connection or SET ROLE assertion.
            with connection.connection.cursor() as cursor:
                cursor.execute("SELECT session_user,current_user")
                resolver_logins.append(cursor.fetchone())

    event.listen(worker, "before_cursor_execute", observe_resolver_login)
    request.addfinalizer(lambda: event.remove(worker, "before_cursor_execute", observe_resolver_login))
    monkeypatch.setattr(
        credential_store,
        "_load_keyring",
        lambda: CredentialKeyring(
            current_key_version=1,
            keys={1: b"s" * 32},
        ),
    )
    monkeypatch.setattr(
        credential_store, "get_session_factory", lambda: sessionmaker(bind=owner)
    )
    account_owner = credential_store.MarketplaceAccountCredentialOwner(7, 42, "wb")
    credential_store.put_marketplace_credential(
        account_owner, "wb_api", {"token": "synthetic-worker-token"}
    )
    # Dedicated adapter must never consult the public/global API factory.
    def forbidden_global_factory():
        raise AssertionError("executor-used-global-credential-factory")

    monkeypatch.setattr(credential_store, "get_session_factory", forbidden_global_factory)
    resolver = credential_store.make_executor_credential_resolver(
        session_factory=worker_factory,
        keyring_loader=lambda: CredentialKeyring(current_key_version=1, keys={1: b"s" * 32}),
        identity=identity,
    )
    login = uuid4().hex
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) "
                "VALUES(:login,'repricer77',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"
            ),
            {"login": login},
        )
        deadline = connection.execute(
            text("SELECT clock_timestamp()+interval '30 minutes'")
        ).scalar_one()
    principal = UserSessionPrincipal(7, "repricer77", 77, login)
    key = scope()
    req = approval_request(key)
    approval = UserApprovalCommands(api).create_intent(req, principal, binding())
    with owner.connect() as connection:
        row_id = connection.execute(
            text(
                "SELECT approval_row_id FROM wb_repricer_price_approvals "
                "WHERE organization_id=7 AND marketplace_account_id=42 AND approval_id=:id"
            ),
            {"id": key.approval_id},
        ).scalar_one()
    fixed = RepricerApprovalBinding(
        7,
        42,
        row_id,
        key.approval_id,
        approval.action_key,
        req.checksum,
        req.canonical_bytes,
    )
    policy = RepricerAuthorityPolicy("synthetic-local-policy", 1)
    actor = ActorContext(
        "synthetic", "repricer77", 7, "admin", frozenset(), session_id=login
    )
    created = RepricerJobCommands(session_factory=sessionmaker(bind=api)).create(
        authenticated_actor=actor,
        approval=fixed,
        policy=policy,
        authority_expires_at=deadline,
    )
    executor = RepricerJobExecutor(
        executor_session_factory=worker_factory,
        identity=identity,
        policy=policy,
        credential_resolver=resolver,
    )
    calls, lock = [], Lock()

    class Transport:
        def post_price_once(self, *, body, credential):
            assert body == req.provider_bytes
            assert credential.binding.owner == account_owner
            # Separate physical root sees marker and can lock it during the fake
            # provider call: no publication row lock spans external I/O.
            with owner.begin() as connection:
                state = connection.execute(
                    text(
                        "SELECT status FROM wb_repricer_price_apply_attempts "
                        "WHERE approval_row_id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": row_id},
                ).scalar_one()
                assert state == "dispatched"
            with lock:
                calls.append(True)
            return OriginalPricePostResponse(
                b'{"data":{"id":900719925474099312345678901}}',
                200,
                True,
                datetime.now(UTC),
            )

    transport = Transport()
    service = DurableApprovalWorker(
        executor=executor, transport=transport, max_response_bytes=4096
    )
    yield SimpleNamespace(
        owner=owner,
        executor=executor,
        locator=created.locator,
        service=service,
        transport=transport,
        calls=calls,
        login=login,
        row_id=row_id,
        resolver_logins=resolver_logins,
        worker_role=identity.marketplace_executor_role,
    )


def test_marker_receipt_restart_and_closed_duplicate(job):
    progress = job.service.run_once(job.locator.queue_payload())
    assert progress.state.approval_status == "applying"
    assert progress.state.attempt_status == "dispatched"
    assert progress.state.receipt.wb_upload_id == "900719925474099312345678901"
    assert len(job.calls) == 1
    assert job.resolver_logins
    assert all(pair == (job.worker_role, job.worker_role) for pair in job.resolver_logins)
    restarted = DurableApprovalWorker(
        executor=job.executor, transport=job.transport, max_response_bytes=4096
    )
    assert restarted.run_once(job.locator.queue_payload()).state == progress.state
    assert len(job.calls) == 1
    outcome = ApplyOutcome(
        AttemptStatus.applied,
        wb_upload_id=progress.state.receipt.wb_upload_id,
        result_code="SYNTHETIC_VERIFIED",
    )
    closed = job.service.record_verified_outcome(
        locator=job.locator, expected=progress.state.expected, outcome=outcome
    )
    assert closed.approval_status == "applied"
    assert (
        restarted.run_once(job.locator.queue_payload()).disposition
        is WorkerDisposition.closed
    )
    assert len(job.calls) == 1


def test_two_real_claim_sessions_one_fake_post(job, monkeypatch):
    rendezvous, claim = Barrier(2), job.executor.claim

    def simultaneous(**kwargs):
        rendezvous.wait(timeout=10)
        return claim(**kwargs)

    monkeypatch.setattr(job.executor, "claim", simultaneous)

    def run():
        try:
            return job.service.run_once(job.locator.queue_payload())
        except RepricerJobError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(isinstance(item, str) for item in results) == 1
    assert len(job.calls) == 1
    with job.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_repricer_price_apply_attempts WHERE approval_row_id=:id"
                ),
                {"id": job.row_id},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    "mode", ["transport_exception", "invalid_receipt", "before_io_denied"]
)
def test_uncertainty_never_becomes_rejection_or_resend(job, monkeypatch, mode):
    if mode == "before_io_denied":

        def denied(**_kwargs):
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED")

        monkeypatch.setattr(job.executor, "before_provider_io", denied)
    else:

        def response(**_kwargs):
            job.calls.append(True)
            if mode == "transport_exception":
                raise RuntimeError("synthetic-raw-exception-must-not-persist")
            return OriginalPricePostResponse(
                b'{"data":{"id":1.5}}', 200, True, datetime.now(UTC)
            )

        monkeypatch.setattr(job.transport, "post_price_once", response)
    result = job.service.run_once(job.locator.queue_payload())
    assert result.state.approval_status == "ambiguous"
    assert job.service.run_once(job.locator.queue_payload()).state == result.state
    assert len(job.calls) == (0 if mode == "before_io_denied" else 1)


def test_marker_commit_uncertainty_never_posts(job, monkeypatch):
    mark = job.executor.mark_dispatch

    def uncertain(**kwargs):
        mark(**kwargs)
        raise RepricerReadbackRequired(locator=job.locator)

    monkeypatch.setattr(job.executor, "mark_dispatch", uncertain)
    with pytest.raises(RepricerReadbackRequired):
        job.service.run_once(job.locator.queue_payload())
    state = job.executor.readback(locator=job.locator)
    assert state.attempt_status == "dispatched"
    assert job.service.run_once(job.locator.queue_payload()).state == state
    assert job.calls == []


def test_crash_after_reservation_can_resume_before_any_dispatch(job, monkeypatch):
    resolve = job.executor.resolve_fetch

    def crash(**_kwargs):
        raise SystemExit("synthetic-before-dispatch")

    monkeypatch.setattr(job.executor, "resolve_fetch", crash)
    with pytest.raises(SystemExit):
        job.service.run_once(job.locator.queue_payload())
    assert job.executor.readback(locator=job.locator).attempt_status == "reserved"
    assert job.calls == []
    monkeypatch.setattr(job.executor, "resolve_fetch", resolve)
    restarted = DurableApprovalWorker(executor=job.executor, transport=job.transport, max_response_bytes=4096)
    assert restarted.run_once(job.locator.queue_payload()).state.attempt_status == "dispatched"
    assert len(job.calls) == 1


def test_applied_requires_matching_durable_receipt(job):
    progress = job.service.run_once(job.locator.queue_payload())
    with pytest.raises(RepricerJobError):
        job.service.record_verified_outcome(
            locator=job.locator, expected=progress.state.expected,
            outcome=ApplyOutcome(AttemptStatus.applied, wb_upload_id="123", result_code="SYNTHETIC_VERIFIED"),
        )
    assert job.executor.readback(locator=job.locator) == progress.state
    assert len(job.calls) == 1


def test_failed_participant_rolls_back_claim(job, monkeypatch):
    original = ApprovalTransaction.claim

    def failure(self, *args):
        original(self, *args)
        raise RuntimeError("synthetic-local-rollback")

    monkeypatch.setattr(ApprovalTransaction, "claim", failure)
    with pytest.raises(RepricerJobError):
        job.service.run_once(job.locator.queue_payload())
    assert job.executor.readback(locator=job.locator).approval_status == "pending"
    assert job.calls == []


def test_receipt_commit_uncertainty_retains_id_no_new_post(job, monkeypatch):
    publish = job.executor.publish_receipt

    def uncertain(**kwargs):
        publish(**kwargs)
        raise RepricerReadbackRequired(locator=job.locator)

    monkeypatch.setattr(job.executor, "publish_receipt", uncertain)
    with pytest.raises(ReceiptPublicationPending) as raised:
        job.service.run_once(job.locator.queue_payload())
    assert raised.value.observation.wb_upload_id == "900719925474099312345678901"
    assert (
        job.executor.readback(locator=job.locator).receipt.wb_upload_id
        == raised.value.observation.wb_upload_id
    )
    job.service.run_once(job.locator.queue_payload())
    assert len(job.calls) == 1


def test_crash_after_acceptance_requires_explicit_recovery(job, monkeypatch):
    post = job.transport.post_price_once

    def crash(**kwargs):
        post(**kwargs)
        raise SystemExit("synthetic-crash")

    monkeypatch.setattr(job.transport, "post_price_once", crash)
    with pytest.raises(SystemExit):
        job.service.run_once(job.locator.queue_payload())
    result = job.service.run_once(job.locator.queue_payload())
    assert result.state.attempt_status == "dispatched" and len(job.calls) == 1
    closed = job.service.record_verified_outcome(
        locator=job.locator,
        expected=result.state.expected,
        outcome=ApplyOutcome(AttemptStatus.ambiguous, "WB_RESULT_UNAVAILABLE"),
    )
    assert closed.approval_status == "ambiguous"
    job.service.run_once(job.locator.queue_payload())
    assert len(job.calls) == 1


def test_revocation_before_marker_prevents_post_allows_owned_closing(job, monkeypatch):
    resolve = job.executor.resolve_fetch

    def revoke(**kwargs):
        result = resolve(**kwargs)
        with job.owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                ),
                {"id": job.login},
            )
        return result

    monkeypatch.setattr(job.executor, "resolve_fetch", revoke)
    with pytest.raises(RepricerJobError):
        job.service.run_once(job.locator.queue_payload())
    state = job.executor.readback(locator=job.locator)
    assert state.attempt_status == "reserved" and job.calls == []
    final = job.service.record_verified_outcome(
        locator=job.locator,
        expected=state.expected,
        outcome=ApplyOutcome(AttemptStatus.failed, "WB_APPLY_AUTHORIZATION_FAILED"),
    )
    assert final.approval_status == "failed"


@pytest.mark.parametrize(
    "change", [{"organization_id": 8}, {"marketplace_account_id": 43}]
)
def test_foreign_queue_scope_never_posts(job, change):
    with pytest.raises(RepricerJobError):
        job.service.run_once({**job.locator.queue_payload(), **change})
    assert job.calls == []


def test_stale_claim_never_posts(job):
    state = job.executor.readback(locator=job.locator)
    with pytest.raises(RepricerJobError):
        job.service.claim(
            locator=job.locator, expected=replace(state.expected, approval_version=1)
        )
    assert job.calls == []
