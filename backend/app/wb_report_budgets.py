"""Optional current budgets, independent of historical report completion.

Bounded provider reads per task, one delayed continuation at a time. No credentials
in broker arguments or persisted jobs; both cache and lease are connection-scoped.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import time
from uuid import uuid4

from app.infra.redis_client import get_redis_client
from app.repricer_cache.store import get_source_cache, save_source_cache

TTL = 3600
LEASE = 1800
DEADLINE = 3600
BALANCE_TTL = 300
BATCH_SIZE = 4


def connection(token):
    return sha256(token.encode()).hexdigest()[:16]


def keys(org, digest):
    return f"ads_budgets_{digest}", f"ads_budget_job_{digest}", f"wb:optional-budgets:{org}:{digest}"


def saved(org, digest):
    cache_key, _, _ = keys(org, digest)
    entries = (get_source_cache(org, cache_key, slim=False) or {}).get("budgets", {})
    result = {}
    for campaign, entry in entries.items():
        try:
            age = time.time() - datetime.fromisoformat(entry["fetchedAt"].replace("Z", "+00:00")).timestamp()
            if age >= 0 and isinstance(entry.get("value"), dict) and entry['value'].get('totalKopecks') is not None:
                result[str(campaign)] = {**entry, "fresh": age < TTL}
        except (KeyError, TypeError, ValueError):
            continue
    return result


def recent(org, digest):
    return {campaign: entry["value"] for campaign, entry in saved(org, digest).items() if entry["fresh"]}


def current_request(client, context, request):
    """Money has its own timestamped cache, not the one-hour report checkpoint.

    Retain the existing cross-process rate budget, cooldowns and demand checks.
    Only these optional current-money GETs bypass historical HTTP checkpoints.
    """
    from app.wb_api.client import RealWbApiClient
    if isinstance(client.inner, RealWbApiClient):
        return client._request(request, context.check, context)
    return client.request(request)


def balance_keys(org, digest):
    cache, _, lock = keys(org, digest)
    return cache + "_balance", lock + ":balance"


def balance_snapshot(org, digest):
    cache, _ = balance_keys(org, digest)
    snapshot = get_source_cache(org, cache, slim=False) or {}
    try:
        age = time.time() - datetime.fromisoformat(snapshot["fetchedAt"]).timestamp()
        snapshot["fresh"] = 0 <= age < BALANCE_TTL
    except (KeyError, ValueError, TypeError):
        snapshot["fresh"] = False
    return snapshot


def start_balance(org, user, digest, enqueue):
    snapshot = balance_snapshot(org, digest)
    if snapshot["fresh"]:
        return
    cache, lock = balance_keys(org, digest)
    redis, owner = get_redis_client(), uuid4().hex
    if not redis.set(lock, owner, nx=True, ex=600):
        return
    snapshot.update(state="queued", error=None, sequence=-1)
    try:
        save_source_cache(org, cache, snapshot, strict=True)
        enqueue(org, str(user), digest, owner, -1, 0)
    except Exception:
        snapshot.update(state="failed", error="Не удалось поставить обновление баланса в очередь")
        save_source_cache(org, cache, snapshot, strict=True)
        release(redis, lock, owner)


def run_balance(org, user, digest, owner, sequence, resolve_token, enqueue):
    cache, lock = balance_keys(org, digest)
    redis = get_redis_client()
    step_lock = f"{lock}:step:{owner}:{sequence}"
    if not redis.set(step_lock, "1", nx=True, ex=600):
        return {"state": "duplicate"}
    snapshot = balance_snapshot(org, digest)
    deferred = False
    claimed = False
    try:
        lease_owner = redis.get(lock)
        if isinstance(lease_owner, bytes):
            lease_owner = lease_owner.decode()
        if lease_owner != owner or snapshot.get("sequence") != sequence:
            return {"state": "superseded"}
        claimed = True
        token = resolve_token(org, user, None)
        if not token or connection(token) != digest:
            raise ValueError("connection changed")
        from app.wb_api.ads_runtime import _normalize_balance, build_wb_ads_client
        from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest
        from app.wb_api.report_reads import ReportReadSession, _digest
        context = ReportReadSession(org, _digest(token), cache)
        client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario="complete", token_override=token))
        response = current_request(client, context, WbApiRequest(method="GET", path="/adv/v1/balance"))
        if response.ok:
            value = _normalize_balance(response.data)
            if value.get("balanceKopecks") is None:
                snapshot.update(state="failed", error="WB не вернул баланс кабинета")
            else:
                snapshot = dict(state="completed", value=value, fetchedAt=datetime.now(timezone.utc).isoformat())
        elif response.statusCode in {429, 500, 502, 503, 504} and sequence > -3:
            delay = max(1, (response.rateLimit.retryAfterSeconds if response.rateLimit else None) or 60)
            snapshot.update(state="queued", sequence=sequence - 1, error=f"Баланс: WB HTTP {response.statusCode}; повтор через {delay} сек")
            # Do not shorten a longer provider cooldown or monopolize the worker.
            redis.expire(lock, max(600, delay + 120))
            save_source_cache(org, cache, snapshot, strict=True)
            enqueue(org, user, digest, owner, sequence - 1, delay)
            deferred = True
        else:
            snapshot.update(state="failed", error=f"Баланс недоступен: WB HTTP {response.statusCode}. Проверьте права токена Продвижение и повторите открытие вкладки.")
        save_source_cache(org, cache, snapshot, strict=True)
        return {"state": snapshot["state"]}
    except Exception:
        snapshot.update(state="failed", error="Не удалось обновить баланс WB. Проверьте подключение и права токена Продвижение.")
        save_source_cache(org, cache, snapshot, strict=True)
        return {"state": "failed"}
    finally:
        if claimed and not deferred:
            release(redis, lock, owner)
        redis.delete(step_lock)


def public_job(job):
    return {key: job.get(key) for key in ("state", "collected", "total", "error", "updatedAt") if key in job}


def start(org, user, digest, campaign_ids, enqueue, *, include_balance=False):
    if include_balance:
        start_balance(org, user, digest, enqueue)
    ids = sorted({str(value) for value in campaign_ids if str(value).isdigit() and int(value) > 0})
    available = recent(org, digest)
    pending = [value for value in ids if value not in available]
    _, job_key, lock = keys(org, digest)
    if not pending:
        return {"state": "completed", "collected": len(ids), "total": len(ids)}
    redis = get_redis_client()
    owner = uuid4().hex
    if not redis.set(lock, owner, nx=True, ex=LEASE):
        return public_job(get_source_cache(org, job_key, slim=False) or {"state": "queued"})
    job = dict(owner=owner, state="queued", campaigns=pending, cursor=0, sequence=0, attempts=0,
               total=len(ids), collected=len(ids)-len(pending), deadline=time.time()+DEADLINE, updatedAt=time.time())
    try:
        save_source_cache(org, job_key, job, strict=True)
        enqueue(org, str(user), digest, owner, 0, 1)
    except Exception:
        job.update(state="failed", error="Не удалось поставить загрузку бюджетов в очередь")
        save_source_cache(org, job_key, job, strict=True)
        release(redis, lock, owner)
    return public_job(job)


def release(redis, lock, owner):
    redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, lock, owner)


def run_step(org, user, digest, owner, sequence, resolve_token, enqueue):
    if sequence < 0:
        return run_balance(org, user, digest, owner, sequence, resolve_token, enqueue)
    cache_key, job_key, lock = keys(org, digest)
    redis = get_redis_client()
    # Claim this cursor atomically, including protection from duplicate delivery.
    step_lock = f"{lock}:step:{owner}:{sequence}"
    if not redis.set(step_lock, "1", nx=True, ex=600):
        return {"state": "duplicate"}
    try:
        job = get_source_cache(org, job_key, slim=False) or {}
        lease_owner = redis.get(lock)
        if isinstance(lease_owner, bytes):
            lease_owner = lease_owner.decode()
        if lease_owner != owner or job.get("owner") != owner or job.get("sequence") != sequence or job.get("state") not in {"queued", "running"}:
            return {"state": "superseded"}
        cursor = job["cursor"]
        def finish(state, error=None):
            job.update(state=state, updatedAt=time.time())
            if error:
                job["error"] = error
            save_source_cache(org, job_key, job, strict=True)
            release(redis, lock, owner)
            return public_job(job)
        if time.time() > job["deadline"]:
            return finish("failed", "Загрузка бюджетов превысила час; сохранённая часть доступна")
        token = resolve_token(org, user, None)
        if not token or connection(token) != digest:
            return finish("failed", "Подключение WB изменилось; бюджеты прежнего кабинета не используются")
        redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('EXPIRE',KEYS[1],ARGV[2]) end return 0", 1, lock, owner, LEASE)
        from app.wb_api.ads_runtime import _normalize_budget, build_wb_ads_client
        from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest
        from app.wb_api.report_reads import ReportReadSession, _digest
        context = ReportReadSession(org, _digest(token), job_key)
        client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario="complete", token_override=token))
        delay = 1
        entries = (get_source_cache(org, cache_key, slim=False) or {}).get("budgets", {})
        # Four bounded reads share the existing provider limiter (250 ms minimum).
        # Yield between batches so historical report tasks are never starved.
        for _ in range(BATCH_SIZE):
            cursor = job["cursor"]
            campaign = job["campaigns"][cursor]
            response = current_request(client, context, WbApiRequest(method="GET", path="/adv/v1/budget", query={"id": int(campaign)}))
            if response.ok:
                value = _normalize_budget(response.data)
                if value.get("totalKopecks") is None:
                    job["error"] = "WB вернул неполный бюджет части кампаний"
                else:
                    entries[campaign] = dict(value=value, fetchedAt=datetime.now(timezone.utc).isoformat())
                    save_source_cache(org, cache_key, {"budgets": entries}, strict=True)
                    job["collected"] += 1
                job["cursor"] += 1
                job["attempts"] = 0
            elif response.statusCode in {401, 403}:
                return finish("failed", f"Бюджеты недоступны: WB HTTP {response.statusCode}. Проверьте права токена Продвижение.")
            elif response.statusCode == 429 or response.statusCode >= 500:
                job["attempts"] += 1
                retry_after = getattr(response.rateLimit, "retryAfterSeconds", None) or 60
                if job["attempts"] >= 3 or retry_after > 900:
                    return finish("failed", f"Бюджеты временно недоступны: WB HTTP {response.statusCode}; сохранённая часть доступна")
                delay = max(20, retry_after)
                break
            else:
                job["cursor"] += 1
                job["attempts"] = 0
                job["error"] = f"Часть бюджетов недоступна: WB HTTP {response.statusCode}"
            save_source_cache(org, job_key, job, strict=True)
            if job["cursor"] >= len(job["campaigns"]):
                break
        if job["cursor"] >= len(job["campaigns"]):
            return finish("completed" if job["collected"] == job["total"] else "failed")
        job.update(state="running", sequence=sequence+1, updatedAt=time.time())
        save_source_cache(org, job_key, job, strict=True)
        enqueue(org, user, digest, owner, sequence+1, delay)
        return public_job(job)
    except Exception:
        # Provider exception text may contain secrets. Never persist it.
        job = get_source_cache(org, job_key, slim=False) or {}
        if job.get("owner") == owner:
            job.update(state="failed", error="Не удалось загрузить бюджеты; историческая статистика сохранена", updatedAt=time.time())
            save_source_cache(org, job_key, job, strict=True)
            release(redis, lock, owner)
        return public_job(job)
    finally:
        redis.delete(step_lock)


def overlay(org, digest, report):
    """Read-time enrichment only; never alters historical metrics/cache freshness."""
    report = deepcopy(report)
    if not any(item.get("id") == "campaign_budget" for item in report.get("kpis", [])):
        report.setdefault("kpis", []).append({"id": "campaign_budget", "label": "Бюджет РК"})
    available = saved(org, digest)
    totals = {}
    fresh = set()
    for row in report.get("rows", []):
        entry = available.get(str(row.get("campaignId")), {})
        value = entry.get("value")
        for field, key in (("budgetCashKopecks", "cashKopecks"), ("budgetNettingKopecks", "nettingKopecks"), ("budgetTotalKopecks", "totalKopecks")):
            row[field] = value.get(key) if value else None
        row["budgetFetchedAt"] = entry.get("fetchedAt")
        row["budgetFresh"] = entry.get("fresh", False)
        if row.get("budgetTotalKopecks") is not None:
            totals[str(row["campaignId"])] = row["budgetTotalKopecks"]
            if entry.get("fresh"):
                fresh.add(str(row["campaignId"]))
    ids = {str(row.get("campaignId")) for row in report.get("rows", [])
           if str(row.get("campaignId")).isdigit() and int(row["campaignId"]) > 0}
    _, job_key, _ = keys(org, digest)
    job = public_job(get_source_cache(org, job_key, slim=False) or {"state": "idle"})
    if not ids:
        job = ({"state": "failed", "error": "Сохранённый отчёт не содержит ID кампаний WB; текущие бюджеты недоступны"}
               if report.get("rows") else {"state": "completed"})
    if ids and len(fresh) == len(ids):
        job = {"state": "completed"}
    job.update(collected=len(fresh), total=len(ids))
    report["budgetRefresh"] = job
    for kpi in report.get("kpis", []):
        if kpi.get("id") == "campaign_budget":
            kpi["value"] = str(sum(totals.values())) if totals else "—"
            times = [available[campaign]["fetchedAt"] for campaign in totals]
            observed = f" Снимок от {min(times)}." if times else ""
            kpi["hint"] = f"Уникальные РК отчёта: {len(totals)}/{len(ids)}; свежие: {len(fresh)}/{len(ids)}. " + (
                "Частичная сумма. " if len(totals) < len(ids) else "") + (
                "Есть устаревшие значения. " if len(fresh) < len(totals) else "") + (job.get("error") or "") + observed
    snapshot = balance_snapshot(org, digest)
    report["cabinetBalance"] = snapshot.get("value", {})
    report["balanceRefresh"] = {key: snapshot.get(key) for key in ("state", "error", "fetchedAt", "fresh")}
    if snapshot["fresh"]:
        report["balanceRefresh"]["state"] = "completed"
    value = report["cabinetBalance"]
    hint = snapshot.get("error") or ("Текущий снимок" if snapshot["fresh"] else "Сохранённый снимок, обновляем" if value else "Загружаем текущий баланс WB")
    if snapshot.get("fetchedAt"):
        hint += f"; на {snapshot['fetchedAt']}"
    components = [f"{label}: {value[key] / 100:.2f} ₽" for key, label in
                  (("netKopecks", "счёт"), ("bonusKopecks", "бонусы")) if value.get(key) is not None]
    if components:
        hint += "; отдельно: " + ", ".join(components)
    if value.get("cashbacks"):
        hint += "; промо-бонусы: " + ", ".join(
            f"{entry.get('sum')} ₽ (до {entry.get('percent')}%, срок {entry.get('expiration_date')})"
            for entry in value["cashbacks"] if isinstance(entry, dict))
    balance_kpi = {"id": "cabinet_balance", "label": "Баланс кабинета",
                   "value": str(value["balanceKopecks"]) if value.get("balanceKopecks") is not None else "—", "hint": hint}
    report["kpis"] = [item for item in report.get("kpis", []) if item.get("id") != "cabinet_balance"] + [balance_kpi]
    return report
