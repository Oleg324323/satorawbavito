"""Apply the last saved browser observation to its exact seller catalogue offer."""
from datetime import datetime, timedelta, timezone
from typing import Any

from app.wb_api.price_units import wb_goods_price_to_kopecks

CONNECTION_KEY = "wb_browser_prices_connection"
PRICES_KEY = "wb_browser_prices"
_BUYER_FIELDS = (
    "buyerPriceNoWalletKopecks", "buyerPriceKopecks", "buyerPriceNoWallet", "buyerPrice", "clientPrice",
    "buyerPriceWithWalletKopecks", "buyerPriceWithWallet", "walletPrice", "walletPct",
    "buyerPriceSource", "buyerPriceObservedAt", "buyerPriceSellerKopecks", "buyerPriceExpiresAt",
    "buyerPriceOriginalKopecks",
)


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def apply_browser_prices(
    goods: list[dict[str, Any]], connection: dict[str, Any] | None,
    prices: dict[str, Any] | None, *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not connection or not connection.get("marketplaceAccountId"):
        return goods
    now = now or datetime.now(timezone.utc)
    connection_expiry = timestamp(connection.get("expiresAt"))
    prices = prices or {}
    active = (connection.get("activeHash") and connection_expiry and connection_expiry > now
              and prices.get("marketplaceAccountId") == connection["marketplaceAccountId"]
              and prices.get("accountBindingVersion") == connection.get("ingestionBindingVersion"))
    observations = prices.get("items", {}) if active else {}
    if not isinstance(observations, dict):
        observations = {}
    result = []
    for good in goods:
        item = {key: value for key, value in good.items() if key not in _BUYER_FIELDS}
        sizes = good.get("sizes") or []
        size = {key: value for key, value in (sizes[0] if sizes else {}).items() if key not in _BUYER_FIELDS}
        item["sizes"] = [size] if sizes else []
        observation = observations.get(f"{good.get('nmID')}:{size.get('sizeID')}") or {}
        observed_at = timestamp(observation.get("observedAt"))
        seller_at = timestamp(observation.get("sellerPriceObservedAt"))
        seller_price = (wb_goods_price_to_kopecks(size.get("discountedPrice"))
                        or wb_goods_price_to_kopecks(size.get("price")))
        buyer_price = observation.get("buyerPriceNoWalletKopecks")
        wallet_price = observation.get("buyerPriceWithWalletKopecks")
        if (observed_at and seller_at and connection_expiry
                and observed_at <= now + timedelta(seconds=30)
                and seller_at <= now + timedelta(seconds=30)
                and observation.get("nmId") == good.get("nmID")
                and observation.get("sizeId") == size.get("sizeID")
                and observation.get("sellerPriceKopecks") == seller_price
                and type(buyer_price) is int and 0 < buyer_price <= seller_price):
            size.update(
                buyerPriceNoWalletKopecks=buyer_price,
                buyerPriceOriginalKopecks=observation.get("originalPriceKopecks"),
                buyerPriceWithWalletKopecks=(wallet_price if type(wallet_price) is int and 0 < wallet_price <= buyer_price else None),
                buyerPriceSource="wb_browser", buyerPriceObservedAt=observed_at.isoformat(),
                buyerPriceSellerKopecks=seller_price,
                # Saved observations do not expire by age. Keep connection
                # expiry and the original observation time for authorization
                # and the independent automatic-price safety checks.
                buyerPriceExpiresAt=connection_expiry.isoformat(),
            )
        result.append(item)
    return result


def rows_expire_at(rows: list[dict[str, Any]]) -> str | None:
    deadlines = [value for row in rows
                 if (value := (row.get("analytics") or {}).get("buyerPriceExpiresAt"))]
    return min(deadlines, key=lambda value: timestamp(value) or datetime.min.replace(tzinfo=timezone.utc)) if deadlines else None
