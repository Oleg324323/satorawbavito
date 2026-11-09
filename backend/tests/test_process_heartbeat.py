import json
import gc
import threading
import weakref
from dataclasses import replace
from types import SimpleNamespace

import pytest
from celery import Celery, bootsteps
from celery.beat import PersistentScheduler, Service
from celery.signals import heartbeat_sent
from celery.worker.consumer.heart import Heart as HeartStep
from celery.worker.heartbeat import Heart

from app.config import Settings, get_settings
from app.infra import heartbeat as hb
from app.infra.heartbeat import read_process_freshness


def test_disabled_reader_never_constructs_client():
    def forbidden_client(*args, **kwargs):
        raise AssertionError("disabled heartbeat constructed a client")

    assert read_process_freshness(
        Settings(), client_factory=forbidden_client
    ) == {"status": "disabled"}


def configured(**changes):
    fields = dict(
        process_heartbeat_enabled=True,
        process_heartbeat_namespace="test_ns",
        process_heartbeat_expected_worker_ids=("test_w1", "test_w2"),
        process_heartbeat_expected_beat_ids=("test_b1",),
        process_heartbeat_worker_instance_id="test_w1",
        process_heartbeat_beat_instance_id="test_b1",
        process_heartbeat_max_age_seconds=10.0,
        process_heartbeat_retention_seconds=30,
        process_heartbeat_beat_max_interval_seconds=2.0,
        process_heartbeat_redis_connect_timeout_seconds=0.1,
        process_heartbeat_redis_socket_timeout_seconds=0.2,
        process_heartbeat_redis_retry_attempts=0,
        redis_url="redis://synthetic.invalid/0",
    )
    fields.update({"process_heartbeat_" + k: v for k, v in changes.items()})
    return Settings(**fields)


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected client construction")


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class FakeRedis:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.calls = []
        self.closed = 0

    def set(self, key, value, *, ex):
        self.calls.append(("SET", key, value, ex))
        self.values[key] = (value, self.clock() + ex)
        return True

    def mget(self, keys):
        self.calls.append(("MGET", tuple(keys)))
        return [self.values[k][0] if k in self.values and self.values[k][1] > self.clock()
                else None for k in keys]

    def close(self):
        self.closed += 1


def marker(**changes):
    fields = dict(v=1, namespace="test_ns", kind="worker", instance_id="test_w1",
                  incarnation="a" * 32, published_at=100.0)
    fields.update(changes)
    return json.dumps(fields)


@pytest.mark.parametrize("field", [
    "namespace", "expected_worker_ids", "expected_beat_ids", "max_age_seconds",
    "retention_seconds", "beat_max_interval_seconds", "redis_connect_timeout_seconds",
    "redis_socket_timeout_seconds", "redis_retry_attempts",
])
def test_each_enabled_policy_field_is_required(field):
    result = read_process_freshness(configured(**{field: None}), client_factory=forbidden)
    assert result == {"status": "invalid_configuration"}


@pytest.mark.parametrize("field,value", [
    ("enabled", "bad-canary"), ("enabled", 1), ("enabled", None),
    ("namespace", ""), ("namespace", "x" * 65), ("namespace", "-bad"),
    ("namespace", "secret-canary/"), ("namespace", "я"), ("namespace", 1),
    ("expected_worker_ids", ()), ("expected_beat_ids", ()),
    ("expected_worker_ids", ("same", "same")),
    ("expected_beat_ids", ("same", "same")),
    ("expected_worker_ids", tuple("w" + str(i) for i in range(33))),
    ("expected_beat_ids", ("bad/",)), ("expected_worker_ids", ("",)),
    ("expected_worker_ids", ("x" * 65,)), ("expected_worker_ids", ["w1"]),
    ("expected_worker_ids", "w1"), ("expected_worker_ids", (True,)),
    ("retention_seconds", True), ("retention_seconds", 30.0),
    ("retention_seconds", 10), ("retention_seconds", 0),
    ("redis_retry_attempts", True), ("redis_retry_attempts", 0.0),
    ("redis_retry_attempts", 1), ("redis_retry_attempts", -1),
    ("beat_max_interval_seconds", 10), ("beat_max_interval_seconds", 11),
    ("redis_connect_timeout_seconds", 9.8),
])
def test_invalid_policy_fails_closed_and_redacts(field, value):
    settings = configured(**{field: value})
    result = hb.resolve_policy(settings)
    assert result.status == "invalid_configuration"
    assert result.error_code == "invalid_configuration"
    assert result.policy is None
    assert read_process_freshness(settings, client_factory=forbidden) == {
        "status": "invalid_configuration"
    }
    assert "canary" not in repr(result)


