"""Default-off owner-published process freshness, independent of readiness."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field

from celery import Celery, bootsteps
from celery.beat import PersistentScheduler
from celery.signals import heartbeat_sent
from celery.worker.consumer.heart import Heart as ConsumerHeart
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import Settings

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_INCARNATION = re.compile(r"[0-9a-f]{32}", re.ASCII)
_MARKER_FIELDS = {"v", "namespace", "kind", "instance_id", "incarnation", "published_at"}
_MAX_MARKER_BYTES = 1024


@dataclass(frozen=True)
class Policy:
    namespace: str
    expected_worker_ids: tuple[str, ...]
    expected_beat_ids: tuple[str, ...]
    max_age_seconds: float
    retention_seconds: int
    beat_max_interval_seconds: float
    redis_connect_timeout_seconds: float
    redis_socket_timeout_seconds: float
    redis_retry_attempts: int
    redis_url: str = field(repr=False)

    def ids(self, kind):
        return self.expected_worker_ids if kind == "worker" else self.expected_beat_ids


@dataclass(frozen=True)
class PolicyResolution:
    status: str
    policy: Policy | None = None
    error_code: str | None = None


def _identifier(value):
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def resolve_policy(settings: Settings, *, owner_kind: str | None = None) -> PolicyResolution:
    enabled = settings.process_heartbeat_enabled
    if enabled is False:
        return PolicyResolution("disabled")
    invalid = PolicyResolution("invalid_configuration", error_code="invalid_configuration")
    if enabled is not True:
        return invalid
    values = {name: getattr(settings, "process_heartbeat_" + name)
              for name in Policy.__dataclass_fields__ if name != "redis_url"}
    if not _identifier(values["namespace"]):
        return invalid
    for name in ("expected_worker_ids", "expected_beat_ids"):
        ids = values[name]
        if (type(ids) is not tuple or not 1 <= len(ids) <= 32
                or any(not _identifier(item) for item in ids) or len(set(ids)) != len(ids)):
            return invalid
    for name in ("max_age_seconds", "beat_max_interval_seconds",
                 "redis_connect_timeout_seconds", "redis_socket_timeout_seconds"):
        if not _finite_number(values[name]) or values[name] <= 0:
            return invalid
    age, retention = values["max_age_seconds"], values["retention_seconds"]
    if (type(retention) is not int or retention <= age
            or values["beat_max_interval_seconds"] >= age
            or values["redis_connect_timeout_seconds"] + values["redis_socket_timeout_seconds"] >= age
            or type(values["redis_retry_attempts"]) is not int or values["redis_retry_attempts"] != 0):
        return invalid
    if owner_kind is not None:
        if owner_kind not in ("worker", "beat"):
            return invalid
        instance_id = getattr(settings, "process_heartbeat_" + owner_kind + "_instance_id")
        if not _identifier(instance_id) or instance_id not in values["expected_" + owner_kind + "_ids"]:
            return invalid
    return PolicyResolution("enabled", Policy(**values, redis_url=settings.redis_url))


def _client(policy):
    # Dedicated client: socket bounds do not establish an end-to-end DNS deadline.
    options = dict(socket_connect_timeout=policy.redis_connect_timeout_seconds,
                   socket_timeout=policy.redis_socket_timeout_seconds,
                   retry=Retry(NoBackoff(), 0), decode_responses=False)
    client = Redis.from_url(policy.redis_url, **options)
    # redis-py gives URL query parameters precedence. Reapply our validated
    # policy to this new, unconnected, privately owned pool before any command.
    client.connection_pool.connection_kwargs.update(options)
    return client


def _close(client):
    if client is not None:
        try:
            client.close()
        except Exception:
            pass  # Never log a Redis exception containing connection details.


def _key(policy, kind, instance_id):
    return f"vella:heartbeat:v1:{policy.namespace}:{kind}:{instance_id}"


class ProcessHeartbeatPublisher:
    def __init__(self, policy, kind, instance_id, *, client_factory=None, clock=None, pid=None):
        self.policy, self.kind, self.instance_id = policy, kind, instance_id
        self._factory, self._clock = client_factory or _client, clock or time.time
        self._pid = pid or (lambda: os.getpid())
        self._lock = threading.RLock()
        self._active = False
        self._client = None

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            if self.kind not in ("worker", "beat") or self.instance_id not in self.policy.ids(self.kind):
                return
            self._owner_pid = self._pid()
            self._incarnation = uuid.uuid4().hex
            self._active = True

    def publish(self) -> bool:
        with self._lock:
            if not self._active or self._owner_pid != self._pid():
                return False
            try:
                now = self._clock()
                if not _finite_number(now):
                    return False
                if self._client is None:
                    self._client = self._factory(self.policy)
                value = json.dumps(dict(v=1, namespace=self.policy.namespace, kind=self.kind,
                                        instance_id=self.instance_id, incarnation=self._incarnation,
                                        published_at=now), separators=(",", ":"), allow_nan=False)
                return self._client.set(_key(self.policy, self.kind, self.instance_id), value,
                                        ex=self.policy.retention_seconds) is True
            except Exception:
                return False

    def _disarm(self):
        with self._lock:
            self._active = False

    def stop(self) -> None:
        # An in-flight SET may complete before this lock is acquired. After stop
        # returns, previously queued callbacks cannot issue another SET.
        with self._lock:
            self._active = False
            client, self._client = self._client, None
            _close(client)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_marker")
        result[key] = value
    return result


def _marker_state(value, policy, kind, instance_id, now):
    if value is None:
        return "missing"
    if type(value) not in (str, bytes) or len(value) > _MAX_MARKER_BYTES:
        return "invalid"
    try:
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_MARKER_BYTES:
            return "invalid"
        marker = json.loads(value, object_pairs_hook=_unique_object)
        if (type(marker) is not dict or set(marker) != _MARKER_FIELDS
                or type(marker["v"]) is not int or marker["v"] != 1
                or marker["namespace"] != policy.namespace or marker["kind"] != kind
                or marker["instance_id"] != instance_id
                or type(marker["incarnation"]) is not str
                or not _INCARNATION.fullmatch(marker["incarnation"])
                or not _finite_number(marker["published_at"]) or not _finite_number(now)):
            return "invalid"
        age = now - marker["published_at"]
        if age < 0:
            return "invalid"
        return "fresh" if age < policy.max_age_seconds else "stale"
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return "invalid"


def read_process_freshness(settings, *, client_factory=None, clock=None):
    resolution = resolve_policy(settings)
    if resolution.status != "enabled":
        return {"status": resolution.status}
    policy = resolution.policy
    slots = [(kind, identity) for kind in ("worker", "beat") for identity in policy.ids(kind)]
    client = None
    try:
        client = (client_factory or _client)(policy)
        values = client.mget([_key(policy, kind, identity) for kind, identity in slots])
        if not isinstance(values, (list, tuple)) or len(values) != len(slots):
            return {"status": "unavailable"}
        now = (clock or time.time)()
        result = {"status": "fresh"}
        for kind in ("worker", "beat"):
            result[kind] = dict(expected=len(policy.ids(kind)), fresh=0, stale=0, missing=0, invalid=0)
        for (kind, identity), value in zip(slots, values):
            state = _marker_state(value, policy, kind, identity, now)
            result[kind][state] += 1
            if state != "fresh":
                result["status"] = "degraded"
        return result
    except Exception:
        return {"status": "unavailable"}
    finally:
        _close(client)


_apps = weakref.WeakKeyDictionary()
_owners = weakref.WeakKeyDictionary()
_registration_lock = threading.RLock()
_RECEIVER_UID = "vella.process-heartbeat.v1"


def _heartbeat_sent(sender=None, **kwargs):
    with _registration_lock:
        owner_ref = _owners.get(sender)
    owner = owner_ref() if owner_ref is not None else None
    if owner is not None:
        owner._heartbeat(sender)


def install_process_heartbeat(app: Celery, settings: Settings) -> None:
    if resolve_policy(settings).status != "enabled":
        return
    with _registration_lock:
        if app in _apps:
            return
        _apps[app] = settings
        # Heart caches receiver presence at construction. Keep this single
        # module receiver alive across individual consumer reconnects/stops.
        heartbeat_sent.connect(_heartbeat_sent, weak=False, dispatch_uid=_RECEIVER_UID)
        app.steps["consumer"].add(WorkerHeartbeatStep)
        if app.conf.beat_scheduler in ("celery.beat:PersistentScheduler", PersistentScheduler):
            app.conf.beat_scheduler = "app.infra.heartbeat:HeartbeatPersistentScheduler"


class WorkerHeartbeatStep(bootsteps.StartStopStep):
    requires = (ConsumerHeart,)

    def __init__(self, consumer, **kwargs):
        super().__init__(consumer, **kwargs)
        self._lock = threading.RLock()
        self._publisher = None
        self._heart_ref = None

    def start(self, consumer) -> None:
        with self._lock:
            self.stop(consumer)
            with _registration_lock:
                settings = _apps.get(consumer.app)
            if settings is None:
                return
            resolution = resolve_policy(settings, owner_kind="worker")
            heart = consumer.heart
            if (resolution.status != "enabled" or heart is None
                    or not _finite_number(heart.interval)
                    or not 0 < heart.interval < resolution.policy.max_age_seconds):
                return
            self._consumer_ref = weakref.ref(consumer)
            self._app_ref = weakref.ref(consumer.app)
            self._heart_ref = weakref.ref(heart)
            self._owner_pid = os.getpid()
            self._publisher = ProcessHeartbeatPublisher(
                resolution.policy, "worker", settings.process_heartbeat_worker_instance_id)
            self._publisher.start()
            with _registration_lock:
                _owners[heart] = weakref.ref(self)

    def _heartbeat(self, heart):
        with self._lock:
            if self._publisher is None or os.getpid() != self._owner_pid:
                return
            consumer = self._consumer_ref()
            if (consumer is None or consumer.app is not self._app_ref()
                    or heart is not self._heart_ref() or consumer.heart is not heart
                    or heart.tref is None or consumer.blueprint.state != bootsteps.RUN):
                return
            self._publisher.publish()

    def stop(self, consumer) -> None:
        with self._lock:
            heart = self._heart_ref() if self._heart_ref is not None else None
            if heart is not None:
                with _registration_lock:
                    reference = _owners.get(heart)
                    if reference is not None and reference() is self:
                        del _owners[heart]
            self._heart_ref = None
            publisher, self._publisher = self._publisher, None
            if publisher is not None:
                publisher.stop()

    def close(self, consumer) -> None:
        self.stop(consumer)

    def shutdown(self, consumer) -> None:
        self.stop(consumer)

    def terminate(self, consumer) -> None:
        self.stop(consumer)


class HeartbeatPersistentScheduler(PersistentScheduler):
    def __init__(self, app, schedule=None, max_interval=None, Producer=None,
                 lazy=False, sync_every_tasks=None, **kwargs):
        self._heartbeat_publisher = None
        super().__init__(app, schedule=schedule, max_interval=max_interval, Producer=Producer,
                         lazy=lazy, sync_every_tasks=sync_every_tasks, **kwargs)
        with _registration_lock:
            settings = _apps.get(self.app)
        if settings is None:
            return
        resolution = resolve_policy(settings, owner_kind="beat")
        if resolution.status != "enabled":
            return
        self.max_interval = min(self.max_interval, resolution.policy.beat_max_interval_seconds)
        if not lazy:
            self._heartbeat_publisher = ProcessHeartbeatPublisher(
                resolution.policy, "beat", settings.process_heartbeat_beat_instance_id)
            self._heartbeat_publisher.start()

    def tick(self, *args, **kwargs):
        interval = super().tick(*args, **kwargs)
        publisher = self._heartbeat_publisher
        if publisher is not None:
            publisher.publish()
            return min(interval, publisher.policy.beat_max_interval_seconds)
        return interval

    def close(self):
        publisher = self._heartbeat_publisher
        if publisher is not None:
            publisher._disarm()
        try:
            return super().close()
        finally:
            if publisher is not None:
                publisher.stop()
