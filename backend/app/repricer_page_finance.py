"""Shared financial-report projection, with separate seller-price inputs.

Source completeness is a completed cursor
chain, not the number of days on which operations happen to exist.
"""
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from functools import wraps
from inspect import signature
from hashlib import sha256
from decimal import Decimal
from typing import Any
from app.repricer_source_provenance import FINANCE_BINDING_VERSION, oldest_source_observation

page_projection: ContextVar[bool] = ContextVar("repricer_page_finance", default=False)
page_connection: ContextVar[str | None] = ContextVar("repricer_page_connection", default=None)
VERSION = "repricer-finance-6-current-storage"
REALIZATION_VERSION = "wb-retail-realization-rrdate-v1"


def realization_cache_compatible(payload):
    """A legacy schema stamp does not establish the reporting-money contract."""
    return (payload.get("realizationVersion") == REALIZATION_VERSION
            and payload.get("realizationDateBasis") == "rrDate"
            and payload.get("realizationMoneyBasis") == "retailAmount"
            and payload.get("reportPeriod", "daily") == "daily"
            and payload.get("reportId") is None)


def report_revenue_kopecks(aggregate):
    """Public realization, not seller unit price, payout, or a truthy fallback."""
    for field in ("realizationRevenueKopecks", "buyerRevenueKopecks", "sellerRevenueKopecks", "revenueGrossKopecks"):
        if field in aggregate:
            return aggregate[field]
    return None


def connection_key(token: str) -> str:
    return sha256(token.encode()).hexdigest()[:24]


def save_operations(org: int, token: str, start: date, end: date, rows: list[dict[str, Any]], *,
                    requested_fields=None, report_period="daily", report_id=None) -> dict[str, Any]:
    from app.repricer_cache.store import save_source_cache
    from app.wb_api.financial_reports import SOURCE_VERSION, MONEY_FIELDS, integer_id
    if report_period not in {"daily", "weekly"}:
        raise ValueError("Invalid financial report period")
    if report_id is not None:
        report_id = integer_id(report_id)
    scope = f"{connection_key(token)}_{start}_{end}"
    key = (f"repricer_finance_{scope}" if report_period == "daily" and report_id is None
           else f"wb_realization_report_{scope}_{report_period}_{report_id or 'period'}")
    available = set(rows[0]) if rows else set()
    for row in rows[1:]:
        available.intersection_update(row)
    for row in rows:
        available.difference_update(field for field in MONEY_FIELDS
                                    if row.get(field) is None or isinstance(row.get(field), bool))
    fields = set(requested_fields) if requested_fields is not None else available
    return save_source_cache(org, key, {
        "operations": rows, "sourceComplete": True, "dateFrom": str(start), "dateTo": str(end),
        "projectionVersion": VERSION, "source": "finance_sales_reports_detailed",
        "sourceVersion": SOURCE_VERSION, "reportPeriod": report_period, "reportId": report_id,
        "requestedFields": sorted(fields), "operationCount": len(rows),
        "availableFields": sorted(available),
        "currencies": sorted({str(row.get("currency") or "").upper() for row in rows}),
    }, strict=True)


def _overlapping_metadata(metas, start=None, end=None):
    # Unknown bounds remain conservative dependencies, never a reason to reuse stale data.
    return [m for m in metas if not (start and m.get("dateTo") and str(m["dateTo"])[:10] < str(start)[:10])
            and not (end and m.get("dateFrom") and str(m["dateFrom"])[:10] > str(end)[:10])]


def page_finance_revision(org: int, start=None, end=None) -> str:
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix
    from app.cabinet.store import get_organization_wb_token_secret
    token = get_organization_wb_token_secret(org)
    key = page_connection.get() or (connection_key(token) if token else "missing")
    metas = [*list_source_cache_ranges_by_prefix(org, f"repricer_finance_{key}_", limit=100),
             *list_source_cache_ranges_by_prefix(org, "stock_history_", limit=500)]
    from app.wb_storage import revision as storage_revision
    return sha256(repr((VERSION, key, storage_revision(org), sorted((m["sourceKey"], str(m.get("fetchedAt"))) for m in _overlapping_metadata(metas, start, end)))).encode()).hexdigest()


