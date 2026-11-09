from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.control_plane.auth import ActorContext
from app.modules.orders import OrderContractValidationError
from app.orders.job_publication import publish_claimed_orders_manifest
from app.platform.integrations.orm import MarketplaceAccountCredentialRow
from app.platform.integrations.publication_guard import PublicationGuardError
from app.platform.integrations.user_orders_job_contract import (
    AvitoOrdersSourceRequest,
    ClaimedUserOrdersJob,
    OrdersExecutionPolicy,
    OrdersJobError,
    OrdersJobRequest,
    TrustedOrdersSourceBinding,
)
from app.platform.integrations.user_orders_jobs import (
    UserOrdersJobs,
    UserOrdersPublicationHandle,
)
from tests.test_orders_projection_repository import item_fact
from tests.test_orders_publication_service import manifest
from tests.test_orders_read_service import prepared as _prepared
from tests.test_orders_read_service import read_db as _read_db
from tests.test_orders_schema_candidate import cluster as _cluster

prepared, read_db, cluster = _prepared, _read_db, _cluster


@pytest.fixture
def claimed(prepared):
    owner, runtime, principal, _ = prepared
    credential_id = uuid4()
    with Session(owner) as session, session.begin():
        now = session.scalar(text("SELECT clock_timestamp()"))
        session.execute(
            text("""UPDATE iam_memberships SET permissions='["cabinet:read","sync:run"]'
            WHERE membership_id=:id"""),
            {"id": principal.membership_id},
        )
        session.add(
            MarketplaceAccountCredentialRow(
                credential_id=credential_id,
                organization_id=91001,
                marketplace_account_id=91101,
                provider="avito",
                credential_kind="avito_oauth_access",
                key_version=1,
                nonce=uuid4().bytes[:12],
                ciphertext=b"synthetic-not-real-ciphertext",
                generation=1,
                expires_at=now + timedelta(hours=1),
            )
        )
    value = manifest(item_fact(), complete=False)
    binding = TrustedOrdersSourceBinding(
        "avito",
        value.source_kind,
        value.adapter_version,
        "avito-order-status-v1",
        value.source_contract_version,
    )
    request = OrdersJobRequest(
        91001, 91101, binding, AvitoOrdersSourceRequest(None, (), 20, 1)
    )

    def no_fetch(*args, **kwargs):
        raise AssertionError("Publication must not resolve secrets or fetch")

    jobs = UserOrdersJobs(
        session_factory=sessionmaker(runtime),
        trusted_sources=(binding,),
        credential_resolver=no_fetch,
    )
    actor = ActorContext(
        actor_id="user:" + principal.user_id,
        user_id=principal.user_id,
        organization_id=91001,
        permission_profile="custom",
        permissions=frozenset({"cabinet:read", "sync:run"}),
        session_id=principal.session_id,
    )
    view = jobs.create(
        authenticated_actor=actor,
        request=request,
        idempotency_key=uuid4(),
        execution_policy=OrdersExecutionPolicy("synthetic-test-only", 1, 1, 600, ()),
        authority_expires_at=now + timedelta(minutes=30),
    )
    claim = jobs.claim(
        organization_id=91001, marketplace_account_id=91101, job_id=view.job_id
    )
    assert type(claim) is ClaimedUserOrdersJob
    yield owner, runtime, principal, jobs, actor, request, value, claim
    with owner.begin() as connection:
        connection.execute(
            text("""UPDATE marketplace_account_credentials SET revoked_at=clock_timestamp(),
            revocation_reason_code='operator_revoked' WHERE credential_id=:id AND revoked_at IS NULL"""),
            {"id": credential_id},
        )


def publish(values, *, value=None, request=None):
    _, runtime, _, jobs, _, original_request, original_value, claim = values
    with Session(runtime) as session:
        result = publish_claimed_orders_manifest(
            session,
            jobs=jobs,
            claim=claim,
            request=original_request if request is None else request,
            manifest=original_value if value is None else value,
        )
        assert not session.in_transaction()
        return result