@pytest.mark.parametrize("field", ["max_age_seconds", "beat_max_interval_seconds",
    "redis_connect_timeout_seconds", "redis_socket_timeout_seconds"])
@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "2"])
def test_numeric_policy_requires_positive_finite_non_boolean(field, value):
    assert hb.resolve_policy(configured(**{field: value})).status == "invalid_configuration"


def test_role_identity_required_only_for_owner():
    settings = configured(worker_instance_id=None, beat_instance_id=None)
    assert hb.resolve_policy(settings).status == "enabled"
    for role in ("worker", "beat", "other"):
        assert hb.resolve_policy(settings, owner_kind=role).status == "invalid_configuration"
    for role in ("worker", "beat"):
        assert hb.resolve_policy(configured(**{role + "_instance_id": "unknown"}),
                                 owner_kind=role).status == "invalid_configuration"


@pytest.mark.parametrize("raw,want", [("false", "disabled"), ("true", "invalid_configuration"),
                                      ("garbage-canary", "invalid_configuration"),
                                      ("1", "invalid_configuration"), ("", "invalid_configuration")])
def test_enabled_env_never_silently_disables_invalid_input(monkeypatch, raw, want):
    monkeypatch.setenv("VELLA_PROCESS_HEARTBEAT_ENABLED", raw)
    assert read_process_freshness(get_settings(), client_factory=forbidden) == {"status": want}


def test_complete_environment_policy_parses_without_repair(monkeypatch):
    values = {"ENABLED": "true", "NAMESPACE": "test_ns", "EXPECTED_WORKER_IDS": "test_w1,test_w2",
              "EXPECTED_BEAT_IDS": "test_b1", "WORKER_INSTANCE_ID": "test_w1",
              "BEAT_INSTANCE_ID": "test_b1", "MAX_AGE_SECONDS": "10",
              "RETENTION_SECONDS": "30", "BEAT_MAX_INTERVAL_SECONDS": "2",
              "REDIS_CONNECT_TIMEOUT_SECONDS": "0.1", "REDIS_SOCKET_TIMEOUT_SECONDS": "0.2",
              "REDIS_RETRY_ATTEMPTS": "0"}
    for field, value in values.items():
        monkeypatch.setenv("VELLA_PROCESS_HEARTBEAT_" + field, value)
    assert hb.resolve_policy(get_settings(), owner_kind="worker").status == "enabled"
    for field, value in [("EXPECTED_WORKER_IDS", "test_w1,,test_w2"),
                         ("EXPECTED_WORKER_IDS", "test_w1, test_w2"),
                         ("RETENTION_SECONDS", "30.0"), ("REDIS_RETRY_ATTEMPTS", "bad-canary"),
                         ("MAX_AGE_SECONDS", "bad-canary")]:
        monkeypatch.setenv("VELLA_PROCESS_HEARTBEAT_" + field, value)
        assert hb.resolve_policy(get_settings()).status == "invalid_configuration"
        monkeypatch.setenv("VELLA_PROCESS_HEARTBEAT_" + field, values[field])