def missing_page_ranges(org: int, token: str, start: date, end: date, *, fresh_only=False) -> list[tuple[date, date]]:
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix, get_source_cache, get_source_cache_completion_flags
    from app.wb_api.financial_reports import raw_source_compatible, SOURCE_VERSION
    covered = set()
    for meta in list_source_cache_ranges_by_prefix(org, f"repricer_finance_{connection_key(token)}_", limit=100):
        left, right = meta.get("dateFrom"), meta.get("dateTo")
        if not left or not right:
            continue
        left, right = date.fromisoformat(str(left)[:10]), date.fromisoformat(str(right)[:10])
        if right < start or left > end:
            continue
        if fresh_only:
            from app.wb_api.source_freshness import source_is_fresh
            if not source_is_fresh(meta):
                continue
        cache = get_source_cache_completion_flags(org, meta["sourceKey"])
        if cache.get("sourceVersion") != SOURCE_VERSION:
            cache = get_source_cache(org, meta["sourceKey"], slim=False) or {}
        if not raw_source_compatible(cache):
            continue
        covered.update(left+timedelta(days=i) for i in range((right-left).days+1))
        if all(start + timedelta(days=i) in covered for i in range((end-start).days+1)):
            return []
    missing = sorted(start+timedelta(days=i) for i in range((end-start).days+1) if start+timedelta(days=i) not in covered)
    ranges = []
    for day in missing:
        if ranges and day == ranges[-1][1]+timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def repricer_finance_scope(fn):
    @wraps(fn)
    def scoped(*args, **kwargs):
        marker = page_projection.set(True)
        connection_marker = page_connection.set(None)
        try:
            return fn(*args, **kwargs)
        finally:
            page_projection.reset(marker)
            page_connection.reset(connection_marker)
    scoped.__signature__ = signature(fn, eval_str=True)
    return scoped


