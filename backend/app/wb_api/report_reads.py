"""Cooperative report demand and durable, credential-scoped read checkpoints.

Only a report worker installs this context. Business writes never use it.
Cancellation happens before the next provider request, after the preceding
response has been durably saved. Redis coordinates processes; DB stores data.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from app.infra.redis_client import get_redis_client
from app.repricer_cache.store import get_source_cache, save_source_cache

LEASE_SECONDS = 120
READ_TTL_SECONDS = 3600
_IDS = re.compile(r"^[a-zA-Z0-9-]{16,80}$")
active_report_read: ContextVar = ContextVar("active_report_read", default=None)


class ReportDemandEnded(BaseException):
    """Control flow, deliberately not swallowed by provider error handlers."""


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _demand_key(org):
    return f"wb:report-demand:{org}"


def _member(user, consumer):
    return _digest(f"{user}:{consumer}")


def touch_demand(request, actor, job_key, *, start=False):
    headers = getattr(request, "headers", {})
    consumer = headers.get("x-wb-consumer", "")
    view = headers.get("x-wb-view", "")
    if not consumer and not view:
        return  # Existing API clients remain compatible.
    if not _IDS.fullmatch(consumer) or not _IDS.fullmatch(view):
        raise HTTPException(400, "Некорректный идентификатор загрузки")
    try:
        sequence = int(headers.get("x-wb-sequence", "0"))
        if not 0 <= sequence <= 2**53:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Некорректный номер загрузки") from None
    redis = get_redis_client()
    key, member = _demand_key(actor.organization_id), _member(actor.user_id, consumer)
    now = time.time()
    try:
        previous = redis.hget(key, member)
        previous = json.loads(previous) if previous else {}
        if not start and (job_key not in previous.get("jobs", {}) or previous.get("view") != view or previous.get("released")):
            return  # An old poll must never replace the current screen.
        for field, raw in redis.hgetall(key).items():
            if json.loads(raw).get("until", 0) < now:
                redis.eval("if redis.call('HGET',KEYS[1],ARGV[1]) == ARGV[2] then return redis.call('HDEL',KEYS[1],ARGV[1]) end return 0", 1, key, field, raw)
        if start and not previous and redis.hlen(key) >= 128:
            raise HTTPException(429, "Слишком много открытых загрузок")
        redis.eval("""
            redis.call('SET', KEYS[2], '1', 'EX', 86400)
            if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
            local raw = redis.call('HGET', KEYS[1], ARGV[1])
            local old = raw and cjson.decode(raw) or nil
            local next = cjson.decode(ARGV[2])
            if ARGV[3] == 'start' then
                if old and (old.sequence or 0) > next.sequence then return 0 end
                if old and old.view == next.view and old.released then return 0 end
            else
                if not old or old.view ~= next.view or old.released or not old.jobs or not old.jobs[next.job] then return 0 end
                next.sequence = old.sequence
            end
            next.jobs = old and old.view == next.view and old.jobs or {}
            next.jobs[next.job] = next['until']
            redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(next))
            redis.call('EXPIRE', KEYS[1], 86400)
            return 1
        """, 3, key, f"{key}:managed:{_digest(job_key)}", f"{key}:released:{member}:{view}", member,
            json.dumps({"job": job_key, "view": view, "sequence": sequence, "until": now + LEASE_SECONDS}),
            "start" if start else "heartbeat")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Очередь загрузки недоступна. Сохранённые данные доступны.") from None


def release_demand(request, actor):
    consumer = request.headers.get("x-wb-consumer", "")
    view = request.headers.get("x-wb-view", "")
    if not _IDS.fullmatch(consumer) or not _IDS.fullmatch(view):
        raise HTTPException(400, "Некорректный идентификатор загрузки")
    # Compare/delete atomically: a late cleanup cannot cancel the new view.
    get_redis_client().eval("""
        redis.call('SET', KEYS[2], '1', 'EX', 86400)
        local raw = redis.call('HGET', KEYS[1], ARGV[1])
        if raw and cjson.decode(raw).view == ARGV[2] then
            local value = cjson.decode(raw)
            value.released = true
            value.jobs = {}
            redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(value))
            return 1
        end
        return 0
    """, 2, _demand_key(actor.organization_id),
        f"{_demand_key(actor.organization_id)}:released:{_member(actor.user_id, consumer)}:{view}",
        _member(actor.user_id, consumer), view)


@dataclass
class ReportReadSession:
    organization_id: int
    credential_digest: str
    job_key: str

    def check(self):
        redis = get_redis_client()
        key = _demand_key(self.organization_id)
        if not redis.exists(f"{key}:managed:{_digest(self.job_key)}"):
            return
        now = time.time()
        for raw in redis.hgetall(key).values():
            value = json.loads(raw)
            if not value.get("released") and value.get("jobs", {}).get(self.job_key, 0) > now:
                return
        raise ReportDemandEnded()

    def request(self, inner, request, invoke):
        from app.wb_api.client import WbApiResponseEnvelope
        # Include actual host and credential, never raw credentials, in identity.
        if _digest(inner.token) != self.credential_digest:
            raise RuntimeError("WB connection changed during collection")
        self.check()
        allowed_posts = {"/api/analytics/v3/sales-funnel/products", "/api/analytics/v3/sales-funnel/products/history",
                         "/content/v2/get/cards/list", "/adv/v1/normquery/stats",
                         "/api/analytics/v1/stocks-report/wb-warehouses", "/api/v2/stocks-report/products/products",
                         "/api/finance/v1/sales-reports/list", "/api/finance/v1/sales-reports/detailed",
                         "/api/v2/list/goods/filter"}
        from app.wb_api.financial_reports import DETAIL_PATH
        suffix = request.path[len(DETAIL_PATH) + 1:] if request.path.startswith(DETAIL_PATH + "/") else ""
        exact_report = suffix.isascii() and suffix.isdecimal() and not suffix.startswith("0")
        if request.method != "GET" and not (request.method == "POST" and (request.path in allowed_posts or exact_report)):
            raise RuntimeError("Unsupported provider operation in report read context")
        identity = json.dumps([inner.base_url, self.credential_digest, request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
        cache_key = f"wb_report_http_{_digest(identity)}"
        redis = get_redis_client()
        lock_key = f"wb:report-read:{self.organization_id}:{_digest(identity)}"
        owner = uuid4().hex
        lock_seconds = max(120, int(inner.timeout_seconds * 2 + 30))

        def cached():
            row = get_source_cache(self.organization_id, cache_key, strict=True) or {}
            if row.get("expiresAt", 0) > time.time() and row.get("response"):
                return WbApiResponseEnvelope.model_validate(row["response"])
            return None

        until = time.monotonic() + 300
        while True:
            self.check()
            result = cached()
            if result is not None:
                return result
            blocked = redis.get(f"{lock_key}:cooldown")
            if blocked:
                return WbApiResponseEnvelope.model_validate_json(blocked)
            if redis.set(lock_key, owner, nx=True, ex=lock_seconds):
                break
            if time.monotonic() >= until:
                raise RuntimeError("Другая загрузка WB не завершилась за 5 минут. Повторите проверку.")
            time.sleep(1)
        try:
            result = cached()  # Another process may have committed just before NX.
            if result is not None:
                return result
            def heartbeat():
                self.check()
                renewed = redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('EXPIRE',KEYS[1],ARGV[2]) end return 0",
                                     1, lock_key, owner, lock_seconds)
                if not renewed:
                    raise RuntimeError("Потеряна блокировка запроса WB; повторите загрузку")
            response = invoke(heartbeat)
            if response.ok and not request.path.endswith('/status'):
                save_source_cache(self.organization_id, cache_key, {
                    "expiresAt": time.time() + READ_TTL_SECONDS,
                    "response": response.model_dump(mode="json"),
                }, strict=True)
            elif response.statusCode in {401, 403, 429}:
                delay = max(5, min(900, (response.rateLimit.retryAfterSeconds if response.rateLimit else None) or 60))
                redis.set(f"{lock_key}:cooldown", response.model_dump_json(), ex=delay)
            return response
        finally:
            redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, lock_key, owner)


def install_report_read(organization_id, token, job_key):
    return active_report_read.set(ReportReadSession(organization_id, _digest(token or ""), job_key))
