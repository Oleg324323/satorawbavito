from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, File, UploadFile
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.envelopes import DataEnvelope, PaginatedEnvelope, UtcDateTime
from app.control_plane.auth import ActorContext, actor_from_request, has_permission
from app.infra.db import get_db_session
from app.platform.catalog.schemas import (
    CatalogSkuView,
    MarketplaceOfferView,
    ProductionMarketplaceLinkView,
    ProductionSkuView,
    WbProductView,
)
from app.platform.catalog.service import (
    CatalogService,
    CatalogSku,
    MarketplaceProduct,
    ProductionSku,
)
from app.platform.economics.costs import (
    CostConflictError,
    CostNotFoundError,
    CostValidationError,
    CostValue,
    CostsService,
)
from app.platform.economics.schemas import CostSetRequest, CostView, CurrentCostSetRequest
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_sync import (
    _good_buyer_price_no_wallet_kopecks,
    _good_seller_price_kopecks,
)

router = APIRouter(tags=["catalog-v2"])


@router.post("/api/v2/wb/costs/import")
def import_wb_costs(
    request: Request, preview: bool = Query(default=True),
    previewHash: str | None = Query(default=None), accountId: int | None = Query(default=None),
    replaceExamples: bool = Query(default=False),
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
):
    # The actor is resolved from the authenticated request, never from the workbook.
    import hashlib
    from collections import Counter
    from app.platform.economics.cost_import import parse_cost_rows, plan_digest
    actor = actor_from_request(request)
    for permission in ("catalog:read", "costs:read", "costs:write"):
        _require(actor, permission)
    member = _cost_membership(session, actor)
    accounts = list(session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all())
    if member.scope_mode != "all":
        accounts = [value for value in accounts if value in (member.allowed_account_ids or [])]
    if accountId is not None:
        accounts = [value for value in accounts if value == accountId]
    if len(accounts) != 1:
        raise HTTPException(409, detail="Выберите один доступный кабинет WB для импорта")
    account_id = accounts[0]
    content = file.file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Максимальный размер файла — 10 МБ")
    try:
        rows = parse_cost_rows(content)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, detail="Не удалось прочитать XLSX: проверьте колонки nmID и Себестоимость") from exc
    if not rows or len(rows) > 20000:
        raise HTTPException(422, detail="В файле должно быть от 1 до 20000 строк")
    mapping, ambiguous = CatalogService(session, actor.organization_id).resolve_wb_product_skus(
        account_id, [row["nmId"] for row in rows if row["nmId"] is not None])
    service = CostsService(session, actor.organization_id)
    file_hash = hashlib.sha256(content).hexdigest()
    sku_counts = Counter(mapping.get(row["nmId"]) for row in rows if not row["error"])
    from datetime import datetime, timezone
    costs = service.get_costs_at(list(set(mapping.values())), datetime.now(timezone.utc))
    links: dict[int, set[int]] = {}
    for sku_id, product_id in session.execute(select(
        MarketplaceOfferRow.catalog_sku_id, MarketplaceOfferRow.marketplace_product_id).where(
        MarketplaceOfferRow.organization_id == actor.organization_id,
        MarketplaceOfferRow.catalog_sku_id.in_(list(set(mapping.values()))))):
        links.setdefault(sku_id, set()).add(product_id)
    for row in rows:
        if row["error"]:
            continue
        sku = mapping.get(row["nmId"])
        if row["nmId"] in ambiguous or sku is None:
            row["error"] = "Товар не найден или имеет несколько SKU; проверьте привязку вариантов"
            continue
        if sku_counts[sku] > 1:
            row["error"] = "Несколько строк меняют общую себестоимость одного SKU"
            continue
        if len(links.get(sku, set())) > 1:
            row["error"] = "Общая себестоимость нескольких карточек: измените её в таблице с подтверждением"
            continue
        _require_cost_sku_scope(session, actor, sku)
        current = costs[sku]
        reference = f"excel:{account_id}:{file_hash}:{sku}"
        if replaceExamples:
            reference += ':replace-examples'
        historical = replaceExamples and current.source == 'user-example' and current.value_state == 'assumed'
        existing = service._by_reference("manual-current", reference) or service._by_reference('example-replacement', reference)
        row.update(catalogSkuId=sku, oldAmountKopecks=current.amount_kopecks,
                   expectedVersion=current.cost_version_id, reference=reference, alreadyApplied=existing is not None,
                   replacesExample=historical, effectiveFrom=current.effective_from.isoformat() if historical else None)
    digest = plan_digest(rows)
    if not preview and previewHash != digest:
        raise HTTPException(409, detail="Данные изменились после проверки. Повторите предварительный просмотр")
    saved = 0
    if not preview:
        for row in rows:
            if row["error"] or row.get("alreadyApplied"):
                continue
            try:
                save = service.replace_example_cost if row['replacesExample'] else service.set_current_cost
                save(catalog_sku_id=row["catalogSkuId"], amount_kopecks=row["amountKopecks"],
                    expected_cost_version_id=row["expectedVersion"], source_reference=row["reference"], created_by_user_id=actor.user_id)
                saved += 1
            except (CostConflictError, CostValidationError, CostNotFoundError):
                session.rollback()
                row["error"] = "Стоимость изменилась или недоступна; проверьте строку повторно"
    return {"preview": preview, "previewHash": digest, "accountId": account_id, "rows": rows,
            "ready": sum(not row["error"] and not row.get("alreadyApplied") for row in rows),
            "saved": saved, "errors": sum(bool(row["error"]) for row in rows),
            "skipped": sum(bool(row.get("alreadyApplied")) for row in rows)}