def project_operations(rows: list[dict[str, Any]], start: date, end: date, *, allocate_account_costs=True) -> dict[str, Any]:
    from app.repricer_bff import (
        aggregate_finance_report_rows, _date_from_any, _finance_seller_revenue_kopecks,
        _kopecks_from_rub, _finance_raw_expense_totals,
    )

    projected = []
    buyer: dict[str, int] = {}
    compensation: dict[tuple[str, str], int] = {}
    adjustments: dict[tuple[str, str], int] = {}
    blockers = set()
    daily_buyer: dict[tuple[str, str], int] = {}
    seen: dict[str, dict[str, Any]] = {}
    # These charges are posting-day charges; logistics/penalties carry their
    # own service-operation date. Do not date all expense families by a sale.
    posting_fields = {"paidStorage", "paidAcceptance", "deduction", "paymentSchedule"}
    service_fields = {"deliveryService", "penalty", "additionalPayment",
                      "cashbackAmount", "cashbackDiscount", "cashbackCommissionChange"}
    for original in rows:
        identity = str(original.get("rrdId") or "")
        if not identity:
            raise ValueError("WB_FINANCE_INVALID_ROW_ID")
        if identity in seen:
            if seen[identity] != original:
                raise ValueError("WB_FINANCE_CONFLICTING_ROWS")
            continue
        seen[identity] = original
        doc = str(original.get("docTypeName") or "").strip().casefold()
        oper = str(original.get("sellerOperName") or "").strip().casefold()
        quantity = int(original.get("quantity") or 0)
        sale_day = _date_from_any(original.get("saleDt"))
        posting_day = _date_from_any(original.get("rrDate"))
        trade = ((doc, oper) in {("продажа", "продажа"), ("возврат", "возврат")}
                 and (quantity > 0 or (quantity == 0 and not allocate_account_costs)))
        if trade and original.get("retailPriceWithDisc") is None:
            raise ValueError("WB_FINANCE_INVALID_SELLER_PRICE")
        if trade and original.get("forPay") is None:
            raise ValueError("WB_FINANCE_MISSING_PAYABLE")
        nm = str(int(original.get("nmId") or 0))
        if trade and sale_day and start <= sale_day <= end:
            item = dict(original)
            for key in posting_fields | service_fields:
                item[key] = 0
            seller = _finance_seller_revenue_kopecks(original, quantity)
            # Derive commission from one consistent seller monetary basis.
            item["retailAmount"] = str(Decimal(seller) / 100)
            projected.append(item)
            sign = -1 if doc == "возврат" else 1
            buyer[nm] = buyer.get(nm, 0) + sign * _kopecks_from_rub(original.get("retailAmount") or 0)
            daily_key = (sale_day.isoformat(), nm)
            daily_buyer[daily_key] = daily_buyer.get(daily_key, 0) + sign * _kopecks_from_rub(original.get("retailAmount") or 0)
        # Non-merchandise payable is an adjustment, never sold units. Preserve
        # signed corrections separately from the trade's seller payout.
        if not trade and sale_day and start <= sale_day <= end:
            amount = _kopecks_from_rub(original.get("forPay") or 0)
            if amount:
                key = (sale_day.isoformat(), nm)
                signed = -amount if doc == "возврат" else amount
                adjustments[key] = adjustments.get(key, 0) + signed
                if "компенсац" in oper and "коррек" not in oper:
                    compensation[key] = compensation.get(key, 0) + signed
                elif "коррек" not in oper:
                    blockers.add("WB_FINANCE_UNCLASSIFIED_PAYABLE")
        for fields, day in ((posting_fields, posting_day), (service_fields, sale_day or posting_day)):
            if not day or not start <= day <= end:
                continue
            if not any(original.get(key) not in (None, 0, "0", "0.00") for key in fields):
                continue
            item = dict(original)
            item.update(docTypeName="", sellerOperName=original.get("sellerOperName"),
                        quantity=0, retailAmount=0, retailPriceWithDisc=0, forPay=0,
                        ppvzSalesCommission=0, acquiringFee=0,
                        saleDt=day.isoformat())
            for key in posting_fields | service_fields:
                item[key] = original.get(key, 0) if key in fields else 0
                if key in fields and key in {"cashbackAmount", "cashbackDiscount", "cashbackCommissionChange"} and doc == "возврат":
                    raw = item[key]
                    if raw is not None:
                        item[key] = str(-Decimal(str(raw)))
            projected.append(item)

    account_rows = [row for row in projected if int(row.get("nmId") or 0) <= 0]
    # Keep the legacy planning aggregator's default unchanged. Public financial
    # views must not turn a cabinet fee into an observed product expense.
    product_rows = projected if allocate_account_costs else [row for row in projected if int(row.get("nmId") or 0) > 0]
    result = aggregate_finance_report_rows(
        product_rows, date_from=datetime.combine(start, datetime.min.time()),
        date_to=datetime.combine(end, datetime.min.time()))
    if not allocate_account_costs:
        result["diagnostics"]["rawExpenseTotals"] = _finance_raw_expense_totals(projected)
        result["unallocatedFinancials"] = _finance_raw_expense_totals(account_rows)
        result["unallocatedDailyFinancials"] = {
            str(day): _finance_raw_expense_totals([row for row in account_rows if _date_from_any(row.get("saleDt")) == day])
            for day in {_date_from_any(row.get("saleDt")) for row in account_rows} if day is not None}
    result.pop("rows", None)
    # Preserve the original buyer-payment metric; it is not the commission base.
    for nm, row in result["aggregates"].items():
        row["settlementPayableConfirmed"] = (
            row.get("commissionSource") == "buyerRevenueKopecks-payableKopecks-acquiringKopecks"
            or row.get("salesUnits") == row.get("returnsUnits") == row.get("payableKopecks") == 0
        )
        row["buyerRevenueKopecks"] = buyer.get(nm, 0)
        row["commissionSource"] = "sellerRevenueKopecks-tradePayableKopecks-acquiringKopecks"
    for day, daily_rows in result["dailyAggregates"].items():
        for nm, row in daily_rows.items():
            row["settlementPayableConfirmed"] = row.get("commissionSource") == "buyerRevenueKopecks-payableKopecks-acquiringKopecks" or row.get("salesUnits") == row.get("returnsUnits") == row.get("payableKopecks") == 0
            row["buyerRevenueKopecks"] = daily_buyer.get((day, nm), 0)
            row["commissionSource"] = "sellerRevenueKopecks-tradePayableKopecks-acquiringKopecks"
    for (day, nm), amount in adjustments.items():
        # Use a complete zero-operation row rather than injecting six revenue
        # fields into an otherwise missing SKU record.
        if nm == "0":
            if allocate_account_costs:
                raise ValueError("WB_FINANCE_UNALLOCATED_PAYABLE")
            account = result["unallocatedFinancials"]
            account["additionalPaymentKopecks"] = account.get("additionalPaymentKopecks", 0) + amount
            account["compensationKopecks"] = account.get("compensationKopecks", 0) + compensation.get((day, nm), 0)
            continue
        if nm not in result["aggregates"] or nm not in result["dailyAggregates"].get(day, {}):
            seed = {"rrdId": f"adjustment-{day}-{nm}", "nmId": int(nm),
                    "quantity": 0, "docTypeName": "", "saleDt": day, "forPay": 0}
            empty = aggregate_finance_report_rows([seed], date_from=datetime.combine(start, datetime.min.time()),
                                                  date_to=datetime.combine(end, datetime.min.time()))
            result["aggregates"].setdefault(nm, empty["aggregates"][nm])
            result["dailyAggregates"].setdefault(day, {}).setdefault(nm, empty["dailyAggregates"][day][nm])
        result["aggregates"][nm]["additionalPaymentKopecks"] += amount
        result["dailyAggregates"][day][nm]["additionalPaymentKopecks"] += amount
    for nm, row in result["aggregates"].items():
        row["compensationKopecks"] = sum(value for (_, product), value in compensation.items() if product == nm)
        if not allocate_account_costs:
            row["financeAdSpendAuthoritative"] = True
    for day, daily_rows in result["dailyAggregates"].items():
        for nm, row in daily_rows.items():
            row["compensationKopecks"] = compensation.get((day, nm), 0)
    from app.repricer_bff import build_finance_diagnostics_from_aggregates
    # Retain raw account-level fees even when there are no product allocation
    # weights. They are cabinet expenses, never an invented product row.
    raw_expenses = result.get("diagnostics", {}).get("rawExpenseTotals")
    result["diagnostics"] = build_finance_diagnostics_from_aggregates(result["aggregates"])
    if isinstance(raw_expenses, dict):
        raw_expenses = dict(raw_expenses)
        raw_expenses["additionalPaymentKopecks"] = raw_expenses.get("additionalPaymentKopecks", 0) + sum(adjustments.values())
        result["diagnostics"]["rawExpenseTotals"] = raw_expenses
    result.update(projectionVersion=VERSION, compensationKopecks=sum(compensation.values()),
                  projectionBlockers=sorted(blockers))
    if blockers:
        for row in result["aggregates"].values():
            row["settlementComponentsConfirmed"] = False
    return result


