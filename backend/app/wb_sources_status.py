"""Read-only, account-bound evidence for the WB Sources screen. Never syncs."""
from datetime import date, datetime, timezone
from hashlib import sha256

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from app.infra.db import get_session_factory, set_tenant_context
from app.platform.finance.access import require_wb_account_scope
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.wb_credentials import WbCredentialBindingError, resolve_bound_wb_credential
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.repricer_source_provenance import source_binding_matches, source_observations_fresh, source_timestamp

SOURCE_IDS = ('one-c', 'dds-rules', 'baskets', 'ads', 'stocks', 'prices', 'plans', 'assignments', 'excel')
META_FIELDS = ('organizationId', 'marketplaceAccountId', 'strategyTokenFingerprint', 'strategySourceBinding',
    'pnlAdvertisingContext', 'pnlCampaignReceiptVersion', 'source', 'sourceComplete', 'aggregateComplete',
    'coverageState', 'missingDates', 'coveredDays', 'requestedDays', 'count', 'dateFrom', 'dateTo',
    'fetchedAt', 'oldestFetchedAt', 'oldestSourceFetchedAt', 'state')


def receipt_status(payload, *, now):
    """Describe saved evidence, not overall account/report readiness."""
    observed = source_timestamp(payload.get('fetchedAt'))
    result = dict(status='unknown', observedAt=observed.isoformat() if observed else None,
        dateFrom=payload.get('dateFrom'), dateTo=payload.get('dateTo'),
        count=payload.get('count') if type(payload.get('count')) is int and payload['count'] >= 0 else None,
        detail='Нет подтверждённых данных о состоянии источника.')
    if observed is None or observed > now:
        return result
    if payload.get('state') in ('failed', 'error'):
        return {**result, 'status': 'failed', 'detail': 'Сохранена ошибка загрузки. Откройте связанный раздел.'}
    if payload.get('state') in ('queued', 'running', 'loading'):
        return {**result, 'status': 'loading', 'detail': 'Сохранено незавершённое состояние загрузки.'}
    if (payload.get('sourceComplete') is False or payload.get('aggregateComplete') is False
            or payload.get('coverageState') not in (None, 'ok') or payload.get('missingDates')
            or (type(payload.get('coveredDays')) is int and type(payload.get('requestedDays')) is int
                and payload['coveredDays'] < payload['requestedDays'])):
        return {**result, 'status': 'partial', 'detail': 'Сохранённые данные неполные.'}
    if result['count'] is None and payload.get('sourceComplete') is not True and payload.get('aggregateComplete') is not True:
        return result
    try:
        end = date.fromisoformat(payload['dateTo'])
        if payload.get('dateFrom') is not None and date.fromisoformat(payload['dateFrom']) > end:
            return result
    except (ValueError, TypeError, KeyError):
        return result
    fresh = source_observations_fresh(payload, now=now)
    return {**result, 'status': 'ready' if fresh else 'stale',
        'detail': 'Сохранённые данные актуальны по правилам источника.' if fresh else 'Сохранённые данные требуют обновления.'}


def _read_receipts(session, org, *, predicate):
    row = WbRepricerSourceCacheRow
    timestamp_present = (cast(row.payload, JSONB).has_key('fetchedAt') if session.bind.dialect.name == 'postgresql'
        else func.json_type(row.payload, '$.fetchedAt').is_not(None))
    # Do not deserialize product arrays, operations or daily ledgers for a status page.
    fields = [(row.payload[key].as_boolean() if key in ('sourceComplete', 'aggregateComplete')
        else row.payload[key]).label(key) for key in META_FIELDS]
    records = session.execute(select(row.fetched_at, timestamp_present, *fields).where(
            row.organization_id == org, predicate).order_by(row.fetched_at.desc()).limit(32)).all()
    for record in records:
        payload = {key: value for key, value in zip(META_FIELDS, record[2:]) if value is not None}
        payload['fetchedAt'] = payload.get('fetchedAt') if record[1] else record[0].isoformat()
        yield payload