@router.get("/api/v2/wb/costs/accounts")
def cost_import_accounts(request: Request, session: Session = Depends(get_db_session)):
    actor = actor_from_request(request)
    _require(actor, "catalog:read")
    _require(actor, "costs:read")
    member = _cost_membership(session, actor)
    rows = session.scalars(select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all()
    return [{"id": row.marketplace_account_id, "name": f"WB · {row.external_account_id}"} for row in rows
            if member.scope_mode == "all" or row.marketplace_account_id in (member.allowed_account_ids or [])]


def get_catalog_actor(request: Request) -> ActorContext:
    return actor_from_request(request)


def _require(actor: ActorContext, permission: str) -> None:
    if not has_permission(actor, permission):
        raise HTTPException(
            status_code=403,
            detail={"code": "NO_ACCESS", "message": f"NO_ACCESS:{permission}"},
        )


def _require_cost_sku_scope(session: Session, actor: ActorContext, catalog_sku_id: int) -> None:
    from app.infra.db import set_tenant_context
    set_tenant_context(session, actor.organization_id)
    membership = _cost_membership(session, actor)
    if membership.scope_mode != "all":
        accounts = set(session.scalars(select(MarketplaceOfferRow.marketplace_account_id).where(
            MarketplaceOfferRow.organization_id == actor.organization_id,
            MarketplaceOfferRow.catalog_sku_id == catalog_sku_id)).all())
        if not accounts or not accounts.issubset(set(membership.allowed_account_ids or [])):
            raise HTTPException(status_code=403, detail={"code": "NO_ACCESS"})


def _cost_membership(session: Session, actor: ActorContext) -> IamMembershipRow:
    """Migrate an existing authorized legacy user, never reactivate a denied member."""
    from app.cabinet.orm import LkUserRow, LkUserPermissionRow
    from app.cabinet.store import _sync_membership
    from app.cabinet.permissions import permissions_from_profile
    from app.infra.db import set_tenant_context
    set_tenant_context(session, actor.organization_id)
    member = session.scalar(select(IamMembershipRow).where(
        IamMembershipRow.organization_id == actor.organization_id, IamMembershipRow.user_id == actor.user_id))
    if member is None:
        user = session.scalar(select(LkUserRow).where(LkUserRow.organization_id == actor.organization_id,
            LkUserRow.user_id == actor.user_id, LkUserRow.is_active.is_(True)))
        if user is None:
            raise HTTPException(403, detail={"code": "NO_ACCESS"})
        permissions = set(session.scalars(select(LkUserPermissionRow.permission).where(
            LkUserPermissionRow.user_id == actor.user_id)).all()) | set(permissions_from_profile(user.permission_profile))
        if not {"costs:read", "catalog:read"}.issubset(permissions):
            raise HTTPException(403, detail={"code": "NO_ACCESS", "message": "Нет права просмотра себестоимости"})
        _sync_membership(session, user, permissions)
        session.flush()
        member = session.scalar(select(IamMembershipRow).where(
            IamMembershipRow.organization_id == actor.organization_id, IamMembershipRow.user_id == actor.user_id))
    if member is None or not member.is_active:
        raise HTTPException(403, detail={"code": "NO_ACCESS", "message": "Доступ участника отключён"})
    return member


def _cost_view(cost: CostValue, *, visible: bool) -> CostView:
    if not visible:
        return CostView(valueState="restricted")
    return CostView(
        costVersionId=cost.cost_version_id,
        amountKopecks=cost.amount_kopecks,
        valueState=cost.value_state,
        effectiveFrom=cost.effective_from,
        source=cost.source,
        sourceReference=cost.source_reference,
        evidenceStatus=cost.evidence_status,
        supersedesCostVersionId=cost.supersedes_cost_version_id,
        createdAt=cost.created_at,
    )


def _sku_view(sku: CatalogSku, *, costs_visible: bool) -> CatalogSkuView:
    return CatalogSkuView(
        catalogSkuId=sku.catalog_sku_id,
        code=sku.code,
        printId=sku.print_id,
        productType=sku.product_type,
        color=sku.color,
        size=sku.size,
        managerMembershipId=sku.manager_membership_id,
        currentCost=_cost_view(sku.current_cost, visible=costs_visible),
        createdAt=sku.created_at,
        updatedAt=sku.updated_at,
    )


def _goods_by_nm_id(session: Session, organization_id: int) -> dict[str, dict]:
    pages = session.scalars(
        select(WbRepricerGoodsCacheRow)
        .where(WbRepricerGoodsCacheRow.organization_id == organization_id)
        .order_by(WbRepricerGoodsCacheRow.page_offset.asc())
    ).all()
    rows = [
        item for page in pages for item in page.goods_payload if isinstance(item, dict)
    ]
    return {
        str(raw_nm_id): row
        for row in rows
        if isinstance(row, dict)
        and (raw_nm_id := row.get("nmID") or row.get("nmId")) is not None
    }


def _wb_product_view(
    product: MarketplaceProduct,
    *,
    costs_visible: bool,
    goods_by_nm_id: dict[str, dict],
) -> WbProductView:
    snapshot = goods_by_nm_id.get(product.external_product_id)
    snapshot_status = None
    if snapshot is not None:
        raw_status = snapshot.get("productStatus") or snapshot.get("status")
        snapshot_status = str(raw_status).strip() if raw_status is not None else None
    return WbProductView(
        marketplaceProductId=product.marketplace_product_id,
        marketplaceAccountId=product.marketplace_account_id,
        externalProductId=product.external_product_id,
        sellerArticle=product.seller_article,
        brand=product.brand,
        title=product.title,
        imageUrl=product.image_url,
        productStatus=snapshot_status or product.product_status,
        priceBeforeSppKopecks=(
            _good_seller_price_kopecks(snapshot) if snapshot is not None else None
        ),
        priceAfterSppKopecks=(
            _good_buyer_price_no_wallet_kopecks(snapshot)
            if snapshot is not None
            else None
        ),
        priceSnapshotAvailable=snapshot is not None,
        offers=[
            MarketplaceOfferView(
                marketplaceOfferId=offer.marketplace_offer_id,
                externalOfferKey=offer.external_offer_key,
                offerStatus=offer.offer_status,
                catalogSku=(
                    _sku_view(offer.catalog_sku, costs_visible=costs_visible)
                    if offer.catalog_sku
                    else None
                ),
            )
            for offer in product.offers
        ],
        createdAt=product.created_at,
        updatedAt=product.updated_at,
    )


def _production_sku_view(
    row: ProductionSku, *, costs_visible: bool
) -> ProductionSkuView:
    sku = _sku_view(row.sku, costs_visible=costs_visible)
    return ProductionSkuView(
        **sku.model_dump(),
        marketplaceLinks=[
            ProductionMarketplaceLinkView(
                marketplace=link.marketplace,
                marketplaceAccountId=link.marketplace_account_id,
                marketplaceProductId=link.marketplace_product_id,
                externalProductId=link.external_product_id,
                externalOfferKey=link.external_offer_key,
                sellerArticle=link.seller_article,
                title=link.title,
                productStatus=link.product_status,
                offerStatus=link.offer_status,
            )
            for link in row.marketplace_links
        ],
    )


def _not_found(exc: CostNotFoundError) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "CATALOG_SKU_NOT_FOUND", "message": str(exc)},
    )


