"""Stock reports omit retired columns and do not load their dedicated sources."""
from types import SimpleNamespace
from app.routers import wb_reports_bff as reports


def test_stock_report_does_not_request_or_return_ktr_and_decisions(monkeypatch):
    requested = []
    def cache(org, key, **kwargs):
        requested.append(key)
        assert key not in {'ktr_table', 'stock_decision_rules'}
        return {'status': 'cached', 'data': []}
    monkeypatch.setattr(reports, 'get_source_cache', cache)
    monkeypatch.setattr(reports, 'get_source_cache_fetched_at', lambda *args: '2026-11-03T00:00:00Z')
    monkeypatch.setattr(reports, 'ensure_daily_stock_history', lambda *a, **kw: {})
    monkeypatch.setattr(reports, '_catalog_meta_by_nm', lambda org: {})
    monkeypatch.setattr(reports, '_period_cache', lambda *a, **kw: {'aggregates': {'123': {'localizationPct': 100}}})
    stock = SimpleNamespace(nm_id=123, chrt_id=1, warehouse_id=1, warehouse_name='WB', region_name='Center',
        quantity=10, in_way_to_client=2, in_way_from_client=1, available_units=11,
        was_out_of_stock=False, stock_type='wb', total_stock_units=10)
    snapshot = SimpleNamespace(stocks=[stock], orders=[], sales=[], financial_rows=[],
        logistics_cost_by_nm_kopecks={}, source_status='fresh')
    report = reports._build_stock_report_payload({'from':'2026-11-01','to':'2026-11-03'}, snapshot,
        organization_id=1, wb_token=None, read_only=True)
    assert 'local_orders_exact' in requested
    assert not {'ktrIndex', 'decision', 'decisionStatus'} & {c['key'] for c in report['columns']}
    assert not {'ktrIndex', 'decision', 'decisionStatus'} & report['rows'][0].keys()
    assert report['rows'][0]['wbStockUnits'] == 10
    assert report['rows'][0]['availableUnits'] == 11
    assert report['rows'][0]['localizationPct'] == 100
    assert 'КТР' not in report['rows'][0]['comment']
    assert all(item.get('title') != 'КТР' for item in report['managementCards'])
    assert not {'ktr_table','stock_decision_rules'} & {item.get('entity') for item in report['sourceCoverage']}
