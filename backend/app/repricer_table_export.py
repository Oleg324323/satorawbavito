"""Read-only repricer table export; separate from the settings import template."""
import json
from math import floor, isfinite
from typing import Any

from app.repricer_nomenclature_excel import build_xlsx

# All data columns, independent of UI visibility. Money originates in kopecks.
COLUMNS = [
    ("Артикул поставщика", "meta.articleId", 0), ("Название", "meta.name", 0),
    ("Фото URL", "meta.imageUrl", 0), ("WB-арт.", "meta.nmId", 0),
    ("Предмет", "meta.subject", 0), ("Бренд", "meta.brand", 0),
    ("Статус товара", "meta.status", 0), ("ABC", "analytics.abcCode", 0),
    ("Акция", "analytics.promotionStatusText", 0), ("Ответственный", "meta.managerName", 0),
    ("Цена до СПП", "export.buyerPriceKopecks", 2),
    ("Себестоимость, ₽/шт.", "settings.cogsKopecks", 2),
    ("% СПП", "export.tableSppPct", 3), ("Комиссия", "analytics.commissionDisplayPct", 3),
    ("Маржа факт, %", "export.marginPct", 3),
    ("Маржа факт, ₽/шт.", "export.marginPerUnitKopecks", 2),
    ("Прибыль за период, ₽", "analytics.settlementProfitKopecks", 2),
    ("Корзины", "analytics.baskets", 1),
    ("Продажи, шт.", "analytics.grossSalesUnits", 1),
    ("Продажи, ₽", "analytics.grossSalesAmountKopecks", 2),
    ("Заказы, шт.", "analytics.reportOrdersUnits", 1),
    ("Заказы, ₽", "analytics.ordersAmountKopecks", 2),
    ("Выкуп", "analytics.buyoutPct", 3),
    ("Остаток WB", "analytics.circulationStockUnits", 1),
    ("Остаток WB, ₽", "export.stockKopecks", 2),
    ("Стратегия", "strategy.name", 0), ("Комментарий", "commentSummary.latestText", 0),
    ("Возвраты, шт.", "analytics.returnsUnits", 1),
    ("Выручка за вычетом возвратов, ₽", "analytics.sellerRevenueKopecks", 2),
    ("Состояние финансов", "analytics.financeState", 0),
    ("Состояние воронки", "analytics.basketsState", 0),
    ("Состояние выкупа", "analytics.buyoutState", 0),
]


def table_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    result = []
    for item in items:
        analytics = item.get("analytics") or {}
        profit = analytics.get("settlementProfitKopecks") if analytics.get("settlementFormulaVersion") == "wb-final-payout-cogs-tax-v1" else None
        revenue = analytics.get("revenueKopecks")
        units = analytics.get("salesUnits")
        returns = analytics.get("returnsUnits")
        net_units = units - returns if units is not None and returns is not None else None
        stock = analytics.get("circulationStockUnits")
        price = (item.get("meta") or {}).get("currentPriceKopecks")
        base_price = analytics.get("basePriceKopecks")
        buyer_price = analytics.get("buyerPriceNoWalletKopecks")
        table_spp = None
        if (isinstance(base_price, (int, float)) and isinstance(buyer_price, (int, float))
                and isfinite(base_price) and isfinite(buyer_price) and 0 < buyer_price <= base_price):
            table_spp = floor((base_price - buyer_price) * 1000 / base_price + 0.5) / 10
        item = {**item, "export": {
            "buyerPriceKopecks": buyer_price if isinstance(buyer_price, (int, float)) and isfinite(buyer_price)
                and buyer_price > 0 and isinstance(base_price, (int, float)) and buyer_price <= base_price else None,
            "tableSppPct": table_spp,
            "marginPct": profit / revenue * 100 if profit is not None and revenue is not None and revenue > 0 else None,
            "marginPerUnitKopecks": profit / net_units if profit is not None and net_units is not None and net_units > 0 else None,
            "stockKopecks": stock * price if stock is not None and price is not None else None,
        }}
        cells = []
        for _label, path, style in COLUMNS:
            value: Any = item
            for key in path.split("."):
                value = value.get(key) if isinstance(value, dict) else None
            if path in {"analytics.returnsUnits", "analytics.sellerRevenueKopecks"} and analytics.get("financeState") == "no_data":
                value = None
            if path == "strategy.name" and (item.get("strategy") or {}).get("assignmentSource") not in {"manual", "xlsx"}:
                value = None
            if path == "meta.nmId" and value is not None:
                value = str(value)  # Identifiers must not become scientific notation.
            elif style in (2, 3) and value is not None:
                value = value / 100
            cells.append(value)
        result.append(cells)
    return result


def render_repricer_table(items, *, date_from, date_to, filters, cache):
    metadata = [[], ["Период с", str(date_from)], ["Период по", str(date_to)],
        ["Часовой пояс", "Europe/Moscow"], ["Фильтры и сортировка", filters],
        ["Товаров", len(items)],
        ["Определения", "Продажи: финансовые операции до возвратов. Заказы и выкуп: WB Sales Funnel по дате заказа. Пусто = нет данных, 0 = наблюдаемый ноль."],
        ["Состояние источников", json.dumps(cache, ensure_ascii=False, default=str)]]
    return build_xlsx([[c[0] for c in COLUMNS], *table_rows(items), *metadata],
        sheet_name="Репрайсер WB", column_formats=[c[2] for c in COLUMNS], data_rows=len(items))
