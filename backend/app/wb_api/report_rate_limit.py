"""Redis-coordinated read budgets, shared by report workers for one credential.

Runs immediately before HTTP dispatch, not when a job is queued. Waiting never
reserves a future slot, so cancelled views do not delay the next active view.
"""
import math
import time
from hashlib import sha256

from app.infra.redis_client import get_redis_client


class ReportReadBudget:
    def __init__(self, organization_id, credential_digest, host, policy):
        self.policy = policy
        identity = sha256(f"{host}:{credential_digest}:{policy.category}".encode()).hexdigest()
        self.key = f"wb:report-budget:{organization_id}:{identity}"

    def acquire(self, heartbeat):
        from app.wb_api.client import RateLimitInfo
        redis = get_redis_client()
        deadline = time.monotonic() + 300
        while True:
            heartbeat()
            # Redis time prevents worker clock skew from bypassing the budget.
            remaining = redis.pttl(self.key + ':cooldown')
            if remaining > 0:
                return RateLimitInfo(retryAfterSeconds=math.ceil(remaining / 1000))
            wait_ms = redis.eval('''
                local cooldown = redis.call('PTTL', KEYS[2])
                if cooldown > 0 then return -cooldown end
                local clock = redis.call('TIME')
                local now = clock[1] * 1000 + clock[2] / 1000
                local old = redis.call('HMGET', KEYS[1], 'tokens', 'updated', 'sent')
                local burst, rate, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
                local tokens = math.min(burst, (tonumber(old[1]) or burst) + math.max(0, now - (tonumber(old[2]) or now)) * rate)
                local wait = math.max(0, (tonumber(old[3]) or 0) + tonumber(ARGV[4]) - now, (cost - tokens) / rate)
                if wait > 0 then return math.ceil(wait) end
                redis.call('HSET', KEYS[1], 'tokens', tokens-cost, 'updated', now, 'sent', now)
                redis.call('PEXPIRE', KEYS[1], ARGV[5])
                return 0
            ''', 2, self.key, self.key + ':cooldown', self.policy.burst,
                self.policy.limit / (self.policy.periodSeconds * 1000), self.policy.defaultRequestCost,
                self.policy.intervalMs, max(60000, self.policy.periodSeconds * 2000,
                    math.ceil(self.policy.burst / self.policy.limit * self.policy.periodSeconds * 2000)))
            if wait_ms < 0:
                return RateLimitInfo(retryAfterSeconds=math.ceil(-wait_ms / 1000))
            if wait_ms == 0:
                return None
            if time.monotonic() >= deadline:
                raise RuntimeError('Лимит WB занят более 5 минут. Повторите загрузку позже.')
            time.sleep(min(wait_ms / 1000, 1))

    def observe(self, response):
        redis = get_redis_client()
        info = response.rateLimit
        if info and info.remaining is not None:
            redis.eval('''
                if redis.call('EXISTS', KEYS[1]) == 1 then
                    local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens')) or 0
                    redis.call('HSET', KEYS[1], 'tokens', math.min(tokens, math.max(0, tonumber(ARGV[1]))))
                end
                return 1
            ''', 1, self.key, info.remaining)
        delay = (info.retryAfterSeconds or 0) if info else 0
        if info and info.remaining == 0:
            delay = max(delay, info.resetAfterSeconds or 0)
        if response.statusCode == 429:
            delay = max(delay, 1 if delay else 60)
        if delay > 0:
            # A late successful response must not shorten another worker's 429.
            redis.eval('''
                if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[1]) then
                    redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
                end
                return 1
            ''', 1, self.key + ':cooldown', math.ceil(delay * 1000))
        if response.statusCode == 409 and self.policy.status409Cost > self.policy.defaultRequestCost:
            redis.eval('''
                if redis.call('EXISTS', KEYS[1]) == 1 then
                    local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens')) or 0
                    redis.call('HSET', KEYS[1], 'tokens', math.max(0, tokens - tonumber(ARGV[1])))
                end
                return 1
            ''', 1, self.key, self.policy.status409Cost - self.policy.defaultRequestCost)
