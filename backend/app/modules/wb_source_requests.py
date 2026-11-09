"""Immutable collection identity, not a provider client or authorization grant.

Only unfiltered seller prices and WB warehouse stocks are described here. FBS,
filtered products, buyer prices and transport fallback require separate contracts.
Page offset is intentionally excluded: the page manifest records pagination.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class CollectionRequestError(ValueError):
    """Unsafe or unresolved collection context, without echoing source input."""


class SourceKind(str, Enum):
    prices = "wb_goods_prices_v1"
    wb_warehouse = "wb_warehouse_v1"


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    organization_id: int
    marketplace_account_id: int
    source_kind: SourceKind
    parser_version: str
    page_limit: int

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.marketplace_account_id, self.page_limit):
            if type(value) is not int or value <= 0:
                raise CollectionRequestError("positive internal integer required")
        if not isinstance(self.source_kind, SourceKind):
            raise CollectionRequestError("supported source kind required")
        if (type(self.parser_version) is not str or not self.parser_version
                or self.parser_version != self.parser_version.strip()
                or any(not 32 <= ord(char) <= 126 for char in self.parser_version)):
            raise CollectionRequestError("nonblank printable ASCII parser version required")
        if len(self.canonical_bytes) > 65536:
            raise CollectionRequestError("collection request exceeds storage envelope")

    @property
    def canonical_bytes(self) -> bytes:
        warehouse = self.source_kind is SourceKind.wb_warehouse
        payload = {
            "schema": "wb-source-collection/v1",
            "organizationId": self.organization_id,
            "accountId": self.marketplace_account_id,
            "sourceKind": self.source_kind.value,
            "parserVersion": self.parser_version,
            "method": "POST",
            "path": ("/api/analytics/v1/stocks-report/wb-warehouses" if warehouse
                     else "/api/v2/list/goods/filter"),
            "query": {},
            "body": {"stockType": "wb"} if warehouse else {},
            "pageLimit": self.page_limit,
        }
        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False).encode("ascii")
        except ValueError:
            raise CollectionRequestError("collection request cannot be encoded") from None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()
