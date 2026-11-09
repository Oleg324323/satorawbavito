from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait

from sqlalchemy import text

from app.config import get_settings
from app.infra.db import get_engine
from app.infra.heartbeat import read_process_freshness
from app.infra.redis_client import get_redis_client

Check = Callable[[], None]
READINESS_TIMEOUT_SECONDS = 1.0
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health")


def _database() -> None:
    with get_engine().begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL statement_timeout = 1000"))
        if connection.execute(text("SELECT 1")).scalar_one() != 1:
            raise RuntimeError("Database check failed")


def _redis(url: str | None = None) -> None:
    if get_redis_client(url).ping() is not True:
        raise RuntimeError("Redis check failed")


def _default_checks() -> dict[str, Check]:
    settings = get_settings()
    return {
        "database": _database,
        "redis": _redis,
        "celeryBroker": lambda: _redis(settings.celery_broker_url),
        "celeryResultBackend": lambda: _redis(settings.celery_result_backend),
    }


def readiness_status(
    checks: Mapping[str, Check] | None = None,
    *,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
) -> dict[str, object]:
    required_checks = checks if checks is not None else _default_checks()
    futures: dict[Future[None], str] = {
        _EXECUTOR.submit(check): name for name, check in required_checks.items()
    }
    done, _ = wait(futures, timeout=max(0.0, timeout_seconds)) if futures else (set(), set())

    results: dict[str, str] = {}
    for future, name in futures.items():
        if future not in done:
            future.cancel()
            results[name] = "failed"
            continue
        try:
            future.result()
        except Exception:
            results[name] = "failed"
        else:
            results[name] = "ok"

    ready = all(state == "ok" for state in results.values())
    results.update(worker="not_monitored", beat="not_monitored")
    payload = {"status": "ready" if ready else "not_ready", "checks": results}
    freshness = read_process_freshness(get_settings())
    if freshness["status"] != "disabled":
        payload["freshness"] = freshness
        for kind in ("worker", "beat"):
            counts = freshness.get(kind)
            results[kind] = (
                "fresh" if counts["fresh"] == counts["expected"] else "degraded"
            ) if counts is not None else freshness["status"]
    return payload
