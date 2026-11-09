"""Known WB product results must not be discarded by optional 1C status."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from app.routers import wb_reports_bff as reports


def payload():
    def row(nm, profit, margin):
        values = dict(nmId=nm,label=f"WB {nm}",revenueKopecks=10000,
            overheadKopecks=100,netProfitKopecks=profit,marginPct=margin,taxKopecks=0)
        return SimpleNamespace(label=values['label'],marginPct=margin,
            model_dump=lambda **kw: deepcopy(values))
    return SimpleNamespace(blockerIds=['WB_PNL_TAX_UNCONFIRMED','tax_mapping_missing'],
        sourceStatus='partial',groupBy='sku', rows=[row(101,2000,20),row(102,-500,-5),row(103,None,None)],
        totals=SimpleNamespace(revenueKopecks=30000,netProfitKopecks=None,marginPct=None))


@pytest.mark.parametrize('status', ['not_loaded','pending','processing','failed','unavailable','disabled'])
def test_financial_wb_keeps_known_results_and_unknowns_without_1c(status):
    source = payload()
    result = reports._map_pnl_to_report_response(source, {'from':'2026-10-12','to':'2026-10-18'},
        {'status':status}, finance_allowed=True, financial_only=True)
    assert [r['netProfitKopecks'] for r in result['rows']] == [2000,-500,None]
    assert [r['marginPct'] for r in result['rows']] == [20,-5,None]
    assert result['rows'][0]['overheadKopecks'] == 100
    assert result['financialConfirmationStatus'] == 'pending'
    assert result['profitBasis'] == 'wb_financial_report'
    assert result['kpis'][1]['value'] == '—'  # not a fabricated full-report total
    assert source.rows[0].model_dump()['netProfitKopecks'] == 2000


@pytest.mark.parametrize('financial_only,allowed', [(False,True),(True,False),(False,False)])
def test_operational_and_permission_gates_are_preserved(financial_only,allowed):
    result = reports._map_pnl_to_report_response(payload(), {'from':'2026-10-12','to':'2026-10-18'},
        {'status':'pending'},finance_allowed=allowed,financial_only=financial_only)
    assert all(r['netProfitKopecks'] is None and r['marginPct'] is None for r in result['rows'])
    assert result['chart']['points'] == []


def test_financial_preview_does_not_reuse_operational_hidden_results(monkeypatch):
    monkeypatch.setattr(reports, '_saved_preview_revision', lambda *args: 'fixed')
    calls = []

    def build(**kwargs):
        calls.append(kwargs['source'])
        return reports._map_pnl_to_report_response(payload(), {}, {'status':'pending'},
            finance_allowed=True, financial_only=kwargs['source'] == 'financial')

    monkeypatch.setattr(reports, '_saved_report_preview_uncached', build)
    monkeypatch.setattr(reports, '_saved_previews', reports.OrderedDict())
    args = dict(actor=SimpleNamespace(organization_id=71), report_id='pnl',
        date_from=date(2026,10,12), date_to=date(2026,10,18), group_by='sku', finance_allowed=True)
    operational = reports._saved_report_preview(**args, source='operational')
    financial = reports._saved_report_preview(**args, source='financial')
    repeated = reports._saved_report_preview(**args, source='financial')
    assert operational['rows'][0]['netProfitKopecks'] is None
    assert financial['rows'][0]['netProfitKopecks'] == repeated['rows'][0]['netProfitKopecks'] == 2000
    assert calls == ['operational','financial']


def test_worker_publishes_financial_preview_with_its_source_mode(monkeypatch):
    from app import repricer_tasks as tasks
    calls, saved = [], []
    monkeypatch.setattr(reports, '_saved_report_preview',
        lambda **kwargs: calls.append(kwargs) or {'rows':[{'netProfitKopecks':2000}]})
    monkeypatch.setattr(reports, '_save_exact_report_payload_cache', lambda **kwargs: saved.append(kwargs))
    # The tested function is the cache-only publication boundary, not tax/DB I/O.
    tasks._publish_loading_report_snapshot.__wrapped__(71,'viewer','pnl',
        date(2026,10,12),date(2026,10,18),'sku','financial',True)
    assert calls[0]['source'] == saved[0]['source'] == 'financial'
    assert saved[0]['report']['rows'][0]['netProfitKopecks'] == 2000
