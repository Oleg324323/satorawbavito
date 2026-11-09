import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.infra import health
from app.infra import redis_client as redis_module


def client() -> TestClient:
    return TestClient(main_module.create_app())


def test_database_readiness_supports_local_sqlite(monkeypatch):
    from sqlalchemy import create_engine
    engine = create_engine('sqlite+pysqlite:///:memory:')
    monkeypatch.setattr(health, 'get_engine', lambda: engine)
    try:
        health._database()
    finally:
        engine.dispose()


def test_postgres_readiness_keeps_transaction_local_timeout(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    statements = []
    def execute(statement):
        statements.append(str(statement))
        return SimpleNamespace(scalar_one=lambda: 1)
    @contextmanager
    def begin():
        yield SimpleNamespace(dialect=SimpleNamespace(name='postgresql'), execute=execute)
    monkeypatch.setattr(health, 'get_engine', lambda: SimpleNamespace(begin=begin))
    health._database()
    assert statements == ['SET LOCAL statement_timeout = 1000', 'SELECT 1']


def test_liveness_does_not_call_dependency_checks(monkeypatch):
    def fail() -> None:
        raise RuntimeError("dependencies are down")

    monkeypatch.setattr(main_module, "readiness_status", fail, raising=False)

    response = client().get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("status", "status_code"),
    (("ready", 200), ("not_ready", 503)),
)
def test_readiness_uses_stable_status_contract(monkeypatch, status, status_code):
    payload = {
        "status": status,
        "checks": {
            "database": "ok" if status == "ready" else "failed",
            "redis": "ok",
            "celeryBroker": "ok",
            "celeryResultBackend": "ok",
            "worker": "not_monitored",
            "beat": "not_monitored",
        },
    }
    monkeypatch.setattr(main_module, "readiness_status", lambda: payload, raising=False)

    response = client().get("/health/ready")

    assert response.status_code == status_code
    assert response.json() == payload


def test_legacy_health_contract_is_unchanged():
    response = client().get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "local",
        "contractVersion": "0.19.05-runtime-boundary",
        "wbApiMode": "fake",
        "wbLiveSyncEnabled": False,
        "realPriceApplyEnabled": False,
        "repricerLocalPriceApplyEnabled": True,
        "repricerPreserveLocalPriceOverrides": True,
        "legacyOneCEnabled": True,
        "apiDocsEnabled": True,
        "infrastructure": {
            "databaseConfigured": True,
            "redisConfigured": True,
            "celeryBrokerConfigured": True,
            "celeryResultBackendConfigured": True,
        },
    }


def test_readiness_reports_healthy_required_dependencies():
    checks = {
        "database": lambda: None,
        "redis": lambda: None,
        "celeryBroker": lambda: None,
        "celeryResultBackend": lambda: None,
    }

    assert health.readiness_status(checks) == {
        "status": "ready",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "celeryBroker": "ok",
            "celeryResultBackend": "ok",
            "worker": "not_monitored",
            "beat": "not_monitored",
        },
    }


@pytest.mark.parametrize("dependency", ("database", "redis"))
def test_readiness_fails_closed_for_required_dependency(dependency):
    def fail() -> None:
        raise RuntimeError("dependency unavailable")

    checks = {
        "database": lambda: None,
        "redis": lambda: None,
        "celeryBroker": lambda: None,
        "celeryResultBackend": lambda: None,
    }
    checks[dependency] = fail

    payload = health.readiness_status(checks)

    assert payload["status"] == "not_ready"
    assert payload["checks"][dependency] == "failed"


def test_readiness_timeout_is_bounded():
    checks = {
        "database": lambda: time.sleep(0.2),
        "redis": lambda: None,
    }

    started_at = time.monotonic()
    payload = health.readiness_status(checks, timeout_seconds=0.01)

    assert time.monotonic() - started_at < 0.1
    assert payload == {
        "status": "not_ready",
        "checks": {
            "database": "failed",
            "redis": "ok",
            "worker": "not_monitored",
            "beat": "not_monitored",
        },
    }


