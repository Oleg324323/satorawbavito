# T1 Stage 5 heartbeat design — local, default off

2026-09-09. Design only; implementation and tests have not been run by this
design pass. Scope is `backend/app/infra/heartbeat.py`,
`backend/tests/test_process_heartbeat.py`, and necessary changes in
`backend/app/config.py`, `backend/app/infra/{celery_app,health}.py`.
No task modules, provider clients, schedules, secrets, deployment or services.

## Choice and alternatives

1. **Selected: app-scoped consumer bootstep + existing Heart signal; beat
   PersistentScheduler subclass.** Lifecycle ownership is explicit, publishing
   is driven by existing worker heartbeats and completed scheduler ticks, and
   persistence remains Celery-owned. Requires tests against installed Celery
   lifecycle behavior, especially cached signal receiver detection and offline
   emission. No extra timer or task is introduced.
2. Worker-ready/shutdown signals alone plus a beat-init tick wrapper: fewer
   classes, but worker shutdown is too late for the offline heartbeat, ready
   fires only once across consumer restarts, and wrapping instance methods
   complicates cleanup. Reject.
3. Replace the worker Heart implementation and wrap the beat service loop:
   precise event discrimination, but unnecessarily duplicates framework lifecycle
   internals and expands compatibility risk. Reject for this bounded change.

## Source evidence

Installed source is under `backend/.venv/lib/python3.13/site-packages`.

- `celery/worker/heartbeat.py:24-61`: Heart caches receiver presence in its
  constructor. `_send` emits the same signal without an event argument for
  online, periodic and offline events. `start` sends online before assigning
  `tref`; `stop` cancels and clears `tref` before sending offline.
- `celery/worker/consumer/heart.py:19-38`: the built-in consumer Heart step honors
  `without_heartbeat`; start constructs/starts Heart and stop/shutdown stops it.
- `celery/bootsteps.py:128-177`: close invokes step close methods; stop/shutdown
  ordering uses reverse dependency order. A dependent fencing step stops before
  the Heart dependency. Implement all relevant lifecycle methods explicitly;
  do not rely only on the worker shutdown signal.
- `celery/worker/consumer/consumer.py:427-430`: ready callback is cleared after
  first invocation, so worker_ready alone cannot manage reconnect generations.
  Evloop is `last=True` and enters its loop from start at lines 777-796.
- `celery/worker/worker.py:244-252`: worker shutdown signal follows shutdown work.
- `celery/beat.py:603-677`: PersistentScheduler.close syncs and closes its store;
  Service repeatedly calls tick, sleeps its returned interval, and closes the
  scheduler in finally. Default scheduler is PersistentScheduler.
- `app/infra/redis_client.py`: existing helper has bounded sockets but does not
  override retries. Installed `redis/client.py:285` supplies a retry default.
  Use a dedicated lazy heartbeat client; do not change this shared helper.
- `app/infra/health.py`: readiness currently measures DB/Redis dependencies and
  appends worker/beat `not_monitored`. Liveness is a separate existing route.

## Explicit configuration contract

New Settings fields/env names use prefix `process_heartbeat_` /
`VELLA_PROCESS_HEARTBEAT_`:

| Settings suffix | Disabled default | Enabled requirement |
| --- | --- | --- |
| enabled / ENABLED | false | strict true/false parsing; malformed is configuration error |
| namespace / NAMESPACE | None | explicit nonsecret bounded identifier |
| worker_instance_id / WORKER_INSTANCE_ID | None | required only in worker owner; member of expected workers |
| beat_instance_id / BEAT_INSTANCE_ID | None | required only in beat owner; member of expected beats |
| expected_worker_ids / EXPECTED_WORKER_IDS | () | explicit nonempty unique tuple |
| expected_beat_ids / EXPECTED_BEAT_IDS | () | explicit nonempty unique tuple |
| max_age_seconds / MAX_AGE_SECONDS | None | positive finite number |
| retention_seconds / RETENTION_SECONDS | None | positive integer strictly greater than max age |
| beat_max_interval_seconds / BEAT_MAX_INTERVAL_SECONDS | None | positive finite number strictly less than max age |
| redis_connect_timeout_seconds / REDIS_CONNECT_TIMEOUT_SECONDS | None | positive finite number |
| redis_socket_timeout_seconds / REDIS_SOCKET_TIMEOUT_SECONDS | None | positive finite number |
| redis_retry_attempts / REDIS_RETRY_ATTEMPTS | None | explicit integer zero, the only supported policy in this bounded implementation |

