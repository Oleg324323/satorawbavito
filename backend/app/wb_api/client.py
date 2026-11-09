from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, Field

from app.config import get_settings

try:
    import httpx
except Exception:  # pragma: no cover - optional import for fake-only mode
    httpx = None  # type: ignore[assignment]

HttpMethod = Literal["GET", "POST", "PATCH", "PUT", "DELETE"]


class RateLimitInfo(BaseModel):
    limit: Optional[int] = None
    remaining: Optional[int] = None
    retryAfterSeconds: Optional[int] = None
    resetAfterSeconds: Optional[int] = None


class WbApiRequest(BaseModel):
    method: HttpMethod
    path: str = Field(min_length=1)
    query: Dict[str, Any] = Field(default_factory=dict)
    jsonBody: Optional[Dict[str, Any]] = None


class WbApiError(BaseModel):
    statusCode: int
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    operationalAlert: bool = False
    rateLimit: Optional[RateLimitInfo] = None


class WbApiResponseEnvelope(BaseModel):
    request: WbApiRequest
    statusCode: int
    ok: bool
    data: Any = None
    error: Optional[WbApiError] = None
    rateLimit: Optional[RateLimitInfo] = None
    wbRequestId: Optional[str] = None


class WbApiClient(Protocol):
    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        ...


class WbRateLimitPolicy(BaseModel):
    category: str = Field(min_length=1)
    periodSeconds: int = Field(gt=0)
    limit: int = Field(gt=0)
    intervalMs: int = Field(ge=0)
    burst: int = Field(gt=0)
    defaultRequestCost: int = Field(default=1, gt=0)
    status409Cost: int = Field(default=1, gt=0)


class WbRateLimitWait(BaseModel):
    category: str = Field(min_length=1)
    waitSeconds: float = Field(ge=0)
    reason: Literal["interval", "token_bucket", "header_retry"]


