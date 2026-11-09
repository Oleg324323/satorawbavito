from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.db import set_tenant_context
from app.platform.catalog.orm import (
    CatalogSkuRow,
    MarketplaceOfferRow,
    MarketplaceProductRow,
)
from app.platform.clock import utc_now
from app.platform.economics.costs import CostsService, CostValueState, EvidenceStatus
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.store import get_source_cache, list_cached_goods
from app.repricer_persistence.store import load_algorithm_settings, load_runtime_state

BackfillStatus = Literal["mapped", "needs_review", "rejected"]


class BackfillValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LegacyCostSnapshot:
    captured_at: datetime
    goods: list[dict[str, Any]]
    content_cards: list[dict[str, Any]]
    runtime_state: dict[str, Any]
    algorithm_settings: dict[str, Any]
    costs_excel: dict[str, Any]


@dataclass(frozen=True)
class BackfillCost:
    amount_kopecks: int | None
    value_state: CostValueState
    effective_from: datetime
    source: str
    source_reference: str
    evidence_status: EvidenceStatus
    priority: int
    sequence: int = 0


@dataclass(frozen=True)
class BackfillItem:
    external_product_id: str | None
    seller_article: str | None
    status: BackfillStatus
    reason: str
    brand: str | None = None
    title: str | None = None
    image_url: str | None = None
    product_type: str | None = None
    product_status: str | None = None
    costs: tuple[BackfillCost, ...] = ()


@dataclass(frozen=True)
class BackfillPreview:
    items: tuple[BackfillItem, ...]

    @property
    def mapped_count(self) -> int:
        return sum(item.status == "mapped" for item in self.items)

    @property
    def needs_review_count(self) -> int:
        return sum(item.status == "needs_review" for item in self.items)

    @property
    def rejected_count(self) -> int:
        return sum(item.status == "rejected" for item in self.items)