Zero retry support is a deliberate bounded implementation constraint, not a
production value silently supplied on the user's behalf. An owner must explicitly
select that policy to enable this implementation. Retries beyond zero require a
separate total-deadline design. No production TTL, polling interval, socket timeout,
namespace or process identity is selected in code, env files or this report.
Use the existing configured `settings.redis_url` for shared storage; do not add
another credential source. Socket timeouts must fit within the configured max age;
validate their sum is less than max age. Do not claim a hard end-to-end bound on
DNS from socket timeouts alone; rollout must account for DNS/service topology.

Use ASCII `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` for namespace and identity; reject
duplicates, empty tokens and unsupported characters rather than repairing them.
At most 32 identities per kind, bounding reader keys to 64. These are technical
input limits, not a default deployment topology. Never derive an ID from hostname,
PID, Celery node name or environment. PID is only a local ownership fence.

Parsing must retain invalid-enabled/configuration state with a fixed internal
error code; do not turn a malformed true flag or policy into apparently healthy
disabled state. A literal disabled flag short-circuits all validation requiring
deployment values and has no Redis/client/signal/scheduler side effects.
API validation needs expected IDs and policy but not its own publisher identity.
Publisher role validation additionally requires that role's explicit instance ID.
Invalid policy causes no publication or Redis construction; reader reports
`invalid_configuration`. No raw invalid values enter health or exceptions/logs.

## Helper interfaces and storage

Suggested exact public signatures (internal decomposition may stay smaller):

```python
def resolve_policy(settings: Settings, *, owner_kind: str | None = None) -> PolicyResolution: ...
def read_process_freshness(settings: Settings, *, client_factory=None, clock=None) -> dict[str, object]: ...
def install_process_heartbeat(app: Celery, settings: Settings) -> None: ...

class ProcessHeartbeatPublisher:
    def __init__(self, policy, kind, instance_id, *, client_factory=None, clock=None, pid=None): ...
    def start(self) -> None: ...
    def publish(self) -> bool: ...
    def stop(self) -> None: ...

class WorkerHeartbeatStep(bootsteps.StartStopStep):
    requires = (celery.worker.consumer.heart.Heart,)
    def start(self, consumer) -> None: ...
    def close(self, consumer) -> None: ...
    def stop(self, consumer) -> None: ...
    def shutdown(self, consumer) -> None: ...
    def terminate(self, consumer) -> None: ...

class HeartbeatPersistentScheduler(PersistentScheduler):
    def tick(self, *args, **kwargs): ...
    def close(self): ...
```

Heartbeat client creation is lazy, uses explicit `Retry(NoBackoff(), 0)` and the
validated socket settings, and emits no raw errors. No connection during import,
app construction, receiver installation or disabled health. Client injection is
the only test I/O path. Owned clients/pools are closed locally on owner stop;
no Redis DELETE/cleanup command is needed, so shutdown cannot delete a successor.
API reader uses one client/one bounded MGET per read and closes its owned local
client. Do not issue a GET per identity or scan an inventory.

One bounded Redis key per expected logical process identity:
`vella:heartbeat:v1:<namespace>:<kind>:<instance_id>`.
SET with expiry `EX retention_seconds`; no registry, SCAN or permanent tombstone.
Compact JSON payload, bounded (e.g. 1 KiB maximum):