@router.get("/api/v2/catalog/skus", response_model=PaginatedEnvelope[CatalogSkuView])
def get_catalog_skus(
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    asOf: UtcDateTime | None = Query(default=None),
) -> PaginatedEnvelope[CatalogSkuView]:
    _require(actor, "catalog:read")
    costs_visible = has_permission(actor, "costs:read")
    rows, total = CatalogService(session, actor.organization_id).list_skus(
        limit=limit,
        offset=offset,
        as_of=asOf,
        include_costs=costs_visible,
    )
    return PaginatedEnvelope(
        items=[_sku_view(row, costs_visible=costs_visible) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v2/catalog/skus/{catalogSkuId}", response_model=DataEnvelope[CatalogSkuView]
)
def get_catalog_sku(
    catalogSkuId: int,
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
    asOf: UtcDateTime | None = Query(default=None),
) -> DataEnvelope[CatalogSkuView]:
    _require(actor, "catalog:read")
    costs_visible = has_permission(actor, "costs:read")
    try:
        row = CatalogService(session, actor.organization_id).get_sku(
            catalogSkuId,
            as_of=asOf,
            include_cost=costs_visible,
        )
    except CostNotFoundError as exc:
        raise _not_found(exc) from exc
    return DataEnvelope(data=_sku_view(row, costs_visible=costs_visible))


@router.get(
    "/api/v2/catalog/skus/{catalogSkuId}/cost-history",
    response_model=DataEnvelope[list[CostView]],
)
def get_catalog_sku_cost_history(
    catalogSkuId: int,
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
) -> DataEnvelope[list[CostView]]:
    _require(actor, "catalog:read")
    _require(actor, "costs:read")
    try:
        rows = CostsService(session, actor.organization_id).list_cost_history(
            catalogSkuId
        )
    except CostNotFoundError as exc:
        raise _not_found(exc) from exc
    return DataEnvelope(data=[_cost_view(row, visible=True) for row in rows])


@router.post(
    "/api/v2/catalog/skus/{catalogSkuId}/cost", response_model=DataEnvelope[CostView]
)
def post_catalog_sku_cost(
    catalogSkuId: int,
    payload: CostSetRequest,
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
) -> DataEnvelope[CostView]:
    _require(actor, "catalog:read")
    _require(actor, "costs:write")
    _require_cost_sku_scope(session, actor, catalogSkuId)
    try:
        row = CostsService(session, actor.organization_id).set_cost(
            catalog_sku_id=catalogSkuId,
            amount_kopecks=payload.amountKopecks,
            value_state=payload.valueState,
            effective_from=payload.effectiveFrom,
            source="manual",
            source_reference=payload.sourceReference,
            evidence_status=payload.evidenceStatus,
            supersedes_cost_version_id=payload.supersedesCostVersionId,
            created_by_user_id=actor.user_id,
        )
    except CostNotFoundError as exc:
        raise _not_found(exc) from exc
    except CostConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "COST_COMMAND_CONFLICT", "message": str(exc)},
        ) from exc
    except CostValidationError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_COST", "message": str(exc)}
        ) from exc
    return DataEnvelope(data=_cost_view(row, visible=True))


