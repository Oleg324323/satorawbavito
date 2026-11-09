from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app import wb_sources_status as sources

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
ACTOR = SimpleNamespace(organization_id=2, user_id='synthetic')
SECRET = 'synthetic-not-a-wb-token'
FINGERPRINT = 'wb_' + sha256(SECRET.encode()).hexdigest()[:12]


@pytest.fixture
def saved(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    MarketplaceAccountRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(sources, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(sources, 'require_wb_account_scope', lambda session, actor, account: None)
    monkeypatch.setattr(sources, 'resolve_bound_wb_credential', lambda *a, **kw: (1, 'synthetic-ref', SECRET))
    from app.routers import wb_browser_prices
    monkeypatch.setattr(wb_browser_prices, '_status', lambda *a: dict(configured=False))
    with factory() as db:
        db.add(MarketplaceAccountRow(marketplace_account_id=1, organization_id=2, marketplace='wb',
            external_account_id='synthetic', status='connected', ingestion_binding_version=1))
        db.commit()
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda c, cur, sql, *a: statements.append(sql))
    def save(key, payload, org=2):
        with factory() as db:
            db.add(WbRepricerSourceCacheRow(organization_id=org, source_key=key, payload=payload, fetched_at=NOW))
            db.commit()
    yield save, statements, factory
    engine.dispose()


def test_status_uses_bound_evidence_and_never_writes_or_loads_ledgers(saved):
    save, statements, _ = saved
    save('stocks', dict(strategyTokenFingerprint=FINGERPRINT, count=0, dateTo='2026-11-05'))
    save('baskets_2026-10-05_2026-10-11', dict(strategyTokenFingerprint=FINGERPRINT,
        count=2, aggregateComplete=False, dateFrom='2026-10-05', dateTo='2026-10-11'))
    # Reject newer/foreign or unbound advertising evidence instead of inferring ownership.
    save('ads_2026-10-05_2026-10-11', dict(count=100, source='wb_ads_fullstats', dateTo='2026-10-11'))
    statements.clear()
    result = sources.read_sources_status(ACTOR, now=NOW)
    items = {row['id']: row for row in result['items']}
    assert items['stocks']['status'] == 'ready' and items['stocks']['count'] == 0
    assert items['baskets']['status'] == 'partial' and items['ads']['status'] == 'unknown'
    assert items['one-c']['status'] == 'unknown' and items['one-c']['observedAt'] is None
    assert 'synthetic-ref' not in str(result) and SECRET not in str(result) and FINGERPRINT not in str(result)
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements)
    assert not any('SELECT wb_repricer_source_cache.payload,' in sql for sql in statements)


@pytest.mark.parametrize('foreign', [dict(marketplaceAccountId=99), dict(organizationId=99), dict(strategyTokenFingerprint='foreign')])
def test_foreign_receipts_are_unknown(saved, foreign):
    save, _, _ = saved
    save('stocks', {**dict(strategyTokenFingerprint=FINGERPRINT, count=2, dateTo='2026-11-05'), **foreign})
    result = sources.read_sources_status(ACTOR, now=NOW)
    assert next(row for row in result['items'] if row['id'] == 'stocks')['status'] == 'unknown'


def test_null_timestamp_is_not_replaced_with_a_newer_receipt(saved):
    save, _, _ = saved
    save('stocks', dict(strategyTokenFingerprint=FINGERPRINT, fetchedAt=None, count=1, dateTo='2026-11-05'))
    assert next(row for row in sources.read_sources_status(ACTOR, now=NOW)['items'] if row['id'] == 'stocks')['status'] == 'unknown'


def test_scope_is_enforced_before_reading_source_metadata(saved, monkeypatch):
    _, statements, _ = saved
    def denied(*a):
        raise HTTPException(403, 'ACCOUNT_SCOPE_DENIED')
    monkeypatch.setattr(sources, 'require_wb_account_scope', denied)
    statements.clear()
    with pytest.raises(HTTPException) as error:
        sources.read_sources_status(ACTOR, 1, now=NOW)
    assert error.value.status_code == 403
    assert not any('wb_repricer_source_cache' in sql for sql in statements)


