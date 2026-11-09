"""Real token incarnation checks and the actual dormant router's JSON wire."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.platform.integrations import ingestion_tokens
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner
from app.platform.integrations.ingestion_api import (
    AuthenticatedIngestionTokens,
    CommittedIngestionAcknowledgement,
    TrustedIngestionPeerResolver,
    create_ingestion_router,
)
from app.platform.integrations.ingestion_limits import IngestionLimits, IngestionPolicy
from app.platform.integrations.ingestion_publication_guard import acquire_ingestion_publication_guard
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_marketplace_credential_fetch_postgres as fetch_tests
from tests import test_publication_guard_postgres as guard_tests
from tests.test_credential_maintenance_inert_postgres import cluster, pg_database  # noqa: F401

pg_store = fetch_tests.pg_store
data = guard_tests.data


@pytest.fixture
def bound_token(data, monkeypatch):
    d = data
    monkeypatch.setattr(ingestion_tokens, "get_session_factory", lambda: d.factory)
    d.ingestion_owner = MarketplaceAccountCredentialOwner(d.org, d.org + 100000, "avito")
    issued = ingestion_tokens.issue_ingestion_token(
        d.ingestion_owner, expires_at=datetime.now(UTC) + timedelta(minutes=10))
    d.bearer = issued.reveal()
    d.issued = issued.metadata
    return d


@pytest.mark.parametrize("field, replacement, original", [
    ("external_account_id", "replacement-seller", "avito-seller"),
    ("credential_ref", "replacement-ref", None),
    ("status", "disconnected", "connected"),
])
def test_token_cannot_reauthorize_restored_account_identity(bound_token, field, replacement, original):
    d = bound_token
    admission = ingestion_tokens.verify_ingestion_token(d.bearer)
    assert admission.token_id == d.issued.token_id
    assert admission.account_binding.binding_version == 1
    with d.factory() as session, session.begin():
        guard = acquire_ingestion_publication_guard(session, raw_bearer=d.bearer)
        assert guard.require_participation(session) is guard
        assert guard.binding_version == 1 and guard.owner == d.ingestion_owner

    for value in (replacement, original):
        with Session(d.engine) as session, session.begin():
            session.execute(update(MarketplaceAccountRow).where(
                MarketplaceAccountRow.marketplace_account_id == d.ingestion_owner.marketplace_account_id,
            ).values(**{field: value}))
        with pytest.raises(ingestion_tokens.IngestionTokenStoreError, match="^ingestion_token_invalid$"):
            ingestion_tokens.verify_ingestion_token(d.bearer)
        with d.factory() as session, session.begin():
            with pytest.raises(PublicationGuardError, match="^publication_authority_invalid$"):
                acquire_ingestion_publication_guard(session, raw_bearer=d.bearer)
            session.rollback()

    with Session(d.engine) as session:
        assert session.scalar(select(MarketplaceAccountRow.ingestion_binding_version).where(
            MarketplaceAccountRow.marketplace_account_id == d.ingestion_owner.marketplace_account_id)) == 3
    replacement_token = ingestion_tokens.issue_ingestion_token(
        d.ingestion_owner, expires_at=datetime.now(UTC) + timedelta(minutes=10))
    fresh = ingestion_tokens.verify_ingestion_token(replacement_token.reveal())
    assert fresh.token_id != admission.token_id
    assert fresh.account_binding.binding_version == 3


@pytest.mark.parametrize("run_id", [2**53 + 1, 2**63 - 1])
def test_http_acknowledgement_preserves_large_run_id_as_decimal_string(bound_token, run_id):
    d = bound_token
    policy = IngestionPolicy(
        approval_reference="synthetic-acceptance", approval_version=1,
        max_token_lifetime_seconds=600, max_body_bytes=1024,
        ip_bucket_capacity=10, ip_bucket_window_seconds=60,
        token_bucket_capacity=10, token_bucket_window_seconds=60,
        peer_policy_reference="synthetic-loopback", peer_policy_version=1,
    )
    service = AuthenticatedIngestionTokens(
        session_factory=d.factory, policy=policy, decoder_max_body_bytes=1024)
    counters, deliveries = [], []

    class FakeRedis:
        async def eval(self, _script, number_of_keys, key, capacity, window):
            counters.append((number_of_keys, key, capacity, window))
            return 1

    def decoder(payload, *, organization_id, marketplace_account_id, observed_at):
        assert payload == b'{"synthetic":true}'
        assert (organization_id, marketplace_account_id) == (d.org, d.org + 100000)
        assert observed_at.tzinfo is not None
        return (organization_id, marketplace_account_id)

    def fake_sink(*, raw_bearer, admission, envelope, observed_at):
        # This injected sink tests response serialization only. Actual domain
        # sink persistence/idempotency belongs to root integration acceptance.
        assert raw_bearer == d.bearer and admission.token_id == d.issued.token_id
        assert admission.account_binding.binding_version == 1
        assert envelope == (d.org, d.org + 100000) and observed_at.tzinfo is not None
        deliveries.append(admission.token_id)
        return CommittedIngestionAcknowledgement(run_id=run_id, state="partial", replayed=False)

    app = FastAPI()
    app.include_router(create_ingestion_router(
        token_service=service, limits=IngestionLimits(redis_client=FakeRedis(), policy=policy),
        trusted_peer=TrustedIngestionPeerResolver(
            policy_reference=policy.peer_policy_reference, policy_version=policy.peer_policy_version,
            resolve=lambda _request: "127.0.0.1"),
        decoder=decoder, decoder_max_body_bytes=1024, sink=fake_sink,
    ))
    with TestClient(app) as client:
        response = client.post("/ingestion/avito/browser-snapshots",
                               headers={"Authorization": f"Bearer {d.bearer}",
                                        "Content-Type": "application/json"},
                               content=b'{"synthetic":true}')
    assert response.status_code == 200, response.text
    assert response.json() == {"run_id": str(run_id), "state": "partial", "replayed": False}
    assert f'"run_id":"{run_id}"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert len(counters) == 2 and deliveries == [d.issued.token_id]
    assert d.bearer not in response.text
