from __future__ import annotations

import copy
import json
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.repricer_nomenclature_excel import build_xlsx
from tests.test_abc_pnl_v2 import api, headers

PATH = "/api/v2/wb/reports/table.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def payload() -> dict[str, object]:
    return {
        "reportKind": "abc",
        "marketplaceAccountId": 31,
        "dateFrom": "2026-08-17",
        "dateTo": "2026-08-23",
        "headers": ["SKU", "Сумма, ₽", "Неизвестно"],
        "rows": [
            [f"sku-{index}", 0 if index == 0 else -index / 100, None]
            for index in range(65)
        ],
        "source": {
            "state": "partial",
            "formulaVersion": "abc-v2",
            "financeFormulaVersion": "finance-v2",
            "financeSnapshotChecksum": "finance-checksum",
            "financeObservedAt": "2026-08-26T03:50:00Z",
            "advertisingSnapshotChecksum": "ads-checksum",
            "costLedgerRevision": 7,
            "economicsRevision": 9,
            "blockerIds": ["PRELIMINARY"],
            "filterDescription": "SKU содержит “товар”\nМаржа < 0",
        },
    }


def workbook_parts(
    content: bytes,
) -> tuple[ElementTree.Element, ElementTree.Element, list[str]]:
    with ZipFile(BytesIO(content)) as archive:
        names = archive.namelist()
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    return sheet, workbook, names


def row_values(row: ElementTree.Element) -> list[str | None]:
    values: list[str | None] = []
    for cell in row.findall("x:c", NS):
        if cell.get("t") == "inlineStr":
            values.append(
                "".join(text.text or "" for text in cell.findall(".//x:t", NS))
            )
        else:
            value = cell.find("x:v", NS)
            values.append(None if value is None else value.text)
    return values