@router.post("/api/v2/catalog/skus/{catalogSkuId}/current-cost", response_model=DataEnvelope[CostView])
def post_catalog_sku_current_cost(
    catalogSkuId: int,
    payload: CurrentCostSetRequest,
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
) -> DataEnvelope[CostView]:
    _require(actor, "catalog:read")
    _require(actor, "costs:read")
    _require(actor, "costs:write")
    # A shared CatalogSku changes every linked offer.
    _require_cost_sku_scope(session, actor, catalogSkuId)
    try:
        row = CostsService(session, actor.organization_id).set_current_cost(
            catalog_sku_id=catalogSkuId, amount_kopecks=payload.amountKopecks,
            expected_cost_version_id=payload.expectedCostVersionId,
            source_reference=payload.sourceReference, created_by_user_id=actor.user_id,
        )
    except CostNotFoundError as exc:
        raise _not_found(exc) from exc
    except CostConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail={"code": "COST_VERSION_CONFLICT", "message": str(exc)}) from exc
    except CostValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail={"code": "INVALID_COST", "message": str(exc)}) from exc
    return DataEnvelope(data=_cost_view(row, visible=True))


def _wb_cost_account(session: Session, actor: ActorContext, *, lock=False) -> int:
    _require(actor, "catalog:read")
    _require(actor, "costs:read")
    from app.infra.db import set_tenant_context
    set_tenant_context(session, actor.organization_id)
    query = select(MarketplaceAccountRow.marketplace_account_id).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")
    accounts = session.scalars(query.with_for_update() if lock else query).all()
    # Legacy table has no explicit account identity. Never guess among accounts.
    if len(accounts) != 1:
        raise HTTPException(status_code=409, detail={"code": "COST_ACCOUNT_AMBIGUOUS", "message": "Требуется однозначный WB-аккаунт"})
    membership = _cost_membership(session, actor)
    if membership.scope_mode != "all" and accounts[0] not in (membership.allowed_account_ids or []):
        raise HTTPException(status_code=403, detail={"code": "NO_ACCESS"})
    return accounts[0]


