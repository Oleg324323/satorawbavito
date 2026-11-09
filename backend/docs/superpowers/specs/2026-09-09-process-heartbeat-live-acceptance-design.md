# Process heartbeat: disposable process acceptance

2026-09-09, T1 local acceptance design. Existing heartbeat implementation696d4be
and synthetic/framework tests are not live Redis or subprocess lifecycle proof.
This slice adds that proof without enabling any deployment flag or business task.
No implementation/test result is claimed by this document.

## Scope and evidence

Use the existing `app.infra.heartbeat` publisher, installed consumer bootstep,
PersistentScheduler subclass and read-only aggregate helper unchanged. Current
source was read fully. Installed Celery/kombu/redis source supports Redis over
Unix sockets: kombu transport handles `socket://` host paths; redis-py supports
`unix://`; connection setup retains explicit connect/socket timeouts. Confirm the
actual broker URL through its parsed transport before starting processes.

Actual production Celery factory imports domain tasks and can populate legacy
schedules. Do not start it. A committed test-only launcher creates an isolated
Celery app with no application task imports, empty beat_schedule, explicit
`result_expires=None` (prevent default backend_cleanup entry), JSON-only content,
no result storage and a private queue. It calls the real heartbeat installer.
Thus this proves the real installed worker/beat owner lifecycle and wire, not
the production app's task registry, all worker modes or business readiness.

Choose actual subprocesses rather than invoking the publisher in the pytest
process. Existing in-process fake tests remain useful for precise callback races;
they are not rerouted to a live service. Start a solo worker, one persistent beat
and a separately owned Redis server only for this test. No provider job is ever
queued. Prefork/gevent/eventlet and deployment networking remain unverified.

## Isolation protocol

Allocate a fresh short private directory under /private/tmp with mode0700; validate
resolved ownership/path, short Unix socket length and absence before starting.
No use of an existing Redis socket/port, user env, service file or credentials.
Redis is foreground, port0, owned Unix socket0700, save disabled, appendonly no,
dir inside that directory, no daemon/log file configuration or existing config.
Read-only observed executable `/usr/local/bin/redis-server --version` reports
8.10.1; absence or process-start failure is an explicit failed gate, not a skip.

Parent and children use scrubbed env and worktree backend/.venv. The launcher
receives only own temp paths and synthetic nonsecret test policy, never .env.
Validate the absolute invocation path under the own `.venv/bin` together with
resolved `sys.prefix` equal to the own resolved `.venv`, distinct from
`sys.base_prefix`. Normal venv interpreter symlinks may resolve to the installed
base Python binary; that binary equality does not prove the environment owner.
Wrap every process in a plan-owned sandbox that denies all network and all
previously denied secret locations. Permit network-bind/inbound/outbound only
for this exact canonical Redis Unix socket. No PostgreSQL access is needed.
macOS shipped profiles demonstrate exact literal socket predicates for these
operations; verify parsing and actual denial probes before starting Redis.
Probe denied TCP/UDP and an owned synthetic .env canary, not real secret files.
Do not broaden to all local networking to make the harness pass.

The parent fixture records exact child PIDs/socket/directory and retains bounded
synthetic diagnostic output locally. It never reads another process's logs or
operational snapshots. No subprocess command imports the main production app.
Set explicit synthetic node names to avoid hostname-based identity claims.

## Synthetic policy and observations

Use explicitly test-local values, not config defaults or production recommendations:
one expected worker and one beat, unique synthetic namespace, max age4seconds,
retention12seconds, worker heartbeat1second, beat cap0.5seconds, connect/socket
timeouts0.2seconds each and explicit retry0. These are fixtures only; adjust only
if a measured test timing issue requires a documented test-policy amendment.

1. Before owners start, real helper returns two missing slots and writes nothing.
2. Start beat alone: its real successful ticks create a beat marker; worker stays
   missing. Start solo worker: its periodic real Heart creates the worker marker.
   Repeated reads eventually see both fresh and publication timestamps advance.
   Verify positive TTL bounded by12seconds; never infer TTL solely from JSON.
3. Demonstrate liveness differs from progress: SIGSTOP only the exact owned beat
   subprocess, wait using a monotonic bounded polling deadline until its marker
   is stale while PID still exists and worker remains fresh. Finally SIGCONT it
   and observe fresh/new publication without replacing the Redis key manually.
   The fixture must resume a paused child before shutdown even on assertion failure.
4. Stop worker through normal SIGTERM warm shutdown; wait for successful natural
   process exit, then record last marker. With no worker, repeated reads must not
   extend TTL or alter the marker; observe stale then Redis-expired missing.
   Start worker again with same logical identity and verify a new incarnation and
   fresh marker. Restart is test-owned lifecycle, not an exclusivity claim.
5. Stop beat normally and observe no further refresh after natural exit. With
   both owners stopped, repeated real helper reads perform MGET but no SET: use
   this own Redis's commandstats before/after, not only a fake client. No API-side
   publication can satisfy missing owners. Existing health tests independently
   bind this same helper to readiness response behavior.
6. A separate isolated worker with `without_heartbeat` never creates its expected
   marker although process runs. Disabled heartbeat setup leaves Redis key absent.
   No online/offline signal is interpreted as a fresh periodic owner loop.
7. Shut down the exact owned Redis through its own socket with NOSAVE. The real
   reader returns safe unavailable, without URL/key/namespace/exception reflection.

No sleeps or blocking waits over60seconds. Polling uses a monotonic deadline,
short wait and specific observed predicate; missing progress fails with bounded
safe diagnostics, never extends a deadline indefinitely. Tests may take longer
than fake tests because real TTL expiry is a required observation.

## Cleanup and failure semantics

Each fixture finally resumes only its paused owned PIDs, requests normal worker/
beat shutdown and Redis NOSAVE shutdown, then reaps them. Success requires natural
completion and expected exit codes, socket absent and no live owned child. Do not
turn kill/timeout into successful shutdown evidence. If cleanup fails, report it
as a failed gate; emergency termination may affect only the recorded test-owned
PID and must remain disclosed, never masked by green test counts. Do not restart
or signal a working user service. Remove only the validated temporary directory
after owned processes are gone, with Python TemporaryDirectory's scoped cleanup.
No broad shell delete, environment mutation or persistent Redis data.

## Separate required gate

The live module is `tests/process_heartbeat_live_gate.py`, deliberately separate
from automatic unit-test filename discovery. It runs explicitly through the owned
sandbox launcher. Full unit/DB pytest may already run under a stricter unrelated
network sandbox, which a child cannot broaden to bind a new Redis socket. Do not
add skips, collection-ignore hooks or weaken that outer sandbox. Release CI must
invoke this additional live gate explicitly before claiming combined readiness;
ordinary pytest alone does not include or prove it.

## Change boundaries and rollout

Initial change is test launcher, live acceptance module and handoff only. If it
reproduces a real production-code defect, retain RED evidence and request a
bounded controller plan amendment before fixing that source. Do not weaken
assertions, add skips, force worker heartbeat directly from test/API or change
default flags. Ruff existing heartbeat source debt is not silently refactored.

Required evidence: exact commands/exits/counts, process modes/versions, live
fresh→stale→missing/restart observations, key TTL and command counters, safe
unavailable result, sandbox probes and exact cleanup. Existing focused heartbeat/
health/scheduler gates still pass. No whole-suite, broker task transport, result
backend, production identity/topology, DNS deadline, cross-host clock, leader
fencing or scheduler source-diff proof is implied. Production policy remains
explicitly owner-controlled and default-off.