def test_ambiguous_account_and_disconnected_are_not_guessed(saved):
    _, _, factory = saved
    with factory() as db:
        db.get(MarketplaceAccountRow, 1).status = 'disconnected'
        db.commit()
    assert next(row for row in sources.read_sources_status(ACTOR, now=NOW)['items'] if row['id'] == 'stocks')['status'] == 'disconnected'
    with factory() as db:
        db.get(MarketplaceAccountRow, 1).status = 'connected'
        db.add(MarketplaceAccountRow(marketplace_account_id=3, organization_id=2, marketplace='wb',
            external_account_id='second', status='connected', ingestion_binding_version=1))
        db.commit()
    assert sources.read_sources_status(ACTOR, now=NOW)['marketplaceAccountId'] is None
    with factory() as db:
        db.get(MarketplaceAccountRow, 1).status = 'disconnected'
        db.commit()
    assert next(row for row in sources.read_sources_status(ACTOR, 1, now=NOW)['items'] if row['id'] == 'stocks')['status'] == 'disconnected'


@pytest.mark.parametrize('overrides,status', [({}, 'ready'), ({'sourceComplete': False}, 'partial'),
    ({'count': 0}, 'ready'), ({'fetchedAt': (NOW-timedelta(days=2)).isoformat()}, 'stale'),
    ({'fetchedAt': None}, 'unknown'), ({'state': 'failed'}, 'failed'),
    ({'state': 'running'}, 'loading'), ({'coveredDays': 1, 'requestedDays': 2}, 'partial')])
def test_receipt_states(overrides, status):
    payload = {**dict(count=1, fetchedAt=NOW.isoformat(), dateTo='2026-11-05'), **overrides}
    assert sources.receipt_status(payload, now=NOW)['status'] == status


def test_retained_extension_observations_do_not_acquire_a_new_ttl(saved, monkeypatch):
    from app.routers import wb_browser_prices
    monkeypatch.setattr(wb_browser_prices, '_status', lambda *a: dict(configured=True, knownPrices=2,
        lastObservedAt='2026-10-01T00:00:00Z', tokenPrefix='must-not-leak'))
    result = sources.read_sources_status(ACTOR, now=NOW)
    prices = next(row for row in result['items'] if row['id'] == 'prices')
    assert prices['status'] == 'ready' and prices['observedAt'].startswith('2026-10-01')
    assert 'must-not-leak' not in str(result)


def test_one_unavailable_reader_preserves_other_independent_sources(saved, monkeypatch):
    from app.routers import wb_browser_prices
    from sqlalchemy.exc import SQLAlchemyError
    save, _, _ = saved
    save('stocks', dict(strategyTokenFingerprint=FINGERPRINT, count=1, dateTo='2026-11-05'))
    def unavailable(*a):
        raise SQLAlchemyError('private database detail')
    monkeypatch.setattr(wb_browser_prices, '_status', unavailable)
    result = sources.read_sources_status(ACTOR, now=NOW)
    items = {item['id']: item for item in result['items']}
    assert items['stocks']['status'] == 'ready' and items['prices']['status'] == 'failed'
    assert 'private' not in str(result)


def test_route_requires_permission_before_reading_metadata(monkeypatch):
    from app.routers import wb_repricer_bff as router
    from app.control_plane.auth import ActorContext
    from starlette.requests import Request
    actor = ActorContext('synthetic', 'synthetic', 2, 'synthetic', frozenset())
    monkeypatch.setattr(router, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(sources, 'read_sources_status', lambda *a: pytest.fail('Unauthorized source read'))
    with pytest.raises(HTTPException) as denied:
        router.get_wb_sources_status(Request({'type': 'http'}), None)
    assert denied.value.status_code == 403
