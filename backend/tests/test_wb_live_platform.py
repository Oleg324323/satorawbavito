"""Focused current-head integration. Only synthetic tokens and owned PostgreSQL."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session

from app.cabinet import store as cabinet
from app.cabinet.orm import LkSessionRow
from app.config import Settings
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.security.marketplace_credentials import CredentialKeyring
from app.wb_live.contracts import JobLocator, WbLiveError
from app.wb_live.connection import WbAccountConnection
from app.wb_live.repository import WbLiveRepository, _rows
from app.wb_live.orm import WbLiveSyncSourceRow as Source, WbLiveProductRow as Product
from app.wb_live.router import ConnectRequest
from tests import test_orders_schema_candidate as candidate
from tests import test_marketplace_credential_fetch_postgres as fetch_tests
from tests import test_publication_guard_postgres as guard_tests

cluster = candidate.cluster
pg_store = fetch_tests.pg_store
data = guard_tests.data

@pytest.fixture(scope="module")
def pg_database(cluster):
    role = "wb_live_test_" + uuid4().hex
    worker, dispatcher, api_role = ("wb_live_worker_" + uuid4().hex, "wb_live_dispatch_" + uuid4().hex, "wb_live_api_" + uuid4().hex)
    with candidate.disposable_database(cluster, (role, worker, dispatcher, api_role)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260910_0080")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION wb_live_due_jobs(integer) TO {role}")
                sql = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
                sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))
                for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                    ("wb_live_worker", worker), ("wb_live_dispatch", dispatcher), ("wb_live_api", api_role)):
                    sql = sql.replace(old, new)
                c.execute(text(sql))
            runtime._wb_live_worker_role = worker
            runtime._wb_live_api_role = api_role
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()

@pytest.fixture
def live(data):
    d = data
    with d.engine.begin() as c:
        c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            permissions=["integrations:write", "catalog:read", "cabinet:read", "sync:read"]))
    d.actor = ActorContext(actor_id=f"owner-{d.org}", user_id=f"owner-{d.org}", organization_id=d.org,
        permission_profile="custom", permissions=frozenset({"integrations:write"}), session_id=f"login-{d.org}")
    d.keys = lambda: CredentialKeyring(current_key_version=7, keys={7: b"a" * 32})
    d.repo = WbLiveRepository(d.factory, d.keys)
    return d

def start(d, key="synthetic-request-1"):
    view = d.repo.create_job(d.actor, d.org, key)
    return JobLocator(d.org, d.org, view["jobId"])

def content():
    return [{"nmId": 9223372036854000, "vendorCode": "zero-price", "title": "Synthetic item", "brand": None,
        "subjectId": None, "subjectName": None, "photoUrl": None,
        "sourceUpdatedAt": "2026-09-10T12:00:00+00:00", "sizes": [{"chrtId": 1001, "techSize": "M", "skus": ["synthetic-sku"]}]}]

def commit(d, lease, *, complete=True):
    rows = content() if lease.source == "content" else [{"nmId": 9223372036854000,
        "discountPct": 0, "clubDiscountPct": None, "sizes": [{"chrtId": 1001,
            "priceKopecks": 0, "discountedPriceKopecks": None, "clubPriceKopecks": None}]}]
    checkpoint = {"updatedAt": "2026-09-10T12:00:00+00:00", "nmID": 9223372036854000} if lease.source == "content" else {"offset": 1}
    return d.repo.commit_batch(lease, rows=rows, next_checkpoint=checkpoint, complete=complete,
        page_digest="a" * 64, next_due_at=datetime.now(timezone.utc))

def test_intent_replay_dedup_and_atomic_projection(live):
    d = live
    locator = start(d)
    assert start(d) == locator
    assert start(d, "another-request-key") == locator
    assert locator in d.repo.due_jobs()
    first = d.repo.claim_batch(locator)
    assert first.source == "content"
    assert d.repo.resolve_for_fetch(first).secret.reveal()["token"] == "synthetic-guard"
    assert commit(d, first)
    assert not commit(d, first)  # duplicate delivery cannot publish/count twice
    second = d.repo.claim_batch(locator)
    assert second.source == "prices"
    assert commit(d, second)
    view = d.repo.status(d.actor, d.org)
    assert view["state"] == "completed"
    assert [r["processed"] for r in view["sources"]] == [1, 1]
    with d.engine.connect() as c:
        row = c.execute(text("SELECT price_kopecks,discounted_price_kopecks,club_price_kopecks FROM wb_live_product_sizes WHERE organization_id=:o"), {"o": d.org}).one()
        assert tuple(row) == (0, None, None)
        assert c.scalar(text("SELECT count(*) FROM wb_live_pages WHERE organization_id=:o"), {"o": d.org}) == 2
        assert c.scalar(text("SELECT min(revision) FROM wb_live_sync_sources WHERE organization_id=:o"), {"o": d.org}) == 1

def test_expired_lease_is_recovered_with_cas(live):
    d = live
    locator = start(d)
    old = d.repo.claim_batch(locator)
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "content").values(
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    new = d.repo.claim_batch(locator)
    assert new.source == old.source and new.lease_token != old.lease_token
    assert not commit(d, old)
    assert commit(d, new)

def test_background_read_survives_logout_but_not_current_permission_loss(live):
    d = live
    locator = start(d)
    with d.engine.begin() as c:
        c.execute(update(LkSessionRow).where(LkSessionRow.session_id == d.actor.session_id).values(
            revoked_at=datetime.now(timezone.utc)))
    lease = d.repo.claim_batch(locator)
    assert lease is not None
    assert d.repo.resolve_for_fetch(lease).secret.reveal()["token"] == "synthetic-guard"
    assert commit(d, lease)
    with d.engine.begin() as c:
        c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(permissions=[]))
    assert d.repo.claim_batch(locator) is None
    with d.engine.connect() as c:
        assert c.scalar(text("SELECT state FROM wb_live_sync_jobs WHERE job_id=:j"), {"j": locator.job_id}) == "failed"

def test_rotation_after_fetch_blocks_publication(live):
    d = live
    locator = start(d)
    lease = d.repo.claim_batch(locator)
    d.repo.resolve_for_fetch(lease)
    from app.platform.integrations.credential_store import put_marketplace_credential
    put_marketplace_credential(d.owner, "wb_api", {"token": "synthetic-replacement"})
    with pytest.raises(WbLiveError, match="WB_BINDING_CHANGED"):
        commit(d, lease)
    with d.engine.connect() as c:
        assert c.scalar(select(Product.nm_id).where(Product.organization_id == d.org)) is None

def test_existing_synthetic_reencryption_is_not_blocked_by_historical_job_fk(live):
    from app.platform.integrations.credential_store import reencrypt_credential
    d = live
    locator = start(d)
    lease = d.repo.claim_batch(locator)
    new = reencrypt_credential(d.credential.credential_id, d.credential.generation, 8, account_identity=d.owner)
    assert new.generation == d.credential.generation + 1
    with pytest.raises(WbLiveError, match="WB_BINDING_CHANGED"):
        d.repo.resolve_for_fetch(lease)
    assert d.repo.claim_batch(locator) is None

def test_partial_source_failure_preserves_healthy_refresh_and_read_only_status(live):
    d = live
    locator = start(d)
    commit(d, d.repo.claim_batch(locator))
    failed = d.repo.claim_batch(locator)
    assert failed.source == "prices"
    assert d.repo.fail_batch(failed, error_code="WB_ACCESS_DENIED")
    assert d.repo.status(d.actor, d.org)["state"] == "partial"
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "content").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert locator in d.repo.due_jobs()
    assert d.repo.claim_batch(locator).source == "content"
    with d.engine.begin() as c:
        c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            permissions=["sync:read"], scope_mode="selected", allowed_account_ids=[d.org]))
    view = d.repo.status(d.actor, d.org)
    assert next(r for r in view["sources"] if r["source"] == "prices")["errorCode"] == "WB_ACCESS_DENIED"
    with pytest.raises(WbLiveError, match="WB_ACCESS_DENIED"):
        start(d, "read-only-cannot-create")
    with pytest.raises(WbLiveError, match="WB_ACCESS_DENIED"):
        d.repo.status(d.actor, d.org + 1)

def test_explicit_retry_rearms_failed_source_with_durable_authority_and_locking(live):
    from concurrent.futures import ThreadPoolExecutor
    from app.platform.integrations.credential_store import reencrypt_credential
    d = live
    locator = start(d)
    commit(d, d.repo.claim_batch(locator))
    prices = d.repo.claim_batch(locator)
    commit(d, prices, complete=False)
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "prices").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    prices = d.repo.claim_batch(locator)
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "prices").values(attempt=8))
    assert d.repo.fail_batch(prices, error_code="WB_RETRY_EXHAUSTED")
    def persisted():
        with d.engine.connect() as c:
            return {r.source: dict(r._mapping) for r in c.execute(select(Source).where(
                Source.organization_id == d.org).order_by(Source.source))}
    before = persisted()
    assert start(d) == locator  # old idempotency key is readback, never a retry
    assert persisted() == before

    # Another currently permitted administrator must not substitute its principal
    # for the original durable read job, even though it can manage this account.
    other_user, other_login = f"replacement-{d.org}", f"retry-other-{d.org}"
    with Session(d.engine) as s, s.begin():
        s.add(IamMembershipRow(membership_id=d.org + 400000, organization_id=d.org, user_id=other_user,
            role="custom", permissions=["integrations:write"], scope_mode="all", allowed_account_ids=[], is_active=True))
        s.add(LkSessionRow(session_id=other_login, user_id=other_user, issued_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            refresh_token_hash=uuid4().hex))
    other = replace(d.actor, actor_id=other_user, user_id=other_user, session_id=other_login)
    with pytest.raises(WbLiveError, match="WB_ACCESS_DENIED"):
        d.repo.create_job(other, d.org, "other-initiator-retry")
    assert persisted() == before

    assert start(d, "explicit-new-retry") == locator
    after = persisted()
    assert after["content"] == before["content"]
    assert after["prices"]["state"] == "queued" and after["prices"]["attempt"] == 0
    assert all(after["prices"][key] is None for key in ("error_code", "lease_token", "lease_expires_at"))
    for field in ("checkpoint", "run_id", "processed", "revision", "next_due_at"):
        assert after["prices"][field] == before["prices"][field]
    # Two physical transactions both observe already rearmed work; neither
    # resets its state/timestamp again. The account/job/source locks serialize them.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: start(d, key), ("concurrent-retry-one", "concurrent-retry-two")))
    assert results == [locator, locator]
    assert persisted() == after

    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "prices").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    running = d.repo.claim_batch(locator)
    assert running.source == "prices" and running.attempt == 1
    active = persisted()
    assert start(d, "already-running-retry") == locator
    assert persisted() == active
    with d.engine.begin() as c:
        c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(permissions=[]))
    with pytest.raises(WbLiveError, match="WB_ACCESS_DENIED"):
        start(d, "revoked-membership-retry")
    assert persisted() == active
    with d.engine.begin() as c:
        c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(permissions=["integrations:write"]))
    reencrypt_credential(d.credential.credential_id, d.credential.generation, 8, account_identity=d.owner)
    with pytest.raises(WbLiveError, match="WB_BINDING_CHANGED"):
        start(d, "stale-generation-retry")
    assert persisted() == active

def test_short_source_refresh_is_independent_and_whole_job_refresh_retains_replay(live):
    d = live
    locator = start(d)
    lease = d.repo.claim_batch(locator)
    commit(d, lease)
    prices = d.repo.claim_batch(locator)
    commit(d, prices, complete=False)
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "content").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    refresh = d.repo.claim_batch(locator)
    assert refresh.source == "content" and refresh.run_id != lease.run_id
    assert refresh.checkpoint == {"updatedAt": "2026-09-10T12:00:00+00:00", "nmID": 9223372036854000}
    commit(d, refresh)
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "prices").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    commit(d, d.repo.claim_batch(locator))
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == "content").values(
            next_due_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    successor = d.repo.claim_batch(locator)
    assert successor.locator.job_id != locator.job_id
    assert start(d) == locator

def test_verified_connection_encrypted_and_rechecked_after_io(live):
    d = live
    seller = str(uuid4())
    service = WbAccountConnection(session_factory=d.factory, keyring_loader=d.keys, seller_verifier=lambda _: seller)
    account, credential = service.connect(d.actor, token="synthetic-new-seller-key", display_name="My shop")
    assert account["externalAccountId"] == seller and account["displayName"] == "My shop"
    assert account in service.list_accounts(d.actor)
    again, replacement = service.connect(d.actor, token="synthetic-new-seller-key", display_name="My shop")
    assert again["marketplaceAccountId"] == account["marketplaceAccountId"]
    assert replacement.generation == credential.generation + 1
    with d.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM lk_user_wb_tokens WHERE wb_token='synthetic-new-seller-key'")) == 0
    def revoke(_):
        with d.engine.begin() as c:
            c.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(is_active=False))
        return str(uuid4())
    service._verify = revoke
    with pytest.raises(WbLiveError, match="WB_ACCESS_DENIED"):
        service.connect(d.actor, token="synthetic-revoked-key")

def test_current_head_forced_rls_no_context_denies_rows(live):
    d = live
    start(d)
    with d.factory() as s:
        assert s.scalar(text("SELECT count(*) FROM wb_live_sync_jobs")) == 0
        assert s.scalar(text("SELECT count(*) FROM wb_live_products")) == 0
        s.execute(text("SELECT set_config('app.organization_id',:o,true),set_config('app.marketplace_account_id',:a,true)"),
            {"o": str(d.org), "a": str(d.org + 1)})
        assert s.scalar(text("SELECT count(*) FROM wb_live_sync_jobs")) == 0

def test_live_registration_login_are_postgres_only(pg_database, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.control_plane import auth
    owner, runtime = pg_database
    runtime = create_engine(runtime.url.set(username=runtime._wb_live_api_role), hide_parameters=True)
    factory = sessionmaker(runtime, expire_on_commit=False)
    settings = replace(Settings(), wb_live_sync_enabled=True)
    monkeypatch.setattr(cabinet, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(cabinet, "get_engine", lambda: runtime)
    monkeypatch.setattr(cabinet, "get_session_factory", lambda: factory)
    email = uuid4().hex + "@example.invalid"
    memory_before = len(cabinet._MEMORY.users)
    org, user = cabinet.register_organization_owner(email=email, password_hash=auth.hash_password("SyntheticPassword99"), full_name="Synthetic", company_name=uuid4().hex)
    record = cabinet.get_user_auth_record_by_email(email)
    assert record.organization_id == org.organizationId
    login = cabinet.create_session_for_user(user=record, refresh_ttl_seconds=3600, refresh_token_hash=uuid4().hex, user_agent=None, ip_address=None)
    assert cabinet.resolve_active_session(session_id=login.session_id, user_id=user.userId)[0].email == email
    assert cabinet.get_user_auth_record_by_email("missing@example.invalid") is None
    assert len(cabinet._MEMORY.users) == memory_before
    with owner.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM lk_users WHERE email LIKE '%@vella.local'")) == 0
        assert c.scalar(text("SELECT count(*) FROM iam_memberships WHERE organization_id=:o AND user_id=:u"), {"o": org.organizationId, "u": user.userId}) == 1
    from sqlalchemy.exc import OperationalError
    monkeypatch.setattr(cabinet, "get_engine", lambda: (_ for _ in ()).throw(OperationalError("synthetic", {}, None)))
    with pytest.raises(HTTPException) as error:
        cabinet.get_user_auth_record_by_email(email)
    assert error.value.status_code == 503 and len(cabinet._MEMORY.users) == memory_before
    runtime.dispose()

def test_actual_worker_column_grants_allow_reads_not_authority_mutations(live):
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.exc import DBAPIError
    d = live
    locator = start(d)
    engine = d.factory.kw["bind"]
    restricted = create_engine(engine.url.set(username=engine._wb_live_worker_role), hide_parameters=True)
    try:
        repo = WbLiveRepository(sessionmaker(restricted, expire_on_commit=False), d.keys)
        assert locator in repo.due_jobs()
        lease = repo.claim_batch(locator)
        assert repo.resolve_for_fetch(lease).secret.reveal()["token"] == "synthetic-guard"
        original = d.repo
        d.repo = repo
        assert commit(d, lease)
        d.repo = original
        for sql in ("UPDATE iam_memberships SET permissions='[]'", "UPDATE iam_memberships SET role='admin'",
            "UPDATE marketplace_accounts SET status='connected'", "UPDATE marketplace_account_credentials SET generation=99",
            "UPDATE marketplace_account_credentials SET ciphertext=decode('00','hex')", "UPDATE lk_sessions SET revoked_at=NULL"):
            with restricted.begin() as c:
                with pytest.raises(DBAPIError) as e:
                    c.exec_driver_sql(sql)
                assert e.value.orig.sqlstate == "42501"
    finally:
        restricted.dispose()

@pytest.mark.parametrize("payload", [{"synthetic-secret-as-key": "secret"}, {"wbToken": "short"}, {"wbToken": "synthetic-token", "displayName": "x" * 129}])
def test_connect_validation_never_reflects_unknown_keys(payload):
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as e:
        ConnectRequest.model_validate(payload)
    assert "synthetic-secret-as-key" not in str(e.value)
    assert all(row["loc"] == () for row in e.value.errors(include_input=False, include_context=False))

@pytest.mark.parametrize("value", [True, -1, 1.5, "12"])
def test_projection_rejects_non_integer_money(value):
    with pytest.raises(WbLiveError, match="WB_RESPONSE_INVALID"):
        _rows("prices", [{"nmId": 1, "sizes": [{"chrtId": 1, "priceKopecks": value}]}])

def test_live_legacy_token_routes_and_registration_stop_before_side_effects(monkeypatch):
    from app.routers import cabinet as routes
    from app.control_plane import auth
    settings = replace(Settings(), wb_live_sync_enabled=True)
    def forbidden(*args, **kwargs):
        pytest.fail("Legacy validation, persistence or authentication was reached")
    for module in (routes, cabinet, auth):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for name in ("_require_permission", "_validate_wb_token_for_user_request", "upsert_user_wb_token", "delete_user_wb_token"):
        monkeypatch.setattr(routes, name, forbidden)
    monkeypatch.setattr(cabinet, "_run_db", forbidden)
    monkeypatch.setattr(auth, "ensure_defaults", forbidden)
    for call in (lambda: routes.put_my_wb_token(None, SimpleNamespace(wbToken="synthetic-key")),
        lambda: routes.delete_my_wb_token(None),
        lambda: cabinet.delete_user_wb_token(user_id="x", organization_id=1, actor_user_id="x"),
        lambda: auth.register_with_email_password(email="x@example.invalid", password="synthetic", full_name="X",
            company_name="X", wb_token="synthetic-key", user_agent=None, ip_address=None)):
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 409
        assert caught.value.detail == {"code": "WB_USE_ACCOUNT_CONNECTION"}
