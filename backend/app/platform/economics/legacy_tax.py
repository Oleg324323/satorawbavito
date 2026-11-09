"""Read-only WB financial inputs; current-setting reports opt in explicitly."""
from datetime import date, timedelta
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.infra.db import get_session_factory, set_tenant_context
from app.modules.wb_reports.abc_pnl import _day_end, _round_basis_points
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceOfferRow, MarketplaceProductRow
from app.platform.catalog.service import CatalogService
from app.platform.economics.orm import CatalogEconomicsOverrideVersionRow, OrganizationEconomicsVersionRow
from app.platform.economics.wb_quarterly_tax import active_settings, wb_policies_for_points, settings_in_session, revision as quarterly_revision
from app.platform.economics.costs import CostsService
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import MOSCOW, Period
from app.repricer_cache.store import finance_cache_uses_current_revenue_basis


def _missing(reason: str) -> dict[str, Any]:
    return {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": reason}


def _revenue(row: dict[str, Any]) -> int | None:
    if "realizationRevenueKopecks" in row:
        value = row["realizationRevenueKopecks"]
        return value if type(value) is int else None
    for key in ("sellerRevenueKopecks", "revenueGrossKopecks", "revenueKopecks", "buyerRevenueKopecks"):
        value = row.get(key)
        if value is not None:
            return value if type(value) is int else None
    return None


def _confirmed(policy: Any) -> bool:
    return bool(policy is not None and policy.tax_basis_points is not None
                and policy.tax_value_state == "configured" and policy.tax_evidence_status == "dated")


def _organization_quarter_policy(settings: dict[str, Any], instant: Any) -> Any:
    """Explicit P&L rate, independent of SKU existence; never planning taxPct."""
    day = instant.astimezone(MOSCOW).date()
    schedule = settings.get("wbQuarterlyTax")
    entry = schedule.get(str(day.year)) if isinstance(schedule, dict) else None
    if not isinstance(entry, dict):
        return None
    index = (day.month - 1) // 3
    flags, rates = entry.get("configured"), entry.get("basisPoints")
    if not (isinstance(flags, list) and len(flags) == 4 and flags[index] is True
            and isinstance(rates, list) and len(rates) == 4):
        return None
    rate = rates[index]
    if type(rate) is not int or not 0 <= rate <= 10000:
        return None
    return SimpleNamespace(tax_basis_points=rate, tax_value_state="configured", tax_evidence_status="dated")


def _prepare_read(session: Any, organization_id: int) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(session, organization_id)


def _settlement_cost(fact, entries, *, sku_id, costs, dates, instants, has_daily, current_cost=None):
    """Resolve inventory cost independently of whether a tax policy is known."""
    missing = {"settlementCogsKopecks": None, "settlementCogsState": "missing"}
    points = [(instants[day], row) for day, row in entries] if has_daily else [(instants[dates[0]], fact)]
    fields = ("salesUnits", "returnsUnits")
    if (any(type(row.get(field)) is not int for row in [fact, *(row for _, row in points)] for field in fields)
            or has_daily and any(sum(row[field] for _, row in points) != fact[field] for field in fields)):
        return {**missing, "settlementCogsReason": "cost_daily_coverage_mismatch"}
    # Charges without any merchandise movement do not require a purchase cost.
    if all(row[field] == 0 for _, row in points for field in fields):
        return {"settlementCogsKopecks": 0, "settlementCogsState": "configured", "settlementCogsReason": None}
    points = [(instant, row) for instant, row in points if row["salesUnits"] or row["returnsUnits"]]
    if sku_id is None and current_cost is None:
        return {**missing, "settlementCogsReason": "cost_mapping_missing_or_ambiguous",
                "settlementCostMissingDates": [str(day) for day, row in entries if row["salesUnits"] or row["returnsUnits"]] if has_daily else [str(day) for day in dates]}
    values = [costs.get((sku_id, instant)) for instant, _ in points]

    def usable(value):
        return value is not None and (value.value_state == 'configured' or
            (value.value_state == 'assumed' and value.source == 'user-example'))

    confirmed = all(usable(value) and value.evidence_status == "dated"
                    and type(value.amount_kopecks) is int for value in values)
    # Daily units cannot be split around an intraday edit. Preserve the existing
    # requirement that the same cost was effective at the beginning of the day.
    confirmed = confirmed and all(
        (opening := costs.get((sku_id, instant - timedelta(days=1) + timedelta(microseconds=1)))) is not None
        and opening.cost_version_id == value.cost_version_id
        for value, (instant, _) in zip(values, points)
    )
    if not has_daily:
        all_values = [costs.get((sku_id, instant)) for instant in instants.values()]
        confirmed = confirmed and all(usable(value) and value.evidence_status == "dated"
            and value.amount_kopecks == values[0].amount_kopecks for value in all_values)
    # P&L explicitly permits persisted current Repricer inputs as a fallback.
    # No other reader passes this option. Keep confirmed historical operation
    # costs, and do not manufacture dated CostValue records for current inputs.
    if not confirmed and type(current_cost) is int and current_cost >= 0:
        historical = [value.amount_kopecks if usable(value) and value.evidence_status == "dated"
            and type(value.amount_kopecks) is int
            and (opening := costs.get((sku_id, instant - timedelta(days=1) + timedelta(microseconds=1)))) is not None
            and opening.cost_version_id == value.cost_version_id else None
            for value, (instant, _) in zip(values, points)]
        # Without daily quantities, a partially covered/changing historical
        # series cannot be combined truthfully with one current unit cost.
        if has_daily or not any(usable(value) and value.evidence_status == "dated" for value in all_values):
            return {"settlementCogsKopecks": sum((amount if amount is not None else current_cost)
                        * (row["salesUnits"] - row["returnsUnits"])
                        for amount, (_, row) in zip(historical, points)),
                    "settlementCogsState": "current",
                    "settlementCogsBasis": "mixed_historical_and_current" if any(value is not None for value in historical) else "saved_repricer_current_cost",
                    "settlementCogsReason": None, "settlementCostMissingDates": []}
    return {
        "settlementCogsState": 'assumed' if confirmed and any(value.value_state == 'assumed' for value in values) else 'configured' if confirmed else 'missing',
        "settlementCogsKopecks": sum(value.amount_kopecks * (row["salesUnits"] - row["returnsUnits"])
            for value, (_, row) in zip(values, points)) if confirmed else None,
        "settlementCogsReason": None if confirmed else "dated_cost_missing_or_daily_basis_incomplete",
        "settlementCostMissingDates": [] if confirmed else [str(day) for day, row in entries
            if row["salesUnits"] or row["returnsUnits"]] if has_daily else [str(day) for day in dates],
    }


def get_legacy_finance_taxes(
    organization_id: int, finance_cache: dict[str, Any], period: Period,
    *, include_costs: bool = False, current_costs: dict[int, int] | None = None,
    allow_unmapped_quarterly_tax: bool = False,
    use_current_settings: bool = False,
) -> dict[str, dict[str, Any]]:
    if use_current_settings:
        return _current_wb_inputs(organization_id, finance_cache, include_costs=include_costs,
                                  current_costs=current_costs)
    source = finance_cache.get("aggregates")
    aggregates = {str(key): value for key, value in source.items() if isinstance(value, dict)} if isinstance(source, dict) else {}
    if not aggregates:
        return {}
    result = {key: _missing("tax_policy_unavailable") for key in aggregates}
    if not finance_cache_uses_current_revenue_basis(finance_cache):
        return {key: _missing("tax_revenue_basis_unconfirmed") for key in aggregates}
    dates = [period.date_from + timedelta(days=offset) for offset in range(period.days)]
    nm_ids = {key: int(key) for key in aggregates if key.isdigit() and int(key) > 0}
    daily: dict[str, list[tuple[date, dict[str, Any]]]] = {}
    has_daily = isinstance(finance_cache.get("dailyAggregates"), dict)
    business_dates = set(dates)
    # A settlement report can include delayed operations whose saleDt precedes
    # the requested report period. Its aggregate includes those operations too.
    # Reconcile every business day ONLY when this is the exact report window;
    # apply the dated policy/cost at the operation's original business date.
    if (has_daily and str(finance_cache.get("dateFrom"))[:10] == str(period.date_from)
            and str(finance_cache.get("dateTo"))[:10] == str(period.date_to)):
        for raw_day in finance_cache["dailyAggregates"]:
            try:
                business_dates.add(date.fromisoformat(raw_day))
            except (ValueError, TypeError):
                continue
    instants = {day: _day_end(day) for day in business_dates}
    if has_daily:
        for day in sorted(business_dates):
            values = finance_cache["dailyAggregates"].get(day.isoformat()) or {}
            if isinstance(values, dict):
                for key, value in values.items():
                    if isinstance(value, dict):
                        daily.setdefault(str(key), []).append((day, value))
    if include_costs:
        for key, fact in aggregates.items():
            result[key].update(_settlement_cost(fact, daily.get(key, []), sku_id=None, costs={},
                dates=dates, instants=instants, has_daily=has_daily))
    try:
        with get_session_factory()() as session:
            _prepare_read(session, organization_id)
            account_ids = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.organization_id == organization_id,
                MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected",
            )).all()
            if len(account_ids) != 1:
                reason = "tax_account_missing" if not account_ids else "tax_account_ambiguous"
                return {key: {**result[key], **_missing(reason)} for key in aggregates}
            if allow_unmapped_quarterly_tax:
                # The P&L caller already reads connection-scoped finance. Do
                # not accept explicitly foreign receipt metadata either.
                for field, expected, reason in (
                    ("organizationId", organization_id, "tax_organization_mismatch"),
                    ("marketplaceAccountId", account_ids[0], "tax_account_mismatch"),
                ):
                    if finance_cache.get(field, expected) != expected:
                        return {key: {**result[key], **_missing(reason)} for key in aggregates}
            mapping, ambiguous = CatalogService(session, organization_id).resolve_wb_product_skus(account_ids[0], list(nm_ids.values()))
            policies = wb_policies_for_points(session, organization_id, [
                (sku_id, instant) for sku_id in set(mapping.values()) for instant in instants.values()
            ])
            quarter_settings = active_settings(session, organization_id) if allow_unmapped_quarterly_tax else {}
            quarter_policies = {
                instant: _organization_quarter_policy(quarter_settings, instant)
                for instant in instants.values()
            } if allow_unmapped_quarterly_tax else {}
            costs = CostsService(session, organization_id).get_costs_for_points([
                (sku_id, point) for sku_id in set(mapping.values()) for instant in instants.values()
                for point in (instant, instant - timedelta(days=1) + timedelta(microseconds=1))
            ]) if include_costs else {}
            for key, fact in aggregates.items():
                nm_id = nm_ids.get(key)
                sku_id = mapping.get(nm_id)
                if include_costs:
                    result[key].update(_settlement_cost(fact, daily.get(key, []),
                        sku_id=None if nm_id in ambiguous else sku_id, costs=costs,
                        dates=dates, instants=instants, has_daily=has_daily,
                        current_cost=(current_costs or {}).get(nm_id) if nm_id not in ambiguous else None))
                unmapped_quarter = allow_unmapped_quarterly_tax and nm_id is not None and sku_id is None and nm_id not in ambiguous
                if nm_id in ambiguous or (sku_id is None and not unmapped_quarter):
                    result[key].update(_missing("tax_mapping_ambiguous" if nm_id in ambiguous else "tax_mapping_missing"))
                    continue
                if unmapped_quarter and any(str(fact[field]) != str(nm_id)
                        for field in ("nmId", "nmID") if field in fact):
                    result[key].update(_missing("tax_product_identity_unconfirmed"))
                    continue
                revenue = _revenue(fact)
                if revenue is None:
                    result[key].update(_missing("tax_revenue_basis_unconfirmed"))
                    continue
                entries = daily.get(key, [])
                if has_daily:
                    unit_fields = ("salesUnits", "returnsUnits")
                    if (not entries
                        or any(type(row.get(field)) is not int for row in [fact, *(row for _, row in entries)] for field in unit_fields)
                        or any(_revenue(row) is None for _, row in entries)
                        or sum(_revenue(row) for _, row in entries) != revenue
                        or any(sum(row[field] for _, row in entries) != fact[field] for field in unit_fields)):
                        result[key].update(_missing("tax_daily_coverage_mismatch"))
                        continue
                    bases = [(quarter_policies.get(instants[day]) if unmapped_quarter else policies.get((sku_id, instants[day])),
                              _revenue(row)) for day, row in entries]
                else:
                    values = [quarter_policies.get(instant) if unmapped_quarter else policies.get((sku_id, instant))
                              for instant in instants.values()]
                    if not all(_confirmed(policy) for policy in values):
                        result[key].update(_missing("tax_policy_unconfirmed"))
                        continue
                    if len({policy.tax_basis_points for policy in values}) != 1:
                        result[key].update(_missing("tax_daily_basis_missing"))
                        continue
                    bases = [(values[0], revenue)]
                if not all(_confirmed(policy) for policy, _ in bases):
                    result[key].update(_missing("tax_policy_unconfirmed"))
                    continue
                grouped: dict[int, int] = {}
                for policy, amount in bases:
                    rate = policy.tax_basis_points
                    grouped[rate] = grouped.get(rate, 0) + amount
                result[key].update({
                    "taxKopecks": sum(_round_basis_points(amount, rate) for rate, amount in grouped.items()),
                    "factTaxState": "configured", "factTaxReason": None,
                })
            session.rollback()
    except SQLAlchemyError:
        return {key: _missing("tax_policy_unavailable") for key in aggregates}
    return result


