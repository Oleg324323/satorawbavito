"""Price-plane and identity helpers for WB repricing; no I/O or settings writes."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import base64
from typing import Any

from app.wb_api.price_units import wb_goods_price_to_kopecks


def utc_timestamp(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def price_baseline(good: dict[str, Any], observed_at: Any = None) -> dict[str, Any] | None:
    """Keep every size; never guess a missing seller discount or product identity."""
    nm = good.get("nmID")
    discount = good.get("discount")
    if type(nm) is not int or nm <= 0 or type(discount) is not int or not 0 <= discount < 100:
        return None
    if good.get("currencyIsoCode4217") not in {None, "RUB"}:
        return None
    sizes = good.get("sizes")
    if not isinstance(sizes, list) or not sizes:
        return None
    rows = []
    for size in sizes:
        if not isinstance(size, dict) or type(size.get("sizeID")) is not int or size["sizeID"] <= 0:
            return None
        base = wb_goods_price_to_kopecks(size.get("price"))
        seller = wb_goods_price_to_kopecks(size.get("discountedPrice"))
        if base is None or seller is None or base <= 0 or seller <= 0:
            return None
        rows.append({"sizeId": size["sizeID"], "basePriceKopecks": base, "sellerPriceKopecks": seller})
    if len({x["sizeId"] for x in rows}) != len(rows):
        return None
    return {"nmId": nm, "articleId": str(good.get("vendorCode") or ""), "discountPct": discount,
            "clubDiscountPct": good.get("clubDiscount"), "editableSizePrice": good.get("editableSizePrice"),
            "sizes": sorted(rows, key=lambda x: x["sizeId"]), "observedAt": observed_at}


def baseline_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in ("nmId", "articleId", "discountPct", "clubDiscountPct", "editableSizePrice", "sizes")}


def seller_to_base(seller_kopecks: int, discount_pct: int, *, minimum: int | None = None, maximum: int | None = None) -> tuple[int, int]:
    """WB accepts integer RUB base prices; compute actual post-discount kopecks."""
    if type(seller_kopecks) is not int or seller_kopecks <= 0 or type(discount_pct) is not int or not 0 <= discount_pct < 100:
        raise ValueError("INVALID_SELLER_PRICE_OR_DISCOUNT")
    rubles = int((Decimal(seller_kopecks) / (100 - discount_pct)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rubles <= 0 and minimum is None and maximum is None:
        raise ValueError("WB_PRICE_ROUNDS_TO_ZERO")
    unit = 100 - discount_pct
    lower = max(1, (minimum + unit - 1) // unit) if minimum is not None else 1
    upper = maximum // unit if maximum is not None else None
    if upper is not None and lower > upper:
        raise ValueError("NO_REPRESENTABLE_PRICE_WITHIN_LIMITS")
    rubles = max(lower, rubles)
    if upper is not None:
        rubles = min(upper, rubles)
    if rubles <= 0:
        raise ValueError("WB_PRICE_ROUNDS_TO_ZERO")
    return rubles * 100, rubles * (100 - discount_pct)


def seller_price_interval(current: int | None, minimum: int | None, maximum: int | None,
                          step_pct: float, daily_reference: int | None, daily_pct: float) -> tuple[int, int | None]:
    lower, upper = max(1, minimum or 1), maximum
    for reference, percent in ((current, step_pct), (daily_reference, daily_pct)):
        if reference is None or reference <= 0:
            continue
        cap = Decimal(str(percent)) / 100
        low = int((Decimal(reference) * (1 - cap)).to_integral_value(rounding=ROUND_CEILING))
        high = int((Decimal(reference) * (1 + cap)).to_integral_value(rounding=ROUND_FLOOR))
        lower = max(lower, low)
        upper = high if upper is None else min(upper, high)
    return lower, upper


def binding_key(client: Any, organization_id: int | None) -> str | None:
    """A non-secret equality fingerprint, never a credential or send permission."""
    current = client
    for _ in range(6):
        token = getattr(current, "token", None)
        if isinstance(token, str) and token:
            return hashlib.sha256(f"wb-repricer:{organization_id}:".encode() + token.encode()).hexdigest()
        current = getattr(current, "inner", None)
        if current is None:
            break
    return None


def price_write_permission(client: Any) -> str | None:
    """None for non-token test adapters; never emit or persist JWT claims."""
    current = client
    for _ in range(6):
        token = getattr(current, "token", None)
        if isinstance(token, str) and token:
            try:
                part = token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                mask = claims.get("s")
                if type(mask) is not int:
                    return "wb_price_permissions_unknown"
                if mask & (1 << 30):
                    return "wb_token_read_only"
                if not mask & (1 << 3):
                    return "wb_prices_scope_missing"
                return None
            except (ValueError, IndexError, TypeError):
                return "wb_price_permissions_unknown"
        current = getattr(current, "inner", None)
        if current is None:
            break
    return None


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


class ScopedPriceClient:
    """Execution-boundary fence shared by initial submission and read-only retry."""
    def __init__(self, inner: Any, nm_ids: set[int], baselines: dict[int, dict[str, Any]] | None = None, *, on_upload=None):
        self.inner = inner
        self.nm_ids = frozenset(nm_ids)
        self.upload_ids: set[int] = set()
        self.baselines = {key: value for key, value in (baselines or {}).items() if isinstance(value, dict)}
        self.on_upload = on_upload
        self.before_upload = None
        self.mutation_attempted = False

    def request(self, request):
        body = request.jsonBody or {}
        if request.path in {"/api/v2/upload/task", "/api/v2/upload/task/size"} and request.method == "POST":
            rows = body.get("data")
            valid = isinstance(rows, list) and bool(rows) and all(isinstance(row, dict)
                and type(row.get("nmID")) is int and row["nmID"] in self.nm_ids
                and type(row.get("price")) is int and row["price"] > 0
                and set(row) <= {"nmID", "sizeID", "price", "discount", "minPrice"} for row in rows)
            if valid and request.path.endswith("/size"):
                valid = all(type(row.get("sizeID")) is int and row["sizeID"] in
                    {size["sizeId"] for size in self.baselines.get(row["nmID"], {}).get("sizes", [])}
                    and set(row) == {"nmID", "sizeID", "price"} for row in rows)
            elif valid:
                valid = all("sizeID" not in row and type(row.get("discount")) is int and 0 <= row["discount"] < 100 for row in rows)
        elif request.path == "/api/v2/list/goods/filter" and request.method == "POST":
            ids = body.get("nmList")
            valid = isinstance(ids, list) and bool(ids) and set(ids) <= self.nm_ids and set(body) == {"nmList"}
        elif request.path in {"/api/v2/history/tasks", "/api/v2/history/goods/task"} and request.method == "GET":
            valid = request.query.get("uploadID") in self.upload_ids
        else:
            valid = False
        if not valid:
            raise ValueError("WB_PRICE_EXECUTION_SCOPE_VIOLATION")
        if request.method == "POST" and request.path in {"/api/v2/upload/task", "/api/v2/upload/task/size"}:
            if self.before_upload is not None and not self.mutation_attempted:
                self.before_upload()
            self.mutation_attempted = True
        response = self.inner.request(request)
        if request.method == "POST" and request.path.startswith("/api/v2/upload/task") and response.ok:
            data = response.data.get("data", response.data) if isinstance(response.data, dict) else {}
            upload_id = data.get("id") or data.get("uploadID")
            if type(upload_id) is int and upload_id > 0:
                self.upload_ids.add(upload_id)
                if self.on_upload is not None:
                    self.on_upload(upload_id)
        return response
