"""Dormant 0077 price/stock SQL participant; no fetch/auth/commit ownership.

Caller supplies actual paired credential evidence and a live-authorized physical
root. Returned values are provisional until that root commits. No TTL, provider
clock, worker permission, daily fact or current writer activation is inferred.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context
from app.modules.wb_price_snapshots import GoodsPricePage, PriceField, PriceSizeObservation
from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.modules.wb_stock_snapshots import WarehouseStockPage, WarehouseStockObservation, StockCount
from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.platform.integrations.publication_guard import _physical_connection


class CurrentSourceError(ValueError):
    def __init__(self, code="source_persistence_failed"):
        self.code = code if code in ("source_invalid", "source_conflict", "source_persistence_failed") else "source_persistence_failed"
        super().__init__(self.code)


def _int(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CurrentSourceError("source_invalid")
    return value


def _uuid(value):
    if type(value) is not UUID or value.version != 4:
        raise CurrentSourceError("source_invalid")
    return value


def _time(value):
    if type(value) is not datetime or value.utcoffset() is None:
        raise CurrentSourceError("source_invalid")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True, slots=True)
class CurrentRun:
    request: CollectionRequest
    run_id: UUID
    ingest_sequence: int
    state: str
    version: int
    started_at: datetime
    finished_at: datetime | None
    received_at: datetime | None
    manifest_checksum: str | None
    page_count: int
    raw_row_count: int
    fact_count: int
    safe_error_code: str | None
    external_account_id: str
    credential_ref: str | None
    credential_id: UUID
    credential_generation: int


@dataclass(frozen=True, slots=True)
class StoredPriceProduct:
    nm_id: int
    source_size_count: int
    unresolved_size_count: int
    discount: PriceField
    club_discount: PriceField
    known_sizes: tuple[PriceSizeObservation, ...]


@dataclass(frozen=True, slots=True)
class StoredSourcePage:
    run: CurrentRun
    page_no: int
    offset: int
    received_at: datetime
    http_status: int
    raw_checksum: str
    terminal: bool
    rows: tuple[StoredPriceProduct | WarehouseStockObservation, ...]


class CurrentSourceTransaction:
    """Scoped account-first participant for the two closed 0077 source families."""

    def __init__(self, session, request: CollectionRequest):
        if (type(request) is not CollectionRequest or not isinstance(session, Session)
                or not session.in_transaction() or not session.is_active or session.in_nested_transaction()
                or session.new or session.dirty or session.deleted):
            raise CurrentSourceError("source_invalid")
        request.__post_init__()
        _int(request.organization_id, 1, 2**31 - 1)
        _int(request.marketplace_account_id, 1, 2**31 - 1)
        _int(request.page_limit, 1)
        parser = "wb-goods-prices/v1" if request.source_kind is SourceKind.prices else "wb-warehouse-stocks/v1"
        if request.parser_version != parser:
            raise CurrentSourceError("source_invalid")
        self._session, self._request = session, request
        self._family = "price" if request.source_kind is SourceKind.prices else "stock"
        self._root = session.get_transaction()
        self._connection = _physical_connection(session)
        self._physical_root = self._connection.get_transaction()
        self._check()
        set_marketplace_account_context(session, organization_id=request.organization_id,
                                        marketplace_account_id=request.marketplace_account_id)
        self._params = {"org": request.organization_id, "account": request.marketplace_account_id,
                        "source": request.source_kind.value, "parser": request.parser_version,
                        "request_hash": request.checksum}
        self._owner = "organization_id=:org AND marketplace_account_id=:account"
        self._context = self._owner + " AND source_kind=:source AND parser_version=:parser AND request_checksum=:request_hash"
        if self._execute("SELECT marketplace_account_id FROM marketplace_accounts WHERE "
                         + self._owner + " AND marketplace='wb' FOR UPDATE", self._params).scalar_one_or_none() is None:
            raise CurrentSourceError("source_invalid")

    def _check(self):
        if (self._session.get_transaction() is not self._root or not self._root.is_active
                or not self._session.is_active or self._session.in_nested_transaction()
                or self._session.new or self._session.dirty or self._session.deleted
                or _physical_connection(self._session) is not self._connection
                or self._connection.get_transaction() is not self._physical_root
                or self._connection.get_isolation_level() != "READ COMMITTED"):
            raise CurrentSourceError("source_invalid")

    def _execute(self, sql, params):
        self._check()
        try:
            return self._session.execute(text(sql), params)
        except SQLAlchemyError as exc:
            original = getattr(exc, "orig", None)
            sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            raise CurrentSourceError("source_conflict" if sqlstate in ("23505", "40001", "40P01")
                                     else "source_persistence_failed") from None

    def _row(self, run_id):
        row = self._execute(f"SELECT * FROM wb_{self._family}_runs WHERE " + self._context
                            + " AND run_id=:run", {**self._params, "run": _uuid(run_id)}).mappings().one_or_none()
        if row is None or bytes(row["request_bytes"]) != self._request.canonical_bytes:
            raise CurrentSourceError("source_invalid")
        return row

    def _view(self, row):
        return CurrentRun(self._request, row["run_id"], row["ingest_sequence"], row["state"], row["version"],
            row["started_at"], row["finished_at"], row["received_at"], row["manifest_checksum"],
            row["page_count"], row["raw_row_count"], row["fact_count"], row["safe_error_code"],
            row["account_binding_external_account_id"], row["account_binding_credential_ref"],
            row["credential_id"], row["credential_generation"])

    def create(self, *, request_key: UUID, started_at: datetime, resolved_credential):
        """Actual paired wrapper required; this is not evidence of live permission."""
        _uuid(request_key)
        _time(started_at)
        if type(resolved_credential) is not ResolvedCredentialForFetch:
            raise CurrentSourceError("source_invalid")
        b = resolved_credential.binding
        c = b.credential_identity
        if ((b.owner.organization_id, b.owner.marketplace_account_id, b.owner.provider)
                != (self._request.organization_id, self._request.marketplace_account_id, "wb")
                or (c.organization_id, c.marketplace_account_id, c.provider, c.credential_kind)
                != (self._request.organization_id, self._request.marketplace_account_id, "wb", "wb_api")
                or c.expires_at is not None or c.payload_schema_version != 1):
            raise CurrentSourceError("source_invalid")
        _int(c.generation, 1)
        descriptor = _json({"credentialRef": b.credential_ref, "externalAccountId": b.external_account_id,
            "marketplace": "wb", "marketplaceAccountId": self._request.marketplace_account_id,
            "organizationId": self._request.organization_id, "schemaVersion": 1})
        intent = {
            "source_kind": self._request.source_kind.value, "parser_version": self._request.parser_version,
            "page_limit": self._request.page_limit, "request_key": request_key,
            "request_bytes": self._request.canonical_bytes, "request_checksum": self._request.checksum,
            "started_at": started_at, "account_binding_schema_version": 1,
            "account_binding_external_account_id": b.external_account_id,
            "account_binding_credential_ref": b.credential_ref, "account_binding_payload": descriptor,
            "account_binding_checksum": hashlib.sha256(descriptor).hexdigest(), "credential_id": c.credential_id,
            "credential_kind": "wb_api", "credential_generation": c.generation,
            "credential_payload_schema_version": 1, "credential_expires_at": None,
        }
        row = self._execute(f"SELECT * FROM wb_{self._family}_runs WHERE " + self._owner
            + " AND source_kind=:source AND request_key=:request_key", {**self._params, **intent}).mappings().one_or_none()
        if row is not None:
            if any((bytes(row[key]) if isinstance(row[key], memoryview) else row[key]) != value for key, value in intent.items()):
                raise CurrentSourceError("source_conflict")
            return self._view(row)
        names = tuple(intent)
        row = self._execute(f"INSERT INTO wb_{self._family}_runs(organization_id,marketplace_account_id,marketplace,"
            + ",".join(names) + ") VALUES(:org,:account,'wb'," + ",".join(":" + n for n in names)
            + ") RETURNING *", {**self._params, **intent}).mappings().one()
        return self._view(row)

    def _facts(self, page, page_no, run_id):
        owner = {"organization_id": self._request.organization_id, "marketplace_account_id": self._request.marketplace_account_id,
                 "marketplace": "wb", "run_id": run_id, "page_no": page_no}
        products, sizes, stocks = [], [], []
        if self._family == "price":
            for p in page.products:
                p.__post_init__()
                _int(p.nm_id, 1)
                row = {**owner, "nm_id": p.nm_id, "source_size_count": len(p.sizes),
                       "unresolved_size_count": sum(s.source_size_id is None for s in p.sizes)}
                for name in ("discount", "club_discount"):
                    f = getattr(p, name)
                    f.__post_init__()
                    if f.value is not None:
                        _int(f.value, 0, 100)
                    row[name + "_presence"], row[name] = f.presence, f.value
                products.append(row)
                for s in p.sizes:
                    s.__post_init__()
                    if s.source_size_id is None:
                        continue  # Only unresolved count, never an invented size identity.
                    _int(s.source_size_id, 1)
                    row = {key: value for key, value in owner.items() if key != "page_no"}
                    row.update(nm_id=p.nm_id, size_id=s.source_size_id, currency="RUB")
                    for attr, name in (("list_price", "list_price_kopecks"), ("discounted_price", "discounted_price_kopecks"), ("club_price", "club_price_kopecks")):
                        f = getattr(s, attr)
                        f.__post_init__()
                        if f.value is not None:
                            _int(f.value)
                        row[name + "_presence"], row[name] = f.presence, f.value
                    sizes.append(row)
        else:
            for s in page.observations:
                s.__post_init__()
                _int(s.nm_id, 1)
                _int(s.warehouse_id, 1)
                if s.chrt_id is not None:
                    _int(s.chrt_id, 1)
                row = {**owner, "stock_scope": "wb_warehouse", "nm_id": s.nm_id, "chrt_id": s.chrt_id,
                       "warehouse_id": s.warehouse_id, "warehouse_display_name": None}
                for name in ("quantity", "in_way_to_client", "in_way_from_client"):
                    f = getattr(s, name)
                    f.__post_init__()
                    if f.value is not None:
                        _int(f.value)
                    row[name + "_presence"], row[name] = f.presence, f.value
                stocks.append(row)
        return (("wb_price_product_facts", products), ("wb_price_size_facts", sizes)) if self._family == "price" else (("wb_stock_observations", stocks),)

    def append_page(self, run_id, page, *, page_no, http_status):
        """No request_id accepted: transport identifiers may themselves be secret."""
        _int(page_no)
        _int(http_status, 200, 299)
        expected_type = GoodsPricePage if self._family == "price" else WarehouseStockPage
        if type(page) is not expected_type:
            raise CurrentSourceError("source_invalid")
        page.__post_init__()
        if self._family == "price":
            if (page.organization_id, page.marketplace_account_id, page.limit, page.request_checksum) != (
                    self._request.organization_id, self._request.marketplace_account_id, self._request.page_limit, self._request.checksum):
                raise CurrentSourceError("source_invalid")
            count = len(page.products)
        else:
            if page.request != self._request:
                raise CurrentSourceError("source_invalid")
            count = len(page.observations)
        _int(page.offset)
        run = self._row(run_id)
        expected = {"page_no": page_no, "page_offset": page.offset, "requested_limit": self._request.page_limit,
            "received_at": page.received_at, "source_observed_at": None, "http_status": http_status,
            "raw_checksum": page.raw_checksum, "raw_row_count": count, "terminal": page.terminal, "request_id": None}
        params = {**self._params, "run": run_id, "page": page_no, "offset": page.offset}
        facts = self._facts(page, page_no, run_id)
        old = self._execute(f"SELECT * FROM wb_{self._family}_pages WHERE " + self._owner
            + " AND run_id=:run AND (page_no=:page OR page_offset=:offset)", params).mappings().all()
        if old:
            if len(old) != 1 or any(old[0][key] != value for key, value in expected.items()):
                raise CurrentSourceError("source_conflict")
            for table, rows in facts:
                if table == "wb_price_size_facts":
                    sql = "SELECT s.* FROM wb_price_size_facts s JOIN wb_price_product_facts p USING(organization_id,marketplace_account_id,run_id,nm_id) WHERE s.organization_id=:org AND s.marketplace_account_id=:account AND s.run_id=:run AND p.page_no=:page"
                else:
                    sql = f"SELECT * FROM {table} WHERE " + self._owner + " AND run_id=:run AND page_no=:page"
                persisted = self._execute(sql, params).mappings().all()
                # Row order is not an observation identity. Compare complete closed fields.
                if len(persisted) != len(rows) or any(not any(all(stored[k] == v for k, v in row.items()) for stored in persisted) for row in rows):
                    raise CurrentSourceError("source_conflict")
            return False
        if run["state"] != "collecting":
            raise CurrentSourceError("source_conflict")
        values = {"organization_id": self._request.organization_id, "marketplace_account_id": self._request.marketplace_account_id,
                  "marketplace": "wb", "run_id": run_id, **expected}
        self._insert(f"wb_{self._family}_pages", values)
        for table, rows in facts:
            for row in rows:
                self._insert(table, row)
        return True

    def _insert(self, table, values):
        names = tuple(values)
        self._execute(f"INSERT INTO {table}(" + ",".join(names) + ") VALUES("
                      + ",".join(":" + name for name in names) + ")", values)

    def finalize(self, run_id, *, safe_error_code=None, publish_expected_version=None):
        """None means seal history only; integer0 means expect no current head.

        Any head conflict must escape the caller's top-level root. Never retry
        publication from already sealed history, even an exact terminal replay.
        """
        allowed = ("SOURCE_REQUEST_FAILED", "SOURCE_INVALID_PAGE", "SOURCE_PAGINATION_STALLED",
            "SOURCE_PAGINATION_INCOMPLETE", "SOURCE_IDENTITY_UNRESOLVED", "SOURCE_DUPLICATE_IDENTITY",
            "SOURCE_SCOPE_MISMATCH", "SOURCE_REVISION_UNEXPLAINED")
        if safe_error_code is not None and (type(safe_error_code) is not str or safe_error_code not in allowed):
            raise CurrentSourceError("source_invalid")
        if publish_expected_version is not None:
            _int(publish_expected_version)
        run = self._row(run_id)
        params = {**self._params, "run": run_id}
        pages = self._execute(f"SELECT * FROM wb_{self._family}_pages WHERE " + self._owner
                             + " AND run_id=:run ORDER BY page_no", params).mappings().all()
        facts = self._execute("SELECT count(*) FROM " + ("wb_price_product_facts" if self._family == "price" else "wb_stock_observations")
                              + " WHERE " + self._owner + " AND run_id=:run", params).scalar_one()
        unresolved = False
        if self._family == "price":
            facts += self._execute("SELECT count(*) FROM wb_price_size_facts WHERE " + self._owner + " AND run_id=:run", params).scalar_one()
            unresolved = self._execute("SELECT EXISTS(SELECT 1 FROM wb_price_product_facts WHERE " + self._owner
                + " AND run_id=:run AND (source_size_count=0 OR unresolved_size_count>0))", params).scalar_one()
        complete = bool(pages and pages[-1]["terminal"] and not unresolved and safe_error_code is None)
        if unresolved:
            safe_error_code = "SOURCE_IDENTITY_UNRESOLVED"
        elif not complete and safe_error_code is None:
            safe_error_code = "SOURCE_PAGINATION_INCOMPLETE" if pages else "SOURCE_REQUEST_FAILED"
        manifest = None
        if pages:
            triples = [[p["page_offset"], p["requested_limit"], p["raw_checksum"]] for p in pages]
            envelope = (["wb-goods-prices/v1", [self._request.organization_id, self._request.marketplace_account_id,
                         self._request.page_limit, self._request.checksum], triples] if self._family == "price"
                        else ["wb-warehouse-stocks/v1", self._request.checksum, triples])
            manifest = hashlib.sha256(_json(envelope)).hexdigest()
        terminal = {"state": "complete" if complete else "partial" if pages else "failed", "version": 1,
            "received_at": pages[-1]["received_at"] if pages else None, "manifest_checksum": manifest,
            "page_count": len(pages), "raw_row_count": sum(p["raw_row_count"] for p in pages),
            "fact_count": facts, "safe_error_code": safe_error_code}
        if run["state"] != "collecting":
            if any(run[k] != v for k, v in terminal.items()) or publish_expected_version is not None:
                raise CurrentSourceError("source_conflict")
            return self._view(run)
        if publish_expected_version is not None and not complete:
            raise CurrentSourceError("source_conflict")
        row = self._execute(f"UPDATE wb_{self._family}_runs SET " + ",".join(k + "=:" + k for k in terminal)
            + " WHERE " + self._context + " AND run_id=:run AND state='collecting' AND version=0 RETURNING *",
            {**params, **terminal}).mappings().one_or_none()
        if row is None:
            raise CurrentSourceError("source_conflict")
        if publish_expected_version is not None:
            head = {**params, "sequence": row["ingest_sequence"], "version": publish_expected_version + 1,
                    "expected": publish_expected_version}
            if publish_expected_version == 0:
                self._execute(f"INSERT INTO wb_{self._family}_current_heads(organization_id,marketplace_account_id,marketplace,"
                    "source_kind,parser_version,request_checksum,run_id,published_sequence,version) "
                    "VALUES(:org,:account,'wb',:source,:parser,:request_hash,:run,:sequence,:version)", head)
            elif self._execute(f"UPDATE wb_{self._family}_current_heads SET run_id=:run,published_sequence=:sequence,version=:version WHERE "
                + self._context + " AND version=:expected AND published_sequence<:sequence RETURNING version", head).scalar_one_or_none() is None:
                raise CurrentSourceError("source_conflict")
        return self._view(row)

    def latest_attempt(self):
        row = self._execute(f"SELECT * FROM wb_{self._family}_runs WHERE " + self._context
                            + " ORDER BY ingest_sequence DESC LIMIT 1", self._params).mappings().one_or_none()
        return None if row is None else self._view(row)

    def read_page(self, run_id, *, page_no):
        """Exact immutable run/page read, including partial history; no fresh label."""
        _int(page_no)
        run = self._view(self._row(run_id))
        params = {**self._params, "run": run_id, "page": page_no}
        page = self._execute(f"SELECT * FROM wb_{self._family}_pages WHERE " + self._owner
            + " AND run_id=:run AND page_no=:page", params).mappings().one_or_none()
        if page is None:
            return None
        rows = []
        if self._family == "price":
            products = self._execute("SELECT * FROM wb_price_product_facts WHERE " + self._owner
                + " AND run_id=:run AND page_no=:page ORDER BY nm_id", params).mappings().all()
            for product in products:
                sizes = self._execute("SELECT * FROM wb_price_size_facts WHERE " + self._owner
                    + " AND run_id=:run AND nm_id=:nm ORDER BY size_id", {**params, "nm": product["nm_id"]}).mappings().all()
                known = tuple(PriceSizeObservation(s["size_id"],
                    PriceField(s["list_price_kopecks_presence"], s["list_price_kopecks"]),
                    PriceField(s["discounted_price_kopecks_presence"], s["discounted_price_kopecks"]),
                    PriceField(s["club_price_kopecks_presence"], s["club_price_kopecks"])) for s in sizes)
                rows.append(StoredPriceProduct(product["nm_id"], product["source_size_count"],
                    product["unresolved_size_count"], PriceField(product["discount_presence"], product["discount"]),
                    PriceField(product["club_discount_presence"], product["club_discount"]), known))
        else:
            stored = self._execute("SELECT * FROM wb_stock_observations WHERE " + self._owner
                + " AND run_id=:run AND page_no=:page ORDER BY warehouse_id,nm_id,chrt_id NULLS FIRST", params).mappings().all()
            rows = [WarehouseStockObservation(s["nm_id"], s["chrt_id"], s["warehouse_id"],
                    StockCount(s["quantity_presence"], s["quantity"]),
                    StockCount(s["in_way_to_client_presence"], s["in_way_to_client"]),
                    StockCount(s["in_way_from_client_presence"], s["in_way_from_client"])) for s in stored]
        return StoredSourcePage(run, page_no, page["page_offset"], page["received_at"], page["http_status"],
                                page["raw_checksum"], page["terminal"], tuple(rows))

    def current(self):
        head = self._execute(f"SELECT run_id,version FROM wb_{self._family}_current_heads WHERE "
                             + self._context, self._params).mappings().one_or_none()
        return None if head is None else (head["version"], self._view(self._row(head["run_id"])))