def _current_wb_inputs(organization_id, finance_cache, *, include_costs, current_costs):
    """WB report policy: today's saved unit cost and profile rate, for any period.

    No dated records are created and no imported financial amounts are changed.
    The dated service remains available to callers with a different contract.
    """
    from app.platform.economics.wb_quarterly_tax import basis_points, planning_tax_pct
    from app.wb_pnl_costs import saved_current_costs
    aggregates = finance_cache.get("aggregates") or {}
    result = {}
    try:
        with get_session_factory()() as session:
            _prepare_read(session, organization_id)
            accounts = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.organization_id == organization_id,
                MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all()
            settings = active_settings(session, organization_id)
            try:
                rate = basis_points(planning_tax_pct(settings))
            except ValueError:
                rate = None
            session.rollback()
    except (SQLAlchemyError, ValueError):
        return {str(key): {**_missing("tax_policy_unavailable"),
            **({"settlementCogsKopecks": None, "settlementCogsState": "missing"} if include_costs else {})}
            for key in aggregates}
    scope_ok = len(accounts) == 1 and finance_cache.get("organizationId", organization_id) == organization_id \
        and finance_cache.get("marketplaceAccountId", accounts[0]) == accounts[0]
    costs = (current_costs if current_costs is not None else saved_current_costs(organization_id, finance_cache)) if scope_ok and include_costs else {}
    for raw_nm, fact in aggregates.items():
        if not isinstance(fact, dict):
            continue
        nm = int(raw_nm) if str(raw_nm).isdigit() else None
        revenue = _revenue(fact) if finance_cache_uses_current_revenue_basis(finance_cache) else None
        reason = ("tax_account_missing" if not accounts else "tax_account_ambiguous" if len(accounts) != 1 else
                  "tax_account_mismatch" if not scope_ok else "tax_policy_unconfirmed" if rate is None else
                  "tax_revenue_basis_unconfirmed" if type(revenue) is not int else None)
        row = _missing(reason) if reason else {"taxKopecks": _round_basis_points(revenue, rate),
            "factTaxState": "configured", "factTaxReason": None}
        row["factTaxBasis"] = "current_profile_rate"
        if include_costs:
            sales, returns = fact.get("salesUnits"), fact.get("returnsUnits")
            units_known = type(sales) is int and type(returns) is int
            unit_cost = costs.get(nm)
            known = scope_ok and units_known and (sales == returns == 0 or type(unit_cost) is int and unit_cost >= 0)
            row.update(settlementCogsKopecks=(0 if sales == returns == 0 else unit_cost * (sales - returns)) if known else None,
                settlementCogsState="current" if known else "missing", settlementCogsBasis="saved_current_unit_cost",
                settlementCogsReason=None if known else "current_cost_missing_or_ambiguous" if units_known else "cost_units_missing",
                settlementCostMissingDates=[])
        result[str(raw_nm)] = row
    return result