```json
{"v":1,"namespace":"test_ns","kind":"worker","instance_id":"test_w1","incarnation":"<uuid4 hex>","published_at":1234567890.0}
```

Examples are synthetic wire examples, never deployment defaults. Incarnation is
generated on owner lifecycle start in the actual process, not at import/fork
parent. Validate every field, strict types (booleans are not numbers), finite
timestamp, expected namespace/kind/identity and incarnation shape before freshness.
Do not echo invalid payloads. Bound the parse size and reject unrecognized shape.
Use an injected epoch clock for tests. Clock rollback/future timestamp is invalid,
not fresh. Cross-host clock synchronization is an explicit operational assumption;
no tolerance is invented here.

For each exact expected key: no value => `missing`; malformed => `invalid`;
`0 <= age < max_age_seconds` => `fresh`; otherwise => `stale`. Retention creates
a real stale interval before expiry becomes missing. After expiry it is impossible
to distinguish never-started from previously-running without another durable
record, and this feature intentionally does not create one. Redis failure =>
`unavailable`, never a stale cached success. Wrong namespace or another identity
cannot satisfy the expected slot. A valid but older incarnation can overwrite a
newer overlapping owner in the SAME logical slot: this reports last observed
publication for that identity, not exclusive leadership, process enumeration,
deployment-generation fencing or proof that a particular replica is current.
Operators must assign distinct expected identities when replicas must be checked
independently. Incarnation in payload does not itself prevent overlapping owners.

## Lifecycle details

`install_process_heartbeat` is called in the Celery factory for this app only.
When explicitly enabled and valid, install one idempotent module-level
heartbeat_sent receiver before any Heart is constructed; retain it strongly with
a stable dispatch_uid. Do not disconnect it on an individual consumer stop:
Heart caches receiver presence, including across later consumer restarts.
Use weak owner registration (not ever-growing app/consumer references); registration
is local bookkeeping only. An unregistered app/Heart/PID can never publish.
Only configured apps get the consumer step and monitored scheduler setting.

The step starts after built-in Heart.start. It validates app identity by object
identity, captures this exact `consumer.heart`, own PID, owner kind and ID, and
arms publication only if a Heart actually exists and its positive finite interval
is less than max age. Heart disabled => no arm and no synthetic writes. Periodic
receiver requires all captured ownership checks, running owner state, matching
current `consumer.heart`, and `heart.tref is not None`. This rejects online (tref
not assigned), offline (tref cleared), unrelated app/Heart, old reconnect Heart,
and fork-inherited callbacks. The lifecycle step never publishes on start/ready.

close/stop/shutdown/terminate disarm and remove owner mapping idempotently; the
dependency graph orders stop before built-in Heart.stop. Serialize local publish
and stop with a lock so stop returning cannot be followed by a new SET from an
already queued callback; document an in-flight operation may finish before stop
returns. Also check the consumer blueprint is RUN when appropriate to reject
callbacks after partially failed startup. Do not equate this signal with task
processing readiness; it demonstrates the worker's heartbeat loop progressed.

Beat subclass retains PersistentScheduler initialization/setup/sync/store behavior.
Set max_interval only from explicit enabled policy (cap existing max_interval),
preserving schedule contents and due times. Call `super().tick()` first; only after
successful completion publish from the scheduler owner, then return the smaller
of its returned delay and explicit beat cap. If tick blocks or raises, the prior
marker ages out. Lazy scheduler instances used for inspection must not publish or
create an active publisher. close disarms before invoking `super().close()` and
closes its own client in finally; base persistence errors still propagate. No
independent timer, beat-init publication, new schedule entry or keyring call.
If the user overrides beat scheduler externally, this instrumentation is absent
and the expected beat remains missing/stale; do not silently instrument another
scheduler or claim coverage.

## Health wire and compatibility

