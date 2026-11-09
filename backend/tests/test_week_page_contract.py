from datetime import date
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile
from xml.etree import ElementTree

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import wb_week_export as export
from app.routers.wb_reports_bff import _previous_period, _report_source_bounds, _delta_pct, router


@pytest.mark.parametrize("start,end,previous_start,previous_end", [
    ("2026-09-02", "2026-09-08", "2026-08-26", "2026-09-01"),
    ("2026-09-09", "2026-09-16", "2026-09-01", "2026-09-08"),
    ("2026-09-09", "2026-09-18", "2026-08-30", "2026-09-08"),
])
def test_equal_inclusive_periods(start, end, previous_start, previous_end):
    assert _previous_period(date.fromisoformat(start), date.fromisoformat(end)) == (
        date.fromisoformat(previous_start), date.fromisoformat(previous_end))
    assert _report_source_bounds("week-over-week", date.fromisoformat(start), date.fromisoformat(end)) == (
        date.fromisoformat(previous_start), date.fromisoformat(end))


def test_missing_and_zero_comparison_base():
    assert _delta_pct(10, 0) is None
    assert _delta_pct(10, None) is None
    assert _delta_pct(None, 10) is None
    assert _delta_pct(0, 10) == -100


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(export, "actor_from_request", lambda request: SimpleNamespace(organization_id=123))
    monkeypatch.setattr(export, "assert_permission_or_audit", lambda **kwargs: None)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def payload():
    return {"dateFrom": "2026-09-09", "dateTo": "2026-09-18", "sourceState": "partial",
            "headers": ["Товар", "Продажи"], "rows": [["=NotAFormula", "—"], ["Лчbt_0212", "100 ₽"]]}


def test_real_week_route_exports_valid_xlsx_and_period_metadata(api):
    response = api.post("/api/wb/reports/week-over-week/table.xlsx", json=payload())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "wb-week-2026-09-09-2026-09-18.xlsx" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as workbook:
        xml = workbook.read("xl/worksheets/sheet1.xml")
        ElementTree.fromstring(xml)
        text = xml.decode()
        assert "2026-08-30 — 2026-09-08 (10 дн.)" in text
        assert "partial" in text and "Лчbt_0212" in text and "—" in text
        assert "=NotAFormula" in text and "<f>" not in text


@pytest.mark.parametrize("change", [
    {"dateTo": "2026-09-08"}, {"rows": [["too short"]]},
    {"rows": [["bad\x01xml", "1"]]}, {"rows": [[float("inf"), "1"]]},
])
def test_invalid_week_export_rejected(api, change):
    # JSON infinity is tested with literal JSON to avoid the client's strict encoder.
    import json
    response = api.post("/api/wb/reports/week-over-week/table.xlsx",
                        content=json.dumps({**payload(), **change}), headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_week_export_requires_report_permission(api, monkeypatch):
    def denied(**kwargs):
        raise HTTPException(403)
    monkeypatch.setattr(export, "assert_permission_or_audit", denied)
    assert api.post("/api/wb/reports/week-over-week/table.xlsx", json=payload()).status_code == 403


def test_week_export_keeps_all_financial_columns_both_periods_and_unallocated_note(api):
    from app.wb_week_finance import FIELDS
    data = payload() | {"headers": ["Товар", *[label for label, source in FIELDS.values()]],
        "rows": [["Product 101", *["-1,23 ₽\nПред.: 4,56 ₽ · —" for _ in FIELDS]],
                 ["Product 102", *["0 ₽\nПред.: — · —" for _ in FIELDS]]],
        "warning": "Без nmId: штрафы 5 ₽; не распределено по товарам"}
    response = api.post("/api/wb/reports/week-over-week/table.xlsx", json=data)
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as workbook:
        xml = workbook.read("xl/worksheets/sheet1.xml").decode()
        for label, source in FIELDS.values():
            assert label in xml
        assert "-1,23 ₽" in xml and "Пред.: 4,56 ₽" in xml and "0 ₽" in xml
        assert "Без nmId: штрафы 5 ₽" in xml
        assert "2026-09-09 — 2026-09-18 (10 дн.)" in xml
        assert "2026-08-30 — 2026-09-08 (10 дн.)" in xml
