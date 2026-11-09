"""Read-only Repricer selection. Never changes financial facts or source caches."""
import math
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TYPES = {"shirt", "hoodie", "longsleeve", "shorts", "balaclava"}
DEFAULT_TYPES = {"shirt", "hoodie", "longsleeve"}
ATTRS = {"female", "male", "white", "black"}


class RepricerFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    marginFrom: Decimal | None = None
    marginTo: Decimal | None = None
    basketsFrom: Decimal | None = Field(default=None, ge=0)
    basketsTo: Decimal | None = Field(default=None, ge=0)
    ordersFrom: Decimal | None = Field(default=None, ge=0)
    ordersTo: Decimal | None = Field(default=None, ge=0)
    priceFrom: Decimal | None = Field(default=None, ge=0)
    priceTo: Decimal | None = Field(default=None, ge=0)
    types: list[Literal["shirt", "hoodie", "longsleeve", "shorts", "balaclava"]] = Field(default_factory=list)
    attrs: list[Literal["female", "male", "white", "black"]] = Field(default_factory=list)
    strategy: str = Field(default="all", max_length=200)
    stock: Literal["all", "ending", "low", "normal", "excess"] = "all"
    promo: Literal["all", "yes", "no"] = "all"
    segment: Literal["all", "problem", "nopmin", "loss", "changes24h", "illiquid"] = "all"

    @model_validator(mode="after")
    def valid_ranges(self):
        for key in ("margin", "baskets", "orders", "price"):
            left, right = getattr(self, key + "From"), getattr(self, key + "To")
            if left is not None and right is not None and left > right:
                raise ValueError(f"{key}: from must not exceed to")
        # Empty selections are unrestricted on the legacy main Repricer only.
        self.types = sorted(set(self.types))
        self.attrs = sorted(set(self.attrs))
        return self

    @property
    def restricted_types(self):
        values = set(self.types)
        return values if values and values not in (TYPES, DEFAULT_TYPES) else set()

    @property
    def restricted_attrs(self):
        values = set(self.attrs)
        return values if values != ATTRS else set()

    @property
    def active(self):
        return bool(self.restricted_types or self.restricted_attrs
            or any(getattr(self, key + side) is not None for key in ("margin", "baskets", "orders", "price") for side in ("From", "To"))
            or any(getattr(self, key) != "all" for key in ("strategy", "stock", "promo", "segment")))


