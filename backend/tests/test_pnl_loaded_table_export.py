from copy import deepcopy

import pytest

from tests.test_abc_pnl_v2 import api, headers
from tests.test_report_table_export import NS, workbook_parts, row_values, XLSX_MIME

PATH = "/api/wb/reports/pnl/table.xlsx"


def payload():
    return dict(reportKind="pnl", sourceKind="loaded-pnl", dateFrom="2026-10-01", dateTo="2026-10-08",
        headers=["Товар", "Выручка", "Штрафы, ₽", "Платная приёмка, ₽"],
        rows=[[f"SKU-{i}\nWB {1000+i}", i, -1.23, None if i else 0] for i in range(160)],
        sourceState="stale", warning="Часть данных отсутствует", filterDescription="Все товары")


@pytest.mark.parametrize("start,end", [("2026-10-01", "2026-10-08"), ("2026-09-11", "2026-09-13")])
def test_exports_loaded_pnl_without_canonical_or_provider_fetch(api, monkeypatch, start, end):
    def fail(*a, **kw):
        pytest.fail("export must not fetch/recalculate financial reports")
    monkeypatch.setattr("app.modules.wb_reports.abc_pnl.WbAbcPnlService.get_page", fail)
    monkeypatch.setattr("app.repricer_bff.fetch_finance_report_aggregates", fail)
    body = payload()
    body.update(dateFrom=start, dateTo=end)
    body["rows"][0][0] = "=literal & <product>\nWB 1000"
    response = api.post(PATH, json=body, headers=headers("finance-all"))
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == f'attachment; filename="wb-pnl-{start}-{end}.xlsx"'
    sheet, workbook, _ = workbook_parts(response.content)
    assert workbook.find(".//x:sheet", NS).get("name") == "P&L"
    rows = sheet.findall("x:sheetData/x:row", NS)
    assert row_values(rows[1]) == ["=literal & <product>\nWB 1000", "0", "-1.23", "0"]
    assert row_values(rows[160]) == ["SKU-159\nWB 1159", "159", "-1.23", None]
    assert rows[1].findall("x:c", NS)[2].get("s") == "2"
    assert sheet.find(".//x:f", NS) is None
    metadata = {values[0]: values[1] for row in rows[162:] if len(values := row_values(row)) == 2}
    assert metadata["Период с"] == start
    assert metadata["Период по"] == end
    assert metadata["Состояние источников"] == "stale"
    assert metadata["Организация"] == "1"
    assert metadata["Экспортировано строк"] == "160"


def test_loaded_pnl_export_requires_login_and_finance_permission(api):
    assert api.post(PATH, json=payload()).status_code == 401
    assert api.post(PATH, json=payload(), headers=headers("viewer")).status_code == 403


@pytest.mark.parametrize("change", [
    {"dateTo": "2026-09-30"}, {"dateTo": "2027-01-01"},
    {"reportKind": "abc"}, {"marketplaceAccountId": 32}, {"organizationId": 2},
    {"rows": [["bad width"]]}, {"rows": [["text", True, 0, None]]},
    {"rows": [["bad\u0001", 0, 0, None]]}, {"rows": [["x", 0, 0, None]] * 10001},
])
def test_loaded_pnl_export_keeps_validation_and_org_boundary(api, change):
    body = deepcopy(payload())
    body.update(change)
    assert api.post(PATH, json=body, headers=headers("finance-all")).status_code == 422


def test_loaded_pnl_export_rejects_large_body(api):
    response = api.post(PATH, content=b"x" * (5 * 1024 * 1024 + 1), headers=headers("finance-all"))
    assert response.status_code == 413