@dataclass(frozen=True)
class BackfillApplyResult:
    applied_count: int
    cost_command_count: int
    preview: BackfillPreview


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BackfillValidationError("captured_at must include a timezone")
    return value.astimezone(timezone.utc)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _money(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _rub_kopecks(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source(value: Any, fallback: str) -> str:
    source = _text(value)
    return source if source and len(source) <= 64 else fallback


def _image(row: dict[str, Any]) -> str | None:
    for key in ("imageUrl", "photoUrl"):
        value = _text(row.get(key))
        if value:
            return value
    for photo in row.get("photos") or []:
        if not isinstance(photo, dict):
            continue
        for key in ("big", "c516x688", "square"):
            value = _text(photo.get(key))
            if value:
                return value
    return None


def _legacy_type(
    article: str, rows: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    subject = " ".join(
        value.lower()
        for row in rows
        for value in (_text(row.get("subjectName")), _text(row.get("title")))
        if value
    )
    if "лонгслив" in subject:
        return "longsleeve", "longsleeve"
    if any(marker in subject for marker in ("худи", "толстов", "свитшот")):
        return "hoodie", "hoodie"
    if "футбол" in subject:
        return "tshirt", "tshirt"

    from app.repricer_bff import _article_type

    marker = _article_type(article)
    garment = {"F": "tshirt", "H": "hoodie", "L": "longsleeve"}.get(marker)
    return garment, garment


def _reference(source: str, nm_id: int, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{source}:{nm_id}:{sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _cost(
    *,
    nm_id: int,
    amount: int | None,
    effective_from: datetime | None,
    captured_at: datetime,
    source: str,
    payload: dict[str, Any],
    priority: int,
    assumed: bool,
    sequence: int = 0,
) -> BackfillCost:
    effective = effective_from or captured_at
    evidence: EvidenceStatus = "dated" if effective_from else "undated"
    if amount is None:
        state: CostValueState = "missing"
    elif assumed and amount > 0:
        state = "assumed"
    else:
        state = "configured"
    return BackfillCost(
        amount_kopecks=amount,
        value_state=state,
        effective_from=effective,
        source=source,
        source_reference=_reference(
            source,
            nm_id,
            {
                **payload,
                "effectiveFrom": effective.isoformat(),
                "evidenceStatus": evidence,
            },
        ),
        evidence_status=evidence,
        priority=priority,
        sequence=sequence,
    )


def _matching_excel_rows(
    item: BackfillItem, rows: list[Any]
) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
    matches: list[tuple[int, dict[str, Any]]] = []
    conflict = False
    expected_nm = int(item.external_product_id or 0)
    expected_article = item.seller_article or ""
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        nm_id = _positive_int(raw.get("nmId") or raw.get("nmID"))
        article = _text(raw.get("vendorCode") or raw.get("articleId"))
        if nm_id == expected_nm or article == expected_article:
            if nm_id is not None and nm_id != expected_nm:
                conflict = True
            elif article is not None and article != expected_article:
                conflict = True
            else:
                matches.append((index, raw))
    return matches, conflict


def _costs_for_item(
    item: BackfillItem, snapshot: LegacyCostSnapshot
) -> tuple[tuple[BackfillCost, ...], str | None]:
    nm_id = int(item.external_product_id or 0)
    article = item.seller_article or ""
    captured_at = _aware_utc(snapshot.captured_at)
    runtime = snapshot.runtime_state
    algorithm = snapshot.algorithm_settings
    costs: list[BackfillCost] = []
    _product_type, garment = (
        _legacy_type(article, [])
        if item.product_type is None
        else (item.product_type, item.product_type)
    )
    garment = garment if garment in {"tshirt", "hoodie", "longsleeve"} else None

    if garment:
        from app.repricer_bff import TYPE_DEFAULTS

        marker = {"tshirt": "F", "hoodie": "H", "longsleeve": "L"}[garment]
        type_default = _money((TYPE_DEFAULTS.get(marker) or {}).get("cogsKopecks"))
        if type_default is not None and type_default > 0:
            costs.append(
                _cost(
                    nm_id=nm_id,
                    amount=type_default,
                    effective_from=None,
                    captured_at=captured_at,
                    source="legacy_type_default",
                    payload={"garment": garment, "cogsKopecks": type_default},
                    priority=5,
                    assumed=True,
                )
            )

    garment_history = (
        (algorithm.get("cogsByGarmentHistory") or {}).get(garment) if garment else None
    )
    if isinstance(garment_history, list):
        for index, entry in enumerate(garment_history):
            if not isinstance(entry, dict) or "cogsRub" not in entry:
                return (), "invalid_cost"
            amount = _rub_kopecks(entry.get("cogsRub"))
            if amount is None or amount == 0:
                return (), "invalid_cost"
            effective = _datetime(entry.get("effectiveFrom"))
            costs.append(
                _cost(
                    nm_id=nm_id,
                    amount=amount,
                    effective_from=effective,
                    captured_at=captured_at,
                    source=_source(entry.get("source"), "legacy_garment_history"),
                    payload={"garment": garment, "index": index, **entry},
                    priority=10,
                    assumed=True,
                    sequence=index,
                )
            )

    sku_history = (runtime.get("cogsHistory") or {}).get(article)
    if isinstance(sku_history, list):
        for index, entry in enumerate(sku_history):
            if not isinstance(entry, dict) or "cogsKopecks" not in entry:
                return (), "invalid_cost"
            raw_amount = entry.get("cogsKopecks")
            amount = None if raw_amount is None else _money(raw_amount)
            if raw_amount is not None and amount is None:
                return (), "invalid_cost"
            effective = _datetime(entry.get("effectiveFrom"))
            costs.append(
                _cost(
                    nm_id=nm_id,
                    amount=amount,
                    effective_from=effective,
                    captured_at=captured_at,
                    source=_source(entry.get("source"), "legacy_sku_history"),
                    payload={"article": article, "index": index, **entry},
                    priority=20,
                    assumed=effective is None,
                    sequence=index,
                )
            )

    excel_rows = snapshot.costs_excel.get("applied")
    if not isinstance(excel_rows, list):
        excel_rows = (
            snapshot.costs_excel.get("items")
            if isinstance(snapshot.costs_excel.get("items"), list)
            else []
        )
    matched_excel, excel_conflict = _matching_excel_rows(item, excel_rows)
    if excel_conflict:
        return (), "conflicting_cost_identity"
    excel_meta = (
        snapshot.costs_excel.get("meta")
        if isinstance(snapshot.costs_excel.get("meta"), dict)
        else {}
    )
    imported_at = _datetime(excel_meta.get("importedAt"))
    for index, entry in matched_excel:
        if "cogsKopecks" not in entry or entry.get("cogsKopecks") is None:
            continue
        amount = _money(entry.get("cogsKopecks"))
        if amount is None:
            return (), "invalid_cost"
        costs.append(
            _cost(
                nm_id=nm_id,
                amount=amount,
                effective_from=imported_at,
                captured_at=captured_at,
                source="legacy_costs_excel",
                payload={
                    "fileHash": excel_meta.get("fileHash"),
                    "index": index,
                    **entry,
                },
                priority=30,
                assumed=imported_at is None,
                sequence=index,
            )
        )

    garment_values = algorithm.get("cogsByGarmentRub")
    if (
        garment
        and isinstance(garment_values, dict)
        and garment_values.get(garment) is not None
    ):
        amount = _rub_kopecks(garment_values.get(garment))
        if amount is None or amount == 0:
            return (), "invalid_cost"
        costs.append(
            _cost(
                nm_id=nm_id,
                amount=amount,
                effective_from=None,
                captured_at=captured_at,
                source="legacy_garment_default",
                payload={"garment": garment, "cogsRub": garment_values.get(garment)},
                priority=40,
                assumed=True,
            )
        )

    sku_override = (runtime.get("skuSettingsOverrides") or {}).get(article)
    if isinstance(sku_override, dict) and "cogsKopecks" in sku_override:
        raw_amount = sku_override.get("cogsKopecks")
        amount = None if raw_amount is None else _money(raw_amount)
        if raw_amount is not None and amount is None:
            return (), "invalid_cost"
        costs.append(
            _cost(
                nm_id=nm_id,
                amount=amount,
                effective_from=None,
                captured_at=captured_at,
                source="legacy_sku_override",
                payload={"article": article, "cogsKopecks": raw_amount},
                priority=50,
                assumed=True,
            )
        )

    if not costs:
        costs.append(
            _cost(
                nm_id=nm_id,
                amount=None,
                effective_from=None,
                captured_at=captured_at,
                source="legacy_missing",
                payload={"article": article},
                priority=0,
                assumed=False,
            )
        )
    costs.sort(
        key=lambda value: (
            value.effective_from,
            value.priority,
            value.sequence,
            value.source_reference,
        )
    )
    return tuple(costs), None


def load_legacy_cost_snapshot(
    organization_id: int, *, captured_at: datetime | None = None
) -> LegacyCostSnapshot:
    content = get_source_cache(organization_id, "content_cards", slim=False) or {}
    cards = content.get("cards") if isinstance(content.get("cards"), list) else []
    return LegacyCostSnapshot(
        captured_at=_aware_utc(captured_at or utc_now()),
        goods=deepcopy(list_cached_goods(organization_id)),
        content_cards=deepcopy(cards),
        runtime_state=deepcopy(load_runtime_state(organization_id) or {}),
        algorithm_settings=deepcopy(load_algorithm_settings(organization_id) or {}),
        costs_excel=deepcopy(
            get_source_cache(organization_id, "costs_excel", slim=False) or {}
        ),
    )


def _snapshot_items(snapshot: LegacyCostSnapshot) -> list[BackfillItem]:
    _aware_utc(snapshot.captured_at)
    by_nm: dict[int, list[dict[str, Any]]] = {}
    rejected: list[BackfillItem] = []
    for raw in [*snapshot.goods, *snapshot.content_cards]:
        if not isinstance(raw, dict):
            rejected.append(
                BackfillItem(None, None, "rejected", "invalid_snapshot_row")
            )
            continue
        nm_id = _positive_int(raw.get("nmID") or raw.get("nmId"))
        if nm_id is None:
            rejected.append(
                BackfillItem(
                    _text(raw.get("nmID") or raw.get("nmId")),
                    _text(raw.get("vendorCode")),
                    "rejected",
                    "invalid_external_product_id",
                )
            )
            continue
        by_nm.setdefault(nm_id, []).append(raw)

    items: list[BackfillItem] = []
    for nm_id, rows in sorted(by_nm.items()):
        articles = {_text(row.get("vendorCode")) for row in rows} - {None}
        if len(articles) != 1:
            items.append(
                BackfillItem(
                    str(nm_id),
                    None,
                    "needs_review",
                    (
                        "missing_seller_article"
                        if not articles
                        else "conflicting_seller_article"
                    ),
                )
            )
            continue
        article = next(iter(articles))
        if len(article) > 128:
            rejected.append(
                BackfillItem(str(nm_id), article, "rejected", "seller_article_too_long")
            )
            continue
        product_type, _garment = _legacy_type(article, rows)
        preferred = list(reversed(rows))
        item = BackfillItem(
            external_product_id=str(nm_id),
            seller_article=article,
            status="mapped",
            reason="confirmed_external_ids",
            brand=next(
                (
                    _text(row.get("brand"))
                    for row in preferred
                    if _text(row.get("brand"))
                ),
                None,
            ),
            title=next(
                (
                    _text(row.get("title") or row.get("name"))
                    for row in preferred
                    if _text(row.get("title") or row.get("name"))
                ),
                None,
            ),
            image_url=next((_image(row) for row in preferred if _image(row)), None),
            product_type=product_type,
            product_status=next(
                (
                    _text(row.get("status") or row.get("productStatus"))
                    for row in preferred
                    if _text(row.get("status") or row.get("productStatus"))
                ),
                None,
            ),
        )
        costs, issue = _costs_for_item(item, snapshot)
        items.append(
            replace(item, status="needs_review", reason=issue)
            if issue
            else replace(item, costs=costs)
        )

    article_to_items: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.seller_article:
            article_to_items.setdefault(item.seller_article, []).append(index)
    for indexes in article_to_items.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            items[index] = replace(
                items[index],
                status="needs_review",
                reason="duplicate_seller_article",
                costs=(),
            )

    confirmed_articles = set(article_to_items)
    legacy_articles = set(
        (snapshot.runtime_state.get("skuSettingsOverrides") or {})
    ) | set((snapshot.runtime_state.get("cogsHistory") or {}))
    for article in sorted(
        str(value) for value in legacy_articles if str(value) not in confirmed_articles
    ):
        items.append(
            BackfillItem(
                None, article, "needs_review", "cost_without_confirmed_product"
            )
        )

    known_nm_ids = {
        int(item.external_product_id) for item in items if item.external_product_id
    }
    excel_rows = snapshot.costs_excel.get("applied")
    if not isinstance(excel_rows, list):
        excel_rows = (
            snapshot.costs_excel.get("items")
            if isinstance(snapshot.costs_excel.get("items"), list)
            else []
        )
    orphan_excel: set[tuple[int | None, str | None]] = set()
    for row in excel_rows:
        if not isinstance(row, dict) or row.get("cogsKopecks") is None:
            continue
        nm_id = _positive_int(row.get("nmId") or row.get("nmID"))
        article = _text(row.get("vendorCode") or row.get("articleId"))
        if nm_id in known_nm_ids or article in confirmed_articles:
            continue
        orphan_excel.add((nm_id, article))
    items.extend(
        BackfillItem(
            str(nm_id) if nm_id else None,
            article,
            "needs_review",
            "cost_without_confirmed_product",
        )
        for nm_id, article in sorted(
            orphan_excel, key=lambda value: (value[0] or 0, value[1] or "")
        )
    )
    return [*items, *rejected]


def _validate_account(
    session: Session, organization_id: int, marketplace_account_id: int
) -> None:
    set_tenant_context(session, organization_id)
    account = session.scalar(
        select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == organization_id,
            MarketplaceAccountRow.marketplace_account_id == marketplace_account_id,
        )
    )
    if account is None or account.marketplace != "wb":
        raise BackfillValidationError(
            "WB marketplace account not found for organization"
        )


def preview_cost_backfill(
    session: Session,
    *,
    organization_id: int,
    marketplace_account_id: int,
    snapshot: LegacyCostSnapshot,
) -> BackfillPreview:
    _validate_account(session, organization_id, marketplace_account_id)
    items = _snapshot_items(snapshot)
    checked: list[BackfillItem] = []
    for item in items:
        if item.status != "mapped":
            checked.append(item)
            continue
        sku = session.scalar(
            select(CatalogSkuRow).where(
                CatalogSkuRow.organization_id == organization_id,
                CatalogSkuRow.code == item.seller_article,
            )
        )
        product = session.scalar(
            select(MarketplaceProductRow).where(
                MarketplaceProductRow.organization_id == organization_id,
                MarketplaceProductRow.marketplace_account_id == marketplace_account_id,
                MarketplaceProductRow.external_product_id == item.external_product_id,
            )
        )
        if product is not None and product.seller_article not in {
            None,
            item.seller_article,
        }:
            checked.append(
                replace(
                    item,
                    status="needs_review",
                    reason="canonical_product_identity_conflict",
                    costs=(),
                )
            )
            continue
        if product is not None:
            offer = session.scalar(
                select(MarketplaceOfferRow).where(
                    MarketplaceOfferRow.organization_id == organization_id,
                    MarketplaceOfferRow.marketplace_product_id
                    == product.marketplace_product_id,
                    MarketplaceOfferRow.external_offer_key == item.seller_article,
                )
            )
            if (
                offer is not None
                and offer.catalog_sku_id is not None
                and (sku is None or offer.catalog_sku_id != sku.catalog_sku_id)
            ):
                checked.append(
                    replace(
                        item,
                        status="needs_review",
                        reason="canonical_offer_identity_conflict",
                        costs=(),
                    )
                )
                continue
        checked.append(item)
    return BackfillPreview(tuple(checked))


def _chunks(items: list[BackfillItem], size: int):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def apply_cost_backfill(
    session: Session,
    *,
    organization_id: int,
    marketplace_account_id: int,
    snapshot: LegacyCostSnapshot,
    chunk_size: int = 100,
    include_costs: bool = True,
) -> BackfillApplyResult:
    if chunk_size < 1:
        raise BackfillValidationError("chunk_size must be positive")
    preview = preview_cost_backfill(
        session,
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        snapshot=snapshot,
    )
    mapped = [item for item in preview.items if item.status == "mapped"]
    cost_commands = 0
    for chunk in _chunks(mapped, chunk_size):
        identities: list[tuple[BackfillItem, int]] = []
        set_tenant_context(session, organization_id)
        for item in chunk:
            sku = session.scalar(
                select(CatalogSkuRow).where(
                    CatalogSkuRow.organization_id == organization_id,
                    CatalogSkuRow.code == item.seller_article,
                )
            )
            if sku is None:
                sku = CatalogSkuRow(
                    organization_id=organization_id,
                    code=item.seller_article,
                    product_type=item.product_type,
                )
                session.add(sku)
                session.flush()

            product = session.scalar(
                select(MarketplaceProductRow).where(
                    MarketplaceProductRow.organization_id == organization_id,
                    MarketplaceProductRow.marketplace_account_id
                    == marketplace_account_id,
                    MarketplaceProductRow.external_product_id
                    == item.external_product_id,
                )
            )
            if product is None:
                product = MarketplaceProductRow(
                    organization_id=organization_id,
                    marketplace_account_id=marketplace_account_id,
                    external_product_id=item.external_product_id,
                    seller_article=item.seller_article,
                    brand=item.brand,
                    title=item.title,
                    image_url=item.image_url,
                    product_status=item.product_status,
                )
                session.add(product)
                session.flush()
            else:
                for field in (
                    "seller_article",
                    "brand",
                    "title",
                    "image_url",
                    "product_status",
                ):
                    if (
                        getattr(product, field) is None
                        and getattr(item, field) is not None
                    ):
                        setattr(product, field, getattr(item, field))

            offer = session.scalar(
                select(MarketplaceOfferRow).where(
                    MarketplaceOfferRow.organization_id == organization_id,
                    MarketplaceOfferRow.marketplace_product_id
                    == product.marketplace_product_id,
                    MarketplaceOfferRow.external_offer_key == item.seller_article,
                )
            )
            if offer is None:
                session.add(
                    MarketplaceOfferRow(
                        organization_id=organization_id,
                        marketplace_account_id=marketplace_account_id,
                        marketplace_product_id=product.marketplace_product_id,
                        external_offer_key=item.seller_article,
                        catalog_sku_id=sku.catalog_sku_id,
                    )
                )
            elif offer.catalog_sku_id is None:
                offer.catalog_sku_id = sku.catalog_sku_id
            identities.append((item, sku.catalog_sku_id))
        session.commit()

        service = CostsService(session, organization_id)
        for item, catalog_sku_id in identities:
            for cost in item.costs if include_costs else ():
                service.set_cost(
                    catalog_sku_id=catalog_sku_id,
                    amount_kopecks=cost.amount_kopecks,
                    value_state=cost.value_state,
                    effective_from=cost.effective_from,
                    source=cost.source,
                    source_reference=cost.source_reference,
                    evidence_status=cost.evidence_status,
                )
                cost_commands += 1
    return BackfillApplyResult(len(mapped), cost_commands, preview)


def confirm_saved_costs(
    session: Session,
    *,
    organization_id: int,
    marketplace_account_id: int,
    snapshot: LegacyCostSnapshot,
    effective_from: datetime,
) -> dict[str, int]:
    """Explicit operator confirmation, never called by report reads or syncs.

    Reuse the identity checks/importer, but not its assumed/default cost import.
    Only values actually entered in legacy settings are eligible. Existing
    canonical histories are left alone, including later or missing versions.
    The caller must obtain permission for the values and their effective date.
    """
    effective_at = _aware_utc(effective_from)
    if effective_at > _aware_utc(snapshot.captured_at):
        raise BackfillValidationError("confirmation cannot begin in the future")
    result = apply_cost_backfill(session, organization_id=organization_id,
        marketplace_account_id=marketplace_account_id, snapshot=snapshot,
        include_costs=False)
    confirmed = preserved = missing = 0
    from app.platform.economics.orm import CatalogCostVersionRow

    for item in result.preview.items:
        if item.status != "mapped":
            continue
        candidates = [value for value in item.costs
                      if value.source not in {"legacy_type_default", "legacy_missing"}]
        if not candidates or candidates[-1].amount_kopecks is None:
            missing += 1
            continue
        set_tenant_context(session, organization_id)
        sku = session.scalar(select(CatalogSkuRow).where(
            CatalogSkuRow.organization_id == organization_id,
            CatalogSkuRow.code == item.seller_article).with_for_update())
        if session.scalar(select(CatalogCostVersionRow.cost_version_id).where(
            CatalogCostVersionRow.organization_id == organization_id,
            CatalogCostVersionRow.catalog_sku_id == sku.catalog_sku_id).limit(1)) is not None:
            preserved += 1
            continue
        CostsService(session, organization_id).set_cost(
            catalog_sku_id=sku.catalog_sku_id,
            amount_kopecks=candidates[-1].amount_kopecks, value_state="configured",
            effective_from=effective_at, source="user_confirmed_legacy_cost",
            source_reference=_reference("confirmed", int(item.external_product_id), {
                "nmId": item.external_product_id, "effectiveFrom": effective_at.isoformat(),
                "cost": candidates[-1].amount_kopecks,
            }), evidence_status="dated")
        confirmed += 1
    return {"mapped": result.applied_count, "confirmed": confirmed,
            "preserved": preserved, "missing": missing,
            "needsReview": result.preview.needs_review_count,
            "rejected": result.preview.rejected_count}