def test_partial_evidence_and_job_result_commit_together_with_real_actor(claimed):
    owner, _, principal, jobs, actor, request, value, claim = claimed
    assert (
        jobs.request_for_fetch(claim=claim).canonical_bytes == request.canonical_bytes
    )
    result = publish(claimed)
    assert result.state == "partial" and result.replayed is False
    stored = jobs.status(authenticated_actor=actor, locator=claim.locator)
    assert stored.state == "succeeded" and stored.result_sync_run_id == result.run_id
    assert stored.result_coverage_state == "partial"
    with owner.connect() as connection:
        run = connection.execute(
            text("""SELECT state,manifest_state,source_run_key,payload_checksum,
            requested_from,requested_to FROM order_sync_runs WHERE sync_run_id=:run"""),
            {"run": result.run_id},
        ).one()
        assert tuple(run) == (
            "partial",
            "partial",
            claim.source_run_key,
            value.checksum,
            None,
            None,
        )
        audit_actor = connection.scalar(
            text("""SELECT actor_user_id FROM lk_audit_events
            WHERE object_type='orders_sync_run' AND object_id=:run
            AND action='orders.manifest_published'"""),
            {"run": str(result.run_id)},
        )
        assert audit_actor == principal.user_id
        assert (
            connection.scalar(
                text("""SELECT last_seen_sync_run_id FROM marketplace_orders
            WHERE organization_id=91001 AND marketplace_account_id=91101
            AND external_order_id=:id"""),
                {"id": value.observations[0].identity.external_order_id},
            )
            is None
        )


@pytest.mark.parametrize("change", ["complete", "empty", "request", "page", "source"])
def test_invalid_or_unproven_source_cannot_create_run(claimed, change):
    owner, _, _, jobs, actor, request, value, claim = claimed
    if change == "complete":
        value = manifest(value.observations[0], complete=True)
    elif change == "empty":
        value = replace(value, pages=(replace(value.pages[0], observations=()),))
    elif change == "request":
        request = replace(
            request, source_request=replace(request.source_request, limit=19)
        )
    elif change == "page":
        value = replace(value, pages=(replace(value.pages[0], number=2),))
    else:
        value = replace(value, source_contract_version="synthetic-other-contract")
    with pytest.raises(OrderContractValidationError):
        publish(claimed, value=value, request=request)
    assert (
        jobs.status(authenticated_actor=actor, locator=claim.locator).state == "running"
    )
    with owner.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": claim.source_run_key},
            )
            == 0
        )


def test_final_failure_rolls_back_both_domain_and_job_transition(claimed, monkeypatch):
    owner, _, _, jobs, actor, _, _, claim = claimed
    original = UserOrdersPublicationHandle.complete

    def deny_after_transition(handle, *, result_sync_run_id):
        original(handle, result_sync_run_id=result_sync_run_id)
        raise OrdersJobError("JOB_FENCE_INVALID")

    monkeypatch.setattr(UserOrdersPublicationHandle, "complete", deny_after_transition)
    with pytest.raises(OrdersJobError, match="JOB_FENCE_INVALID"):
        publish(claimed)
    assert (
        jobs.status(authenticated_actor=actor, locator=claim.locator).state == "running"
    )
    with owner.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": claim.source_run_key},
            )
            == 0
        )


def test_revoked_session_cannot_publish_claimed_evidence(claimed):
    owner, _, principal, _, _, _, _, claim = claimed
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
            ),
            {"id": principal.session_id},
        )
    with pytest.raises((OrdersJobError, PublicationGuardError)):
        publish(claimed)
    with owner.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": claim.source_run_key},
            )
            == 0
        )


def test_two_real_database_sessions_cannot_publish_one_claim_twice(claimed):
    owner, runtime, _, _, _, _, _, claim = claimed
    barrier, lock, pids = Barrier(2), Lock(), set()

    def overlap(connection, _record, _proxy):
        with lock:
            pids.add(connection.info.backend_pid)
        barrier.wait(timeout=10)

    def attempt():
        try:
            return publish(claimed)
        except (OrdersJobError, PublicationGuardError) as exc:
            return exc

    event.listen(runtime, "checkout", overlap)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
    finally:
        event.remove(runtime, "checkout", overlap)
    assert len(pids) == 2
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    with owner.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": claim.source_run_key},
            )
            == 1
        )


@pytest.mark.parametrize("commit_first", [False, True])
def test_uncertain_commit_requires_readback_not_refetch(
    claimed, monkeypatch, commit_first
):
    owner, runtime, _, jobs, _, _, _, claim = claimed
    original = runtime.dialect.do_commit

    def uncertain(connection):
        if commit_first:
            original(connection)
        raise OperationalError(
            "COMMIT", {}, RuntimeError("synthetic transport failure")
        )

    with monkeypatch.context() as context:
        context.setattr(runtime.dialect, "do_commit", uncertain)
        with pytest.raises(OrdersJobError, match="READBACK_REQUIRED"):
            publish(claimed)
    result = jobs.readback(
        organization_id=claim.locator.organization_id,
        marketplace_account_id=claim.locator.marketplace_account_id,
        job_id=claim.locator.job_id,
    )
    assert result == {
        "job_id": str(claim.locator.job_id),
        "state": "succeeded" if commit_first else "running",
    }
    with owner.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
            {"key": claim.source_run_key},
        ) == int(commit_first)