def test_export_returns_ordered_typed_table_and_trusted_metadata(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_provider(*_args: object, **_kwargs: object) -> None:
        pytest.fail("table-copy export must not call report providers")

    monkeypatch.setattr(
        "app.platform.finance.service.FinanceService.get_page", fail_provider
    )
    monkeypatch.setattr(
        "app.platform.advertising.service.AdvertisingService.get_pnl_source",
        fail_provider,
    )
    monkeypatch.setattr(
        "app.modules.wb_reports.abc_pnl.WbAbcPnlService.get_page", fail_provider
    )
    request = payload()
    request["rows"][0] = ["=SUM(1,1)\nтовар & <tag>", 0, None]
    request["rows"][1] = ["+cmd", -0.01, "0"]
    request["rows"][2][0] = "-literal"
    request["rows"][3][0] = "@name"

    response = api.post(PATH, json=request, headers=headers("finance-all"))

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="wb-abc-2026-08-17-2026-08-23.xlsx"'
    )
    sheet, workbook, names = workbook_parts(response.content)
    assert workbook.find(".//x:sheet", NS).get("name") == "ABC"
    rows = sheet.findall(".//x:sheetData/x:row", NS)
    assert row_values(rows[0]) == ["SKU", "Сумма, ₽", "Неизвестно"]
    assert row_values(rows[1]) == ["=SUM(1,1)\nтовар & <tag>", "0", None]
    assert row_values(rows[2]) == ["+cmd", "-0.01", "0"]
    first_data_cells = rows[1].findall("x:c", NS)
    second_data_cells = rows[2].findall("x:c", NS)
    assert [cell.get("t") for cell in first_data_cells] == ["inlineStr", None, None]
    assert first_data_cells[2].find("x:v", NS) is None
    assert second_data_cells[1].get("t") is None
    assert second_data_cells[2].get("t") == "inlineStr"
    assert [row_values(row)[0] for row in rows[3:66]] == [
        "-literal",
        "@name",
        *(f"sku-{index}" for index in range(4, 65)),
    ]
    assert all(row.find("x:c", NS).get("t") == "inlineStr" for row in rows[1:5])
    assert row_values(rows[66]) == []
    metadata = {
        values[0]: values[1] for row in rows[67:] if len(values := row_values(row)) >= 2
    }
    assert metadata == {
        "Экспорт": "Копия загруженной таблицы; не заверенный финансовый расчёт",
        "Организация": "1",
        "Аккаунт WB": "31",
        "Период с": "2026-08-17",
        "Период по": "2026-08-23",
        "Часовой пояс": "Europe/Moscow",
        "Деньги": "рубли",
        "Состояние источника": "partial",
        "Версия формулы": "abc-v2",
        "Версия финансовой формулы": "finance-v2",
        "Контрольная сумма финансов": "finance-checksum",
        "Финансы наблюдались": "2026-08-26T03:50:00+00:00",
        "Контрольная сумма рекламы": "ads-checksum",
        "Ревизия себестоимости": "7",
        "Ревизия экономики": "9",
        "Блокировки": "PRELIMINARY",
        "Фильтры": "SKU содержит “товар”\nМаржа < 0",
        "Экспортировано строк": "65",
    }
    assert sheet.find(".//x:f", NS) is None
    assert not any("externalLink" in name for name in names)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(extra="forbidden"),
        lambda body: body["source"].update(extra="forbidden"),
        lambda body: body.update(marketplaceAccountId=True),
        lambda body: body.update(headers=["x"] * 33, rows=[]),
        lambda body: body.update(headers=["x" * 129], rows=[]),
        lambda body: body.update(headers=["x"], rows=[["y", "z"]]),
        lambda body: body.update(headers=["x"], rows=[["y" * 4097]]),
        lambda body: body.update(headers=["x"], rows=[[True]]),
        lambda body: body.update(rows=body["rows"] * 154),
        lambda body: body["source"].update(financeObservedAt="2026-08-26T03:50:00"),
        lambda body: body["source"].update(costLedgerRevision=True),
        lambda body: body["source"].update(blockerIds=["x"] * 65),
        lambda body: body["source"].update(filterDescription="x" * 4097),
        lambda body: body.update(headers=["bad\u0001header"], rows=[]),
    ],
    ids=[
        "root-extra",
        "source-extra",
        "boolean-account",
        "too-many-columns",
        "long-header",
        "wrong-row-width",
        "long-cell",
        "boolean-cell",
        "too-many-rows",
        "naive-observed-at",
        "boolean-revision",
        "too-many-blockers",
        "long-filter",
        "xml-control",
    ],
)
def test_export_rejects_invalid_shape(api: TestClient, mutate) -> None:
    request = copy.deepcopy(payload())
    mutate(request)

    response = api.post(PATH, json=request, headers=headers("finance-all"))

    assert response.status_code == 422


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_export_rejects_nonfinite_numbers(api: TestClient, constant: str) -> None:
    request = payload()
    request["rows"] = [["sku", 1, None]]
    raw = json.dumps(request, ensure_ascii=False).replace(
        '["sku", 1, null]', f'["sku", {constant}, null]'
    )

    response = api.post(
        PATH,
        content=raw.encode(),
        headers={**headers("finance-all"), "Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_export_rejects_invalid_period(api: TestClient) -> None:
    request = payload()
    request.update(dateFrom="2026-08-23", dateTo="2026-08-17")

    response = api.post(PATH, json=request, headers=headers("finance-all"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PERIOD"


def test_export_rejects_body_over_five_mib(api: TestClient) -> None:
    response = api.post(
        PATH,
        content=b"{" + b" " * (5 * 1024 * 1024) + b"}",
        headers={**headers("finance-all"), "Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_export_requires_permission_and_account_scope(api: TestClient) -> None:
    assert api.post(PATH, json=payload()).status_code == 401
    assert api.post(PATH, json=payload(), headers=headers("viewer")).status_code == 403

    scoped = payload()
    scoped["marketplaceAccountId"] = 33
    assert (
        api.post(PATH, json=scoped, headers=headers("finance-scoped")).status_code
        == 403
    )

    foreign = payload()
    foreign["marketplaceAccountId"] = 32
    assert (
        api.post(PATH, json=foreign, headers=headers("finance-all")).status_code == 404
    )


def test_pnl_sheet_name_and_default_writer_compatibility(api: TestClient) -> None:
    request = payload()
    request.update(reportKind="pnl", rows=[])
    response = api.post(PATH, json=request, headers=headers("finance-all"))

    assert response.status_code == 200
    _, workbook, _ = workbook_parts(response.content)
    assert workbook.find(".//x:sheet", NS).get("name") == "P&L"

    _, default_workbook, _ = workbook_parts(build_xlsx([["existing caller"]]))
    assert default_workbook.find(".//x:sheet", NS).get("name") == "repricer"


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), "bad\x01"]
)
def test_generic_writer_rejects_invalid_xml_values(value: object) -> None:
    with pytest.raises(ValueError):
        build_xlsx([[value]])
