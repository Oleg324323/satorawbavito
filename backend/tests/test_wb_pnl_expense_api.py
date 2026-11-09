from io import BytesIO
from zipfile import ZipFile

import pytest

from app import wb_pnl_expenses as fees
from app.modules.wb_reports.table_export import ReportTableExportRequest, render_report_table_export
from tests.test_abc_pnl_v2 import api, headers, params
from tests.test_report_table_export import payload, workbook_parts, row_values, NS
from tests.test_wb_pnl_expenses import operation, project


def test_pnl_opt_in_does_not_change_abc_and_rejects_ambiguous_cache(api, monkeypatch):
    path = '/api/v2/wb/reports/abc-pnl'
    monkeypatch.setattr(fees, 'load_pnl_finance', lambda *a: pytest.fail('ambiguous cache must not be read'))
    abc = api.get(path, params=params(), headers=headers('finance-all'))
    assert abc.status_code == 200
    assert abc.json()['items'][0]['pnlExpenses'] is None
    pnl = api.get(path, params={**params(), 'includePnlExpenses': True}, headers=headers('finance-all'))
    assert pnl.status_code == 200
    assert pnl.json()['items'][0]['pnlExpenses'] == dict(penaltyKopecks=None, acceptanceKopecks=None, state='unavailable')
    assert pnl.json()['items'][0]['netProfitKopecks'] == abc.json()['items'][0]['netProfitKopecks']


def test_pnl_opt_in_maps_raw_nm_and_signed_amount_without_provider_request(api, monkeypatch):
    monkeypatch.setattr('app.platform.advertising.raw_backfill._connected_wb_account_id', lambda *a: 31)
    cache = project([operation(1, 453200669, penalty='-2', paidAcceptance='3'),
                     operation(2, 101, penalty='99', paidAcceptance='88'),
                     operation(3, 0, penalty='7', paidAcceptance='8')])
    cache['pnlExpenseRevision'] = 'synthetic-revision'
    monkeypatch.setattr(fees, 'load_pnl_finance', lambda *a: cache)
    response = api.get('/api/v2/wb/reports/abc-pnl', params={**params(), 'includePnlExpenses': True}, headers=headers('finance-all'))
    assert response.status_code == 200
    body = response.json()
    item = next(row for row in body['items'] if row['nmId'] == 453200669)
    assert item['pnlExpenses'] == dict(penaltyKopecks=-200, acceptanceKopecks=300, state='confirmed')
    assert body['meta']['pnlExpenseRevision'] == 'synthetic-revision'
    assert next(row for row in body['meta']['pnlExpenseRows'] if row['nmId'] is None) == dict(
        nmId=None, penaltyKopecks=700, acceptanceKopecks=800, state='confirmed')
    assert api.get('/api/v2/wb/reports/abc-pnl', params={**params(), 'includePnlExpenses': True}, headers=headers('viewer')).status_code == 403


def test_pnl_xlsx_signed_fees_zero_and_missing_have_ruble_formats():
    request = payload()
    request.update(reportKind='pnl', headers=['Товар', 'Штрафы, ₽', 'Платная приёмка, ₽'],
                   rows=[['101', -1.23, 0], ['102', None, 4.56], ['Нераспределённые расходы WB', 7.89, 0]])
    body = render_report_table_export(ReportTableExportRequest.model_validate(request), organization_id=1)
    sheet, _, _ = workbook_parts(body)
    rows = sheet.findall('x:sheetData/x:row', NS)
    assert row_values(rows[1]) == ['101', '-1.23', '0']
    assert row_values(rows[2]) == ['102', None, '4.56']
    assert row_values(rows[3]) == ['Нераспределённые расходы WB', '7.89', '0']
    assert rows[1].findall('x:c', NS)[1].get('s') == '2'
    with ZipFile(BytesIO(body)) as archive:
        assert '₽' in archive.read('xl/styles.xml').decode()
