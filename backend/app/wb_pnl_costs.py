"""Bulk saved current WB unit costs, without creating historical records."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.infra.db import get_session_factory
from app.platform.catalog.service import CatalogService
from app.platform.economics.costs import CostsService
from app.platform.economics.legacy_tax import _prepare_read
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_bff import _article_type
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow

VERSION = "wb-saved-current-costs-v2-card-category"


def _money(value, *, rubles=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)) * (100 if rubles else 1)
        if amount.is_finite() and amount >= 0 and amount == amount.to_integral_value():
            return int(amount)
    except (InvalidOperation, ValueError):
        pass
    return None


def _saved_inputs(session, org):
    # Do not hydrate audit events, approvals and other runtime JSON to read costs.
    overrides = session.scalar(select(WbRepricerRuntimeStateRow.state_payload["skuSettingsOverrides"]).where(
        WbRepricerRuntimeStateRow.organization_id == org))
    algorithm = session.scalar(select(WbRepricerAlgorithmSettingsRow.settings_payload).where(
        WbRepricerAlgorithmSettingsRow.organization_id == org))
    return {"skuSettingsOverrides": overrides or {}}, algorithm or {}


def current_cost_revision(org):
    """Include actual cost values, not second-resolution settings timestamps."""
    try:
        with get_session_factory()() as session:
            _prepare_read(session, org)
            runtime, algorithm = _saved_inputs(session, org)
            goods = session.execute(select(func.count(), func.max(WbRepricerGoodsCacheRow.fetched_at)).where(
                WbRepricerGoodsCacheRow.organization_id == org)).one()
            cards_observed = session.scalar(select(WbRepricerSourceCacheRow.fetched_at).where(
                WbRepricerSourceCacheRow.organization_id == org,
                WbRepricerSourceCacheRow.source_key == "content_cards"))
            inputs = [VERSION, cards_observed, algorithm.get("cogsByGarmentRub"), {
                article: settings.get("cogsKopecks")
                for article, settings in (runtime.get("skuSettingsOverrides") or {}).items()
                if isinstance(settings, dict) and "cogsKopecks" in settings
            }, list(goods)]
            session.rollback()
    except SQLAlchemyError:
        return "unavailable"
    return sha256(json.dumps(inputs, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _card_garments(session, org, context, ids):
    """Use saved WB category evidence only for otherwise unclassified articles."""
    from app.wb_pnl_advertising import _instant
    cached = session.scalar(select(WbRepricerSourceCacheRow).where(
        WbRepricerSourceCacheRow.organization_id == org,
        WbRepricerSourceCacheRow.source_key == "content_cards",
        WbRepricerSourceCacheRow.fetched_at >= _instant(context["connectedAt"])))
    if cached is None:
        return {}
    payload = cached.payload or {}
    if (payload.get("organizationId", org) != org
            or payload.get("marketplaceAccountId", context["marketplaceAccountId"]) != context["marketplaceAccountId"]):
        return {}
    categories = {"футболки": "tshirt", "худи": "hoodie", "лонгсливы": "longsleeve"}
    result = defaultdict(set)
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        nm = card.get("nmID") or card.get("nmId")
        if not str(nm).isdigit() or int(nm) not in ids:
            continue
        article = str(card.get("vendorCode") or "").strip().casefold()
        category = categories.get(str(card.get("subjectName") or card.get("object") or "").strip().casefold())
        result[int(nm)].add((article, category))
    return result


def saved_current_costs(org, finance_cache):
    """Bulk, tenant/account-scoped costs keyed by WB nmID; no provider writes."""
    from app.wb_pnl_advertising import _instant, _source_context

    context = _source_context(org)
    if context is None or finance_cache.get("marketplaceAccountId", context["marketplaceAccountId"]) != context["marketplaceAccountId"]:
        return {}
    aggregates = finance_cache.get("aggregates") or {}
    ids = {int(key) for key in aggregates if str(key).isdigit() and int(key) > 0}
    if not ids:
        return {}
    try:
        with get_session_factory()() as session:
            _prepare_read(session, org)
            accounts = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.organization_id == org, MarketplaceAccountRow.marketplace == "wb",
                MarketplaceAccountRow.status == "connected")).all()
            if accounts != [context["marketplaceAccountId"]]:
                return {}
            runtime, algorithm = _saved_inputs(session, org)
            mapping, ambiguous = CatalogService(session, org).resolve_wb_product_skus(accounts[0], list(ids))
            current = CostsService(session, org).get_costs_at(list(set(mapping.values())), datetime.now(timezone.utc))

            articles, products, goods_articles = defaultdict(set), defaultdict(set), defaultdict(set)

            def identity(nm, row, *, from_goods=False):
                if not isinstance(row, dict) or not str(nm).isdigit() or int(nm) <= 0:
                    return
                article = str(row.get("vendorCode") or "").strip()
                if article:
                    articles[int(nm)].add(article)
                    products[article.casefold()].add(int(nm))
                    if from_goods:
                        goods_articles[int(nm)].add(article)

            for nm, row in aggregates.items():
                identity(nm, row)
            for rows in (finance_cache.get("dailyAggregates") or {}).values():
                if isinstance(rows, dict):
                    for nm, row in rows.items():
                        identity(nm, row)
            # Read original pages, not the nmID-deduplicated display list: a
            # conflicting article must not be silently overwritten. Old-account
            # observations predating the current binding are ineligible.
            pages = session.scalars(select(WbRepricerGoodsCacheRow).where(
                WbRepricerGoodsCacheRow.organization_id == org,
                WbRepricerGoodsCacheRow.fetched_at >= _instant(context["connectedAt"]))).all()
            for page in pages:
                for good in page.goods_payload or []:
                    if isinstance(good, dict):
                        identity(good.get("nmID") or good.get("nmId"), good, from_goods=True)

            overrides = runtime.get("skuSettingsOverrides") or {}
            override_costs = defaultdict(set)
            for article, settings in overrides.items():
                if isinstance(settings, dict) and "cogsKopecks" in settings:
                    override_costs[str(article).strip().casefold()].add(_money(settings["cogsKopecks"]))
            garments = algorithm.get("cogsByGarmentRub") or {}
            unknown_types = {nm for nm in ids if len({article.casefold() for article in articles[nm]}) == 1
                and _article_type(next(iter(articles[nm]))) not in {"F", "H", "L"}}
            card_garments = _card_garments(session, org, context, unknown_types) if unknown_types and garments else {}
            result = {}
            for nm in ids - ambiguous:
                # Canonical current cost is also the Repricer's highest-priority
                # displayed unit cost. It remains a CURRENT fallback here.
                cost = current.get(mapping.get(nm))
                amount = _money(cost.amount_kopecks) if (cost and cost.value_state in {"configured", "assumed"}
                    and cost.source not in {"legacy_type_default", "legacy_missing"}) else None
                if amount is not None:
                    result[nm] = amount
                    continue
                # WB finance lowercases vendorCode while the product catalog
                # preserves its spelling. nmID still must match exactly; only
                # case differences are equivalent, never a different article.
                if len({article.casefold() for article in articles[nm]}) != 1:
                    continue
                candidates = goods_articles[nm] or articles[nm]
                if len(candidates) != 1:
                    continue
                article = next(iter(candidates))
                if len(products[article.casefold()]) != 1:
                    continue
                saved = override_costs.get(article.casefold())
                if saved:
                    amount = next(iter(saved)) if len(saved) == 1 else None
                else:
                    garment = {"F": "tshirt", "H": "hoodie", "L": "longsleeve"}.get(_article_type(article))
                    candidates = card_garments.get(nm, set())
                    if garment is None and len(candidates) == 1:
                        card_article, card_garment = next(iter(candidates))
                        if card_article == article.casefold():
                            garment = card_garment
                    amount = _money(garments.get(garment), rubles=True)
                if amount is not None:
                    result[nm] = amount
            session.rollback()
            return result
    except SQLAlchemyError:
        return {}  # Unknown remains unknown; never use process-global defaults.
