"""Import the seller portal funnel export, including impressions absent from the API."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal

from app.promotion_excel import _worksheet_rows


def parse_funnel_export(content: bytes) -> list[dict]:
    info = dict(row[:2] for row in _worksheet_rows(content) if len(row) >= 2)
    periods = []
    for label in ("Значение за текущий период", "Прошлый период"):
        matches = re.findall(r"\d{2}-\d{2}-\d{4}", info.get(label, ""))
        if len(matches) != 2:
            raise ValueError("В файле отсутствуют даты периода WB")
        start, end = (datetime.strptime(value, "%d-%m-%Y").date() for value in matches)
        if start > end:
            raise ValueError("Некорректные даты периода WB")
        periods.append((start, end))
    rows = _worksheet_rows(content, "xl/worksheets/sheet4.xml")
    headers = rows[1]
    required = {"Артикул WB", "Показы", "Переходы в карточку", "Положили в корзину", "Заказали товаров, шт"}
    if not required.issubset(headers):
        raise ValueError("Ожидается Excel «Воронка продаж» из кабинета WB")
    records = [dict(zip(headers, row)) for row in rows[2:]]
    fields = {
        "impressions": "Показы", "openCount": "Переходы в карточку",
        "cartCount": "Положили в корзину", "wishlistCount": "Добавили в отложенные",
        "orderCount": "Заказали товаров, шт", "buyoutCount": "Выкупили, шт",
        "cancelCount": "Отменили, шт", "orderSumKopecks": "Заказали на сумму, ₽",
        "buyoutSumKopecks": "Выкупили на сумму, ₽", "cancelSumKopecks": "Отменили на сумму, ₽",
        "avgPriceKopecks": "Средняя цена, ₽", "localizationPct": "Локальные заказы, %",
        "buyoutPct": "Процент выкупа",
    }
    result = []
    for index, (start, end) in enumerate(periods):
        aggregates = {}
        for record in records:
            nm_id = str(record.get("Артикул WB") or "")
            if not nm_id.isdigit() or int(nm_id) <= 0 or nm_id in aggregates:
                raise ValueError("Пустой или повторяющийся артикул WB")
            item = {"nmId": int(nm_id), "vendorCode": record.get("Артикул продавца"),
                    "title": record.get("Название"), "category": record.get("Предмет"), "brand": record.get("Бренд")}
            for field, label in fields.items():
                if index:
                    label = ("Выкупы, шт" if field == "buyoutCount" else label) + " (предыдущий период)"
                raw = record.get(label)
                if raw is not None and raw.strip() not in {"", "—", "-"}:
                    value = Decimal(raw.replace(",", "."))
                    if not value.is_finite() or value < 0:
                        raise ValueError(f"Некорректное значение WB: {label}")
                    item[field] = int(value * 100) if field.endswith("Kopecks") else float(value) if field.endswith("Pct") else int(value)
            views, opens, carts, orders = (item.get(key) for key in ("impressions", "openCount", "cartCount", "orderCount"))
            item["ctrPct"] = round(opens / views * 100, 4) if views and opens is not None else None
            item["addToCartPct"] = round(carts / opens * 100, 4) if opens and carts is not None else None
            item["cartToOrderPct"] = round(orders / carts * 100, 4) if carts and orders is not None else None
            aggregates[nm_id] = item
        result.append({"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "aggregates": aggregates,
                       "count": len(aggregates), "source": "seller_export", "fileSha256": hashlib.sha256(content).hexdigest(),
                       "observedAt": datetime.now(timezone.utc).isoformat()})
    current, previous = (payload["aggregates"] for payload in result)
    for nm_id, item in current.items():
        prior = previous.get(nm_id, {})
        for field in ("impressions", "openCount", "cartCount", "orderCount", "orderSumKopecks", "buyoutCount", "buyoutSumKopecks"):
            before, now = prior.get(field), item.get(field)
            item["previous" + field[0].upper() + field[1:]] = before
            item[field.replace("Kopecks", "") + "DeltaPct"] = round((now - before) / before * 100, 4) if before and now is not None else None
    # The file's independent WB total detects a truncated or filtered product sheet.
    totals = _worksheet_rows(content, "xl/worksheets/sheet3.xml")
    control = dict(zip(totals[1], totals[2]))
    for index, payload in enumerate(result):
        label = "Показы" + (" (предыдущий период)" if index else "")
        if sum(row.get("impressions", 0) for row in payload["aggregates"].values()) != int(control[label]):
            raise ValueError("Сумма показов товаров не совпадает с итогом WB")
    return result


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    from app.cabinet import orm  # Register the organization foreign key for the CLI.
    from app.repricer_cache.store import get_source_cache, save_source_cache

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--organization", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.organization <= 0:
        parser.error("organization must be positive")
    for payload in parse_funnel_export(args.file.read_bytes()):
        if args.apply:
            key = f"funnel_seller_{payload['dateFrom']}_{payload['dateTo']}"
            save_source_cache(args.organization, key, payload)
            saved = get_source_cache(args.organization, key, strict=True) or {}
            if saved.get("fileSha256") != payload["fileSha256"]:
                raise RuntimeError("Выгрузка WB не сохранена")
        print(payload["dateFrom"], payload["dateTo"], payload["count"], "saved" if args.apply else "validated")