def _wb_cost_sku(session: Session, org: int, account: int, nm: int) -> int | None:
    mapping, ambiguous = CatalogService(session, org).resolve_wb_product_skus(account, [nm])
    if nm in ambiguous:
        raise HTTPException(status_code=409, detail={"code": "COST_MAPPING_AMBIGUOUS", "message": "Нет однозначного соответствия CatalogSku; проверьте варианты товара"})
    return mapping.get(nm)


def _bind_wb_cost_sku(session: Session, org: int, account: int, nm: int) -> int:
    """Bind only an explicitly edited, known WB product; never seed default costs."""
    product = session.scalar(select(MarketplaceProductRow).where(
        MarketplaceProductRow.organization_id == org,
        MarketplaceProductRow.marketplace_account_id == account,
        MarketplaceProductRow.external_product_id == str(nm)))
    if product is None:
        from app.wb_pnl_advertising import _source_context, _instant
        context = _source_context(org)
        if not context or context["marketplaceAccountId"] != account:
            raise HTTPException(409, detail={"code": "COST_SOURCE_UNAVAILABLE", "message": "Обновите каталог WB перед сохранением"})
        pages = session.scalars(select(WbRepricerGoodsCacheRow).where(
            WbRepricerGoodsCacheRow.organization_id == org,
            WbRepricerGoodsCacheRow.fetched_at >= _instant(context["connectedAt"]))).all()
        articles = {str(good.get("vendorCode") or "").strip()
            for page in pages for good in page.goods_payload or []
            if isinstance(good, dict) and str(good.get("nmID") or good.get("nmId")) == str(nm)}
        if not articles:
            raise HTTPException(404, detail={"code": "WB_PRODUCT_NOT_FOUND", "message": "Товар не найден в сохранённом каталоге WB"})
        if "" in articles or len({article.casefold() for article in articles}) != 1:
            raise HTTPException(409, detail={"code": "COST_MAPPING_AMBIGUOUS", "message": "Конфликт артикулов в каталоге WB"})
        product = MarketplaceProductRow(organization_id=org, marketplace_account_id=account,
            external_product_id=str(nm), seller_article=sorted(articles)[0])
        session.add(product)
        session.flush()
    # Use account + nmID, not a possibly shared/reused seller article.
    sku = CatalogSkuRow(organization_id=org, code=f"wb:{account}:{nm}")
    session.add(sku)
    session.flush()
    offers = session.scalars(select(MarketplaceOfferRow).where(
        MarketplaceOfferRow.organization_id == org,
        MarketplaceOfferRow.marketplace_account_id == account,
        MarketplaceOfferRow.marketplace_product_id == product.marketplace_product_id)).all()
    if offers:
        for offer in offers:
            offer.catalog_sku_id = sku.catalog_sku_id
    else:
        session.add(MarketplaceOfferRow(organization_id=org, marketplace_account_id=account,
            marketplace_product_id=product.marketplace_product_id,
            external_offer_key=f"wb:{nm}", catalog_sku_id=sku.catalog_sku_id))
    session.flush()
    return sku.catalog_sku_id


