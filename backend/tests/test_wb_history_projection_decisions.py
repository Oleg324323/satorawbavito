"""SQL proof admission under a genuine created/claimed history job and handle."""
import json
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.orders.bindings import account_binding_checksum, serialize_account_bindings
from app.orders.serialization import observation_checksum, serialize_observation
from app.platform.integrations.user_orders_job_contract import OrdersExecutionPolicy
from app.platform.integrations.wb_history_projection import (
    WbHistoryProjectionHandle,
    WbHistoryProjectionJobs,
)
from app.platform.integrations.wb_history_projection_role import (
    HistoryProjectionRoleIdentity,
)
from app.wb_live.statistics_orders import iter_orders_page
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_history_projection_codecs as codecs
from tests import test_wb_live_history_postgres as history

cluster = codecs.cluster
pg_store = codecs.pg_store
data = codecs.data
live = codecs.live


@pytest.fixture(scope="module")
def pg_database(cluster):
    api, worker, dispatcher, projection, helper = ("projection_" + uuid4().hex for _ in range(5))
    with candidate.disposable_database(cluster, (api, worker, dispatcher, projection, helper)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "20260910_0085")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=api), hide_parameters=True)
        projector = create_engine(owner.url.set(username=projection), hide_parameters=True)
        try:
            script = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            script = "\n".join(line for line in script.splitlines() if not line.startswith("\\"))
            for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                    ("wb_live_api", api), ("wb_live_worker", worker), ("wb_live_dispatch", dispatcher)):
                script = script.replace(old, new)
            with owner.begin() as connection:
                connection.execute(text(script))
                connection.exec_driver_sql(f'ALTER ROLE "{helper}" NOLOGIN')
                for signature in codecs.CODECS:
                    connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION {signature} TO "{worker}"')
                connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION public.wb_state_json(jsonb),public.repricer_ascii_json_string(text),public.wb_sku_override_decimal(numeric) TO "{worker}"')
                identities = dict(connection.execute(text("SELECT rolname,oid FROM pg_roles WHERE rolname IN (:runtime,:helper)"),
                    {"runtime": projection, "helper": helper}).all())
            psql = shutil.which("psql")
            assert psql is not None
            grants = subprocess.run([psql, "-X", "-h", database.host, "-p", str(database.port), "-U", database.owner,
                "-d", database.name, "-v", f"projection_role={projection}", "-v", f"projection_helper_owner={helper}",
                "-f", str(candidate.ROOT / "ops/wb-history-projection-role.sql")], env=candidate.isolated_environment(),
                capture_output=True, text=True, timeout=60, check=False)
            assert grants.returncode == 0, grants.stderr
            runtime._history_api_role, runtime._history_worker_role = api, worker
            runtime._projection_engine = projector
            runtime._projection_role_identity = HistoryProjectionRoleIdentity(identities[projection], projection, identities[helper], helper)
            yield owner, runtime
        finally:
            projector.dispose()
            runtime.dispose()
            owner.dispose()


@pytest.fixture
def prepared(live):
    d = live
    worker, source = codecs.capture(d)
    try:
        with d.engine.begin() as c:
            c.execute(text("UPDATE wb_live_sync_sources SET next_due_at=clock_timestamp() WHERE organization_id=:o AND source=:s"),
                {"o": d.org, "s": history.SOURCE})
            c.execute(text("UPDATE iam_memberships SET permissions='[\"integrations:write\",\"sync:run\",\"cabinet:read\"]' WHERE membership_id=:o"), {"o": d.org})
        from app.wb_live.contracts import JobLocator
        locator = JobLocator(d.org, d.org, str(source["job"]))
        lease = d.repo.claim_batch(locator)
        page = history.begin(d, lease)
        end = next(iter_orders_page([b"[]"], organization_id=d.org, marketplace_account_id=d.org,
            date_from=lease.checkpoint["dateFrom"], observed_at=datetime.now(UTC)))
        assert d.repo.commit_history_page(lease, page_id=page, end=end, next_due_at=datetime.now(UTC))
        api_engine = d.factory.kw["bind"]
        d.factory = sessionmaker(api_engine._projection_engine, expire_on_commit=False)
        service = WbHistoryProjectionJobs(session_factory=d.factory,
            execution_policy=OrdersExecutionPolicy("synthetic-explicit-history", 1, 2, 600, (1,)),
            authority_deadline_provider=lambda **values: values["database_now"] + timedelta(minutes=30), max_selection_pages=3,
            role_identity=api_engine._projection_role_identity)
        view = service.create(authenticated_actor=d.actor, organization_id=d.org, marketplace_account_id=d.org,
            history_job_id=source["job"], history_run_id=UUID(lease.run_id), idempotency_key=uuid4())
        claim = service.claim(organization_id=d.org, marketplace_account_id=d.org, job_id=view.job_id)
        yield d, service, claim
    finally:
        worker.dispose()


