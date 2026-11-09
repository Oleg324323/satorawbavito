"""Strict WB Prices source parser for future canonical snapshot ingestion.

Only seller and Club fields are interpreted. No provider calls, publication,
buyer-price inference, legacy unit guessing, or offer-mapping lookup occurs here.
Raw-byte checksums refer to source responses, not reserialized observations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Literal


class PriceSourceValidationError(ValueError):
    """Sanitized source-shape/unit error; the caller retains raw run evidence."""


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 9223372036854775807:
        raise PriceSourceValidationError("source integer outside storage range")
    return value


def _exact_number(value: object, scale: int) -> int:
    """Convert without Decimal context rounding or allocating unbounded powers."""
    if type(value) is int:
        return _integer(value * (10 ** scale))
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise PriceSourceValidationError("finite nonnegative JSON number required")
    if not value:
        return 0
    parts = value.as_tuple()
    exponent = parts.exponent + scale
    if len(parts.digits) + exponent > 19:
        raise PriceSourceValidationError("source money outside storage range")
    digits = parts.digits
    if exponent < 0:
        remove = -exponent
        if remove >= len(digits) or any(digits[-remove:]):
            raise PriceSourceValidationError("fractional storage unit not supported")
        digits = digits[:-remove]
        exponent = 0
    integer = int("".join(str(digit) for digit in digits)) * (10 ** exponent)
    return _integer(integer)


@dataclass(frozen=True, slots=True)
class PriceField:
    presence: Literal["missing", "null", "value"]
    value: int | None

    def __post_init__(self) -> None:
        if self.presence == "value":
            _integer(self.value)
        elif self.presence not in ("missing","null") or self.value is not None:
            raise PriceSourceValidationError("field presence/value mismatch")


def _field(row: dict, name: str, *, money: bool = False) -> PriceField:
    if name not in row:
        return PriceField("missing", None)
    if row[name] is None:
        return PriceField("null", None)
    value = _exact_number(row[name], 2 if money else 0)
    if not money and value > 100:
        raise PriceSourceValidationError("source percentage outside 0..100")
    return PriceField("value", value)


@dataclass(frozen=True, slots=True)
class PriceSizeObservation:
    source_size_id: int | None
    list_price: PriceField
    discounted_price: PriceField
    club_price: PriceField

    def __post_init__(self) -> None:
        if self.source_size_id is not None:
            _integer(self.source_size_id,1)
        if not all(isinstance(value,PriceField) for value in
                   (self.list_price,self.discounted_price,self.club_price)):
            raise PriceSourceValidationError("typed size price fields required")


@dataclass(frozen=True, slots=True)
class PriceProductObservation:
    nm_id: int
    discount: PriceField
    club_discount: PriceField
    sizes: tuple[PriceSizeObservation, ...]

    def __post_init__(self) -> None:
        _integer(self.nm_id,1)
        if type(self.sizes) is not tuple or any(not isinstance(size,PriceSizeObservation) for size in self.sizes):
            raise PriceSourceValidationError("immutable typed sizes required")
        ids=[size.source_size_id for size in self.sizes if size.source_size_id is not None]
        if len(ids) != len(set(ids)):
            raise PriceSourceValidationError("duplicate source size identity")
        for value in (self.discount,self.club_discount):
            if not isinstance(value,PriceField) or (value.value is not None and value.value > 100):
                raise PriceSourceValidationError("typed source percentage required")

    @property
    def source_sizes_identified(self) -> bool:
        """Source identity availability only; no canonical CatalogSku mapping proof."""
        return bool(self.sizes) and all(size.source_size_id is not None for size in self.sizes)


@dataclass(frozen=True, slots=True)
class GoodsPricePage:
    organization_id: int
    marketplace_account_id: int
    offset: int
    limit: int
    received_at: datetime
    raw_checksum: str
    products: tuple[PriceProductObservation, ...]
    request_checksum: str
    source_observed_at: None = None

    def __post_init__(self) -> None:
        for value in (self.organization_id,self.marketplace_account_id,self.limit):
            _integer(value,1)
        _integer(self.offset)
        for value in (self.raw_checksum,self.request_checksum):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}",value) is None:
                raise PriceSourceValidationError("source SHA-256 required")
        if (not isinstance(self.received_at,datetime) or self.received_at.tzinfo is None
                or self.received_at.utcoffset() is None or self.source_observed_at is not None):
            raise PriceSourceValidationError("aware receive time and unknown source time required")
        if (type(self.products) is not tuple or len(self.products)>self.limit
                or any(not isinstance(product,PriceProductObservation) for product in self.products)):
            raise PriceSourceValidationError("immutable typed product page required")
        if len({product.nm_id for product in self.products}) != len(self.products):
            raise PriceSourceValidationError("duplicate product identity")

    @property
    def terminal(self) -> bool:
        """One short page, not proof of a complete collection/run."""
        return len(self.products) < self.limit

    @property
    def next_offset(self) -> int | None:
        return None if self.terminal else self.offset + self.limit


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PriceSourceValidationError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise PriceSourceValidationError("non-finite JSON constant")


def parse_goods_price_page(
    raw_json: bytes, *, organization_id: int, marketplace_account_id: int,
    offset: int, limit: int, received_at: datetime, request_checksum: str,
) -> GoodsPricePage:
    """Parse one raw `/api/v2/list/goods/filter` page with explicit RUB units.

    Missing size identity stays unresolved. Unknown source fields remain bound
    by the raw checksum, but are not projected into new inferred facts. Duplicate
    product/size identities reject the page; never silently drop a malformed row
    and infer completion from a shortened page. The caller owns run continuity.
    """
    _integer(organization_id, 1)
    _integer(marketplace_account_id, 1)
    _integer(offset)
    _integer(limit, 1)
    if type(request_checksum) is not str or re.fullmatch(r"[0-9a-f]{64}",request_checksum) is None:
        raise PriceSourceValidationError("collection request checksum required")
    if (not isinstance(received_at, datetime) or received_at.tzinfo is None
            or received_at.utcoffset() is None):
        raise PriceSourceValidationError("aware received time required")
    if type(raw_json) is not bytes:
        raise PriceSourceValidationError("raw source bytes required")
    try:
        payload = json.loads(raw_json.decode("utf-8"), parse_float=Decimal,
                             parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, DecimalException):
        raise PriceSourceValidationError("invalid source JSON") from None
    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), dict)
            or not isinstance(payload["data"].get("listGoods"), list)):
        raise PriceSourceValidationError("explicit listGoods page required")
    for node in (payload,payload["data"]):
        if (("error" in node and node["error"] is not False)
                or node.get("errorText") not in (None,"")):
            raise PriceSourceValidationError("provider reported a source error")
        if "currencyIsoCode" in node and node["currencyIsoCode"] != "RUB":
            raise PriceSourceValidationError("source currency is not RUB")
    goods = payload["data"]["listGoods"]
    if len(goods) > limit:
        raise PriceSourceValidationError("page exceeds requested limit")
    products = []
    seen_products = set()
    for product in goods:
        if not isinstance(product, dict) or not isinstance(product.get("sizes"), list):
            raise PriceSourceValidationError("product with explicit sizes required")
        if "currencyIsoCode" in product and product["currencyIsoCode"] != "RUB":
            raise PriceSourceValidationError("source currency is not RUB")
        nm_id = _integer(product.get("nmID"), 1)
        if nm_id in seen_products:
            raise PriceSourceValidationError("duplicate product identity")
        seen_products.add(nm_id)
        sizes = []
        seen_sizes = set()
        for size in product["sizes"]:
            if not isinstance(size, dict):
                raise PriceSourceValidationError("size object required")
            size_id = size.get("sizeID")
            if size_id is not None:
                _integer(size_id, 1)
                if size_id in seen_sizes:
                    raise PriceSourceValidationError("duplicate source size identity")
                seen_sizes.add(size_id)
            sizes.append(PriceSizeObservation(size_id, _field(size,"price",money=True),
                                              _field(size,"discountedPrice",money=True),
                                              _field(size,"clubDiscountedPrice",money=True)))
        products.append(PriceProductObservation(nm_id, _field(product,"discount"),
                                                _field(product,"clubDiscount"), tuple(sizes)))
    return GoodsPricePage(organization_id,marketplace_account_id,offset,limit,
                          received_at,hashlib.sha256(raw_json).hexdigest(),tuple(products),request_checksum)


@dataclass(frozen=True, slots=True)
class GoodsPriceRun:
    pages: tuple[GoodsPricePage, ...]
    products: tuple[PriceProductObservation, ...] = field(init=False)
    complete: bool = field(init=False)
    manifest_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        products,complete,checksum = _price_run_values(self.pages)
        object.__setattr__(self,"products",products)
        object.__setattr__(self,"complete",complete)
        object.__setattr__(self,"manifest_checksum",checksum)

    @property
    def received_at(self) -> datetime:
        return self.pages[-1].received_at


def assemble_goods_price_run(pages: tuple[GoodsPricePage, ...]) -> GoodsPriceRun:
    """Validate a contiguous prefix; only an explicit short final page completes.

    One source request context (excluding page offset) is mandatory. This proves
    transport continuity, not provider transaction isolation, identity mapping,
    or freshness. No latest/current publication or persistence occurs here.
    """
    return GoodsPriceRun(pages)


def _price_run_values(pages: tuple[GoodsPricePage,...]) -> tuple[tuple[PriceProductObservation,...],bool,str]:
    if type(pages) is not tuple or not pages or any(not isinstance(page,GoodsPricePage) for page in pages):
        raise PriceSourceValidationError("nonempty immutable page sequence required")
    first = pages[0]
    context = (first.organization_id,first.marketplace_account_id,first.limit,first.request_checksum)
    expected_offset = 0
    previous_time = None
    products = []
    seen = set()
    manifests = []
    for page in pages:
        if (page.organization_id,page.marketplace_account_id,page.limit,page.request_checksum) != context:
            raise PriceSourceValidationError("price page context mismatch")
        try:
            received_time = page.received_at.astimezone(UTC)
        except OverflowError:
            raise PriceSourceValidationError("receipt outside supported UTC range") from None
        if page.offset != expected_offset or (previous_time is not None and received_time < previous_time):
            raise PriceSourceValidationError("price page continuity mismatch")
        for product in page.products:
            if product.nm_id in seen:
                raise PriceSourceValidationError("cross-page product identity repeats")
            seen.add(product.nm_id)
            products.append(product)
        manifests.append((page.offset,page.limit,page.raw_checksum))
        expected_offset = page.next_offset
        previous_time = received_time
    manifest = json.dumps(["wb-goods-prices/v1",context,manifests],
                          separators=(",",":"),ensure_ascii=True).encode("ascii")
    return tuple(products),pages[-1].terminal,hashlib.sha256(manifest).hexdigest()