def test_publisher_lifecycle_wire_expiry_and_reader_aggregate():
    clock = Clock()
    redis = FakeRedis(clock)
    policy = hb.resolve_policy(configured()).policy
    writer = hb.ProcessHeartbeatPublisher(policy, "worker", "test_w1",
                                         client_factory=lambda *a, **kw: redis, clock=clock)
    assert writer.publish() is False
    writer.start()
    assert redis.calls == []
    assert writer.publish() is True
    op, key, value, ex = redis.calls[0]
    assert (op, key, ex) == ("SET", "vella:heartbeat:v1:test_ns:worker:test_w1", 30)
    payload = json.loads(value)
    assert set(payload) == {"v", "namespace", "kind", "instance_id", "incarnation", "published_at"}
    assert payload["published_at"] == 100.0
    assert len(payload["incarnation"]) == 32
    read = lambda: read_process_freshness(configured(), client_factory=lambda *a, **kw: redis, clock=clock)
    assert read() == {"status": "degraded",
        "worker": {"expected": 2, "fresh": 1, "stale": 0, "missing": 1, "invalid": 0},
        "beat": {"expected": 1, "fresh": 0, "stale": 0, "missing": 1, "invalid": 0}}
    assert redis.calls[-1] == ("MGET", (
        "vella:heartbeat:v1:test_ns:worker:test_w1", "vella:heartbeat:v1:test_ns:worker:test_w2",
        "vella:heartbeat:v1:test_ns:beat:test_b1"))
    clock.value = 110
    assert read()["worker"]["stale"] == 1
    clock.value = 130
    assert read()["worker"]["missing"] == 2
    writer.stop()
    assert writer.publish() is False
    assert redis.closed == 4
    writer.start()
    writer.publish()
    assert json.loads(redis.calls[-1][2])["incarnation"] != payload["incarnation"]
    writer.stop()


@pytest.mark.parametrize("value", [
    "canary" * 300, "bad-canary", "[]", "null", "{}", b"\xff",
    marker(v=True), marker(v=1.0), marker(v=2), marker(namespace="other-canary"),
    marker(kind="beat"), marker(instance_id="test_w2"), marker(incarnation="bad-canary"),
    marker(published_at=True), marker(published_at="100"), marker(published_at=float("nan")),
    marker(published_at=float("inf")), marker(published_at=101), marker(extra="canary"),
])
def test_invalid_marker_is_bounded_redacted_and_cannot_satisfy_expected_slot(value):
    clock = Clock()
    redis = FakeRedis(clock)
    redis.values["vella:heartbeat:v1:test_ns:worker:test_w1"] = (value, 200)
    result = read_process_freshness(configured(), client_factory=lambda *a, **kw: redis, clock=clock)
    assert result["worker"] == {"expected": 2, "fresh": 0, "stale": 0, "missing": 1, "invalid": 1}
    assert "canary" not in repr(result)
    assert redis.closed == 1


def test_redis_errors_and_local_close_errors_are_redacted():
    class Broken(FakeRedis):
        def mget(self, keys):
            raise RuntimeError("raw-error-canary")

        def set(self, *args, **kwargs):
            raise RuntimeError("raw-error-canary")

        def close(self):
            super().close()
            raise RuntimeError("close-error-canary")

    redis = Broken(Clock())
    assert read_process_freshness(configured(), client_factory=lambda *a, **kw: redis) == {"status": "unavailable"}
    assert redis.closed == 1
    publisher = hb.ProcessHeartbeatPublisher(hb.resolve_policy(configured()).policy, "worker", "test_w1",
                                             client_factory=lambda *a, **kw: redis)
    publisher.start()
    assert publisher.publish() is False
    publisher.stop()