@router.post("/api/v2/wb/products/{nmId}/current-cost", response_model=DataEnvelope[CostView])
def post_wb_product_current_cost(
    nmId: int, payload: CurrentCostSetRequest,
    actor: ActorContext = Depends(get_catalog_actor), session: Session = Depends(get_db_session),
):
    for permission in ("catalog:read", "costs:read", "costs:write"):
        _require(actor, permission)
    if nmId <= 0:
        raise HTTPException(422, detail={"code": "INVALID_WB_PRODUCT"})
    # Serialize first binding as well as subsequent cost writes.
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            session.execute(text("BEGIN IMMEDIATE"))
    account = _wb_cost_account(session, actor, lock=True)
    try:
        sku = _wb_cost_sku(session, actor.organization_id, account, nmId)
        if sku is None:
            sku = _bind_wb_cost_sku(session, actor.organization_id, account, nmId)
        _require_cost_sku_scope(session, actor, sku)
        cost = CostsService(session, actor.organization_id).set_current_cost(
            catalog_sku_id=sku, amount_kopecks=payload.amountKopecks,
            expected_cost_version_id=payload.expectedCostVersionId,
            source_reference=payload.sourceReference, created_by_user_id=actor.user_id)
    except (CostConflictError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(409, detail={"code": "COST_VERSION_CONFLICT", "message": "Себестоимость изменилась; обновите значение"}) from exc
    except CostValidationError as exc:
        session.rollback()
        raise HTTPException(422, detail={"code": "INVALID_COST", "message": str(exc)}) from exc
    except CostNotFoundError as exc:
        session.rollback()
        raise _not_found(exc) from exc
    except HTTPException:
        session.rollback()
        raise
    return DataEnvelope(data=_cost_view(cost, visible=True))


@router.get("/api/v2/wb/products/{nmId}/current-cost")
def get_wb_product_current_cost(
    nmId: int, actor: ActorContext = Depends(get_catalog_actor), session: Session = Depends(get_db_session),
):
    if nmId <= 0:
        raise HTTPException(422, detail={"code": "INVALID_WB_PRODUCT"})
    account = _wb_cost_account(session, actor)
    sku_id = _wb_cost_sku(session, actor.organization_id, account, nmId)
    cost = CostsService(session, actor.organization_id).get_current_cost(sku_id) if sku_id else None
    if cost is None or cost.amount_kopecks is None or cost.source in {"legacy_type_default", "legacy_missing"}:
        from app.wb_pnl_costs import saved_current_costs
        amount = saved_current_costs(actor.organization_id,
            {"marketplaceAccountId": account, "aggregates": {str(nmId): {}}}).get(nmId)
        current = {"amountKopecks": amount, "costVersionId": cost.cost_version_id if cost else None, "effectiveFrom": None,
            "source": "saved_wb_settings" if amount is not None else None}
    else:
        current = _cost_view(cost, visible=True)
    linked = session.scalars(select(MarketplaceOfferRow.marketplace_product_id).where(
        MarketplaceOfferRow.organization_id == actor.organization_id,
        MarketplaceOfferRow.catalog_sku_id == sku_id)).all() if sku_id else []
    return DataEnvelope(data={"catalogSkuId": sku_id, "linkedProductCount": max(1, len(set(linked))),
        "canWrite": has_permission(actor, "costs:write"), "currentCost": current})


@router.get("/api/v2/wb/products", response_model=PaginatedEnvelope[WbProductView])
def get_wb_products(
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    asOf: UtcDateTime | None = Query(default=None),
) -> PaginatedEnvelope[WbProductView]:
    _require(actor, "catalog:read")
    costs_visible = has_permission(actor, "costs:read")
    rows, total = CatalogService(session, actor.organization_id).list_wb_products(
        limit=limit,
        offset=offset,
        as_of=asOf,
        include_costs=costs_visible,
    )
    goods = _goods_by_nm_id(session, actor.organization_id)
    return PaginatedEnvelope(
        items=[
            _wb_product_view(row, costs_visible=costs_visible, goods_by_nm_id=goods)
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v2/production/skus", response_model=PaginatedEnvelope[ProductionSkuView]
)
def get_production_skus(
    actor: ActorContext = Depends(get_catalog_actor),
    session: Session = Depends(get_db_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    asOf: UtcDateTime | None = Query(default=None),
) -> PaginatedEnvelope[ProductionSkuView]:
    _require(actor, "catalog:read")
    costs_visible = has_permission(actor, "costs:read")
    rows, total = CatalogService(session, actor.organization_id).list_production_skus(
        limit=limit,
        offset=offset,
        as_of=asOf,
        include_costs=costs_visible,
    )
    return PaginatedEnvelope(
        items=[_production_sku_view(row, costs_visible=costs_visible) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