Preserve the complete legacy readiness payload when disabled, including worker
and beat `not_monitored`. Existing dependency-derived readiness status and HTTP
code remain the same. When enabled add a separate top-level `freshness` object;
do not imply that this is business-job freshness or make heartbeat success erase
a failed readiness dependency. Suggested redacted wire:

```json
{"status":"ready","checks":{"database":"ok","redis":"ok","celeryBroker":"ok","celeryResultBackend":"ok","worker":"degraded","beat":"fresh"},"freshness":{"status":"degraded","worker":{"expected":2,"fresh":1,"stale":1,"missing":0,"invalid":0},"beat":{"expected":1,"fresh":1,"stale":0,"missing":0,"invalid":0}}}
```

Enabled checks.worker/checks.beat are set to the same fixed role
aggregate state (`fresh`, `degraded`, `unavailable`, `invalid_configuration`) while
still remaining outside the dependency-readiness decision. Choose this enabled
representation consistently in tests; disabled behavior stays byte-for-byte
equivalent. No raw instance IDs, namespace, key names, hostnames, URLs, incarnation,
timestamp, payload or exception appears in health. `freshness.status` is `fresh`
only if every expected member is fresh; otherwise `degraded`, `unavailable` or
`invalid_configuration`. Standalone helper returns `disabled` without I/O when
disabled. The live route stays dependency-free without importing a publisher.

## RED-first verification plan

Use synthetic settings/clock/Redis fake; no network, DB, providers or live Celery
process. Run commands with the existing OS profile `offline-local.sb` (`deny
network*`), scrubbed env and this backend's own .venv. Implementation report must
record exact commands, RED results, final results and known baseline failures.

1. Policy: disabled/no side effects; missing and malformed required fields,
   duplicate/oversized IDs, wrong publisher role/ID, invalid enabled flag,
   bool/nan/infinity/type edge cases; no lazy client on invalid policy.
2. Writer/reader: exact one SET+EX from owner only; expected-key-only one MGET;
   no API SET; stale then expiry/missing; malformed/oversized marker, namespace
   and identity mismatch, other worker cannot satisfy missing expected one;
   future timestamps, unavailable Redis, canary raw-error/payload redaction.
3. Real installed Heart with fake timer/eventer: receiver connected before its
   construction; online no SET, periodic SET, offline no SET; without-heartbeat
   no SET; wrong app/Heart/PID ignored; sender callback after close ignored.
   Assert real bootstep start/close/stop/shutdown ordering, reconnect Heart
   replacement, repeated idempotent installation and no growing registrations.
   Include offline emission when lifecycle receiver would otherwise still be
   armed (tref gate), and queued/in-flight callback shutdown ordering.
4. Real PersistentScheduler subclass with synthetic schedule and patched storage
   or a temporary shelf: completed tick publishes only beat; no init/lazy write;
   blocked/failed tick cannot refresh; configured cap keeps valid ticks observable;
   close calls base sync/store-close and blocks all later publication. Test real
   Service loop/finally with fake scheduler/time so close cleanup is evidenced.
   No provider task is dispatched; patch its send path where necessary.
5. Focused new suite, then hermetic inspected existing health/config/scheduler
   tests; preserve off-state payload and schedules. No blanket full-suite claim.

## Critic pass and limits

Local preflight-critic/principal-software-engineer skills were not found at the
specified paths or in searched skill/plugin roots. Performed an isolated source
and requirements review instead. Corrected lifecycle design to include cached
receiver installation, direct offline tref guard, weak app/owner bookkeeping,
reconnect semantics, persistent close and dedicated no-retry bounded MGET client.

No genuine owner blocker prevents local default-off implementation. Production
enablement is intentionally blocked on explicit operator policy and topology:
expected distinct identities, TTL/max age, beat interval, socket deadlines and
explicit supported retry choice. This design does not choose them. No claim of
leader fencing, real Redis TTL integration, all worker modes, hard DNS deadline,
production readiness, business job completion, broker backlog or clock-skew
tolerance. Required final testing evidence still belongs to implementation.
