"""Bounded preliminary Orders evidence; not the canonical Orders projection.

The owning repository supplies the same session, lease and publication guards
as content/prices. No provider, keyring, private ORM base or alternate authority.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import text

from app.modules.orders import Marketplace, make_wb_source_line_key, map_wb_statistics_status
from app.orders.serialization import deserialize_observation, observation_checksum, serialize_observation
from app.wb_live.contracts import WbLiveError
from app.wb_live.statistics_orders import HistoryOrderRow, OrdersPageEnd, _instant, orders_request

HISTORY_SOURCE = "wb-statistics-supplier-orders"
HISTORY_INTERVAL = 10800
_OWNER = "organization_id=:o AND marketplace_account_id=:a AND job_id=:j AND source=:source"
_PAGE = _OWNER + " AND page_id=:page"
_LEASE_PAGE = _PAGE + " AND run_id=:run AND lease_token=:lease"
_PAGE_COLUMNS = ("page_id AS page, state, receipt, input_date_from AS date_from, request_checksum, "
    "credential_id, credential_generation, account_incarnation")


def _invalid():
    raise WbLiveError("WB_RESPONSE_INVALID")


def _hash(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        _invalid()
    return value


def validate_date_from(value):
    # Native text is retained byte-for-byte; parsing is validation/comparison only.
    try:
        _instant(value, allow_date=True)
    except (ValueError, TypeError, AttributeError):
        _invalid()
    return value


def _scope(lease, page_id=None):
    if lease.source != HISTORY_SOURCE or type(lease.checkpoint) is not dict or set(lease.checkpoint) != {"dateFrom"}:
        _invalid()
    date_from = validate_date_from(lease.checkpoint["dateFrom"])
    try:
        scope = {"o": lease.locator.org_id, "a": lease.locator.account_id, "j": UUID(lease.locator.job_id),
            "source": HISTORY_SOURCE, "run": UUID(lease.run_id), "lease": UUID(lease.lease_token)}
        if page_id is not None:
            if type(page_id) is not str:
                _invalid()
            scope["page"] = UUID(page_id)
        checksum = hashlib.sha256(orders_request(scope["o"], scope["a"], date_from)[1]).hexdigest()
    except (ValueError, TypeError, AttributeError):
        _invalid()
    return scope, date_from, checksum


def _page(s, scope):
    page = s.execute(text("SELECT " + _PAGE_COLUMNS + " FROM wb_live_history_pages WHERE " + _LEASE_PAGE + " FOR UPDATE"),
        scope).mappings().first()
    if page is None:
        raise WbLiveError("WB_SYNC_CONFLICT")
    return page


def _binding(page, lease, date_from, checksum):
    if (page["date_from"] != date_from or page["request_checksum"] != checksum
            or page["credential_id"] != lease.credential_id or page["credential_generation"] != lease.generation
            or page["account_incarnation"] != lease.account_incarnation):
        raise WbLiveError("WB_SYNC_CONFLICT")


def begin_history_page(repo, lease, *, request_checksum):
    scope, date_from, checksum = _scope(lease)
    if _hash(request_checksum) != checksum:
        _invalid()
    with repo._session(lease.locator) as s:
        repo._leased(s, lease)
        existing = s.execute(text("SELECT " + _PAGE_COLUMNS + " FROM wb_live_history_pages WHERE " + _OWNER
            + " AND run_id=:run AND lease_token=:lease FOR UPDATE"), scope).mappings().first()
        if existing is not None:
            _binding(existing, lease, date_from, checksum)
            return str(existing["page"])
        scope.update(page=uuid4(), date_from=date_from, request_checksum=checksum,
            credential_id=lease.credential_id, credential_generation=lease.generation,
            account_incarnation=lease.account_incarnation)
        s.execute(text("INSERT INTO wb_live_history_pages(organization_id,marketplace_account_id,job_id,source,"
            "run_id,lease_token,page_id,input_date_from,request_checksum,credential_id,credential_generation,account_incarnation) "
            "VALUES(:o,:a,:j,:source,:run,:lease,:page,:date_from,:request_checksum,:credential_id,:credential_generation,:account_incarnation)"), scope)
        return str(scope["page"])


def _prepare(lease, rows, first):
    if type(first) is not int or not 0 <= first < 100000 or type(rows) is not tuple or not 1 <= len(rows) <= 1000 or first + len(rows) > 100000:
        _invalid()
    prepared, seen = [], set()
    lower = _instant(lease.checkpoint["dateFrom"], allow_date=True)
    for ordinal, row in enumerate(rows, first):
        try:
            if type(row) is not HistoryOrderRow or type(row.nm_id) is not int or not 0 < row.nm_id < 2**63:
                _invalid()
            if row.barcode is not None and (type(row.barcode) is not str or not 1 <= len(row.barcode) <= 255
                    or row.barcode != row.barcode.strip() or "\x00" in row.barcode):
                _invalid()
            raw_checksum = _hash(row.source_row_checksum)
            payload = serialize_observation(row.observation)
            observation = deserialize_observation(payload)
            identity = observation.identity
            srid = identity.external_order_id
            if ((identity.organization_id, identity.marketplace_account_id, identity.marketplace)
                    != (lease.locator.org_id, lease.locator.account_id, Marketplace.WB)
                    or not 1 <= len(srid) <= 512 or srid != srid.strip() or srid in seen
                    or observation.source_kind != HISTORY_SOURCE
                    or observation.adapter_version != "wb-statistics-orders-stream-v1"
                    or len(observation.items) != 1 or type(observation.wb_is_cancelled) is not bool
                    or type(observation.wb_cancel_evidence_present) is not bool
                    or observation.status != map_wb_statistics_status(None, observation.wb_is_cancelled, observation.wb_cancel_evidence_present)):
                _invalid()
            item = observation.items[0]
            if (item.quantity != 1 or item.stable_unit_id != srid or item.stable_order_line_id is not None
                    or item.identity.source_line_key != make_wb_source_line_key(srid)
                    or item.identity.external_item_id != str(row.nm_id) or item.identity.occurrence_index != 0):
                _invalid()
            effective = _instant(observation.source_revision)
            if observation.effective_at != effective or effective < lower:
                _invalid()
            lower = effective
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            if len(encoded) > 16384:
                _invalid()
            prepared.append({"ordinal": ordinal, "srid": srid, "nm_id": row.nm_id, "barcode": row.barcode,
                "semantic_checksum": observation_checksum(observation), "source_row_checksum": raw_checksum,
                "observation": encoded, "source_revision": observation.source_revision, "effective_at": effective})
            seen.add(srid)
        except WbLiveError:
            raise
        except (ValueError, TypeError, AttributeError, UnicodeError):
            _invalid()
    return prepared


def _count(s, scope):
    return s.execute(text("SELECT count(*) AS count, min(ordinal) AS first, max(ordinal) AS last "
        "FROM wb_live_history_rows WHERE " + _PAGE), scope).mappings().one()


def _last(s, scope):
    return s.execute(text("SELECT ordinal, source_revision, effective_at FROM wb_live_history_rows WHERE "
        + _PAGE + " ORDER BY ordinal DESC LIMIT 1"), scope).mappings().first()


def stage_history_rows(repo, lease, *, page_id, first_ordinal, rows):
    scope, date_from, checksum = _scope(lease, page_id)
    prepared = _prepare(lease, rows, first_ordinal)
    with repo._session(lease.locator) as s:
        repo._leased(s, lease)
        page = _page(s, scope)
        _binding(page, lease, date_from, checksum)
        if page["state"] != "staging":
            raise WbLiveError("WB_SYNC_CONFLICT")
        count = _count(s, scope)["count"]
        if first_ordinal > count:
            raise WbLiveError("WB_SYNC_CONFLICT")
        # At most the ordinal range and the incoming <=1000 identities are read.
        existing = s.execute(text("SELECT ordinal, srid, semantic_checksum, source_row_checksum, nm_id, barcode "
            "FROM wb_live_history_rows WHERE " + _PAGE
            + " AND (ordinal BETWEEN :first AND :last OR srid=ANY(:srids)) ORDER BY ordinal LIMIT 2001"),
            {**scope, "first": first_ordinal, "last": first_ordinal + len(rows) - 1,
             "srids": [r["srid"] for r in prepared]}).mappings().all()
        by_ordinal = {r["ordinal"]: r for r in existing}
        by_srid = {r["srid"]: r["ordinal"] for r in existing}
        suffix = []
        for row in prepared:
            old = by_ordinal.get(row["ordinal"])
            if old is not None:
                if any(old[key] != row[key] for key in ("srid", "semantic_checksum", "source_row_checksum", "nm_id", "barcode")):
                    raise WbLiveError("WB_SYNC_CONFLICT")
            else:
                if row["ordinal"] != count + len(suffix) or row["srid"] in by_srid:
                    raise WbLiveError("WB_SYNC_CONFLICT")
                suffix.append(row)
        if suffix:
            last = _last(s, scope)
            if last is not None and suffix[0]["effective_at"] < last["effective_at"]:
                _invalid()
            s.execute(text("INSERT INTO wb_live_history_rows(organization_id,marketplace_account_id,job_id,source,page_id,"
                "ordinal,srid,nm_id,barcode,semantic_checksum,source_row_checksum,observation,source_revision,effective_at) "
                "VALUES(:o,:a,:j,:source,:page,:ordinal,:srid,:nm_id,:barcode,:semantic_checksum,:source_row_checksum,"
                "CAST(:observation AS jsonb),:source_revision,:effective_at)"), [{**scope, **row} for row in suffix])
        return True


def commit_history_page(repo, lease, *, page_id, end, next_due_at):
    scope, date_from, checksum = _scope(lease, page_id)
    if (type(end) is not OrdersPageEnd or type(end.row_count) is not int or not 0 <= end.row_count <= 100000
            or type(end.terminal) is not bool or end.terminal != (end.row_count == 0)
            or _hash(end.request_checksum) != checksum):
        _invalid()
    _hash(end.raw_checksum)
    validate_date_from(end.next_date_from)
    if not isinstance(next_due_at, datetime) or next_due_at.utcoffset() is None:
        _invalid()
    if end.terminal:
        if end.next_date_from != date_from:
            _invalid()
    else:
        try:
            progressed = _instant(end.next_date_from) > _instant(date_from, allow_date=True)
        except (ValueError, TypeError, AttributeError):
            _invalid()
        if not progressed:
            _invalid()
    receipt = {"next_date_from": end.next_date_from, "terminal": end.terminal, "row_count": end.row_count,
        "raw_checksum": end.raw_checksum, "request_checksum": end.request_checksum}
    with repo._session(lease.locator) as s:
        # The old lease can be gone on ACK replay; current durable authority cannot.
        job = repo._job(s, lease.locator)
        guard = repo._guard(s, job)
        job = repo._job(s, lease.locator, lock=True)
        page = _page(s, scope)
        _binding(page, lease, date_from, checksum)
        if (job.credential_id != lease.credential_id or job.credential_generation != lease.generation
                or job.account_incarnation != lease.account_incarnation):
            raise WbLiveError("WB_BINDING_CHANGED")
        if page["state"] == "published":
            if page["receipt"] != receipt:
                raise WbLiveError("WB_SYNC_CONFLICT")
            return True
        job, source, now = repo._lease_from_guard(s, lease, guard)
        count = _count(s, scope)
        if count["count"] != end.row_count or (end.row_count and (count["first"] != 0 or count["last"] != end.row_count - 1)):
            _invalid()
        if end.row_count:
            last = _last(s, scope)
            if last["source_revision"] != end.next_date_from:
                _invalid()
        s.execute(text("UPDATE wb_live_history_pages SET state='published', receipt=CAST(:receipt AS jsonb), published_at=:now "
            "WHERE " + _LEASE_PAGE + " AND state='staging'"), {**scope, "receipt": json.dumps(receipt), "now": now})
        source.checkpoint = {"dateFrom": end.next_date_from}
        source.processed, source.revision, source.attempt = source.processed + end.row_count, source.revision + 1, 0
        source.state, source.error_code, source.updated_at = ("completed" if end.terminal else "queued"), None, now
        source.next_due_at = max(source.next_due_at, next_due_at.astimezone(timezone.utc), now + timedelta(seconds=HISTORY_INTERVAL))
        source.lease_token = source.lease_expires_at = None
        repo._rollup(s, job, now)
        return True
