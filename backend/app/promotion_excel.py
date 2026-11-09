from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET

from app.wb_api.price_units import wb_goods_price_to_kopecks


_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class ParsedPromotionExcel:
    original_filename: str
    normalized_filename: str
    file_hash: str
    rows_total: int
    rows_parsed: int
    thresholds: list[dict[str, Any]]
    status: str
    error_text: str | None = None


def normalize_promotion_name(value: str) -> str:
    return (
        value.lower()
        .replace("ё", "е")
        .replace(".xlsx", "")
        .replace(".xls", "")
        .replace("«", "")
        .replace("»", "")
        .replace('"', "")
        .replace("'", "")
        .replace("\u00a0", " ")
    )


def normalized_filename(value: str) -> str:
    text = normalize_promotion_name(value).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    for prefix in (
        "товары для исключения из акции ",
        "товары для участия в акции ",
        "товары из акции ",
        "товары акции ",
    ):
        if text.startswith(prefix):
            return text.removeprefix(prefix).strip()
    return text


def _normalize_header(value: Any) -> str:
    text = normalize_promotion_name(str(value or ""))
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def _cell_column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(0, index - 1)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    values: list[str] = []
    for item in root.findall("x:si", _NS):
        parts = [node.text or "" for node in item.findall(".//x:t", _NS)]
        values.append("".join(parts))
    return values


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//x:t", _NS)).strip()
    raw = cell.findtext("x:v", default="", namespaces=_NS)
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return str(raw or "").strip()


def _worksheet_rows(content: bytes, sheet_name: str = "xl/worksheets/sheet1.xml") -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(content)) as zf:
        shared = _shared_strings(zf)
        if sheet_name not in zf.namelist():
            sheet_name = next((name for name in zf.namelist() if name.startswith("xl/worksheets/sheet")), sheet_name)
        root = ET.fromstring(zf.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.findall(".//x:sheetData/x:row", _NS):
            values: list[str] = []
            for cell in row.findall("x:c", _NS):
                index = _cell_column_index(cell.attrib.get("r", "A1"))
                while len(values) <= index:
                    values.append("")
                values[index] = _cell_value(cell, shared)
            if any(value.strip() for value in values):
                rows.append(values)
        return rows


def _find_column(headers: list[str], aliases: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in aliases:
            return index
    for index, header in enumerate(headers):
        if any(len(alias) > 4 and alias in header for alias in aliases):
            return index
    return None


def _money_kopecks(value: Any) -> int | None:
    text = str(value or "").replace("\u00a0", " ").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:[\s.,]\d+)*", text)
    if not match:
        return None
    normalized = match.group(0).replace(" ", "").replace(",", ".")
    return wb_goods_price_to_kopecks(normalized)


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").replace("\u00a0", " ").strip()
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def _percent_or_none(value: Any) -> float | None:
    text = str(value or "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def parse_promotion_excel(content: bytes, filename: str) -> ParsedPromotionExcel:
    file_hash = hashlib.sha256(content).hexdigest()
    normalized = normalized_filename(filename)
    try:
        rows = _worksheet_rows(content)
    except Exception as exc:
        return ParsedPromotionExcel(
            original_filename=filename,
            normalized_filename=normalized,
            file_hash=file_hash,
            rows_total=0,
            rows_parsed=0,
            thresholds=[],
            status="error",
            error_text=f"XLSX не распознан: {exc}",
        )

    aliases = {
        "nm_id": {"nmid", "nm", "артикулwb", "номенклатура", "кодноменклатуры"},
        "vendor_code": {"vendorcode", "артикулпродавца", "артикулпоставщика", "артикул"},
        "brand": {"бренд", "brand"},
        "subject": {"предмет", "категория", "subject", "object"},
        "current_price": {"текущаяцена", "текущаярозничнаяцена", "розничнаяцена", "цена", "ценапродавца"},
        "current_discount": {"текущаяскидка", "скидка"},
        "threshold_price": {
            "ценадляучастия",
            "плановаяцена",
            "порогвхода",
            "минимальнаяцена",
            "ценавакции",
            "акционнаяцена",
        },
        "threshold_discount": {"скидкадляучастия", "плановаяскидка", "скидкавакции", "акционнаяскидка"},
        "status": {"статус", "участие", "статусучастия"},
    }

    header_index = -1
    columns: dict[str, int | None] = {}
    for index, row in enumerate(rows[:40]):
        normalized_headers = [_normalize_header(value) for value in row]
        candidate = {key: _find_column(normalized_headers, values) for key, values in aliases.items()}
        if candidate["nm_id"] is not None and candidate["threshold_price"] is not None:
            header_index = index
            columns = candidate
            break

    if header_index < 0:
        return ParsedPromotionExcel(
            original_filename=filename,
            normalized_filename=normalized,
            file_hash=file_hash,
            rows_total=max(0, len(rows) - 1),
            rows_parsed=0,
            thresholds=[],
            status="error",
            error_text="Не найдены колонки NmId и порога входа",
        )

    def value(row: list[str], key: str) -> str:
        column = columns.get(key)
        if column is None or column >= len(row):
            return ""
        return row[column]

    thresholds: list[dict[str, Any]] = []
    data_rows = rows[header_index + 1 :]
    for row in data_rows:
        nm_id = _int_or_none(value(row, "nm_id"))
        threshold_price = _money_kopecks(value(row, "threshold_price"))
        if nm_id is None or threshold_price is None:
            continue
        thresholds.append(
            {
                "nmId": nm_id,
                "vendorCode": value(row, "vendor_code") or None,
                "brand": value(row, "brand") or None,
                "subjectName": value(row, "subject") or None,
                "currentPriceKopecks": _money_kopecks(value(row, "current_price")),
                "currentDiscountPct": _percent_or_none(value(row, "current_discount")),
                "promoThresholdKopecks": threshold_price,
                "promoThresholdDiscountPct": _percent_or_none(value(row, "threshold_discount")),
                "wbStatus": value(row, "status") or None,
                "rawRow": {str(i): cell for i, cell in enumerate(row) if cell},
            }
        )

    return ParsedPromotionExcel(
        original_filename=filename,
        normalized_filename=normalized,
        file_hash=file_hash,
        rows_total=len(data_rows),
        rows_parsed=len(thresholds),
        thresholds=thresholds,
        status="parsed" if thresholds else "error",
        error_text=None if thresholds else "В файле не найдено строк с NmId и порогом входа",
    )