@contextmanager
def guarded(prepared):
    d, service, claim = prepared
    with d.factory() as session:
        session.begin()
        try:
            base = service._jobs.acquire_publication_guard(session, claim=claim)
            handle = WbHistoryProjectionHandle(base)
            yield session, handle
        finally:
            session.rollback()  # Every admission probe rolls back; no fake participant/result.


def staged(session, handle):
    p, account = handle.page, handle.account_binding
    return session.scalar(text("""INSERT INTO order_sync_runs
 (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,adapter_version,mapping_version,
 source_contract_version,source_snapshot,account_binding_schema_version,account_binding_external_account_id,
 account_binding_credential_ref,account_binding_payload,account_binding_checksum)
 VALUES(:org,:account,'wb','wb-statistics-supplier-orders',:key,'wb-statistics-orders-stream-v1','wb-statistics-status-v1',
 'wb-history-positive-partial-v1',:snapshot,1,:external,:reference,:payload,:checksum) RETURNING sync_run_id"""),
        {"org": p.organization_id, "account": p.marketplace_account_id, "key": handle.source_run_key,
            "snapshot": handle.source_snapshot, "external": account.external_account_id,
            "reference": account.credential_ref, "payload": serialize_account_bindings(p.organization_id, (account,)),
            "checksum": account_binding_checksum(p.organization_id, (account,))})


def observation(session, handle, run):
    row = handle._plan.rows[0].observation
    p = {"org": row.identity.organization_id, "account": row.identity.marketplace_account_id,
        "run": run, "external": row.identity.external_order_id}
    p["order"] = session.scalar(text("INSERT INTO marketplace_orders(organization_id,marketplace_account_id,marketplace,external_order_id) "
        "VALUES(:org,:account,'wb',:external) RETURNING order_id"), p)
    p.update(revision=row.source_revision, checksum=observation_checksum(row), payload=json.dumps(serialize_observation(row)),
        effective=row.effective_at, observed=row.observed_at)
    p["observation"] = session.scalar(text("""INSERT INTO order_observations
 (organization_id,marketplace_account_id,sync_run_id,order_id,source_kind,adapter_version,source_revision,
 payload_checksum,normalized_evidence,source_effective_at,observed_at)
 VALUES(:org,:account,:run,:order,'wb-statistics-supplier-orders','wb-statistics-orders-stream-v1',:revision,
 :checksum,CAST(:payload AS jsonb),:effective,:observed) RETURNING observation_id"""), p)
    return p


def test_forged_decision_fields_rejected_before_membership_insert(prepared):
    with guarded(prepared) as (session, handle):
        p = observation(session, handle, staged(session, handle))
        with pytest.raises(DBAPIError) as caught:
            session.execute(text("""INSERT INTO order_sync_memberships
 (organization_id,marketplace_account_id,sync_run_id,order_id,observation_id,coverage_role,observed_at,
 history_decision_version,history_pre_order_version,history_outcome)
 VALUES(:org,:account,:run,:order,:observation,'observed',:observed,1,1,'initial_projection')"""), p)
        assert caught.value.orig.diag.message_primary == "orders_history_proof_input"


def test_dedicated_role_rejects_parent_update_before_derived_membership(prepared):
    with guarded(prepared) as (session, handle):
        p = observation(session, handle, staged(session, handle))
        with pytest.raises(DBAPIError) as caught:
            session.execute(text("UPDATE marketplace_orders SET version=version+1,last_seen_sync_run_id=:run "
                "WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order"), p)
        assert caught.value.orig.sqlstate == "42501"