def known(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and math.isfinite(value)


def garment(row):
    meta = row.get("meta") or {}
    raw = " ".join(str(meta.get(key) or "") for key in ("subject", "name", "articleId")).casefold()
    for key, pattern in (("longsleeve", r"лонг|long\s*sleeve|longsleeve"), ("hoodie", r"худи|hoodie|толстов"),
                         ("shorts", r"шорт|short"), ("balaclava", r"балак|balaclava"), ("shirt", r"футбол|\b(?:t-?shirt|shirt)\b|\bf[bc]")):
        if re.search(pattern, raw):
            return key
    return None


def attributes(row, card=None):
    meta = row.get("meta") or {}
    raw = " ".join(str(meta.get(key) or "") for key in ("subject", "name", "articleId", "gender", "color")).casefold().replace("ё", "е")
    result = set()
    for key, pattern in (("female", r"жен|\b(?:female|woman|women|unisex)\b|унисекс"),
                         ("male", r"муж|\b(?:male|man|men|unisex)\b|унисекс"),
                         ("white", r"бел|white|\b(?:wb|fb|hb|lb)"), ("black", r"чер|black|\b(?:bc|fc|hc|lc)")):
        if re.search(pattern, raw):
            result.add(key)
    # Saved WB characteristics override title/article heuristics for their own
    # dimension. Read once in the route, never query per product.
    for item in (card or {}).get("characteristics") or []:
        name = str(item.get("name") or "").casefold()
        values = item.get("value") or []
        text = " ".join(map(str, values if isinstance(values, list) else [values])).casefold().replace("ё", "е")
        if name in {"пол", "gender"}:
            result.difference_update({"female", "male"})
            if re.search(r"жен|\b(?:female|woman|women|unisex)\b|унисекс", text):
                result.add("female")
            if re.search(r"муж|\b(?:male|man|men|unisex)\b|унисекс", text):
                result.add("male")
        if name in {"цвет", "color", "colour"}:
            result.difference_update({"white", "black"})
            if re.search(r"бел|white", text):
                result.add("white")
            if re.search(r"чер|black", text):
                result.add("black")
    return result


def saved_attribute_cards(organization_id):
    from app.wb_pnl_advertising import _source_context
    from app.wb_pnl_filters import _saved_cache
    context = _source_context(organization_id)
    if context is None:
        return {}
    cards = _saved_cache(organization_id, "content_cards", context).get("cards") or []
    return {str(card.get("nmID") or card.get("nmId")): card for card in cards}


def displayed_profit(row):
    """Select the same confirmed profit basis as the existing product cell."""
    a = row.get("analytics") or {}
    if a.get("financeState") not in {"ok", "fallback"} or a.get("settlementFormulaVersion") != "wb-final-payout-cogs-tax-v1":
        return None
    campaign = a.get("campaignAdvertisingVersion") == "wb-repricer-campaign-advertising-v1"
    profit = a.get("campaignProfitKopecks") if campaign else a.get("settlementProfitKopecks")
    if profit is None and campaign and a.get("settlementBlockers") == ["settlement_account_expenses_unreconciled"]:
        profit = a.get("campaignAttributedProfitKopecks")
    return profit if known(profit) else None


def displayed_margin(row):
    profit = displayed_profit(row)
    revenue = (row.get("analytics") or {}).get("revenueKopecks")
    # Match the existing JS cell's Math.round, including negative half steps.
    return math.floor(profit / revenue * 10000 + .5) / 100 if known(profit) and known(revenue) and revenue > 0 else None


def compile_filter(filters: RepricerFilters, cards=None):
    from app.routers.wb_repricer_bff import _row_ui_status
    bounds = [(key, getattr(filters, key + "From"), getattr(filters, key + "To"))
              for key in ("margin", "baskets", "orders", "price")
              if getattr(filters, key + "From") is not None or getattr(filters, key + "To") is not None]
    types, attrs = filters.restricted_types, filters.restricted_attrs

    def matches(row):
        meta, a, settings, strategy = (row.get(key) or {} for key in ("meta", "analytics", "settings", "strategy"))
        card = (cards or {}).get(str(meta.get("nmId")))
        if types and garment(row) not in types or attrs and not attributes(row, card).intersection(attrs):
            return False
        if filters.promo != "all" and a.get("promotionStatus") != filters.promo:
            return False
        if filters.strategy != "all" and filters.strategy not in {str(strategy.get("id") or ""), str(meta.get("activeStrategyId") or ""), str(meta.get("activeTypedStrategyId") or "")}:
            return False
        stock = a.get("wbStockUnits") if a.get("stockState") in {"ok", "fallback"} else None
        if filters.stock != "all" and (not known(stock)
                or filters.stock == "ending" and stock > 5 or filters.stock == "low" and not 5 < stock <= 20
                or filters.stock == "normal" and not 20 < stock <= 100 or filters.stock == "excess" and stock <= 100):
            return False
        margin = displayed_margin(row)
        pmin = settings.get("effectivePMinKopecks", settings.get("pMinKopecks"))
        no_min = known(pmin) and pmin <= 0
        profit = displayed_profit(row)
        sales, returns = a.get("salesUnits"), a.get("returnsUnits")
        loss = (known(margin) and margin < 0 or known(profit) and profit < 0
                and known(sales) and known(returns) and sales > returns)
        illiquid = _row_ui_status(row) in {"illiquid", "liq"}
        if (filters.segment == "nopmin" and not no_min or filters.segment == "loss" and not loss
                or filters.segment == "illiquid" and not illiquid
                or filters.segment == "problem" and not (known(margin) and margin < 10 or no_min or illiquid or known(stock) and stock <= 20)
                or filters.segment == "changes24h" and meta.get("currentPriceKopecks") == a.get("basePriceKopecks", meta.get("currentPriceKopecks"))):
            return False
        for key, left, right in bounds:
            if key == "margin":
                value = margin
            elif key == "price":
                value = meta.get("currentPriceKopecks")
            elif key == "baskets":
                value = a.get("baskets") if a.get("basketsState") in {"ok", "fallback", "partial"} else None
            else:
                value = a.get("reportOrdersUnits", a.get("ordersUnits")) if a.get("ordersSource") != "finance_sales_fallback" else None
            if not known(value):
                return False
            factor = 100 if key == "price" else 1
            number = Decimal(str(value))
            if left is not None and number < left * factor or right is not None and number > right * factor:
                return False
        return True

    return matches