def read_sources_status(actor, account_id=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    items = {key: dict(id=key, status='unknown', observedAt=None, dateFrom=None, dateTo=None,
        count=None, detail='Нет подтверждённых данных о состоянии источника.') for key in SOURCE_IDS}
    with get_session_factory()() as session:
        set_tenant_context(session, actor.organization_id)
        accounts = session.scalars(select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == actor.organization_id,
            MarketplaceAccountRow.marketplace == 'wb')).all()
        candidates = [row for row in accounts if row.marketplace_account_id == account_id] if account_id is not None else [row for row in accounts if row.status == 'connected']
        if account_id is None and not candidates and len(accounts) == 1:
            candidates = accounts  # A single saved disconnected account is explicit evidence.
        if account_id is not None:
            require_wb_account_scope(session, actor, account_id)
        if len(candidates) != 1:
            return dict(marketplaceAccountId=None, items=list(items.values()),
                message='Не удалось однозначно определить подключённый аккаунт WB.')
        account = candidates[0]
        require_wb_account_scope(session, actor, account.marketplace_account_id)
        result = dict(marketplaceAccountId=account.marketplace_account_id, items=list(items.values()), message=None)
        if account.status != 'connected':
            for key in ('baskets', 'ads', 'stocks', 'prices'):
                items[key].update(status='disconnected', detail='Аккаунт WB не подключён.')
            return result
        try:
            _, _, secret = resolve_bound_wb_credential(session, actor.organization_id,
                marketplace_account_id=account.marketplace_account_id)
        except WbCredentialBindingError:
            result['message'] = 'Привязка источников к текущему аккаунту не подтверждена.'
            return result
        fingerprint = 'wb_' + sha256(secret.encode()).hexdigest()[:12]
        connection = sha256(secret.encode()).hexdigest()[:24]
        row = WbRepricerSourceCacheRow
        scoped_receipts = ((row.payload['strategyTokenFingerprint'].as_string() == fingerprint)
            | (row.payload['pnlAdvertisingContext']['connection'].as_string() == connection))
        for source, predicate in (('baskets', row.source_key.like('baskets_%')),
                                  ('stocks', row.source_key == 'stocks'),
                                  ('ads', row.source_key.like('pnl_ads_v2_%') | row.source_key.like('ads_%'))):
            for payload in _read_receipts(session, actor.organization_id, predicate=predicate & scoped_receipts):
                explicit_account = payload.get('marketplaceAccountId', account.marketplace_account_id)
                context = payload.get('pnlAdvertisingContext')
                bound = source_binding_matches(payload, organization_id=actor.organization_id, fingerprint=fingerprint)
                if source == 'ads' and isinstance(context, dict):
                    bound = (context.get('connection') == connection
                        and context.get('marketplaceAccountId') == account.marketplace_account_id
                        and context.get('bindingVersion') == account.ingestion_binding_version
                        and payload.get('pnlCampaignReceiptVersion') == 2)
                if (not bound or explicit_account != account.marketplace_account_id
                        or payload.get('organizationId', actor.organization_id) != actor.organization_id
                        or source == 'ads' and payload.get('source') != 'wb_ads_fullstats'):
                    continue
                items[source].update(receipt_status(payload, now=now))
                break
        # Reuse the extension's account/credential/observation validation. Its
        # retained observations do not expire merely because collection stopped.
        from app.routers.wb_browser_prices import _status
        try:
            prices = _status(session, account, now)
        except (SQLAlchemyError, ValueError, TypeError, KeyError):
            items['prices'].update(status='failed', detail='Не удалось прочитать состояние наблюдений расширения.')
            return result
        if prices['configured']:
            items['prices'].update(status='ready' if prices['knownPrices'] else 'partial',
                observedAt=prices['lastObservedAt'], count=prices['knownPrices'],
                detail='Подтверждённые сохранённые цены расширения и СПП; не подтверждает готовность изменения цены.'
                    if prices['knownPrices'] else 'Расширение подключено; сохранённых наблюдений цен пока нет.')
        return result