def test_client_is_lazy_dedicated_no_retry_and_bounded(monkeypatch):
    import redis
    calls = []
    fake = FakeRedis(Clock())
    fake.connection_pool = SimpleNamespace(connection_kwargs={})
    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return fake
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    policy = hb.resolve_policy(configured()).policy
    publisher = hb.ProcessHeartbeatPublisher(policy, "worker", "test_w1", clock=Clock())
    publisher.start()
    assert calls == []
    publisher.publish()
    url, options = calls[0]
    assert url == "redis://synthetic.invalid/0"
    assert options["socket_connect_timeout"] == 0.1
    assert options["socket_timeout"] == 0.2
    attempts = []
    def fail():
        attempts.append(True)
        raise redis.exceptions.ConnectionError("synthetic")
    with pytest.raises(redis.exceptions.ConnectionError):
        options["retry"].call_with_retry(fail, lambda exc: None)
    assert len(attempts) == 1
    publisher.stop()


class Timer:
    def call_repeatedly(self, interval, callback, args):
        self.callback, self.args = callback, args
        return object()

    def cancel(self, ref):
        pass

    def fire(self):
        self.callback(*self.args)


class Eventer:
    enabled = True

    def __init__(self):
        self.on_enabled, self.on_disabled = set(), set()
        self.events = []

    def send(self, event, **kwargs):
        self.events.append(event)


class Consumer:
    def __init__(self, app):
        self.app = app
        self.timer, self.event_dispatcher = Timer(), Eventer()
        self.steps = []


def worker(monkeypatch, *, settings=None, without_heartbeat=False, heartbeat_interval=2):
    app = Celery("test-heartbeat", broker="memory://", backend="cache+memory://", set_as_current=False)
    hb.install_process_heartbeat(app, settings or configured())
    redis = FakeRedis(Clock())
    monkeypatch.setattr(hb, "_client", lambda policy: redis)
    # Keep actual Heart and our dependency edge; omit unrelated broker/event
    # connection bootsteps, whose I/O is represented by the timer/eventer above.
    monkeypatch.setattr(HeartStep, "requires", ())
    consumer = Consumer(app)
    consumer.blueprint = bootsteps.Blueprint(steps=(hb.WorkerHeartbeatStep,))
    consumer.blueprint.apply(consumer, without_heartbeat=without_heartbeat,
                             heartbeat_interval=heartbeat_interval)
    consumer.blueprint.start(consumer)
    step = next(s for s in consumer.steps if isinstance(s, hb.WorkerHeartbeatStep))
    return app, consumer, step, redis


def test_real_heart_online_periodic_offline_and_reconnect(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch)
    heart = consumer.heart
    assert isinstance(heart, Heart)
    assert heart._send_sent_signal is not None
    assert redis.calls == []  # Both real online and owner start are silent.
    consumer.timer.fire()
    assert len(redis.calls) == 1
    first = json.loads(redis.calls[0][2])["incarnation"]
    # Offline must be rejected even while the owner remains armed.
    heart.stop()
    assert len(redis.calls) == 1
    heart.start()
    assert len(redis.calls) == 1
    consumer.timer.fire()
    assert len(redis.calls) == 2
    consumer.blueprint.restart(consumer)
    assert redis.closed == 1
    consumer.blueprint.start(consumer)
    assert consumer.heart is not heart
    heart.tref = object()
    heartbeat_sent.send(sender=heart)
    assert len(redis.calls) == 2
    consumer.timer.fire()
    assert len(redis.calls) == 3
    assert json.loads(redis.calls[-1][2])["incarnation"] != first
    consumer.blueprint.stop(consumer)
    consumer.timer.fire()
    assert len(redis.calls) == 3
    assert redis.closed == 2
    app.close()


@pytest.mark.parametrize("hook", ["close", "stop", "shutdown", "terminate"])
def test_each_worker_shutdown_hook_fences_queued_callbacks(monkeypatch, hook):
    app, consumer, step, redis = worker(monkeypatch)
    consumer.timer.fire()
    getattr(step, hook)(consumer)
    consumer.timer.fire()
    getattr(step, hook)(consumer)
    assert len(redis.calls) == 1
    assert redis.closed == 1
    consumer.heart.stop()
    app.close()