def test_real_membership_derives_initial_prestate_not_caller_values(prepared):
    with guarded(prepared) as (session, handle):
        p = observation(session, handle, staged(session, handle))
        row = session.execute(text("""INSERT INTO order_sync_memberships
 (organization_id,marketplace_account_id,sync_run_id,order_id,observation_id,coverage_role,observed_at)
 VALUES(:org,:account,:run,:order,:observation,'observed',:observed)
 RETURNING history_decision_version,history_pre_order_version,history_pre_sync_run_id,history_pre_observation_id,history_outcome"""), p).one()
        assert tuple(row) == (1, 1, None, None, "initial_projection")


def test_initial_parent_helper_projects_only_derived_membership_values(prepared):
    with guarded(prepared) as (session, handle):
        assert session.scalar(text("SELECT to_regprocedure('public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)')")) is not None, "Narrow initial parent helper is missing"
        p = observation(session, handle, staged(session, handle))
        session.execute(text("""INSERT INTO order_sync_memberships
 (organization_id,marketplace_account_id,sync_run_id,order_id,observation_id,coverage_role,observed_at)
 VALUES(:org,:account,:run,:order,:observation,'observed',:observed)"""), p)
        version = session.scalar(text("SELECT public.wb_history_projection_apply_parent(:org,:account,:run,:order)"), p)
        assert version == 2
        row = session.execute(text("SELECT version,last_seen_sync_run_id,raw_status,canonical_status,mapping_state,mapping_version,source_updated_at "
            "FROM marketplace_orders WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order"), p).one()
        expected = handle._plan.rows[0].observation
        assert tuple(row) == (2, p["run"], expected.status.raw_status, expected.status.canonical_status,
            expected.status.mapping_state, expected.status.mapping_version, expected.effective_at)


def test_status_with_pinned_role_returns_original_job(prepared):
    d, service, claim = prepared
    status = service.status(authenticated_actor=d.actor, locator=claim.locator)
    assert status.job_id == claim.locator.job_id and status.state == "running"


def test_status_rejects_substituted_owner_factory(prepared):
    from app.platform.integrations.user_orders_job_contract import OrdersJobError

    d, service, claim = prepared
    wrong = WbHistoryProjectionJobs(session_factory=sessionmaker(d.engine, expire_on_commit=False),
        execution_policy=service._dependencies.execution_policy,
        authority_deadline_provider=service._dependencies.authority_deadline_provider,
        max_selection_pages=service._dependencies.max_selection_pages, role_identity=service._role_identity)
    # Disposable owner is used only as a negative substituted connection, never
    # accepted as positive projection authority.
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        wrong.status(authenticated_actor=d.actor, locator=claim.locator)


def test_duplicate_claim_rechecks_late_acl_drift_before_physical_commit(prepared):
    from app.platform.integrations.user_orders_job_contract import OrdersJobError

    d, service, claim = prepared
    observed = []
    role = service._role_identity.runtime_name
    def drift(session):
        with d.engine.begin() as connection:
            connection.exec_driver_sql(f'GRANT UPDATE(version) ON public.marketplace_orders TO "{role}"')
            assert connection.scalar(text("SELECT has_column_privilege(:role,'public.marketplace_orders','version','UPDATE')"), {"role": role})
        observed.append("effective-late-grant")
    def factory():
        session = d.factory()
        event.listen(session, "before_commit", drift, once=True)
        return session
    fenced = WbHistoryProjectionJobs(session_factory=factory,
        execution_policy=service._dependencies.execution_policy,
        authority_deadline_provider=service._dependencies.authority_deadline_provider,
        max_selection_pages=service._dependencies.max_selection_pages, role_identity=service._role_identity)
    rejected = None
    try:
        try:
            fenced.claim(organization_id=d.org, marketplace_account_id=d.org, job_id=claim.locator.job_id)
        except OrdersJobError as error:
            rejected = error
    finally:
        with d.engine.begin() as connection:
            connection.exec_driver_sql(f'REVOKE UPDATE(version) ON public.marketplace_orders FROM "{role}"')
    assert observed == ["effective-late-grant"]
    with d.engine.connect() as connection:
        actual = connection.execute(text("SELECT state,version,history_progress_version FROM user_orders_jobs "
            "WHERE organization_id=:org AND marketplace_account_id=:org AND job_id=:job"),
            {"org": d.org, "job": claim.locator.job_id}).one()
        assert tuple(actual) == ("running", claim.expected_job_version, 0)
        assert connection.scalar(text("SELECT count(*) FROM user_orders_job_audit WHERE organization_id=:org "
            "AND marketplace_account_id=:org AND job_id=:job AND event_kind='job.chunk_committed'"),
            {"org": d.org, "job": claim.locator.job_id}) == 0
    assert rejected is not None, "Duplicate claim committed after effective late ACL drift"
    assert rejected.code == "JOB_FENCE_INVALID" and rejected.__context__ is None