def project_realization_operations(rows, start, end):
    """Financial report membership is rrDate; dated economics keeps saleDt.

    Retain seller-price inputs separately. Only the public realization and its
    matching commission bridge use retailAmount. No price strategy is changed.
    """
    from app.repricer_bff import _date_from_any, build_finance_diagnostics_from_aggregates
    from app.wb_api.financial_reports import is_merchandise, money_kopecks
    scoped = [row for row in rows if (day := _date_from_any(row.get("rrDate"))) and start <= day <= end]
    for row in scoped:
        if not is_merchandise(row):
            continue
        if money_kopecks(row.get("retailAmount")) is None:
            raise ValueError("WB_FINANCE_REALIZATION_AMOUNT_MISSING")
        quantity = row.get("quantity")
        if type(quantity) is not int or quantity < 0:
            raise ValueError("WB_FINANCE_INVALID_TRADE_QUANTITY")
        if not _date_from_any(row.get("saleDt")):
            raise ValueError("WB_FINANCE_TRADE_DATE_MISSING")
    business_days = [_date_from_any(row.get("saleDt")) for row in scoped]
    business_days = [day for day in business_days if day is not None]
    result = project_operations(scoped, min([start, *business_days]), max([end, *business_days]),
                                allocate_account_costs=False)
    # project_operations has already rejected conflicting identities. Use its
    # same deduplicated universe for the account-level bridge below as well.
    scoped = list({str(row["rrdId"]): row for row in scoped}.values())
    amounts, daily, seen, posting_daily = {}, {}, set(), {}
    for row in scoped:
        identity = str(row["rrdId"])
        if identity in seen:
            continue
        seen.add(identity)
        if not is_merchandise(row):
            continue
        nm = str(int(row.get("nmId") or 0))
        day = _date_from_any(row.get("saleDt"))
        field = "realizationReturnsKopecks" if str(row.get("docTypeName")).strip().casefold() == "возврат" else "realizationGrossSalesKopecks"
        value = money_kopecks(row.get("retailAmount"))
        posted = posting_daily.setdefault(_date_from_any(row["rrDate"]).isoformat(), {}).setdefault(nm,
            {"salesUnits": 0, "returnsUnits": 0, "netSalesUnits": 0, "revenueKopecks": 0})
        returned_document = str(row.get("docTypeName")).strip().casefold() == "возврат"
        posted["returnsUnits" if returned_document else "salesUnits"] += row["quantity"]
        posted["netSalesUnits"] += (-1 if returned_document else 1) * row["quantity"]
        posted["revenueKopecks"] += (-1 if returned_document else 1) * value
        for bucket in (amounts.setdefault(nm, {}), daily.setdefault((day.isoformat(), nm), {})):
            bucket[field] = None if value is None or bucket.get(field, 0) is None else bucket.get(field, 0) + value
    for scope, aggregates in [(None, result["aggregates"]), *result["dailyAggregates"].items()]:
        for nm, aggregate in aggregates.items():
            values = amounts.get(nm, {}) if scope is None else daily.get((scope, nm), {})
            gross, returned = (values.get(key, 0) for key in ("realizationGrossSalesKopecks", "realizationReturnsKopecks"))
            revenue = gross - returned if gross is not None and returned is not None else None
            aggregate.update(realizationRevenueKopecks=revenue, realizationVersion=REALIZATION_VERSION,
                             realizationGrossSalesKopecks=gross, realizationReturnsKopecks=returned)
            if revenue is not None:
                delta = int(aggregate.get("sellerRevenueKopecks") or 0) - revenue
                aggregate["commissionKopecks"] -= delta
                aggregate["commissionSource"] = "buyerRevenueKopecks-payableKopecks-acquiringKopecks"
    raw = result.get("diagnostics", {}).get("rawExpenseTotals")
    result["diagnostics"] = build_finance_diagnostics_from_aggregates(result["aggregates"])
    if isinstance(raw, dict):
        raw = dict(raw)
        result["diagnostics"]["rawExpenseTotals"] = raw
    result.update(realizationVersion=REALIZATION_VERSION, realizationDateBasis="rrDate",
                  realizationMoneyBasis="retailAmount", postingDailyRealization=posting_daily)
    unknown_ad_products = {str(int(row.get("nmId") or 0)) for row in scoped
        if money_kopecks(row.get("deduction")) not in (None, 0) and not row.get("bonusTypeName")}
    result["financialAdvertisingComplete"] = not unknown_ad_products
    result["diagnostics"]["financialAdvertisingComplete"] = not unknown_ad_products
    if unknown_ad_products:
        result["projectionBlockers"] = sorted(set(result.get("projectionBlockers", [])) | {"WB_FINANCE_ADVERTISING_CLASSIFICATION_MISSING"})
        for nm, aggregate in result["aggregates"].items():
            if nm in unknown_ad_products or "0" in unknown_ad_products:
                aggregate["financeAdSpendAuthoritative"] = False
                aggregate["settlementComponentsConfirmed"] = False
    account_trades = [products.get("0", {}) for products in posting_daily.values()]
    result["unallocatedRealization"] = {field: sum(row.get(field, 0) for row in account_trades)
        for field in ("salesUnits", "returnsUnits", "netSalesUnits", "revenueKopecks")}
    # Reconcile the account's commission bridge too, without inventing a
    # product to hold trades whose nmId is missing.
    account_commission = sum((-1 if str(row.get("docTypeName")).casefold() == "возврат" else 1)
        * (money_kopecks(row["retailAmount"]) - money_kopecks(row["forPay"]) - (money_kopecks(row.get("acquiringFee")) or 0))
        for row in scoped if is_merchandise(row) and int(row.get("nmId") or 0) <= 0)
    account_costs = result.get("unallocatedFinancials", {})
    account_costs["commissionKopecks"] = account_commission
    raw_costs = result["diagnostics"].get("rawExpenseTotals", {})
    # The service-cost extractor has no trade commission field. Starting from
    # its missing value (zero) erased the product commissions and manufactured
    # an unallocated residual in cabinet summaries. Use the same signed bridge
    # as the product rows, plus genuinely unattributed trades exactly once.
    raw_costs["commissionKopecks"] = sum(
        aggregate["commissionKopecks"] for aggregate in result["aggregates"].values()
    ) + account_commission
    result["diagnostics"]["unallocatedRealization"] = result["unallocatedRealization"]
    return result


