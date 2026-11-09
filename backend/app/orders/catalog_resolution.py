"""Exact Catalog references, not heuristics or manual assignment authority."""

import hashlib
import json
from datetime import UTC

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.contracts import CatalogResolution
from app.orders.ingestion import _integer, _text

_RULE = "orders-catalog-exact-v1"


def resolve_order_catalog(
    session: Session,
    organization_id: int,
    marketplace_account_id: int,
    external_product_id: str,
    *,
    external_offer_key: str | None = None,
    previous: CatalogResolution | None = None,
) -> CatalogResolution:
    """Caller acquires its authorization guard first; locks remain through commit.

    Product UPDATE lock also blocks new Offer FK insertions during selection.
    There is no title/size/color matching and no collapse of multiple variants
    merely because they currently reference the same SKU.
    """
    _integer(organization_id)
    _integer(marketplace_account_id)
    _text(external_product_id)
    if external_offer_key is not None:
        _text(external_offer_key)
    if previous is not None and not isinstance(previous, CatalogResolution):
        raise OrderContractValidationError("Previous Catalog resolution required")
    if previous is not None and previous.state == "manual_override":
        raise OrderContractValidationError(
            "Manual assignment belongs to Production commands"
        )
    if not session.in_transaction():
        raise OrderContractValidationError("Caller transaction required")
    context = session.execute(
        text(
            "SELECT current_setting('app.organization_id',true),current_setting('transaction_isolation')"
        )
    ).one()
    if tuple(context) != (str(organization_id), "read committed"):
        raise OrderContractValidationError(
            "Current tenant READ COMMITTED context required"
        )
    params = {
        "org": organization_id,
        "account": marketplace_account_id,
        "external": external_product_id,
    }
    account = session.execute(
        text("""SELECT marketplace_account_id FROM marketplace_accounts
        WHERE organization_id=:org AND marketplace_account_id=:account FOR UPDATE"""),
        params,
    ).scalar_one_or_none()
    if account is None:
        raise OrderContractValidationError("Catalog account scope missing")
    product = session.execute(
        text("""SELECT marketplace_product_id,updated_at FROM marketplace_products
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND external_product_id COLLATE "C"=:external FOR UPDATE"""),
        params,
    ).one_or_none()
    missing = (
        "stale" if previous is not None and previous.state == "resolved" else "unmapped"
    )
    if product is None:
        return CatalogResolution(missing, None, None, None, _RULE)
    params["product"] = product.marketplace_product_id
    offers = session.execute(
        text("""SELECT marketplace_offer_id,external_offer_key,catalog_sku_id,updated_at
        FROM marketplace_offers WHERE organization_id=:org AND marketplace_account_id=:account
        AND marketplace_product_id=:product ORDER BY marketplace_offer_id FOR SHARE"""),
        params,
    ).all()
    candidates = [
        offer
        for offer in offers
        if external_offer_key is None or offer.external_offer_key == external_offer_key
    ]
    if not candidates:
        return CatalogResolution(
            missing, product.marketplace_product_id, None, None, _RULE
        )
    if len(candidates) != 1:
        return CatalogResolution(
            "ambiguous", product.marketplace_product_id, None, None, _RULE
        )
    offer = candidates[0]
    if offer.catalog_sku_id is None:
        return CatalogResolution(
            missing,
            product.marketplace_product_id,
            offer.marketplace_offer_id,
            None,
            _RULE,
        )
    sku = session.execute(
        text("""SELECT catalog_sku_id,updated_at FROM catalog_skus
        WHERE organization_id=:org AND catalog_sku_id=:sku FOR SHARE"""),
        dict(params, sku=offer.catalog_sku_id),
    ).one_or_none()
    if sku is None:
        return CatalogResolution(
            "stale",
            product.marketplace_product_id,
            offer.marketplace_offer_id,
            None,
            _RULE,
        )
    fingerprint = json.dumps(
        [
            organization_id,
            marketplace_account_id,
            external_product_id,
            offer.external_offer_key,
            product.marketplace_product_id,
            offer.marketplace_offer_id,
            sku.catalog_sku_id,
            *(
                row.updated_at.astimezone(UTC).isoformat(timespec="microseconds")
                for row in (product, offer, sku)
            ),
        ],
        separators=(",", ":"),
    )
    version = _RULE + ":" + hashlib.sha256(fingerprint.encode("ascii")).hexdigest()
    stale = (
        previous is not None
        and previous.state == "resolved"
        and (
            previous.marketplace_product_id,
            previous.marketplace_offer_id,
            previous.catalog_sku_id,
            previous.evidence_version,
        )
        != (
            product.marketplace_product_id,
            offer.marketplace_offer_id,
            sku.catalog_sku_id,
            version,
        )
    )
    return CatalogResolution(
        "stale" if stale else "resolved",
        product.marketplace_product_id,
        offer.marketplace_offer_id,
        None if stale else sku.catalog_sku_id,
        version,
    )
