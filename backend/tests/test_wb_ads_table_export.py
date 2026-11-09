from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree
import pytest
from tests.test_abc_pnl_v2 import api, headers
from tests.test_report_table_export import NS, workbook_parts, row_values, XLSX_MIME
from app.routers import wb_ads_export
from app.platform.finance.access import get_finance_actor

PATH = "/api/wb/reports/ads/table.xlsx"


@pytest.fixture(autouse=True)
def advertising_actor(api, monkeypatch):
    # Reuse the fixture's synthetic authenticated actor, without weakening the
    # real route's session or settings:read checks.
    monkeypatch.setattr(wb_ads_export, "actor_from_request", api.app.dependency_overrides[get_finance_actor])


def payload():
    return dict(reportKind="ads", sourceKind="loaded-ads", dateFrom="2026-10-01", dateTo="2026-10-09",
        headers=["Товар", "ID рекламы", "Все ID товара", "Расход", "Показы", "CTR"],
        rows=[[f"Product {index}", str(index + 42), "42, 43", 6.25, 15, 0.086] for index in range(160)],
        sourceState="partial", warning="Нет части данных", filterDescription='{"query":"Product","sort":"Расход"}')


@pytest.mark.parametrize("start,end", [("2026-10-01", "2026-10-09"), ("2026-09-02", "2026-09-03")])
def test_ads_export_complete_typed_and_selected_period_without_canonical(api, monkeypatch, start, end):
    monkeypatch.setattr("app.routers.wb_reports_bff.build_ads_attribution_snapshot", lambda **kw: pytest.fail("must not collect on export"))
    body = payload()
    body.update(dateFrom=start, dateTo=end)
    body["rows"][0] = ["=literal product", "42", "42, 43", 0, 0, None]
    response = api.post(PATH, json=body, headers=headers("finance-all"))
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert response.headers["cache-control"] == "no-store"
    assert f"wb-ads-{start}-{end}.xlsx" in response.headers["content-disposition"]
    sheet, workbook, _ = workbook_parts(response.content)
    assert workbook.find(".//x:sheet", NS).get("name") == "Реклама"
    rows = sheet.findall("x:sheetData/x:row", NS)
    assert row_values(rows[1]) == ["=literal product", "42", "42, 43", "0", "0", None]
    assert row_values(rows[160])[0] == "Product 159"
    cells = rows[2].findall("x:c", NS)
    assert cells[1].get("t") == "inlineStr"
    assert [cells[index].get("s") for index in (3, 4, 5)] == ["2", "1", "3"]
    assert row_values(rows[2])[5] == "0.086"
    with ZipFile(BytesIO(response.content)) as archive:
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
    assert styles.find("x:cellXfs", NS)[3].get("numFmtId") == "10"  # 0.00%, not integer-rounded 9%
    assert sheet.find(".//x:f", NS) is None
    metadata = {values[0]: values[1] for row in rows[162:] if len(values := row_values(row)) == 2}
    assert metadata["Период с"] == start
    assert metadata["Период по"] == end
    assert metadata["Состояние источников"] == "partial"
    assert metadata["Экспортировано строк"] == "160"


def test_ads_export_requires_authentication(api):
    assert api.post(PATH, json=payload()).status_code == 401


def test_ads_romi_export_preserves_percentages_zero_and_unavailable_without_sales_columns(api):
    body = payload()
    body["headers"] = ["Товар", "ID рекламы", "Расход", "Заказы, ₽", "ROMI"]
    body["rows"] = [["Profitable", "42", 100, 400, 3], ["Break even", "43", 100, 100, 0],
                    ["No orders", "44", 100, 0, -1], ["No spend", "45", 0, 400, None]]
    response = api.post(PATH, json=body, headers=headers("finance-all"))
    assert response.status_code == 200
    sheet, _, _ = workbook_parts(response.content)
    rows = sheet.findall("x:sheetData/x:row", NS)
    assert [row_values(row)[4] for row in rows[1:5]] == ["3", "0", "-1", None]
    assert all(row.findall("x:c", NS)[4].get("s") == "3" for row in rows[1:4])
    assert not any("Продажи" in str(value) for value in row_values(rows[0]))


@pytest.mark.parametrize("change", [{"reportKind": "pnl"}, {"organizationId": 2}, {"marketplaceAccountId": 2},
    {"dateTo": "2026-09-30"}, {"dateTo": "2027-01-01"}, {"rows": [[True] * 6]},
    {"rows": [["bad\u0001"] * 6]}, {"rows": [["x"]]}, {"rows": [[None] * 6] * 10001}])
def test_ads_export_strict_scope_and_bounds(api, change):
    body = deepcopy(payload()); body.update(change)
    assert api.post(PATH, json=body, headers=headers("finance-all")).status_code == 422