DEFAULT_WB_RATE_LIMIT_POLICIES: dict[str, WbRateLimitPolicy] = {
    # https://dev.wildberries.ru/en/openapi/work-with-products — Content reads.
    "content_read": WbRateLimitPolicy(
        category="content_read", periodSeconds=60, limit=100,
        intervalMs=600, burst=5, defaultRequestCost=1, status409Cost=1,
    ),
    "prices_discounts": WbRateLimitPolicy(
        category="prices_discounts",
        periodSeconds=6,
        limit=10,
        intervalMs=600,
        burst=5,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "marketplace": WbRateLimitPolicy(
        category="marketplace",
        periodSeconds=60,
        limit=300,
        intervalMs=200,
        burst=20,
        defaultRequestCost=1,
        status409Cost=5,
    ),
    "ads_fullstats": WbRateLimitPolicy(
        category="ads_fullstats",
        periodSeconds=60,
        limit=3,
        intervalMs=20_000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "ads_meta": WbRateLimitPolicy(
        category="ads_meta",
        periodSeconds=1,
        limit=5,
        intervalMs=200,
        burst=5,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "ads_budget": WbRateLimitPolicy(
        category="ads_budget",
        periodSeconds=1,
        limit=4,
        intervalMs=250,
        burst=4,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "ads_account_money": WbRateLimitPolicy(
        category="ads_account_money",
        periodSeconds=1,
        limit=1,
        intervalMs=1_000,
        burst=5,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "ads_normquery_stats": WbRateLimitPolicy(
        category="ads_normquery_stats",
        periodSeconds=60,
        limit=10,
        intervalMs=6_000,
        burst=20,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "ads_normquery_meta": WbRateLimitPolicy(
        category="ads_normquery_meta",
        periodSeconds=1,
        limit=5,
        intervalMs=200,
        burst=10,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "statistics_main_reports": WbRateLimitPolicy(
        category="statistics_main_reports",
        periodSeconds=60,
        limit=1,
        intervalMs=60_000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "analytics_reports": WbRateLimitPolicy(
        category="analytics_reports",
        periodSeconds=60,
        limit=3,
        intervalMs=22_000,
        burst=3,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "analytics_stock_report": WbRateLimitPolicy(
        category="analytics_stock_report",
        periodSeconds=60,
        limit=3,
        intervalMs=20_000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "analytics_reports_async": WbRateLimitPolicy(
        category="analytics_reports_async",
        periodSeconds=60,
        limit=1,
        intervalMs=60_000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "analytics_task_status": WbRateLimitPolicy(
        category="analytics_task_status",
        periodSeconds=5,
        limit=1,
        intervalMs=5_000,
        burst=5,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "finance_reports": WbRateLimitPolicy(
        category="finance_reports",
        periodSeconds=60,
        limit=1,
        intervalMs=60_000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "common_tariffs": WbRateLimitPolicy(
        category="common_tariffs",
        periodSeconds=60,
        limit=1,
        intervalMs=60_000,
        burst=2,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "feedbacks_api": WbRateLimitPolicy(
        category="feedbacks_api",
        periodSeconds=1,
        limit=3,
        intervalMs=333,
        burst=6,
        defaultRequestCost=1,
        status409Cost=1,
    ),
    "promotions_calendar": WbRateLimitPolicy(
        category="promotions_calendar",
        periodSeconds=1,
        limit=1,
        intervalMs=1000,
        burst=1,
        defaultRequestCost=1,
        status409Cost=1,
    ),
}

DEFAULT_PATH_POLICIES: list[tuple[str, str]] = [
    ("/content/v2/get/cards/list", "content_read"),
    ("/api/v1/calendar/promotions", "promotions_calendar"),
    ("/adv/v3/fullstats", "ads_fullstats"),
    ("/adv/v1/promotion/count", "ads_meta"),
    ("/adv/v1/budget", "ads_budget"),
    ("/adv/v1/balance", "ads_account_money"),
    ("/adv/v1/upd", "ads_account_money"),
    ("/adv/v1/payments", "ads_account_money"),
    ("/adv/v1/normquery/stats", "ads_normquery_stats"),
    ("/adv/v0/normquery", "ads_normquery_meta"),
    ("/api/advert/v2/adverts", "ads_meta"),
    ("/api/v1/supplier/orders", "statistics_main_reports"),
    ("/api/v1/supplier/sales", "statistics_main_reports"),
    ("/api/v1/supplier/stocks", "statistics_main_reports"),
    ("/api/v5/supplier/reportDetailByPeriod", "statistics_main_reports"),
    ("/api/finance/v1/sales-reports/list", "finance_reports"),
    ("/api/finance/v1/sales-reports/detailed", "finance_reports"),
    ("/api/v1/tariffs/commission", "common_tariffs"),
    ("/api/v1/tariffs/box", "common_tariffs"),
    ("/api/v1/tariffs/pallet", "common_tariffs"),
    ("/api/v3/offices", "marketplace"),
    ("/api/v3/stocks/", "marketplace"),
    ("/api/v1/warehouses", "marketplace"),
    ("/api/v1/feedbacks", "feedbacks_api"),
    ("/api/v1/feedback", "feedbacks_api"),
    ("/api/common/v1/rating", "feedbacks_api"),
    ("/api/v1/warehouse_remains/tasks/", "analytics_task_status"),
    ("/api/v1/warehouse_remains", "analytics_reports_async"),
    ("/api/analytics/v3/sales-funnel", "analytics_reports"),
    ("/api/v1/analytics/region-sale", "analytics_reports"),
    ("/api/analytics/v1/stocks-report/wb-warehouses", "analytics_stock_report"),
    ("/api/v2/stocks-report/products/products", "analytics_reports"),
    ("/api/v2/stocks-report/products/sizes", "analytics_reports"),
    ("/api/v2/stocks-report/offices", "analytics_reports"),
    ("/api/analytics/v1/measurement-penalties", "analytics_reports"),
    ("/api/v1/acceptance_report", "analytics_reports_async"),
    ("/api/v1/paid_storage", "analytics_reports_async"),
    ("/api/v2/", "prices_discounts"),
]


def parse_rate_limit_headers(headers: Mapping[str, str]) -> Optional[RateLimitInfo]:
    def parse_int(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    limit = parse_int(headers.get("X-Ratelimit-Limit") or headers.get("x-ratelimit-limit"))
    remaining = parse_int(headers.get("X-Ratelimit-Remaining") or headers.get("x-ratelimit-remaining"))
    retry_after = parse_int(
        headers.get("X-Ratelimit-Retry")
        or headers.get("x-ratelimit-retry")
        or headers.get("Retry-After")
        or headers.get("retry-after")
    )
    reset_after = parse_int(
        headers.get("X-Ratelimit-Reset")
        or headers.get("x-ratelimit-reset")
    )
    if limit is None and remaining is None and retry_after is None and reset_after is None:
        return None
    return RateLimitInfo(limit=limit, remaining=remaining, retryAfterSeconds=retry_after, resetAfterSeconds=reset_after)


def classify_wb_error(status_code: int, message: str, rate_limit: Optional[RateLimitInfo] = None) -> WbApiError:
    if status_code == 401:
        return WbApiError(statusCode=status_code, code="auth_required", message=message, retryable=False)
    if status_code == 402:
        return WbApiError(
            statusCode=status_code,
            code="billing_required",
            message=message,
            retryable=False,
            operationalAlert=True,
        )
    if status_code == 403:
        return WbApiError(statusCode=status_code, code="forbidden_scope", message=message, retryable=False)
    if status_code == 429:
        return WbApiError(
            statusCode=status_code,
            code="rate_limited",
            message=message,
            retryable=True,
            rateLimit=rate_limit,
        )
    if status_code >= 500:
        return WbApiError(statusCode=status_code, code="wb_server_error", message=message, retryable=True)
    return WbApiError(statusCode=status_code, code="wb_request_failed", message=message, retryable=False)


class WbTokenBucketLimiter:
    """In-memory token bucket limiter for a WB API category."""

    def __init__(
        self,
        policy: WbRateLimitPolicy,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self._now = monotonic_now or time.monotonic
        self._tokens = float(policy.burst)
        self._last_refill = self._now()
        self._last_sent: float | None = None
        self._blocked_until: float | None = None

    @property
    def refill_per_second(self) -> float:
        return self.policy.limit / self.policy.periodSeconds

    def _refill(self) -> None:
        now = self._now()
        elapsed = max(0.0, now - self._last_refill)
        if elapsed <= 0:
            return
        self._tokens = min(float(self.policy.burst), self._tokens + elapsed * self.refill_per_second)
        self._last_refill = now

    def wait_before_request(self, cost: int | None = None) -> list[WbRateLimitWait]:
        waits: list[WbRateLimitWait] = []
        actual_cost = float(cost or self.policy.defaultRequestCost)
        now = self._now()
        self._refill()

        if self._last_sent is not None and self.policy.intervalMs > 0:
            min_gap = self.policy.intervalMs / 1000.0
            interval_wait = min_gap - (now - self._last_sent)
            if interval_wait > 0:
                waits.append(
                    WbRateLimitWait(
                        category=self.policy.category,
                        waitSeconds=interval_wait,
                        reason="interval",
                    )
                )

        if self._blocked_until is not None:
            blocked_wait = self._blocked_until - now
            if blocked_wait > 0:
                waits.append(
                    WbRateLimitWait(
                        category=self.policy.category,
                        waitSeconds=blocked_wait,
                        reason="header_retry",
                    )
                )
            else:
                self._blocked_until = None

        token_wait = 0.0
        if self._tokens < actual_cost:
            missing = actual_cost - self._tokens
            token_wait = missing / self.refill_per_second
            waits.append(
                WbRateLimitWait(
                    category=self.policy.category,
                    waitSeconds=token_wait,
                    reason="token_bucket",
                )
            )

        total_wait = max((wait.waitSeconds for wait in waits), default=0.0)
        if total_wait > 0:
            now_after_wait = now + total_wait
            elapsed = max(0.0, now_after_wait - self._last_refill)
            self._tokens = min(float(self.policy.burst), self._tokens + elapsed * self.refill_per_second)
            self._last_refill = now_after_wait
            if self._blocked_until is not None and self._blocked_until <= now_after_wait:
                self._blocked_until = None

        self._tokens = max(0.0, self._tokens - actual_cost)
        self._last_sent = self._last_refill
        return waits

    def observe_response(self, status_code: int, rate_limit: Optional[RateLimitInfo]) -> list[WbRateLimitWait]:
        waits: list[WbRateLimitWait] = []
        if status_code == 409 and self.policy.status409Cost > self.policy.defaultRequestCost:
            penalty = float(self.policy.status409Cost - self.policy.defaultRequestCost)
            self._tokens = max(0.0, self._tokens - penalty)

        if rate_limit is not None:
            if rate_limit.remaining is not None:
                self._tokens = min(self._tokens, float(max(0, rate_limit.remaining)))
            if rate_limit.retryAfterSeconds is not None and rate_limit.retryAfterSeconds > 0:
                waits.append(
                    WbRateLimitWait(
                        category=self.policy.category,
                        waitSeconds=float(rate_limit.retryAfterSeconds),
                        reason="header_retry",
                    )
                )
                self._blocked_until = max(
                    self._blocked_until or 0.0,
                    self._now() + float(rate_limit.retryAfterSeconds),
                )
        return waits


def resolve_rate_limit_policy(path: str) -> str:
    if path.startswith("/api/v1/warehouse_remains/tasks/"):
        if path.endswith("/status"):
            return "analytics_task_status"
        if path.endswith("/download"):
            return "analytics_reports_async"
    if path.startswith("/api/v1/acceptance_report/tasks/") or path.startswith("/api/v1/paid_storage/tasks/"):
        if path.endswith("/status"):
            return "analytics_task_status"
        if path.endswith("/download"):
            return "analytics_reports_async"
    for prefix, category in DEFAULT_PATH_POLICIES:
        if path.startswith(prefix):
            return category
    return "marketplace"


class RateLimitedWbApiClient:
    """WB API client wrapper that preserves rate-limit metadata without blocking UI requests."""

    _shared_lock = threading.RLock()
    _shared_limiters: dict[str, WbTokenBucketLimiter] | None = None

    def __init__(
        self,
        inner: WbApiClient,
        policies: Optional[Mapping[str, WbRateLimitPolicy]] = None,
        monotonic_now: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.inner = inner
        is_real_http_client = inner.__class__.__name__ == "RealWbApiClient"
        self._sleep = sleep_fn or (time.sleep if is_real_http_client else (lambda _seconds: None))
        policy_map = dict(DEFAULT_WB_RATE_LIMIT_POLICIES)
        if policies is not None:
            policy_map.update(policies)
        self._lock = threading.RLock()
        use_shared_limiters = policies is None and monotonic_now is None and sleep_fn is None and is_real_http_client
        if use_shared_limiters:
            with self._shared_lock:
                if RateLimitedWbApiClient._shared_limiters is None:
                    RateLimitedWbApiClient._shared_limiters = {
                        key: WbTokenBucketLimiter(policy)
                        for key, policy in policy_map.items()
                    }
                self._limiters = RateLimitedWbApiClient._shared_limiters
                self._lock = RateLimitedWbApiClient._shared_lock
        else:
            self._limiters = {
                key: WbTokenBucketLimiter(policy, monotonic_now=monotonic_now)
                for key, policy in policy_map.items()
            }

    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        from app.wb_api.report_reads import active_report_read
        context = active_report_read.get()
        if context is not None and isinstance(self.inner, RealWbApiClient):
            return context.request(self.inner, request, lambda heartbeat: self._request(request, heartbeat, context))
        return self._request(request)

    def _request(self, request: WbApiRequest, heartbeat=None, report_session=None) -> WbApiResponseEnvelope:
        category = resolve_rate_limit_policy(request.path)
        limiter = self._limiters.get(category)
        budget = None
        if report_session is not None and limiter is not None:
            from app.wb_api.report_rate_limit import ReportReadBudget
            budget = ReportReadBudget(report_session.organization_id, report_session.credential_digest,
                                      self.inner.base_url, limiter.policy)
            limited = budget.acquire(heartbeat)
            if limited is not None:
                return WbApiResponseEnvelope(request=request, ok=False, statusCode=429, rateLimit=limited,
                    error=WbApiError(statusCode=429, code='WB_SHARED_RATE_LIMIT',
                        message='WB ограничил частоту запросов. Повторите загрузку после паузы.',
                        retryable=True, rateLimit=limited))
            limiter = None  # Do not apply a second process-local wait after the shared slot.

        if limiter is not None:
            with self._lock:
                waits = limiter.wait_before_request()
            wait_seconds = max((wait.waitSeconds for wait in waits), default=0.0)
            if wait_seconds > 0:
                if heartbeat is None:
                    self._sleep(wait_seconds)
                else:
                    while wait_seconds > 0:
                        heartbeat()
                        step = min(1.0, wait_seconds)
                        self._sleep(step)
                        wait_seconds -= step

        if heartbeat is not None:
            heartbeat()
        response = self.inner.request(request)

        if budget is not None:
            budget.observe(response)

        if limiter is not None:
            with self._lock:
                limiter.observe_response(response.statusCode, response.rateLimit)

        return response


class FakeWbApiClient:
    """In-memory WB client for adapter probes. It never calls external WB APIs."""

    def __init__(
        self,
        fixtures: Optional[Mapping[str, Any]] = None,
        errors: Optional[Mapping[str, int]] = None,
        headers: Optional[Mapping[str, Mapping[str, str]]] = None,
    ) -> None:
        self.fixtures = dict(fixtures or {})
        self.errors = dict(errors or {})
        self.headers = {path: dict(value) for path, value in (headers or {}).items()}
        self.requests: List[WbApiRequest] = []

    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        self.requests.append(request)
        headers = self.headers.get(request.path, {})
        rate_limit = parse_rate_limit_headers(headers)
        wb_request_id = headers.get("X-Request-Id") or headers.get("x-request-id")

        if request.path in self.errors:
            status_code = self.errors[request.path]
            return WbApiResponseEnvelope(
                request=request,
                statusCode=status_code,
                ok=False,
                error=classify_wb_error(status_code, f"Fake WB error {status_code}", rate_limit),
                rateLimit=rate_limit,
                wbRequestId=wb_request_id,
            )

        data = self.fixtures.get(request.path)
        if data is None and request.path.startswith("/api/finance/v1/sales-reports/detailed/"):
            data = self.fixtures.get("/api/finance/v1/sales-reports/detailed")
        if data is None:
            status_code = 404
            return WbApiResponseEnvelope(
                request=request,
                statusCode=status_code,
                ok=False,
                error=classify_wb_error(status_code, "Fake fixture is missing", rate_limit),
                rateLimit=rate_limit,
                wbRequestId=wb_request_id,
            )

        status_code = 200
        if (
            request.path.startswith("/api/finance/v1/sales-reports/detailed")
            and isinstance(data, dict)
            and isinstance(data.get("data"), list)
        ):
            body = request.jsonBody or {}
            cursor = int(body.get("rrdId", 0))
            rows = data["data"]
            if request.path.startswith("/api/finance/v1/sales-reports/detailed/"):
                report_id = int(request.path.rsplit("/", 1)[1])
                rows = [item for item in rows if int(item.get("reportId") or 0) == report_id]
            if cursor:
                rows = [item for item in rows if int(item.get("rrdId") or item.get("rrd_id") or 0) > cursor]
            rows = rows[:int(body.get("limit", 100_000))]
            data = {**data, "data": rows}
            status_code = 200 if rows else 204

        if request.path == "/api/finance/v1/sales-reports/list" and isinstance(data, dict) and isinstance(data.get("data"), list):
            body = request.jsonBody or {}
            offset = int(body.get("offset", 0))
            data = {**data, "data": data["data"][offset:offset + int(body.get("limit", 1000))]}

        return WbApiResponseEnvelope(
            request=request,
            statusCode=status_code,
            ok=True,
            data=data,
            rateLimit=rate_limit,
            wbRequestId=wb_request_id,
        )


def build_fake_client(scenario: str) -> FakeWbApiClient:
    catalog_goods_complete = [
        {
            "nmID": 123456,
            "vendorCode": "FBBT_42",
            "brand": "Satorna",
            "subjectName": "Футболки",
            "sizes": [
                {
                    "sizeID": 654321,
                    "price": 139000,
                    "discountedPrice": 122320,
                    "clubDiscountedPrice": 117427.2,
                    "techSizeName": "L",
                }
            ],
            "discount": 12,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
        {
            "nmID": 123457,
            "vendorCode": "HCBT_19",
            "brand": "Satorna",
            "subjectName": "Худи",
            "sizes": [
                {
                    "sizeID": 654322,
                    "price": 299000,
                    "discountedPrice": 285000,
                    "clubDiscountedPrice": 273600,
                    "techSizeName": "M",
                }
            ],
            "discount": 5,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
        {
            "nmID": 123458,
            "vendorCode": "FBBT_55",
            "brand": "Satorna",
            "subjectName": "Футболки",
            "sizes": [
                {
                    "sizeID": 654323,
                    "price": 129000,
                    "discountedPrice": 125000,
                    "clubDiscountedPrice": 120000,
                    "techSizeName": "XL",
                }
            ],
            "discount": 3,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
        {
            "nmID": 123459,
            "vendorCode": "LBBT_03",
            "brand": "Satorna",
            "subjectName": "Лонгсливы",
            "sizes": [
                {
                    "sizeID": 654324,
                    "price": 170000,
                    "discountedPrice": 164000,
                    "clubDiscountedPrice": 157440,
                    "techSizeName": "S",
                }
            ],
            "discount": 4,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
        {
            "nmID": 123460,
            "vendorCode": "HBBT_22",
            "brand": "Satorna",
            "subjectName": "Худи",
            "sizes": [
                {
                    "sizeID": 654325,
                    "price": 249000,
                    "discountedPrice": 239000,
                    "clubDiscountedPrice": 229440,
                    "techSizeName": "L",
                }
            ],
            "discount": 4,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
        {
            "nmID": 123461,
            "vendorCode": "FCBT_18",
            "brand": "Satorna",
            "subjectName": "Футболки",
            "sizes": [
                {
                    "sizeID": 654326,
                    "price": 136000,
                    "discountedPrice": 130000,
                    "clubDiscountedPrice": 124800,
                    "techSizeName": "M",
                }
            ],
            "discount": 4,
            "clubDiscount": 4,
            "editableSizePrice": True,
        },
    ]
    catalog_goods_missing_spp = [
        {
            **catalog_goods_complete[0],
            "clubDiscount": None,
            "sizes": [
                {
                    "sizeID": 654321,
                    "price": 139000,
                    "discountedPrice": 122320,
                    "techSizeName": "L",
                }
            ],
        },
        *catalog_goods_complete[1:],
    ]
    content_cards_catalog = {
        "cards": [
            {
                "nmID": 123456,
                "vendorCode": "FBBT_42",
                "title": "Футболка белая «Принт 42»",
                "object": "Футболки",
                "objectID": 192,
                "brand": "Satorna",
                "sizes": [{"skus": [7654321]}],
            },
            {
                "nmID": 123457,
                "vendorCode": "HCBT_19",
                "title": "Худи чёрное «Принт 19»",
                "object": "Худи",
                "objectID": 2776,
                "brand": "Satorna",
                "sizes": [{"skus": [7654322]}],
            },
            {
                "nmID": 123458,
                "vendorCode": "FBBT_55",
                "title": "Футболка белая «Принт 55»",
                "object": "Футболки",
                "objectID": 192,
                "brand": "Satorna",
                "sizes": [{"skus": [7654323]}],
            },
            {
                "nmID": 123459,
                "vendorCode": "LBBT_03",
                "title": "Лонгслив белый «Принт 3»",
                "object": "Лонгсливы",
                "objectID": 291,
                "brand": "Satorna",
                "sizes": [{"skus": [7654324]}],
            },
            {
                "nmID": 123460,
                "vendorCode": "HBBT_22",
                "title": "Худи белое «Принт 22»",
                "object": "Худи",
                "objectID": 2776,
                "brand": "Satorna",
                "sizes": [{"skus": [7654325]}],
            },
            {
                "nmID": 123461,
                "vendorCode": "FCBT_18",
                "title": "Футболка чёрная «Принт 18»",
                "object": "Футболки",
                "brand": "Satorna",
                "sizes": [{"skus": [7654326]}],
            },
        ],
        "cursor": {"updatedAt": "2026-06-01T00:00:00Z", "nmID": 123461, "total": 6},
    }
    calendar_promotions = {
        "promotions": [
            {
                "id": 1001,
                "name": "Весенняя распродажа",
                "startDateTime": "2026-05-29T00:00:00+03:00",
                "endDateTime": "2026-06-06T23:59:59+03:00",
                "type": "special",
                "allPromo": False,
            },
            {
                "id": 1002,
                "name": "Недельная скидка апрель",
                "startDateTime": "2026-06-01T00:00:00+03:00",
                "endDateTime": "2026-06-10T23:59:59+03:00",
                "type": "auto",
                "allPromo": False,
            },
            {
                "id": 1003,
                "name": "Флеш-акция выходного дня",
                "startDateTime": "2026-06-02T00:00:00+03:00",
                "endDateTime": "2026-06-05T23:59:59+03:00",
                "type": "flash",
                "allPromo": False,
            },
            {
                "id": 1004,
                "name": "Майские праздники",
                "startDateTime": "2026-06-08T00:00:00+03:00",
                "endDateTime": "2026-06-15T23:59:59+03:00",
                "type": "auto",
                "allPromo": False,
            },
        ]
    }
    calendar_promotion_details = {
        "promotions": [
            {
                "id": 1001,
                "description": "WB seasonal promo",
                "advantages": ["traffic boost"],
                "inPromoActionLeftovers": 23,
                "inPromoActionTotal": 23,
                "notInPromoActionLeftovers": 24,
                "notInPromoActionTotal": 24,
                "participationPercentage": 49,
                "type": "special",
                "exceptionProductsCount": 2,
                "ranging": [{"condition": "promo discount", "participationRate": 30, "boost": 1.2}],
            },
            {
                "id": 1002,
                "description": "Weekly promo",
                "advantages": ["increased conversion"],
                "inPromoActionLeftovers": 0,
                "inPromoActionTotal": 0,
                "notInPromoActionLeftovers": 12,
                "notInPromoActionTotal": 12,
                "participationPercentage": 0,
                "type": "auto",
                "exceptionProductsCount": 0,
                "ranging": [{"condition": "threshold from excel", "participationRate": 25, "boost": 1.0}],
            },
            {
                "id": 1003,
                "description": "Flash promo",
                "advantages": ["short-term boost"],
                "inPromoActionLeftovers": 0,
                "inPromoActionTotal": 0,
                "notInPromoActionLeftovers": 8,
                "notInPromoActionTotal": 8,
                "participationPercentage": 0,
                "type": "flash",
                "exceptionProductsCount": 1,
                "ranging": [{"condition": "flash threshold", "participationRate": 28, "boost": 1.4}],
            },
            {
                "id": 1004,
                "description": "Holiday promo",
                "advantages": ["high seasonal demand"],
                "inPromoActionLeftovers": 0,
                "inPromoActionTotal": 0,
                "notInPromoActionLeftovers": 18,
                "notInPromoActionTotal": 18,
                "participationPercentage": 0,
                "type": "auto",
                "exceptionProductsCount": 3,
                "ranging": [{"condition": "holiday threshold", "participationRate": 27, "boost": 1.1}],
            },
        ]
    }
    calendar_promotion_nomenclatures = {
        "nomenclatures": [
            {"promotionID": 1001, "id": 123456, "vendorCode": "FBBT_42", "inAction": True, "price": 122320, "currencyCode": "RUB", "planPrice": 114800, "discount": 12, "planDiscount": 18},
            {"promotionID": 1001, "id": 123457, "vendorCode": "HCBT_19", "inAction": True, "price": 285000, "currencyCode": "RUB", "planPrice": 268000, "discount": 5, "planDiscount": 10},
            {"promotionID": 1001, "id": 123459, "vendorCode": "LBBT_03", "inAction": True, "price": 164000, "currencyCode": "RUB", "planPrice": 139400, "discount": 4, "planDiscount": 15},
            {"promotionID": 1002, "id": 123461, "vendorCode": "FCBT_18", "inAction": False, "price": 130000, "currencyCode": "RUB", "planPrice": None, "discount": 4, "planDiscount": None},
            {"promotionID": 1002, "id": 123458, "vendorCode": "FBBT_55", "inAction": False, "price": 125000, "currencyCode": "RUB", "planPrice": None, "discount": 3, "planDiscount": None},
            {"promotionID": 1003, "id": 123458, "vendorCode": "FBBT_55", "inAction": False, "price": 125000, "currencyCode": "RUB", "planPrice": None, "discount": 3, "planDiscount": None},
            {"promotionID": 1004, "id": 123460, "vendorCode": "HBBT_22", "inAction": False, "price": 239000, "currencyCode": "RUB", "planPrice": 203200, "discount": 4, "planDiscount": 15},
        ]
    }
    advert_min_bids = {
        "adverts": [
            {"advert_id": 22161678, "nm_id": 123456, "bidCpmKopecks": 3500, "payment_type": "cpm", "placement_type": "search"},
            {"advert_id": 22161678, "nm_id": 123457, "bidCpmKopecks": 4200, "payment_type": "cpm", "placement_type": "search"},
        ]
    }
    price_complete = {
        "data": {
            "listGoods": catalog_goods_complete
        },
        "error": False,
        "errorText": "",
    }
    price_missing_spp = {
        "data": {
            "listGoods": catalog_goods_missing_spp
        },
        "error": False,
        "errorText": "",
    }
    buffer_state_complete = {
        "data": {
            "uploadID": 146567,
            "status": 1,
            "uploadDate": "2026-05-27T08:00:00+03:00",
            "activationDate": "2026-05-27T08:05:00+03:00",
            "overAllGoodsNumber": 100,
            "successGoodsNumber": 0,
        },
        "error": False,
        "errorText": "",
    }
    buffer_goods_complete = {
        "data": {
            "uploadID": 146567,
            "bufferGoods": [
                {
                    "nmID": 123456,
                    "sizeID": 654321,
                    "price": 139000,
                    "discount": 12,
                    "errors": [],
                }
            ],
        },
        "error": False,
        "errorText": "",
    }
    history_state_complete = {
        "data": {
            "uploadID": 146567,
            "status": 3,
            "uploadDate": "2026-05-27T08:00:00+03:00",
            "activationDate": "2026-05-27T08:07:00+03:00",
            "overAllGoodsNumber": 100,
            "successGoodsNumber": 100,
        },
        "error": False,
        "errorText": "",
    }
    history_goods_complete = {
        "data": {
            "uploadID": 146567,
            "historyGoods": [
                {
                    "nmID": 123456,
                    "sizeID": 654321,
                    "price": 139000,
                    "discount": 12,
                    "errors": [],
                }
            ],
        },
        "error": False,
        "errorText": "",
    }
    upload_details_missing_errors = {
        "data": {
            "uploadID": 146567,
            "historyGoods": [
                {
                    "nmID": 123456,
                    "sizeID": 654321,
                    "price": 139000,
                    "discount": 12,
                }
            ],
        },
        "error": False,
        "errorText": "",
    }
    quarantine = {
        "data": {"quarantineGoods": [{"nmID": 123456, "sizeID": 1, "reason": None, "price": 139000, "discount": 12}]},
        "error": False,
        "errorText": "",
    }
    upload_task_accepted = {
        "data": {"id": 146567},
        "error": False,
        "errorText": "",
    }
    statistics_orders = [
        {
            "nmId": 123456,
            "warehouseName": "Подольск",
            "regionName": "Московская",
            "finishedPrice": 1145,
            "priceWithDisc": 1547,
            "spp": 26,
            "srid": "srid-order-1",
            "lastChangeDate": "2026-05-28T08:30:00+03:00",
            "date": "2026-05-28T08:25:00+03:00",
        },
        {
            "nmId": 123457,
            "warehouseName": "Коледино",
            "regionName": "Центральный",
            "finishedPrice": 980,
            "priceWithDisc": 1399,
            "spp": 24,
            "srid": "srid-order-2",
            "lastChangeDate": "2026-05-28T08:40:00+03:00",
            "date": "2026-05-28T08:39:00+03:00",
        },
    ]
    statistics_sales = [
        {
            "nmId": 123456,
            "finishedPrice": 1145,
            "priceWithDisc": 1547,
            "forPay": 980,
            "saleID": "S123456",
            "srid": "srid-order-1",
            "lastChangeDate": "2026-05-28T10:00:00+03:00",
            "date": "2026-05-28T09:55:00+03:00",
            "isReturn": False,
        },
        {
            "nmId": 123457,
            "finishedPrice": 980,
            "priceWithDisc": 1399,
            "forPay": 820,
            "saleID": "S123457",
            "srid": "srid-order-2",
            "lastChangeDate": "2026-05-28T10:10:00+03:00",
            "date": "2026-05-28T10:00:00+03:00",
            "isReturn": False,
        },
        {
            "nmId": 123457,
            "finishedPrice": 980,
            "priceWithDisc": 1399,
            "forPay": -820,
            "saleID": "R123457",
            "srid": "srid-order-2-return",
            "lastChangeDate": "2026-05-29T10:10:00+03:00",
            "date": "2026-05-29T10:00:00+03:00",
            "isReturn": True,
        },
    ]
    realization_details = [
        {
            "rrd_id": 123456789,
            "nm_id": 123456,
            "srid": "srid-order-1",
            "retail_amount": 1145,
            "sale_percent": 24,
            "ppvz_for_pay": 980,
            "dlv_prc": 38.5,
            "delivery_rub": 38.5,
        },
        {
            "rrd_id": 123456790,
            "nm_id": 123457,
            "srid": "srid-order-2",
            "retail_amount": 980,
            "sale_percent": 22,
            "ppvz_for_pay": 820,
            "dlv_prc": 27.0,
            "delivery_rub": 27.0,
        },
        {
            "rrd_id": 123456791,
            "nm_id": 123457,
            "srid": "srid-order-2-return",
            "retail_amount": -980,
            "sale_percent": 22,
            "ppvz_for_pay": -820,
            "dlv_prc": -27.0,
            "delivery_rub": 27.0,
        },
    ]
    statistics_stocks = [
        {
            "warehouseName": "Краснодар",
            "nmId": 123456,
            "quantity": 33,
            "inWayToClient": 1,
            "inWayFromClient": 0,
            "quantityFull": 34,
        },
        {
            "warehouseName": "Коледино",
            "nmId": 123457,
            "quantity": 8,
            "inWayToClient": 2,
            "inWayFromClient": 4,
            "quantityFull": 10,
        },
    ]
    analytics_sales_funnel_products = {
        "data": {
            "products": [
                {
                    "product": {"nmId": 123456, "title": "Demo SKU 1", "vendorCode": "FBBT_01"},
                    "statistic": {
                        "selected": {
                            "cartCount": 42,
                            "orderCount": 18,
                            "openCount": 210,
                            "buyoutCount": 14,
                            "conversions": {"buyoutPercent": 77.8},
                        }
                    },
                },
                {
                    "product": {"nmId": 123457, "title": "Demo SKU 2", "vendorCode": "FBBT_02"},
                    "statistic": {
                        "selected": {
                            "cartCount": 9,
                            "orderCount": 4,
                            "openCount": 55,
                            "buyoutCount": 3,
                            "conversions": {"buyoutPercent": 75.0},
                        }
                    },
                },
            ],
            "currency": "RUB",
        }
    }
    analytics_stocks_warehouses = {
        "data": {
            "items": [
                {
                    "nmId": 123456,
                    "chrtId": 7654321,
                    "warehouseId": 507,
                    "warehouseName": "Коледино",
                    "regionName": "Центральный",
                    "quantity": 43,
                    "inWayToClient": 14,
                    "inWayFromClient": 11,
                },
                {
                    "nmId": 123457,
                    "chrtId": 7654322,
                    "warehouseId": 1201,
                    "warehouseName": "Подольск",
                    "regionName": "Московская",
                    "quantity": 0,
                    "inWayToClient": 1,
                    "inWayFromClient": 2,
                },
            ],
            "total": 2,
            "offset": 0,
            "limit": 1000,
        }
    }
    analytics_stocks_offices = {
        "data": [
            {
                "nmId": 123456,
                "chrtId": 7654321,
                "warehouseId": 507,
                "warehouseName": "Коледино",
                "regionName": "Центральный",
                "quantity": 43,
                "inWayToClient": 14,
                "inWayFromClient": 11,
            }
        ]
    }
    warehouse_remains_task = {"data": {"taskId": "warehouse-remains-task-1"}}
    warehouse_remains_task_status = {"data": {"status": "done"}}
    warehouse_remains_task_download = [
        {
            "nmId": 123456,
            "warehouses": [
                {"warehouseName": "Коледино", "quantity": 43},
                {"warehouseName": "В пути до получателей", "quantity": 14},
                {"warehouseName": "В пути возвраты на склад WB", "quantity": 11},
            ],
        },
        {
            "nmId": 123457,
            "warehouses": [
                {"warehouseName": "Подольск", "quantity": 0},
                {"warehouseName": "В пути до получателей", "quantity": 1},
                {"warehouseName": "В пути возвраты на склад WB", "quantity": 2},
            ],
        },
    ]
    penalties = {
        "data": [
            {
                "nmId": 123456,
                "penaltyAmount": 125.5,
                "retentionAmount": 40.0,
                "date": "2026-05-28",
            }
        ]
    }
    finance_sales_reports_list = {
        "data": [
            {
                "reportId": 307401554,
                "currency": "RUB",
                "retailAmountSum": "2125",
                "forPaySum": "980",
                "deliveryServiceSum": "38.5",
                "paidStorageSum": "7.65",
                "paidAcceptanceSum": "873.04",
                "penaltySum": "165.5",
            }
        ]
    }
    finance_sales_reports_detailed = {
        "data": [
            {
                "rrdId": 1,
                "reportId": 307401554,
                "nmId": 123456,
                "docTypeName": "Продажа",
                "sellerOperName": "Продажа",
                "currency": "RUB",
                "ppvzSalesCommission": "165",
                "acquiringFee": "0",
                "quantity": 1,
                "retailAmount": "1145",
                "retailPrice": "1290",
                "retailPriceWithDisc": "1145",
                "forPay": "980",
                "deliveryService": "38.5",
                "paidStorage": "7.65",
                "paidAcceptance": "873.04",
                "penalty": "165.5",
                "saleDt": "2026-05-28T00:00:00Z",
                "rrDate": "2026-05-28",
                "srid": "fake-srid-123456",
            }
        ]
    }
    acceptance_task = {"data": {"taskId": "acceptance-task-1"}}
    acceptance_task_status = {"data": {"id": "acceptance-task-1", "status": "done"}}
    acceptance_task_download = [
        {
            "count": 40,
            "giCreateDate": "2026-05-28",
            "incomeId": 11834106,
            "nmID": 123456,
            "shkCreateDate": "2026-05-28",
            "subjectName": "Футболки",
            "total": 873.04,
        }
    ]
    paid_storage_task = {"data": {"taskId": "paid-storage-task-1"}}
    paid_storage_task_status = {"data": {"id": "paid-storage-task-1", "status": "done"}}
    paid_storage_task_download = [
        {
            "date": "2026-05-28",
            "officeId": 507,
            "warehouse": "Коледино",
            "warehousePrice": 7.65,
            "nmId": 123456,
            "chrtId": 7654321,
        }
    ]
    marketplace_offices = {
        "data": [
            {"id": 507, "name": "Коледино", "region": "Центральный"},
            {"id": 1201, "name": "Подольск", "region": "Московская"},
        ]
    }
    fbw_warehouses = {
        "data": [
            {"warehouseID": 507, "name": "Коледино", "city": "Коледино"},
            {"warehouseID": 1201, "name": "Подольск", "city": "Подольск"},
        ]
    }
    seller_stocks_507 = {"stocks": [{"sku": "fake-barcode-123456", "amount": 5, "chrtId": 7654321, "warehouseId": 507}]}
    seller_stocks_1201 = {"stocks": [{"sku": "fake-barcode-123457", "amount": 2, "chrtId": 7654322, "warehouseId": 1201}]}
    region_sale = {
        "data": [
            {"nmID": 123456, "regionName": "Центральный", "ordersCount": 4},
            {"nmID": 123457, "regionName": "Московская", "ordersCount": 2},
        ]
    }
    stock_product_metrics = {
        "data": {
            "items": [
                {"nmID": 123456, "ordersCount": 4, "avgOrdersPerDay": 0.57},
                {"nmID": 123457, "ordersCount": 2, "avgOrdersPerDay": 0.29},
            ]
        }
    }
    stock_size_office_metrics = {
        "data": {
            "items": [
                {"nmID": 123456, "chrtID": 7654321, "office": "Коледино", "ordersCount": 4},
                {"nmID": 123457, "chrtID": 7654322, "office": "Подольск", "ordersCount": 2},
            ]
        }
    }
    ads_promotion_count = {
        "adverts": [
            {
                "type": 8,
                "status": 9,
                "advert_list": [
                    {"advertId": 22161678, "changeTime": "2026-05-28T10:00:00+03:00"},
                    {"advertId": 28449281, "changeTime": "2026-05-28T10:00:00+03:00"},
                ],
            }
        ],
        "all": 2,
    }
    ads_adverts = [
        {
            "advertId": 22161678,
            "id": 22161678,
            "name": "Search campaign A",
            "status": 9,
            "type": 8,
            "bid_type": "manual",
            "settings": {
                "name": "Search campaign A",
                "payment_type": "cpm",
                "placements": ["search"],
            },
            "timestamps": {
                "created": "2026-05-20T08:00:00+03:00",
                "started": "2026-05-21T08:00:00+03:00",
                "updated": "2026-05-28T10:00:00+03:00",
            },
            "nmCPM": [{"nm": 123456}, {"nm": 123457}],
            "nm_settings": [
                {"nm_id": 123456, "subject": "Футболки", "bids_kopecks": 2200},
                {"nm_id": 123457, "subject": "Худи", "bids_kopecks": 2400},
            ],
        },
        {
            "advertId": 28449281,
            "id": 28449281,
            "name": "Media campaign B",
            "status": 9,
            "type": 9,
            "settings": {
                "name": "Media campaign B",
                "payment_type": "cpm",
                "placements": ["catalog"],
            },
            "timestamps": {
                "created": "2026-05-22T08:00:00+03:00",
                "started": "2026-05-23T08:00:00+03:00",
                "updated": "2026-05-28T10:00:00+03:00",
            },
            "nmCPM": [{"nm": 123458}],
            "nm_settings": [
                {"nm_id": 123458, "subject": "Футболки", "bids_kopecks": 2100},
            ],
        },
    ]
    ads_budget = {
        "cash": 3200.0,
        "netting": 700.0,
        "total": 3900.0,
    }
    ads_balance = {
        "balance": 12500.0,
        "net": 8400.0,
        "bonus": 2100.0,
        "cashbacks": [
            {"sum": 1000.0, "percent": 10, "expiration_date": "2026-06-30"},
        ],
    }
    ads_upd = [
        {
            "updNum": "UPD-22161678-1",
            "updTime": "2026-05-28T12:00:00+03:00",
            "updSum": 1800.0,
            "advertId": 22161678,
            "campName": "Search campaign A",
            "advertType": 8,
            "paymentType": "cpm",
            "advertStatus": 9,
        },
        {
            "updNum": "UPD-28449281-1",
            "updTime": "2026-05-28T12:05:00+03:00",
            "updSum": 150.0,
            "advertId": 28449281,
            "campName": "Media campaign B",
            "advertType": 9,
            "paymentType": "cpm",
            "advertStatus": 9,
        },
    ]
    ads_normquery_stats = {
        "items": [
            {
                "advertId": 22161678,
                "nmId": 123456,
                "dailyStats": [
                    {
                        "date": "2026-05-28",
                        "stats": [
                            {
                                "normQuery": "футболка белая принт",
                                "views": 4200,
                                "clicks": 188,
                                "ctr": 4.48,
                                "cpc": 6.38,
                                "cpm": 285.71,
                                "spend": 1200.0,
                                "atbs": 40,
                                "orders": 12,
                                "shks": 12,
                                "avgPos": 7.2,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    ads_fullstats = [
        {
            "advertId": 22161678,
            "views": 142000,
            "clicks": 5240,
            "atbs": 780,
            "orders": 188,
            "sum": 1800.0,
            "sum_price": 10800.0,
            "days": [
                {
                    "date": "2026-05-28",
                    "views": 142000,
                    "clicks": 5240,
                    "atbs": 780,
                    "orders": 188,
                    "sum": 1800.0,
                    "sum_price": 10800.0,
                    "apps": [
                        {
                            "appType": 1,
                            "nm": [
                                {"nmId": 123456, "views": 98000, "clicks": 4200, "atbs": 600, "orders": 141, "sum": 1200.0, "sum_price": 7400.0},
                                {"nmId": 123457, "views": 44000, "clicks": 1040, "atbs": 180, "orders": 47, "sum": 600.0, "sum_price": 3400.0},
                            ],
                        }
                    ],
                }
            ],
            "apps": [
                {
                    "nm": [
                        {
                            "nmId": 123456,
                            "views": 98000,
                            "clicks": 4200,
                            "atbs": 600,
                            "orders": 141,
                            "sum": 1200.0,
                            "sum_price": 7400.0,
                        },
                        {
                            "nmId": 123457,
                            "views": 44000,
                            "clicks": 1040,
                            "atbs": 180,
                            "orders": 47,
                            "sum": 600.0,
                            "sum_price": 3400.0,
                        },
                    ]
                }
            ],
        },
        {
            "advertId": 28449281,
            "views": 21000,
            "clicks": 410,
            "atbs": 42,
            "orders": 8,
            "sum": 150.0,
            "sum_price": 480.0,
            "days": [
                {
                    "date": "2026-05-28",
                    "views": 21000,
                    "clicks": 410,
                    "atbs": 42,
                    "orders": 8,
                    "sum": 150.0,
                    "sum_price": 480.0,
                    "apps": [],
                }
            ],
            "apps": [],
        },
    ]
    feedbacks_list = {
        "data": {
            "feedbacks": [
                {
                    "id": "YX52RZEBhH9mrcYdEJuD",
                    "text": "Спасибо, всё подошло",
                    "pros": "Удобный",
                    "cons": "Нет",
                    "createdDate": "2025-01-27T11:38:21Z",
                    "productValuation": 5,
                    "productDetails": {
                        "imtId": 202306781,
                        "nmId": 224747484,
                        "productName": "Карандаш",
                        "supplierArticle": "12113156uw",
                        "brandName": "Maped",
                    },
                    "answer": {
                        "text": "Спасибо за отзыв",
                        "editable": True,
                    },
                },
                {
                    "id": "low-rating-review-001",
                    "text": "Размер не подошел, ожидал лучше",
                    "pros": "Ткань нормальная",
                    "cons": "Плохая посадка",
                    "createdDate": "2025-01-29T08:20:00Z",
                    "productValuation": 2,
                    "productDetails": {
                        "imtId": 202306999,
                        "nmId": 446312866,
                        "productName": "Футболка",
                        "supplierArticle": "FBBT_42",
                        "brandName": "Satorna",
                    },
                },
                {
                    "id": "safe-review-001",
                    "text": "Отличная футболка, принт яркий, размер подошел",
                    "pros": "Мягкая ткань",
                    "cons": "",
                    "createdDate": "2025-01-30T09:15:00Z",
                    "productValuation": 5,
                    "productDetails": {
                        "imtId": 202307111,
                        "nmId": 446312900,
                        "productName": "Футболка с принтом",
                        "supplierArticle": "FBBT_43",
                        "brandName": "Satorna",
                    },
                },
            ],
            "count": 3,
        }
    }
    feedback_single = {
        "data": {
            "id": "low-rating-review-001",
            "text": "Размер не подошел, ожидал лучше",
            "pros": "Ткань нормальная",
            "cons": "Плохая посадка",
            "createdDate": "2025-01-29T08:20:00Z",
            "productValuation": 2,
            "productDetails": {
                "imtId": 202306999,
                "nmId": 446312866,
                "productName": "Футболка",
                "supplierArticle": "FBBT_42",
                "brandName": "Satorna",
            },
        }
    }
    feedbacks_rating = {
        "data": {
            "valuation": 4.7,
            "feedbacksCount": 218,
        }
    }
    tariffs_commission = {
        "report": [
            {"subjectID": 192, "subjectName": "Футболки", "kgvpMarketplace": 38.59, "kgvpSupplier": 38.59},
            {"subjectID": 291, "subjectName": "Лонгсливы", "kgvpMarketplace": 38.49, "kgvpSupplier": 38.49},
            {"subjectID": 2776, "subjectName": "Худи", "kgvpMarketplace": 38.49, "kgvpSupplier": 38.49},
        ]
    }
    tariffs_box = {
        "response": {
            "data": {
                "warehouseList": [
                    {"warehouseName": "Коледино", "boxDeliveryBase": "48", "boxDeliveryLiter": "12"},
                    {"warehouseName": "Подольск", "boxDeliveryBase": "52", "boxDeliveryLiter": "13"},
                ]
            }
        }
    }
    tariffs_pallet = {
        "response": {
            "data": {
                "warehouseList": [
                    {"warehouseName": "Коледино", "palletDeliveryValueBase": "320"},
                    {"warehouseName": "Подольск", "palletDeliveryValueBase": "340"},
                ]
            }
        }
    }

    fixtures = {
        "/api/v2/list/goods/size/nm": price_complete,
        "/api/v2/list/goods/filter": price_complete,
        "/content/v2/get/cards/list": content_cards_catalog,
        "/api/v1/calendar/promotions": calendar_promotions,
        "/api/v1/calendar/promotions/details": calendar_promotion_details,
        "/api/v1/calendar/promotions/nomenclatures": calendar_promotion_nomenclatures,
        "/api/v1/calendar/promotions/upload": {"data": {"taskId": "promo-upload-1", "status": "accepted"}},
        "/api/v2/upload/task": upload_task_accepted,
        "/api/v2/upload/task/size": upload_task_accepted,
        "/api/v2/buffer/tasks": buffer_state_complete,
        "/api/v2/buffer/goods/task": buffer_goods_complete,
        "/api/v2/history/tasks": history_state_complete,
        "/api/v2/history/goods/task": history_goods_complete,
        "/api/v2/quarantine/goods": quarantine,
        "/api/v1/supplier/orders": statistics_orders,
        "/api/v1/supplier/sales": statistics_sales,
        "/api/v1/supplier/stocks": statistics_stocks,
        "/api/v5/supplier/reportDetailByPeriod": realization_details,
        "/api/finance/v1/sales-reports/list": finance_sales_reports_list,
        "/api/finance/v1/sales-reports/detailed": finance_sales_reports_detailed,
        "/api/analytics/v3/sales-funnel/products": analytics_sales_funnel_products,
        "/api/analytics/v1/stocks-report/wb-warehouses": analytics_stocks_warehouses,
        "/api/v2/stocks-report/offices": analytics_stocks_offices,
        "/api/v1/warehouse_remains": warehouse_remains_task,
        "/api/v1/warehouse_remains/tasks/warehouse-remains-task-1/status": warehouse_remains_task_status,
        "/api/v1/warehouse_remains/tasks/warehouse-remains-task-1/download": warehouse_remains_task_download,
        "/api/analytics/v1/measurement-penalties": penalties,
        "/api/v1/acceptance_report": acceptance_task,
        "/api/v1/acceptance_report/tasks/acceptance-task-1/status": acceptance_task_status,
        "/api/v1/acceptance_report/tasks/acceptance-task-1/download": acceptance_task_download,
        "/api/v1/paid_storage": paid_storage_task,
        "/api/v1/paid_storage/tasks/paid-storage-task-1/status": paid_storage_task_status,
        "/api/v1/paid_storage/tasks/paid-storage-task-1/download": paid_storage_task_download,
        "/api/v3/offices": marketplace_offices,
        "/api/v1/warehouses": fbw_warehouses,
        "/api/v3/stocks/507": seller_stocks_507,
        "/api/v3/stocks/1201": seller_stocks_1201,
        "/api/v1/analytics/region-sale": region_sale,
        "/api/v2/stocks-report/products/products": stock_product_metrics,
        "/api/v2/stocks-report/products/sizes": stock_size_office_metrics,
        "/adv/v1/promotion/count": ads_promotion_count,
        "/api/advert/v2/adverts": ads_adverts,
        "/adv/v1/budget": ads_budget,
        "/adv/v1/balance": ads_balance,
        "/adv/v1/upd": ads_upd,
        "/adv/v1/normquery/stats": ads_normquery_stats,
        "/api/advert/v1/bids/min": advert_min_bids,
        "/adv/v3/fullstats": ads_fullstats,
        "/api/v1/feedbacks": feedbacks_list,
        "/api/v1/feedback": feedback_single,
        "/api/common/v1/rating": feedbacks_rating,
        "/api/v1/tariffs/commission": tariffs_commission,
        "/api/v1/tariffs/box": tariffs_box,
        "/api/v1/tariffs/pallet": tariffs_pallet,
    }
    errors: Dict[str, int] = {}
    headers: Dict[str, Dict[str, str]] = {}

    if scenario == "missing_spp":
        fixtures["/api/v2/list/goods/filter"] = price_missing_spp
    elif scenario == "missing_task_errors":
        fixtures["/api/v2/buffer/goods/task"] = {
            "data": {
                "uploadID": 146567,
                "bufferGoods": [
                    {
                        "nmID": 123456,
                        "sizeID": 654321,
                        "price": 139000,
                        "discount": 12,
                    }
                ],
            },
            "error": False,
            "errorText": "",
        }
        fixtures["/api/v2/history/goods/task"] = upload_details_missing_errors
    elif scenario == "rate_limited":
        errors["/api/v2/list/goods/filter"] = 429
        headers["/api/v2/list/goods/filter"] = {
            "X-Ratelimit-Limit": "10",
            "X-Ratelimit-Remaining": "0",
            "X-Ratelimit-Retry": "1",
            "X-Ratelimit-Reset": "6",
            "X-Request-Id": "fake-rate-limit",
        }
    elif scenario == "billing_error":
        errors["/api/v2/list/goods/filter"] = 402
    elif scenario == "server_error":
        errors["/api/v2/list/goods/filter"] = 503
        errors["/api/v2/upload/task"] = 503
        errors["/api/v2/history/tasks"] = 503
        errors["/api/v2/history/goods/task"] = 503
    elif scenario == "ads_missing_sku_breakdown":
        fixtures["/adv/v3/fullstats"] = [
            {
                "advertId": 22161678,
                "views": 84000,
                "clicks": 3200,
                "atbs": 440,
                "orders": 81,
                "sum": 950.0,
                "sum_price": 5200.0,
                "apps": [],
            }
        ]
    elif scenario == "ads_source_blocked":
        errors["/adv/v1/promotion/count"] = 429
        headers["/adv/v1/promotion/count"] = {
            "X-Ratelimit-Limit": "3",
            "X-Ratelimit-Remaining": "0",
            "X-Ratelimit-Retry": "20",
            "X-Ratelimit-Reset": "60",
            "X-Request-Id": "fake-ads-rate-limit",
        }
    elif scenario == "reports_source_blocked":
        errors["/api/v1/supplier/orders"] = 429
        headers["/api/v1/supplier/orders"] = {
            "X-Ratelimit-Limit": "1",
            "X-Ratelimit-Remaining": "0",
            "X-Ratelimit-Retry": "60",
            "X-Ratelimit-Reset": "60",
            "X-Request-Id": "fake-reports-rate-limit",
        }
        errors["/api/v5/supplier/reportDetailByPeriod"] = 429
        headers["/api/v5/supplier/reportDetailByPeriod"] = {
            "X-Ratelimit-Limit": "1",
            "X-Ratelimit-Remaining": "0",
            "X-Ratelimit-Retry": "60",
            "X-Ratelimit-Reset": "60",
            "X-Request-Id": "fake-realization-rate-limit",
        }

    return FakeWbApiClient(fixtures=fixtures, errors=errors, headers=headers)


def _error_message_from_payload(payload: Any, default: str) -> str:
    if isinstance(payload, dict):
        for key in ("errorText", "message", "detail", "title", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


class RealWbApiClient:
    """HTTP WB client for live adapter calls."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client_secret: str | None = None,
        timeout_seconds: float = 20.0,
        transport: Any | None = None,
    ) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for RealWbApiClient but is not installed")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.client_secret:
            headers["X-Client-Secret"] = self.client_secret
        return headers

    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        if httpx is None:  # pragma: no cover
            raise RuntimeError("httpx is required for RealWbApiClient but is not installed")

        url = f"{self.base_url}{request.path}"
        try:
            response = self._client.request(
                method=request.method,
                url=url,
                params=request.query or None,
                json=request.jsonBody,
                headers=self._headers(),
            )
        except Exception as exc:
            return WbApiResponseEnvelope(
                request=request,
                statusCode=503,
                ok=False,
                error=WbApiError(
                    statusCode=503,
                    code="wb_transport_error",
                    message=str(exc),
                    retryable=True,
                ),
                rateLimit=None,
                wbRequestId=None,
            )

        header_map = {k: v for k, v in response.headers.items()}
        rate_limit = parse_rate_limit_headers(header_map)
        request_id = response.headers.get("X-Request-Id") or response.headers.get("x-request-id")
        status_code = response.status_code

        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except Exception:
                payload = {"rawText": response.text}

        if 200 <= status_code < 300:
            data: Dict[str, Any] | None
            if isinstance(payload, dict):
                data = payload
            else:
                data = {"data": payload}
            return WbApiResponseEnvelope(
                request=request,
                statusCode=status_code,
                ok=True,
                data=data,
                error=None,
                rateLimit=rate_limit,
                wbRequestId=request_id,
            )

        message = _error_message_from_payload(payload, f"WB HTTP {status_code}")
        return WbApiResponseEnvelope(
            request=request,
            statusCode=status_code,
            ok=False,
            data=payload if isinstance(payload, dict) else None,
            error=classify_wb_error(status_code, message, rate_limit),
            rateLimit=rate_limit,
            wbRequestId=request_id,
        )


def build_wb_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_API_TOKEN is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_api_base_url,
            token=token,
            client_secret=settings.wb_api_client_secret,
            timeout_seconds=settings.wb_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_ads_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_ads_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_ADS_API_TOKEN is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_ads_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_ads_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_statistics_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_statistics_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_STATISTICS_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_statistics_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_statistics_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_analytics_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_analytics_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_ANALYTICS_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_analytics_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_analytics_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_content_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_content_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_CONTENT_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_content_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_content_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_promotions_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_promotions_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_PROMOTIONS_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_promotions_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_promotions_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_finance_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_finance_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_FINANCE_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_finance_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_finance_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_common_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_common_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_COMMON_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_common_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_common_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_marketplace_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_marketplace_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_MARKETPLACE_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_marketplace_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_marketplace_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_supplies_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_supplies_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_SUPPLIES_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_supplies_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_supplies_api_timeout_seconds,
        )
    return build_fake_client(scenario)


def build_wb_feedbacks_client(
    scenario: str = "complete",
    force_mode: Literal["fake", "real"] | None = None,
    token_override: str | None = None,
) -> WbApiClient:
    settings = get_settings()
    mode = force_mode or ("real" if token_override else settings.wb_api_mode)
    if mode == "real":
        token = token_override or settings.wb_feedbacks_api_token or settings.wb_api_token
        if not token:
            raise RuntimeError("VELLA_WB_FEEDBACKS_API_TOKEN (or VELLA_WB_API_TOKEN) is required when VELLA_WB_API_MODE=real")
        return RealWbApiClient(
            base_url=settings.wb_feedbacks_api_base_url,
            token=token,
            client_secret=None,
            timeout_seconds=settings.wb_feedbacks_api_timeout_seconds,
        )
    return build_fake_client(scenario)