def legacy_finance_tax_revision(organization_id: int) -> str:
    """Version saved calculation inputs without loading financial receipts."""
    models = (OrganizationEconomicsVersionRow, CatalogEconomicsOverrideVersionRow, CatalogCostVersionRow,
              CatalogSkuRow, MarketplaceAccountRow, MarketplaceProductRow, MarketplaceOfferRow)
    statements = []
    for model in models:
        statements.append(select(func.count()).select_from(model).where(model.organization_id == organization_id).scalar_subquery())
        if hasattr(model, "updated_at"):
            statements.append(select(func.max(model.updated_at)).where(model.organization_id == organization_id).scalar_subquery())
    # Photo backfill only fills missing URLs. Count changes even when multiple
    # batches commit within SQLite's one-second updated_at resolution.
    statements.append(select(func.count(func.nullif(MarketplaceProductRow.image_url, ''))).where(
        MarketplaceProductRow.organization_id == organization_id).scalar_subquery())
    try:
        with get_session_factory()() as session:
            _prepare_read(session, organization_id)
            revision = list(session.execute(select(*statements)).one())
            revision.append(quarterly_revision(settings_in_session(session, organization_id)))
            session.rollback()
    except SQLAlchemyError:
        return "unavailable"
    from app.wb_pnl_costs import current_cost_revision
    from app.wb_storage import revision as storage_revision
    return hashlib.sha256(json.dumps(["wb-current-inputs-v2", revision,
        current_cost_revision(organization_id), storage_revision(organization_id)], default=str, separators=(",", ":")).encode()).hexdigest()