@pytest.mark.parametrize("interval", [10, 11, float("inf"), float("nan"), -1])
def test_invalid_worker_interval_never_arms(monkeypatch, interval):
    app, consumer, step, redis = worker(monkeypatch, heartbeat_interval=interval)
    consumer.timer.fire()
    assert redis.calls == []
    consumer.blueprint.stop(consumer)
    app.close()


def test_without_heartbeat_and_missing_owner_identity_never_publish(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch, without_heartbeat=True)
    assert consumer.heart is None
    assert redis.calls == []
    step.stop(consumer)
    app.close()
    app, consumer, step, redis = worker(monkeypatch, settings=configured(worker_instance_id=None))
    consumer.timer.fire()
    assert redis.calls == []
    consumer.blueprint.stop(consumer)
    app.close()


def test_worker_app_heart_pid_and_blueprint_fences(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch)
    other_app = Celery("unregistered", broker="memory://", set_as_current=False)
    other_heart = Heart(Timer(), Eventer())
    other_heart.start()
    other_heart.timer.fire()
    assert redis.calls == []
    original = consumer.heart
    consumer.heart = other_heart
    consumer.timer.fire()
    consumer.heart = original
    consumer.app = other_app
    consumer.timer.fire()
    consumer.app = app
    with monkeypatch.context() as patch:
        patch.setattr(hb.os, "getpid", lambda: -123)
        consumer.timer.fire()
    consumer.blueprint.state = bootsteps.CLOSE
    consumer.timer.fire()
    assert redis.calls == []
    consumer.blueprint.state = bootsteps.RUN
    consumer.timer.fire()
    assert len(redis.calls) == 1
    step.stop(consumer)
    other_heart.stop()
    original.stop()
    app.close()
    other_app.close()


def test_installer_is_idempotent_scoped_lazy_and_weak(monkeypatch):
    import redis
    monkeypatch.setattr(redis.Redis, "from_url", forbidden)
    app = Celery("installed", broker="memory://", set_as_current=False)
    other = Celery("other", broker="memory://", set_as_current=False)
    original_scheduler = other.conf.beat_scheduler
    hb.install_process_heartbeat(app, configured())
    receiver_count = len(heartbeat_sent.receivers)
    for _ in range(5):
        hb.install_process_heartbeat(app, configured())
    assert len(heartbeat_sent.receivers) == receiver_count
    assert sum(s is hb.WorkerHeartbeatStep for s in app.steps["consumer"]) == 1
    assert hb.WorkerHeartbeatStep not in other.steps["consumer"]
    assert other.conf.beat_scheduler == original_scheduler
    reference = weakref.ref(app)
    app.close()
    del app
    gc.collect()
    assert reference() is None
    other.close()


@pytest.mark.parametrize("settings", [Settings(), configured(enabled=False, namespace="invalid/"),
                                     configured(namespace=None), configured(enabled="canary")])
def test_disabled_or_invalid_install_has_no_side_effects(settings, monkeypatch):
    import redis
    monkeypatch.setattr(redis.Redis, "from_url", forbidden)
    app = Celery("off", broker="memory://", set_as_current=False)
    original = app.conf.beat_scheduler
    receivers = len(heartbeat_sent.receivers)
    hb.install_process_heartbeat(app, settings)
    assert len(heartbeat_sent.receivers) == receivers
    assert hb.WorkerHeartbeatStep not in app.steps["consumer"]
    assert app.conf.beat_scheduler == original
    app.close()


