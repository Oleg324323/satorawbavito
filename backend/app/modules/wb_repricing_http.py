"""Dormant bounded WB price transport. No scheduling, retry or outcome mutation.

Bootstrap must provide an account-wide durable quota admission callback and use
this POST only via DurableApprovalWorker's committed dispatch/before-I/O guard.
Admission is pacing, not authorization. The adapter cannot prove caller authority.
Official method contracts checked 2026-09-10: dev.wildberries.ru/docs/openapi/item-management.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock

import httpx

from app.modules.wb_repricing_dispatch import CanonicalApplyRequest, build_dispatch_key
from app.modules.wb_repricing_worker import OriginalPricePostResponse, canonical_request
from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.platform.integrations.repricer_job_contract import (
    Redacted,
    RepricerExpectedState,
    RepricerJobLocator,
    integer,
    timestamp,
    upload_id,
)
from app.wb_live.provider import _retry_after


class PriceHttpError(ValueError):
    def __init__(self, *, next_not_before=None):
        self.next_not_before = next_not_before
        super().__init__("WB_PRICE_HTTP_UNAVAILABLE")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PriceHttpError()
        result[key] = value
    return result


def _invalid(value):
    raise PriceHttpError()


def _json(raw):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_invalid,
            parse_float=_invalid,
        )
    except (ValueError, UnicodeError, RecursionError):
        pass
    raise PriceHttpError()


def _number(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise PriceHttpError()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class HistoryState:
    upload_id: str
    status: int
    total: int
    succeeded: int


@dataclass(frozen=True, slots=True, repr=False)
class HistoryGood:
    nm_id: int
    size_id: int | None
    article_id: str
    price_rubles: int | None
    discount_pct: int
    currency: str
    status: int
    has_error: bool


@dataclass(frozen=True, slots=True, repr=False)
class HistoryPage:
    upload_id: str
    offset: int
    limit: int
    goods: tuple[HistoryGood, ...]


@dataclass(frozen=True, slots=True, repr=False)
class HistoryObservation:
    organization_id: int
    marketplace_account_id: int
    path: str
    observed_at: datetime
    next_not_before: datetime
    raw_checksum: str
    result: HistoryState | HistoryPage


def matches_single_product_application(
    request, state_observation, goods_observation, *, receipt_upload_id
):
    """Strict positive evidence for the supported one-product operation only.

    False means insufficient/conflicting evidence, NEVER proof of nonacceptance.
    No domain transition: caller must persist evidence under the closing guard.
    """
    upload_id(receipt_upload_id)
    if type(request) is not CanonicalApplyRequest:
        raise PriceHttpError()
    if (
        type(state_observation) is not HistoryObservation
        or type(goods_observation) is not HistoryObservation
    ):
        raise PriceHttpError()
    for observation in (state_observation, goods_observation):
        if (observation.organization_id, observation.marketplace_account_id) != (
            request.scope.organization_id,
            request.scope.marketplace_account_id,
        ):
            raise PriceHttpError()
    state, page = state_observation.result, goods_observation.result
    if (
        type(state) is not HistoryState
        or type(page) is not HistoryPage
        or state_observation.path != "/api/v2/history/tasks"
        or goods_observation.path != "/api/v2/history/goods/task"
        or state.upload_id != page.upload_id
        or state.upload_id != receipt_upload_id
    ):
        raise PriceHttpError()
    if (
        request.size_id is not None
        or request.min_price_kopecks is not None
        or state.status != 3
        or (state.total, state.succeeded) != (1, 1)
        or page.offset != 0
        or page.limit <= 1
        or len(page.goods) != 1
    ):
        return False
    row = page.goods[0]
    expected = _json(request.provider_bytes)["data"][0]
    return (
        row.status == 2
        and not row.has_error
        and row.currency == "RUB"
        and row.size_id is None
        and row.nm_id == request.nm_id
        and row.article_id == request.article_id
        and row.price_rubles == expected["price"]
        and row.discount_pct == request.discount_pct
    )


def parse_history(raw, *, expected_upload_id, path, offset=0, limit=1000):
    """Identity-bound evidence only, never an applied decision or resend permit."""
    upload_id(expected_upload_id)
    data = _json(raw)
    if (
        type(data) is not dict
        or data.get("error", False) is not False
        or data.get("errorText", "") != ""
    ):
        raise PriceHttpError()
    data = data.get("data")
    if (
        type(data) is not dict
        or type(data.get("uploadID")) is not int
        or str(data["uploadID"]) != expected_upload_id
    ):
        raise PriceHttpError()
    if path == "/api/v2/history/tasks":
        status = _number(data.get("status"))
        total, succeeded = (
            _number(data.get("overAllGoodsNumber")),
            _number(data.get("successGoodsNumber")),
        )
        if status not in (3, 4, 5, 6) or succeeded > total:
            raise PriceHttpError()
        return HistoryState(expected_upload_id, status, total, succeeded)
    if (
        path != "/api/v2/history/goods/task"
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise PriceHttpError()
    _number(offset)
    rows = data.get("historyGoods")
    if type(rows) is not list or len(rows) > limit:
        raise PriceHttpError()
    goods, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise PriceHttpError()
        nm = _number(row.get("nmID"), 1)
        size = row.get("sizeID")
        if size is not None:
            _number(size, 1)
        if (nm, size) in seen:
            raise PriceHttpError()
        seen.add((nm, size))
        price = row.get("price")
        if price is not None:
            _number(price, 1)
        discount, status = _number(row.get("discount")), _number(row.get("status"))
        article, currency, error = (
            row.get("vendorCode"),
            row.get("currencyIsoCode4217"),
            row.get("errorText"),
        )
        if (
            discount > 99
            or status not in (2, 3)
            or type(article) is not str
            or not article.strip()
            or currency != "RUB"
            or (error is not None and type(error) is not str)
        ):
            raise PriceHttpError()
        if status == 2 and error not in (None, ""):
            raise PriceHttpError()
        goods.append(
            HistoryGood(
                nm,
                size,
                article,
                price,
                discount,
                currency,
                status,
                bool(error) or status == 3,
            )
        )
    return HistoryPage(expected_upload_id, offset, limit, tuple(goods))


class _PreparedPricePost(Redacted):
    """Process-local pacing capability, never authorization or durable proof."""

    __slots__ = (
        "__adapter",
        "__binding",
        "__body",
        "__credential",
        "__expected",
        "__locator",
        "__lock",
        "__seal",
        "__used",
    )

    def __init__(self, adapter, seal, body, credential, locator, expected):
        self.__adapter = adapter
        self.__seal, self.__body, self.__credential = seal, body, credential
        self.__binding, self.__locator, self.__expected = (
            credential.binding,
            locator,
            expected,
        )
        self.__lock, self.__used = Lock(), False

    def consume(self, adapter, seal, body, credential, locator, expected):
        with self.__lock:
            if self.__used:
                raise PriceHttpError()
            # Burn before any I/O. A failed use cannot turn into a later resend.
            self.__used = True
            if (
                adapter is not self.__adapter
                or seal is not self.__seal
                or type(body) is not bytes
                or body != self.__body
                or credential is not self.__credential
                or credential.binding != self.__binding
                or locator != self.__locator
                or expected != self.__expected
            ):
                raise PriceHttpError()


class WbPriceHttpAdapter:
    def __init__(
        self,
        *,
        organization_id,
        marketplace_account_id,
        admit_request,
        record_cooldown,
        max_response_bytes,
        transport=None,
        clock=None,
    ):
        integer(organization_id)
        integer(marketplace_account_id)
        if (
            not callable(admit_request)
            or not callable(record_cooldown)
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 4 * 1024 * 1024
        ):
            raise PriceHttpError()
        self._org, self._account = organization_id, marketplace_account_id
        self._admit, self._budget, self._transport = (
            admit_request,
            max_response_bytes,
            transport,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._record_cooldown = record_cooldown
        self._prepare_seal = object()

    def _validate_credential(self, credential):
        if type(credential) is not ResolvedCredentialForFetch or (
            credential.binding.owner.organization_id,
            credential.binding.owner.marketplace_account_id,
            credential.binding.owner.provider,
            credential.binding.credential_identity.credential_kind,
        ) != (self._org, self._account, "wb", "wb_api"):
            raise PriceHttpError()

    def _reserve_quota(self, method, path):
        # Reserve the shared category budget BEFORE I/O, even if the process dies.
        # Conservative base entitlement; no JWT decoding or in-memory limiter.
        try:
            admitted = self._admit(self._org, self._account, method, path, 900)
        except Exception:  # noqa: BLE001 -- fixed safe pre-marker error, not raw DB diagnostics.
            admitted = False
        if admitted is not True:
            raise PriceHttpError()

    def _request(self, *, method, path, credential, body=None, params=None):
        self._validate_credential(credential)
        self._reserve_quota(method, path)
        return self._send(
            method=method, path=path, credential=credential, body=body, params=params
        )

    def _send(self, *, method, path, credential, body=None, params=None):
        result = None
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(20, connect=10),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                started = time.monotonic()
                with client.stream(
                    method,
                    "https://discounts-prices-api.wildberries.ru" + path,
                    content=body,
                    params=params,
                    headers={
                        "Authorization": credential.secret.reveal()["token"],
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    header_time = timestamp(self._clock())
                    delay = max(900, _retry_after(response.headers, header_time))
                    # Persist the account category deadline on headers, before
                    # streaming/parsing can fail. POST feedback cannot depend on
                    # a worker return value (the outcome may be ambiguous).
                    if (
                        self._record_cooldown(
                            self._org,
                            self._account,
                            header_time + timedelta(seconds=delay),
                        )
                        is not True
                    ):
                        raise PriceHttpError()
                    raw = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16384):
                        if (
                            len(raw) + len(chunk) > self._budget
                            or time.monotonic() - started > 30
                        ):
                            raise PriceHttpError()
                        raw.extend(chunk)
                    if time.monotonic() - started > 30:
                        raise PriceHttpError()
                    now = timestamp(self._clock())
                    result = (
                        bytes(raw),
                        response.status_code,
                        now,
                        delay,
                    )
        except Exception:  # noqa: BLE001 -- do not retain transport request/headers or raw exception context.
            result = None
        if result is None:
            raise PriceHttpError()
        return result

    def _validate_body(self, body):
        if type(body) is not bytes or not 1 <= len(body) <= 4096:
            raise PriceHttpError()
        payload = _json(body)
        if (
            type(payload) is not dict
            or set(payload) != {"data"}
            or type(payload["data"]) is not list
            or len(payload["data"]) != 1
        ):
            raise PriceHttpError()
        row = payload["data"][0]
        # Existing kernel can encode size/minPrice, but this endpoint cannot be
        # assumed to honor them. Never strip them or silently reroute the intent.
        if type(row) is not dict or set(row) != {"nmID", "price", "discount"}:
            raise PriceHttpError()
        _number(row["nmID"], 1)
        _number(row["price"], 1)
        if _number(row["discount"]) > 99:
            raise PriceHttpError()

    def prepare_price_once(self, *, body, credential, locator, expected):
        """Validate stored reserved intent and reserve pacing BEFORE dispatch.

        Caller must still commit dispatch and pass executor.before_provider_io.
        Denial leaves the attempt reserved; crashes can waste acquired capacity.
        No per-attempt durable quota idempotency is asserted by this handle.
        """
        self._validate_body(body)
        self._validate_credential(credential)
        invalid = False
        try:
            if (
                type(locator) is not RepricerJobLocator
                or type(expected) is not RepricerExpectedState
            ):
                raise PriceHttpError()
            locator.__post_init__()
            expected.__post_init__()
            request = canonical_request(expected.approval)
            if (
                (locator.organization_id, locator.marketplace_account_id)
                != (self._org, self._account)
                or (request.scope.organization_id, request.scope.marketplace_account_id)
                != (self._org, self._account)
                or request.provider_bytes != body
                or expected.attempt_id is None
                or expected.attempt_version != 0
                or expected.dispatch_key
                != build_dispatch_key(
                    request.scope,
                    expected.approval.action_key,
                    str(expected.attempt_id),
                )
            ):
                raise PriceHttpError()
        except Exception:  # noqa: BLE001 -- no invalid contract/identity details escape.
            invalid = True
        if invalid:
            raise PriceHttpError()
        self._reserve_quota("POST", "/api/v2/upload/task")
        return _PreparedPricePost(
            self, self._prepare_seal, body, credential, locator, expected
        )

    def _consume_prepared(self, *, prepared, body, credential, locator, expected):
        if type(prepared) is not _PreparedPricePost:
            raise PriceHttpError()
        prepared.consume(self, self._prepare_seal, body, credential, locator, expected)
        raw, status, now, _ = self._send(
            method="POST", path="/api/v2/upload/task", credential=credential, body=body
        )
        return OriginalPricePostResponse(raw, status, 200 <= status < 300, now)

    def post_prepared_once(self, *, prepared, body, credential, locator, expected):
        """Consume exactly the prepared reserved state, not its dispatch version."""
        if (
            type(locator) is not RepricerJobLocator
            or type(expected) is not RepricerExpectedState
        ):
            raise PriceHttpError()
        return self._consume_prepared(
            prepared=prepared,
            body=body,
            credential=credential,
            locator=locator,
            expected=expected,
        )

    def post_price_once(self, *, body, credential):
        # Compatibility for direct callers: it still acquires quota exactly once.
        # DurableApprovalWorker uses the bound prepare/consume path instead.
        self._validate_body(body)
        self._validate_credential(credential)
        self._reserve_quota("POST", "/api/v2/upload/task")
        prepared = _PreparedPricePost(
            self, self._prepare_seal, body, credential, None, None
        )
        return self._consume_prepared(
            prepared=prepared,
            body=body,
            credential=credential,
            locator=None,
            expected=None,
        )

    def read_history_once(
        self, *, credential, expected_upload_id, details=False, offset=0, limit=1000
    ):
        upload_id(expected_upload_id)
        if (
            type(details) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise PriceHttpError()
        _number(offset)
        path = "/api/v2/history/goods/task" if details else "/api/v2/history/tasks"
        params = {"uploadID": expected_upload_id}
        if details:
            params.update(offset=offset, limit=limit)
        raw, status, now, delay = self._request(
            method="GET", path=path, credential=credential, params=params
        )
        if status != 200:
            raise PriceHttpError(next_not_before=now + timedelta(seconds=delay))
        result = parse_history(
            raw,
            expected_upload_id=expected_upload_id,
            path=path,
            offset=offset,
            limit=limit,
        )
        return HistoryObservation(
            self._org,
            self._account,
            path,
            now,
            now + timedelta(seconds=delay),
            sha256(raw).hexdigest(),
            result,
        )