def test_readiness_does_not_expose_dependency_errors():
    def fail() -> None:
        raise RuntimeError("postgresql://user:password@private-host/database?token=secret")

    payload = health.readiness_status({"database": fail, "redis": lambda: None})

    serialized = str(payload)
    assert payload["checks"]["database"] == "failed"
    assert "password" not in serialized
    assert "private-host" not in serialized
    assert "secret" not in serialized


def test_default_readiness_checks_current_database_and_redis_dependencies(monkeypatch):
    database_calls = []
    redis_urls = []
    monkeypatch.setattr(health, "_database", lambda: database_calls.append(True), raising=False)
    monkeypatch.setattr(health, "_redis", lambda url=None: redis_urls.append(url), raising=False)

    payload = health.readiness_status(timeout_seconds=0.2)

    settings = main_module.get_settings()
    assert payload == {
        "status": "ready",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "celeryBroker": "ok",
            "celeryResultBackend": "ok",
            "worker": "not_monitored",
            "beat": "not_monitored",
        },
    }
    assert database_calls == [True]
    assert sorted(str(url) for url in redis_urls) == sorted(
        ("None", settings.celery_broker_url, settings.celery_result_backend)
    )


def test_database_probe_uses_existing_engine_with_statement_timeout(monkeypatch):
    statements = []

    class Result:
        @staticmethod
        def scalar_one():
            return 1

    class Connection:
        class dialect:
            name = "postgresql"

        def execute(self, statement):
            statements.append(str(statement))
            return Result()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Engine:
        @staticmethod
        def begin():
            return Transaction()

    monkeypatch.setattr(health, "get_engine", lambda: Engine(), raising=False)

    health._database()

    assert statements == ["SET LOCAL statement_timeout = 1000", "SELECT 1"]


def test_redis_probe_uses_existing_client_factory(monkeypatch):
    calls = []

    class RedisClient:
        @staticmethod
        def ping():
            calls.append("ping")
            return True

    monkeypatch.setattr(
        health,
        "get_redis_client",
        lambda url=None: calls.append(url) or RedisClient(),
        raising=False,
    )

    health._redis("redis://broker:6379/0")

    assert calls == ["redis://broker:6379/0", "ping"]


def test_redis_factory_can_target_the_celery_redis_urls(monkeypatch):
    created_client = object()
    calls = []

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return created_client

    redis_module.get_redis_client.cache_clear()
    monkeypatch.setattr(redis_module.Redis, "from_url", from_url)
    try:
        client = redis_module.get_redis_client("redis://broker:6379/0")
    finally:
        redis_module.get_redis_client.cache_clear()

    assert client is created_client
    assert calls[0][0] == "redis://broker:6379/0"


@pytest.mark.parametrize("database", [0, 3])
def test_celery_unix_readiness_uses_actual_socket_and_selected_database(database):
    from redis.connection import UnixDomainSocketConnection

    redis_module.get_redis_client.cache_clear()
    try:
        target = f"redis+socket:///run/redis/redis-server.sock?virtual_host={database}"
        connection = redis_module.get_redis_client(target).connection_pool
        assert connection.connection_class is UnixDomainSocketConnection
        assert connection.connection_kwargs["path"] == "/run/redis/redis-server.sock"
        assert connection.connection_kwargs["db"] == database
        assert "virtual_host" not in connection.connection_kwargs
    finally:
        redis_module.get_redis_client.cache_clear()


@pytest.mark.parametrize("query", ["virtual_host=0&virtual_host=3", "virtual_host=0&db=3"])
def test_celery_unix_readiness_rejects_ambiguous_database(query):
    with pytest.raises(ValueError, match="REDIS_SOCKET_DATABASE_AMBIGUOUS"):
        redis_module.get_redis_client(f"redis+socket:///run/redis/redis-server.sock?{query}")