def test_stop_serializes_inflight_write_and_rejects_queued_callback(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch)
    entered, release, stopping, stopped = (threading.Event() for _ in range(4))
    original = redis.set
    def blocked_set(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    redis.set = blocked_set
    publishing = threading.Thread(target=consumer.timer.fire)
    def stop():
        stopping.set()
        step.stop(consumer)
        stopped.set()
    shutdown = threading.Thread(target=stop)
    publishing.start()
    assert entered.wait(2)
    shutdown.start()
    assert stopping.wait(2)
    assert not stopped.wait(0.02)
    release.set()
    publishing.join(2)
    shutdown.join(2)
    assert stopped.is_set()
    consumer.timer.fire()
    assert len(redis.calls) == 1
    assert redis.closed == 1
    consumer.heart.stop()
    app.close()


class Store(dict):
    def __init__(self):
        super().__init__()
        self.events = []

    def sync(self):
        self.events.append("sync")

    def close(self):
        self.events.append("close")


def scheduler(monkeypatch, *, lazy=False, settings=None, max_interval=50):
    app = Celery("beat-test", broker="memory://", backend="cache+memory://", set_as_current=False)
    app.conf.update(beat_schedule={"synthetic": {"task": "synthetic.task", "schedule": 3600}},
                    result_expires=None)
    hb.install_process_heartbeat(app, settings or configured())
    clock, store = Clock(), Store()
    redis = FakeRedis(clock)
    monkeypatch.setattr(hb, "_client", lambda policy: redis)
    monkeypatch.setattr(hb.time, "time", clock)
    monkeypatch.setattr(PersistentScheduler, "_open_schedule", lambda self: store)
    monkeypatch.setattr(PersistentScheduler, "apply_async", forbidden)
    instance = hb.HeartbeatPersistentScheduler(app=app, lazy=lazy, max_interval=max_interval,
                                               schedule_filename="unused-synthetic-shelf")
    return app, instance, store, redis, clock


def test_persistent_scheduler_initialization_successful_tick_cap_and_close(monkeypatch):
    app, instance, store, redis, clock = scheduler(monkeypatch)
    assert isinstance(instance, PersistentScheduler)
    assert "synthetic" in store["entries"]
    assert redis.calls == []
    assert instance.max_interval == 2
    assert instance.tick() == 2
    assert redis.calls[0][1] == "vella:heartbeat:v1:test_ns:beat:test_b1"
    assert json.loads(redis.calls[0][2])["published_at"] == 100
    instance.close()
    assert store.events[-2:] == ["sync", "close"]
    assert redis.closed == 1
    instance.tick()
    assert len(redis.calls) == 1
    app.close()


def test_lazy_scheduler_does_not_start_publisher_or_open_store(monkeypatch):
    monkeypatch.setattr(hb.ProcessHeartbeatPublisher, "start", forbidden)
    app, instance, store, redis, clock = scheduler(monkeypatch, lazy=True)
    assert store == {}
    assert store.events == []
    assert redis.calls == []
    app.close()


def test_scheduler_preserves_smaller_interval_and_missing_beat_owner(monkeypatch):
    app, instance, store, redis, clock = scheduler(monkeypatch, max_interval=0.5)
    assert instance.tick() == 0.5
    instance.close()
    app.close()
    app, instance, store, redis, clock = scheduler(monkeypatch, settings=configured(beat_instance_id=None))
    instance.tick()
    assert redis.calls == []
    instance.close()
    app.close()


def test_failed_and_blocked_ticks_do_not_refresh_and_service_finally_closes(monkeypatch):
    app, instance, store, redis, clock = scheduler(monkeypatch)
    instance.tick()
    def failing(self, *args, **kwargs):
        raise RuntimeError("synthetic tick failure")
    with monkeypatch.context() as patch:
        patch.setattr(PersistentScheduler, "tick", failing)
        with pytest.raises(RuntimeError, match="synthetic tick failure"):
            instance.tick()
    assert len(redis.calls) == 1
    entered, release = threading.Event(), threading.Event()
    original = PersistentScheduler.tick
    def blocked(self, *args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(PersistentScheduler, "tick", blocked)
        thread = threading.Thread(target=instance.tick)
        thread.start()
        assert entered.wait(2)
        clock.value = 111
        result = read_process_freshness(configured(), client_factory=lambda p: redis, clock=clock)
        assert result["beat"]["stale"] == 1
        release.set()
        thread.join(2)
    assert len([c for c in redis.calls if c[0] == "SET"]) == 2
    service = Service(app=app)
    service.scheduler = instance
    with monkeypatch.context() as patch:
        patch.setattr(PersistentScheduler, "tick", failing)
        with pytest.raises(RuntimeError, match="synthetic tick failure"):
            service.start()
    assert store.events[-2:] == ["sync", "close"]
    assert redis.closed == 2
    app.close()


def test_scheduler_close_error_still_disarms_and_closes_client(monkeypatch):
    app, instance, store, redis, clock = scheduler(monkeypatch)
    instance.tick()
    def broken_sync():
        raise RuntimeError("synthetic sync failure")
    store.sync = broken_sync
    with pytest.raises(RuntimeError, match="synthetic sync failure"):
        instance.close()
    assert redis.closed == 1
    instance.tick()
    assert len(redis.calls) == 1
    app.close()


@pytest.mark.parametrize("mode,expected", [("missing", "degraded"), ("stale", "degraded"),
    ("fresh", "fresh"), ("unavailable", "unavailable"), ("invalid", "invalid_configuration")])
@pytest.mark.parametrize("dependency_ok", [True, False])
def test_health_keeps_dependency_status_and_adds_only_redacted_freshness(monkeypatch, mode, expected, dependency_ok):
    from app.infra import health
    clock, settings = Clock(), configured()
    redis = FakeRedis(clock)
    if mode in ("fresh", "stale"):
        for kind, identities in (("worker", ("test_w1", "test_w2")), ("beat", ("test_b1",))):
            for identity in identities:
                redis.values[f"vella:heartbeat:v1:test_ns:{kind}:{identity}"] = (
                    marker(kind=kind, instance_id=identity, published_at=89 if mode == "stale" else 100), 200)
    if mode == "unavailable":
        def fail(keys):
            raise RuntimeError("url-host-secret-canary")
        redis.mget = fail
    if mode == "invalid":
        settings = configured(namespace="secret-canary/")
    monkeypatch.setattr(health, "get_settings", lambda: settings)
    monkeypatch.setattr(hb, "_client", lambda policy: redis)
    monkeypatch.setattr(hb.time, "time", clock)
    def dependency():
        if not dependency_ok:
            raise RuntimeError("dependency-canary")
    result = health.readiness_status({"database": dependency})
    assert result["status"] == ("ready" if dependency_ok else "not_ready")
    assert result["checks"]["worker"] == expected
    assert result["checks"]["beat"] == expected
    assert result["freshness"]["status"] == expected
    assert all(call[0] == "MGET" for call in redis.calls)
    assert "canary" not in repr(result)
    assert "test_ns" not in repr(result)
    assert "test_w" not in repr(result)


def test_disabled_health_exact_payload_and_live_never_reads_or_writes(monkeypatch):
    from app.infra import health
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setattr(health, "get_settings", Settings)
    monkeypatch.setattr(hb, "_client", forbidden)
    assert health.readiness_status({"database": lambda: None}) == {
        "status": "ready", "checks": {"database": "ok", "worker": "not_monitored", "beat": "not_monitored"}}
    monkeypatch.setattr(main, "readiness_status", forbidden)
    assert TestClient(main.create_app()).get("/health/live").json() == {"status": "ok"}


def test_celery_factory_installs_app_scoped_hooks(monkeypatch):
    import importlib
    module = importlib.import_module("app.infra.celery_app")
    monkeypatch.setattr(module, "get_settings", configured)
    module.get_celery_app.cache_clear()
    try:
        app = module.get_celery_app()
        assert hb.WorkerHeartbeatStep in app.steps["consumer"]
        assert app.conf.beat_scheduler == "app.infra.heartbeat:HeartbeatPersistentScheduler"
    finally:
        module.get_celery_app.cache_clear()


def test_external_scheduler_override_is_preserved():
    app = Celery("custom", broker="memory://", set_as_current=False)
    app.conf.beat_scheduler = "synthetic.scheduler:CustomScheduler"
    hb.install_process_heartbeat(app, configured())
    assert app.conf.beat_scheduler == "synthetic.scheduler:CustomScheduler"
    assert hb.WorkerHeartbeatStep in app.steps["consumer"]
    app.close()


def test_redis_url_query_cannot_override_validated_socket_policy():
    settings = replace(configured(), redis_url="redis://synthetic.invalid/0?socket_timeout=99&socket_connect_timeout=98&retry_on_timeout=True")
    client = hb._client(hb.resolve_policy(settings).policy)
    try:
        options = client.connection_pool.connection_kwargs
        assert options["socket_timeout"] == 0.2
        assert options["socket_connect_timeout"] == 0.1
    finally:
        client.close()


def test_duplicate_marker_fields_are_rejected():
    clock, redis = Clock(), FakeRedis(Clock())
    value = marker()[:-1] + ', "kind":"worker"}'
    redis.values["vella:heartbeat:v1:test_ns:worker:test_w1"] = (value, 200)
    result = read_process_freshness(configured(), client_factory=lambda p: redis, clock=clock)
    assert result["worker"]["invalid"] == 1


def test_lazy_scheduler_positional_argument_does_not_arm(monkeypatch):
    app = Celery("lazy-positional", broker="memory://", set_as_current=False)
    hb.install_process_heartbeat(app, configured())
    monkeypatch.setattr(hb.ProcessHeartbeatPublisher, "start", forbidden)
    hb.HeartbeatPersistentScheduler(app, None, 50, None, True)
    app.close()


def test_real_blueprint_shutdown_disarms_before_heart_offline(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch)
    consumer.timer.fire()
    observations = []
    original = consumer.heart.stop
    def stop_heart():
        observations.append(redis.closed)
        original()
    consumer.heart.stop = stop_heart
    consumer.blueprint.restart(consumer, method="shutdown")
    assert observations == [1]
    assert len(redis.calls) == 1
    app.close()


def test_dead_worker_registration_does_not_retain_consumer_or_step(monkeypatch):
    app, consumer, step, redis = worker(monkeypatch)
    heart = consumer.heart
    consumer_ref, step_ref = weakref.ref(consumer), weakref.ref(step)
    del consumer, step
    gc.collect()
    assert consumer_ref() is None
    assert step_ref() is None
    heartbeat_sent.send(sender=heart)
    assert redis.calls == []
    heart.stop()
    app.close()


def test_reader_maximum_inventory_is_one_64_key_mget():
    settings = configured(expected_worker_ids=tuple(f"w{i}" for i in range(32)),
                          expected_beat_ids=tuple(f"b{i}" for i in range(32)))
    redis = FakeRedis(Clock())
    result = read_process_freshness(settings, client_factory=lambda p: redis)
    assert result["worker"]["missing"] == 32
    assert result["beat"]["missing"] == 32
    assert len(redis.calls) == 1
    assert redis.calls[0][0] == "MGET"
    assert len(redis.calls[0][1]) == 64


def test_new_settings_preserve_legacy_positional_app_name():
    settings = Settings("synthetic-app", "test", "/synthetic")
    assert (settings.app_name, settings.environment, settings.api_prefix) == (
        "synthetic-app", "test", "/synthetic",
    )


def test_oversized_marker_is_rejected_before_json_parsing(monkeypatch):
    redis = FakeRedis(Clock())
    redis.values["vella:heartbeat:v1:test_ns:worker:test_w1"] = (" " * 1025, 200)
    monkeypatch.setattr(hb.json, "loads", forbidden)
    result = read_process_freshness(configured(), client_factory=lambda p: redis)
    assert result["worker"]["invalid"] == 1
