"""Strict, non-mutating cost import plan; identities are never inferred from titles."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from app.promotion_excel import _normalize_header, _shared_strings, _cell_value, _cell_column_index, _NS


def workbook_rows(content: bytes):
    """Bound expansion before XML parsing; preserve actual worksheet row numbers."""
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > 40 * 1024 * 1024:
            raise ValueError("XLSX слишком большой после распаковки")
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = []
        for node in root.findall("x:sheetData/x:row", _NS):
            number = int(node.attrib["r"])
            if len(rows) >= 20001 or number > 1048576:
                raise ValueError("В файле больше 20000 строк")
            values, formulas = {}, set()
            for cell in node.findall("x:c", _NS):
                column = _cell_column_index(cell.attrib.get("r", "A1"))
                if column > 50:
                    raise ValueError("В шаблоне не должно быть более 51 колонки")
                values[column] = _cell_value(cell, shared)
                if cell.find("x:f", _NS) is not None:
                    formulas.add(column)
            if any(values.values()) or formulas:
                rows.append((number, values, formulas))
        return rows


def parse_cost_rows(content: bytes) -> list[dict]:
    rows = workbook_rows(content)
    if not rows:
        raise ValueError("Пустой файл")
    headers = {column: _normalize_header(value) for column, value in rows[0][1].items()}
    def column(names):
        found = [i for i, name in headers.items() if name in names]
        if len(found) != 1:
            raise ValueError("Первая строка должна содержать по одной колонке nmID и Себестоимость")
        return found[0]
    identity = column({"nmid", "артикулwb", "артикулмп", "артикулмпnmid"})
    amount = column({"себестоимость", "costprice", "себестоимостьруб"})
    result = []
    for number, row, formulas in rows[1:]:
        raw_id = str(row.get(identity, "")).strip()
        raw_cost = str(row.get(amount, "")).strip()
        item = {"row": number, "nmId": None, "amountKopecks": None, "error": None}
        if formulas & {identity, amount}:
            item["error"] = "Формулы не импортируются: вставьте вычисленные значения"
        if not re.fullmatch(r"[1-9][0-9]{0,14}", raw_id):
            item["error"] = "Нужен точный числовой артикул WB (nmID)"
        else:
            item["nmId"] = int(raw_id)
        if not raw_cost:
            item["error"] = item["error"] or "Пустая себестоимость: значение не будет изменено"
        else:
            normalized = re.sub(r"[ \u00a0\u202f]", "", raw_cost).replace(",", ".")
            try:
                if not re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized):
                    raise InvalidOperation()
                value = int(Decimal(normalized) * 100)
                if value > 2_147_483_647:
                    raise InvalidOperation()
                item["amountKopecks"] = value
            except (InvalidOperation, ValueError):
                item["error"] = item["error"] or "Нужна сумма от 0 до 21 474 836,47 ₽ с максимум двумя знаками после запятой"
        result.append(item)
    counts = Counter(item["nmId"] for item in result if item["nmId"] is not None)
    for item in result:
        if counts[item["nmId"]] > 1:
            item["error"] = "Артикул повторяется в файле; удалите дубли"
    return result


def plan_digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
