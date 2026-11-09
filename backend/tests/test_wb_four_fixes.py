"""Repricer regression fixtures: no real account, socket or filesystem database."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from app import repricer_bff as bff, repricer_execution as execution
from app.routers import wb_repricer_bff as router, wb_reports_bff as reports
from app.repricer_persistence.orm import WbRepricerRuntimeStateRow
from tests.test_repricer_configuration_checkpoint import api
from tests.test_repricer_safety_regressions import ARTICLE, Provider, healthy_row, isolated, _collect_night


@pytest.mark.parametrize('missing', [('baskets',), ('ordersUnits',), ('baskets', 'ordersUnits')])
def test_night_without_a_window_does_not_read_daytime_signals(isolated, monkeypatch, missing):
    provider = Provider()
    row = healthy_row(provider)
    row['strategy'].update(id='night_price_mode', typedStrategyId='night_price_mode')
    row['analytics'].update({key: None for key in missing})
    row['settings']['nightMedianEnabled'] = True
    monkeypatch.setattr(execution, '_night_median_collecting_now', lambda: False)
    monkeypatch.setattr(execution, '_night_median_apply_payload', lambda *a: None)
    monkeypatch.setattr(execution, '_baskets_orders_trends', lambda *a: pytest.fail('Night must not use daytime trends'))
    result = execution._execute_single_sku(row, client=provider, actor_id='synthetic', actor_role='admin',
        organization_id=1, options=execution.StrategyExecuteOptions(createDrafts=False, autoApprove=False, applyPrices=False))
    assert result.status == 'skipped' and result.skipReason == 'night_mode_disabled'
    assert 'ждёт' in result.explanation and not result.draftId and not provider.calls


def test_valid_night_does_not_require_daytime_counts(isolated, monkeypatch):
    provider = Provider()
    row = _collect_night(monkeypatch, provider, [2, 8, 10])
    row['analytics'].update(nightBaskets=row['analytics']['baskets'], nightBasketsState='ok',
        nightBasketsObservedAt=row['analytics']['basketsObservedAt'], baskets=None, ordersUnits=None)
    monkeypatch.setattr(execution, '_baskets_orders_trends', lambda *a: pytest.fail('Night must not use daytime trends'))
    result = execution._execute_single_sku(row, client=provider, actor_id='synthetic', actor_role='admin',
        organization_id=1, options=execution.StrategyExecuteOptions(createDrafts=False, autoApprove=False, applyPrices=False))
    assert result.status in {'executed', 'blocked'} and not provider.uploads


@pytest.mark.parametrize('missing', [('baskets',), ('ordersUnits',), ('baskets', 'ordersUnits')])
def test_night_preview_uses_the_same_waiting_path(isolated, monkeypatch, missing):
    provider = Provider()
    row = healthy_row(provider)
    row['strategy'].update(id='night_price_mode', typedStrategyId='night_price_mode')
    row['analytics'].update({key: None for key in missing})
    monkeypatch.setattr(execution, '_night_median_collecting_now', lambda: False)
    monkeypatch.setattr(execution, '_night_median_apply_payload', lambda *a: None)
    result = execution.preview_repricer_strategies([ARTICLE], client=provider, actor_id='synthetic',
        actor_role='admin', organization_id=1, sku_rows=[row])
    assert result.items[0].skipReason == 'night_mode_disabled'
    assert not provider.calls


def test_saved_assignment_ack_survives_execution_failure_and_none_uses_its_revision(api, isolated, monkeypatch):
    original, actor, provider = api
    monkeypatch.setattr(router, '_build_repricing_client', lambda *a: provider)
    def fail(*a, **kw):
        raise RuntimeError('synthetic-sensitive-provider-detail')
    monkeypatch.setattr(router, 'execute_repricer_strategies', fail)
    client = TestClient(original.app, raise_server_exceptions=False)
    response = client.post('/api/v1/wb-repricer/strategy-assignments/bulk', json={
        'articleIds': [ARTICLE], 'strategyId': 'night_price_mode', 'executeAfterAssign': True})
    assert response.status_code == 200
    body = response.json()
    assert body['executionError']['code'] == 'STRATEGY_EXECUTION_FAILED'
    assert 'synthetic-sensitive' not in response.text and 'execution' not in body
    with isolated() as db:
        assert db.get(WbRepricerRuntimeStateRow, 1).state_payload['assignments'][ARTICLE]['strategyId'] == 'night_price_mode'
    cleared = client.post('/api/v1/wb-repricer/strategy-assignments/bulk', json={
        'articleIds': [ARTICLE], 'strategyId': 'none', 'executeAfterAssign': False,
        'expectedRevisions': body['assignmentRevisions']})
    assert cleared.status_code == 200 and cleared.json()['unassignedCount'] == 1
    conflict = client.post('/api/v1/wb-repricer/strategy-assignments/bulk', json={
        'articleIds': [ARTICLE], 'strategyId': 'night_price_mode', 'expectedRevisions': body['assignmentRevisions']})
    assert conflict.status_code == 409 and not provider.calls


@pytest.mark.parametrize('basis,phrase', [('current_wb_stock_units', 'текущему снимку'),
    ('historical_wb_stock_units', 'истории остатков'), ('unknown', 'не подтверждён')])
def test_storage_presentation_changes_neither_amounts_nor_other_warnings(basis, phrase):
    allocation = dict(basis=basis, complete=False, observedDates=[], stockObservedAt='2026-11-01T12:34:56Z')
    metadata = reports._financial_report_metadata(SimpleNamespace(storageAllocation=allocation))
    assert phrase in metadata['warning']
    if basis == 'current_wb_stock_units':
        assert '2026-11-01T12:34:56Z' in metadata['warning'] and 'История сохранена' not in metadata['warning']
    saved = dict(rows=[{'storageKopecks': 123, 'netProfitKopecks': -500}], storageAllocation=allocation,
        warning='Финансы неполные. Часть хранения распределена расчётно по историческим остаткам WB. История сохранена за даты: нет. Остальная сумма не распределена. Расходы 1С не подтверждены.')
    before = deepcopy(saved)
    fixed = reports._with_storage_presentation(saved)
    assert phrase in fixed['warning'] and 'Расходы 1С не подтверждены.' in fixed['warning']
    assert fixed['rows'] == before['rows'] and saved == before
    assert reports._with_storage_presentation(fixed) == fixed


@pytest.mark.parametrize('report_id', ['abc', 'pnl'])
@pytest.mark.parametrize('path', ['direct', 'latest'])
def test_saved_report_http_projection_normalizes_storage_without_rebuilding(monkeypatch, report_id, path):
    from starlette.requests import Request
    from app.control_plane.auth import ActorContext
    actor = ActorContext('synthetic', 'synthetic', 1, 'admin', frozenset({'settings:read', 'finance:read'}))
    monkeypatch.setattr(reports, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(reports, 'assert_permission_or_audit', lambda **kw: None)
    saved = {'report': dict(rows=[dict(storageKopecks=123, netProfitKopecks=-456)],
        storageAllocation=dict(basis='current_wb_stock_units', complete=True, observedDates=[], stockObservedAt='2026-11-01T00:00:00Z'),
        warning='Часть хранения распределена расчётно по историческим остаткам WB. История сохранена за даты: нет.'),
        'dateFrom': '2026-10-05', 'dateTo': '2026-10-11'}
    before = deepcopy(saved)
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: saved if key.startswith('reports_payload_') else {'state': 'completed'})
    monkeypatch.setattr(reports, '_latest_report_payload_cache', lambda **kw: (saved, date(2026, 10, 5), date(2026, 10, 11)))
    monkeypatch.setattr(reports, '_report_payload_cache_is_usable', lambda *a, **kw: True)
    monkeypatch.setattr(reports, '_report_payload_cache_meta', lambda *a, **kw: {'fresh': True})
    monkeypatch.setattr(reports, '_completed_report_job_from_cache', lambda *a, **kw: {'state': 'completed'})
    monkeypatch.setattr(reports, '_apply_report_rules_to_payload', lambda payload, *a, **kw: deepcopy(payload))
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (True, []))
    monkeypatch.setattr(reports, 'save_source_cache', lambda *a, **kw: pytest.fail('No presentation-only persistence'))
    monkeypatch.setattr(reports, 'build_abc_report', lambda *a, **kw: pytest.fail('No report rebuild'))
    if path == 'direct':
        result = reports.get_reports_by_id.__wrapped__(Request({'type': 'http'}), report_id,
            preset='custom', from_='2026-10-05', to='2026-10-11', groupBy='sku', source='financial')
    else:
        result = reports.get_reports_latest_cache.__wrapped__(Request({'type': 'http'}), report_id,
            preset='custom', from_='2026-10-05', to='2026-10-11', groupBy='sku', source='financial', brand=None, brand_options=False)
    assert result['rows'] == before['report']['rows']
    assert 'текущему снимку' in result['warning'] and 'историческим остаткам' not in result['warning']
    assert saved == before