@pytest.mark.parametrize("method", ["readback", "close_denied"])
@pytest.mark.parametrize("terminal", [False, True])
def test_generic_housekeeping_rejects_history_without_inventing_authority(prepared, method, terminal):
    from app.platform.integrations.user_orders_job_contract import OrdersJobError

    d, service, claim = prepared
    if terminal:
        service._jobs.cancel(authenticated_actor=d.actor, locator=claim.locator)
    with pytest.raises(OrdersJobError, match="^SOURCE_CONTRACT_UNAVAILABLE$"):
        getattr(service._jobs, method)(organization_id=d.org, marketplace_account_id=d.org, job_id=claim.locator.job_id)


def test_genuine_publication_cannot_commit_without_sealed_chunk_baseline(prepared):
    from app.platform.integrations.user_orders_job_contract import OrdersJobError

    d, service, claim = prepared
    with d.factory() as session:
        session.begin()
        service._jobs.acquire_publication_guard(session, claim=claim)
        with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
            session.commit()
        session.rollback()


def test_genuine_capture_and_role_reject_version_only_pre_membership_tamper(prepared):
    with guarded(prepared) as (session, handle):
        p = observation(session, handle, staged(session, handle))
        _locator, _plan, baseline = handle._base._history_baseline
        assert baseline == ((p["external"], None, None, None, None),)
        # The former broad fixture reached baseline comparison; the real role
        # now blocks this semantic mutation before any membership can capture it.
        with pytest.raises(DBAPIError) as caught:
            session.execute(text("UPDATE marketplace_orders SET version=version+1 "
                "WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order"), p)
        assert caught.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("field", ["mapping_version", "evidence_source"])
def test_semantic_validator_rejects_null_status_contract_not_sql_unknown(prepared, field):
    with guarded(prepared) as (session, handle):
        payload = handle.captured_rows()[0]["observation"]
        payload["observation"]["status"][field] = None
        with pytest.raises(DBAPIError) as caught:
            session.execute(text("SELECT wb_history_projection_semantic(CAST(:payload AS jsonb))"), {"payload": json.dumps(payload)})
        assert caught.value.orig.diag.message_primary == "orders_history_semantic_invalid"


@pytest.mark.parametrize("kind,code", [("unexpected", "JOB_PERSISTENCE_FAILED"), ("domain", "JOB_CONFLICT")])
def test_real_publish_boundary_discards_error_chain_and_rolls_back(prepared, kind, code):
    from app.modules.orders import OrderContractValidationError
    from app.platform.integrations.user_orders_job_contract import OrdersJobError

    d, service, claim = prepared
    handles = []
    def participant(session, *, handle):
        handles.append(handle)
        staged(session, handle)
        if kind == "domain":
            raise OrderContractValidationError("synthetic-secret-marker-never-return")
        raise RuntimeError("synthetic-secret-marker-never-return")

    with pytest.raises(OrdersJobError) as caught:
        service.publish_chunk(claim=claim, participant=participant)
    assert caught.value.code == code
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert "synthetic-secret-marker" not in repr(caught.value)
    assert len(handles) == 1 and handles[0]._base._guard._failed
    with d.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM order_sync_runs WHERE organization_id=:org"), {"org": d.org}) == 0
        row = connection.execute(text("SELECT state,history_progress_version FROM user_orders_jobs WHERE job_id=:job"),
            {"job": claim.locator.job_id}).one()
        assert tuple(row) == ("running", 0)