def load_page_finance(org: int, start: date, end: date, *, projector=None, include_undated=False,
                      cached_only=False) -> dict[str, Any] | None:
    """Read the source-revision-scoped projection; cached_only never builds or saves."""
    from app.repricer_cache.store import get_source_cache, list_source_cache_ranges_by_prefix, save_source_cache
    from app.repricer_bff import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION, _date_from_any
    from app.wb_api.financial_reports import raw_source_compatible, SOURCE_VERSION
    operations = {}
    unverified_operations = set()
    covered: set[date] = set()
    wanted = {start + timedelta(days=i) for i in range((end-start).days+1)}
    wanted_days = {day.isoformat() for day in wanted}
    observed = ""
    pnl_observations = []
    source_observations = []
    binding_verified = True
    owned: set[str] = set()
    from app.cabinet.store import get_organization_wb_token_secret
    token = get_organization_wb_token_secret(org)
    key = page_connection.get() or (connection_key(token) if token else None)
    from app.repricer_sync import wb_token_fingerprint
    resolved_fingerprint = wb_token_fingerprint(token) if token and connection_key(token) == key else None
    metas = list_source_cache_ranges_by_prefix(org, f"repricer_finance_{key}_", limit=100) if key else []
    histories = list_source_cache_ranges_by_prefix(org, "stock_history_", limit=500) if key else []
    metas = _overlapping_metadata(metas, start, end)
    histories = _overlapping_metadata(histories, start, end)
    # The projector module version separates fee/realization definitions while
    # allowing a worker's DB-only projection to be reused by another process.
    import sys
    projection_id = "default"
    if projector is not None:
        module = sys.modules.get(projector.__module__)
        projection_id = f"{projector.__name__}_{getattr(module, 'VERSION', VERSION)}"
    cache_key = (f"page_finance_projection_{VERSION}_{key}_{start}_{end}_{projection_id}_{int(include_undated)}_binding1"
                 if key else None)
    from app.wb_storage import revision as storage_revision
    source_revision = sha256(repr((storage_revision(org), sorted(
        (m["sourceKey"], str(m.get("dateFrom")), str(m.get("dateTo")), str(m.get("fetchedAt")))
        for m in [*metas, *histories]
    ))).encode()).hexdigest()
    if cache_key:
        cached = get_source_cache(org, cache_key) or {}
        if cached.get("sourceRevision") == source_revision and isinstance(cached.get("result"), dict):
            return cached["result"]
    if cached_only:
        return None
    for meta in sorted(metas, key=lambda m: str(m.get("fetchedAt") or ""), reverse=True):
        left, right = meta.get("dateFrom"), meta.get("dateTo")
        if not left or not right:
            continue
        left, right = date.fromisoformat(str(left)[:10]), date.fromisoformat(str(right)[:10])
        if right < start or left > end:
            continue
        days = {(left + timedelta(days=i)).isoformat() for i in range((right-left).days+1)}
        available = days-owned
        if not available:
            continue
        payload = get_source_cache(org, meta["sourceKey"], slim=False) or {}
        if (payload.get("sourceComplete") is not True or not isinstance(payload.get("operations"), list)
                or payload.get("reportPeriod", "daily") != "daily" or payload.get("reportId") is not None):
            continue
        compatible = raw_source_compatible(payload)
        if not compatible and not include_undated:
            # Do not let an eight-field/foreign-money receipt shadow an older
            # valid source or fabricate zero expenses from absent fields.
            continue
        if include_undated and any(start.isoformat() <= day <= end.isoformat() for day in available):
            pnl_observations.append(payload.get("fetchedAt"))
        receipt_rows = {}
        for row in payload["operations"]:
            if str(row.get("currency") or "").upper() not in {"RUB", "RUR", "643"}:
                continue  # No implicit conversion of unknown/historical currencies.
            parsed = _date_from_any(row.get("rrDate"))
            posting = parsed.isoformat() if parsed else ""
            if include_undated:
                # P&L must flag an undated fee instead of certifying a false
                # zero. Existing repricer/report callers retain their scope.
                parsed = _date_from_any(row.get("rrDate"))
                posting = parsed.isoformat() if parsed else ""
            if posting in available or (include_undated and not posting and left <= end and right >= start):
                identity = str(row["rrdId"])
                if identity in receipt_rows and receipt_rows[identity] != row:
                    raise ValueError("WB_FINANCE_CONFLICTING_ROWS")
                receipt_rows[identity] = row
                if compatible and identity in unverified_operations:
                    operations[identity] = row
                    unverified_operations.discard(identity)
                elif identity in operations and compatible and identity not in unverified_operations:
                    continue  # newest verified observation supersedes old receipts
                elif identity not in operations:
                    operations[identity] = row
                    if not compatible:
                        unverified_operations.add(identity)
        if compatible:
            if available & wanted_days:
                source_observations.append(oldest_source_observation(payload))
                # Do not infer ownership from a field copied into the result.
                # Verify the actual selected receipt's storage namespace.
                binding_verified = binding_verified and str(meta["sourceKey"]).startswith(f"repricer_finance_{key}_")
                for receipt in (meta, payload):
                    if "organizationId" in receipt:
                        binding_verified = binding_verified and type(receipt["organizationId"]) is int and receipt["organizationId"] == org
                    if receipt.get("strategyTokenFingerprint") is not None:
                        binding_verified = binding_verified and receipt["strategyTokenFingerprint"] == resolved_fingerprint
            owned.update(days)
            covered.update(date.fromisoformat(day) for day in available)
        observed = max(observed, str(payload.get("fetchedAt") or ""))
    result = (projector or project_realization_operations)(list(operations.values()), start, end)
    result.update(sourceComplete=wanted <= covered, coverageState="ok" if wanted <= covered else "partial",
                  coveredDays=len(wanted & covered), requestedDays=len(wanted),
                  missingDates=sorted(day.isoformat() for day in wanted-covered),
                  dateFrom=start.isoformat(), dateTo=end.isoformat(), periodDays=len(wanted),
                  fetchedAt=observed or None, revenueBasis=FINANCE_REVENUE_BASIS,
                  financeSchemaVersion=FINANCE_SCHEMA_VERSION,
                  source="finance_sales_reports_detailed", sourceVersion=SOURCE_VERSION,
                  reportPeriod="daily", sourceScope="period")
    result["oldestFetchedAt"] = (min(source_observations).isoformat()
        if source_observations and all(at is not None for at in source_observations) else None)
    if key and binding_verified and source_observations:
        result["strategySourceBinding"] = {"version": FINANCE_BINDING_VERSION,
            "organizationId": org, "connectionKey": key}
        if resolved_fingerprint:
            result["strategyTokenFingerprint"] = resolved_fingerprint
    if include_undated:
        result["pnlExpenseObservedAt"] = pnl_observations
    if result.get("realizationVersion") == REALIZATION_VERSION:
        from app.wb_report_finance import apply_storage_allocation
        apply_storage_allocation(result, org, start, end, histories)
    if not result["sourceComplete"]:
        for row in result["aggregates"].values():
            row["settlementComponentsConfirmed"] = False
    if cache_key:
        # Source metadata invalidates this projection; keep its observation time inside the result.
        save_source_cache(org, cache_key, {"sourceRevision": source_revision, "result": result})
    return result
