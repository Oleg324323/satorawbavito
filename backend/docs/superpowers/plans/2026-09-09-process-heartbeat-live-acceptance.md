# Process heartbeat disposable acceptance plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** Prove real worker/beat self-publication, TTL aging and clean shutdown on
an exclusively owned Unix-socket Redis; no deployment or business tasks.

**Architecture:** Test-only isolated Celery launcher runs existing heartbeat hooks
in actual subprocesses; a scrubbed fixture owns Redis and all process lifecycles.

**Tech Stack:** Installed Python/pytest/Celery/kombu/redis-py, local Redis8.10.1,
macOS sandbox-exec and private Unix sockets; no dependency changes.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-process-heartbeat-live-acceptance-design.md`, read fully.

## Global constraints

- Only T1 worktree/branch. No production, working DB/Redis, providers, secretfiles,
  .env values, GitHub/network, Docker/deploy/push, flags, installs or host changes.
- Own backend/.venv; approved alternate Ruff interpreter lint-only. Redis binary
  is read-only existing installation, never another project's Python runtime.
- Exact own private Unix socket only; deny all IP and secret reads in parent and
  children. No importing `app.infra.celery_app` or domain task modules in launchers.
- No production-code modification in this initial slice. Preserve all old tests;
  no skip or baseline expansion. Real defect requires controller amendment first.
- Child exit/cleanup failure fails the gate. Never label kill/timeout as natural
  shutdown; never signal a PID not captured from this fixture's own subprocess.
- This separate live gate is explicitly invoked, not automatically collected by
  ordinary test_*.py unit/DB runs under their incompatible network sandboxes.
  No skips/collect-ignore or broadening outer isolation. Release CI must invoke
  the additional launcher command before claiming combined readiness.
- Production heartbeat/scheduler policy remains disabled and unspecified. All
  timing/identity settings in this plan are synthetic test values only.

## Task 1: Add real process and Redis acceptance

**Files:**
- Create `backend/tests/_heartbeat_process_fixture.py` (committed test-only launcher).
- Create `backend/tests/process_heartbeat_live_gate.py` (isolated service fixture and acceptance).
- Create `backend/docs/superpowers/reports/2026-09-09-t1-heartbeat-live-handoff.md`.

**Consumes:** actual `app/infra/heartbeat.py`, config Settings, existing fake/
framework tests, installed Celery/Redis/kombu lifecycle. Read these relevant
sources before actions; do not execute app factory or legacy schedules.

**Produces:** reproducible live gate only, not a new heartbeat implementation.

- [ ] Step1 implement narrowly owned harness using exact spec isolation. Validate
  service binary/version and socket directory; inspect actual broker URL parsing
  before connection. Create per-run sandbox profile, prove owned synthetic .env
  and TCP/UDP denial and allowed exact Unix socket. No ambient env or plugins.

Launcher `verify` mode uses only stdlib before spawning the test: allocate the
private root/profile, then invoke the following argv with env-i-equivalent
explicit env. Tests refuse direct execution without that launcher-provided root;
they do not skip. Validate the absolute invocation path under this worktree's
`.venv/bin`, and require resolved `sys.prefix` to equal this worktree's resolved
`.venv` and differ from `sys.base_prefix`. Do not require the interpreter binary's
symlink target to remain inside `.venv`: the existing isolated venv legitimately
links to the installed Python binary. A shared binary target alone never admits
another project's environment.

```python
argv = ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable,
        "-m", "pytest", "-q", "-s", "tests/process_heartbeat_live_gate.py", "--tb=short"]
env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": "/tmp",
       "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
       "SATORNA_TEST_HEARTBEAT_OWNED_ROOT": str(root)}
# root/profile are newly allocated by verify mode, never user-selected existing
# services. The test checks realpath/mode/marker and sandbox denials before I/O.
```

All service/reader I/O then occurs in that sandboxed pytest process or its
inheriting children. `verify` never connects to Redis itself. On unsuccessful
child cleanup retain exact own artifacts and report failure; do not delete a
directory that still contains a live owned service's socket.
- [ ] Step2 launcher constructs isolated app with no domain tasks and empty beat
  schedule, result_expires=None, explicit synthetic names/queue/JSON. Install
  real hook; launch solo worker or real beat with owned shelf path. Assert empty
  schedule before running, no cleanup/default task entry. Control uses only
  parent-owned process handles, not a remotely exposed test command endpoint.

```python
app = Celery("synthetic-heartbeat", broker="redis+socket://" + socket_path,
             set_as_current=False)
app.conf.update(beat_schedule={}, result_expires=None,
                task_default_queue="synthetic-heartbeat-empty",
                task_serializer="json", result_serializer="json",
                accept_content=["json"], enable_utc=True, timezone="UTC")
install_process_heartbeat(app, settings)
assert app.conf.beat_schedule == {}
```

Here settings is an explicitly constructed Settings with the spec's synthetic
policy and `redis_url="unix://" + socket_path`; no get_settings/.env read. Worker
mode uses app.worker_main with poolsolo/concurrency1, without-gossip/mingle,
heartbeat-interval1 and hostname synthetic-worker@local. Beat mode uses app.Beat
with an owned shelf path and the installed monitored scheduler, never sends a
task. Verify actual parsed kombu connection class/path before starting either.
- [ ] Step3 implement every live observation in spec: missing→beat-only→bothfresh,
  advancing timestamps+TTL, paused-live beatstale/resumefresh, natural workerstop
  thenstale→missing, same-ID restart/newincarnation, without-heartbeat/disabled
  absence, read-only MGET counters/noSET, Redis shutdown→safe unavailable.
- [ ] Step4 run focused live gate using scrubbed env and plan-owned denied outer
  profile compatible with the per-run exact-socket child profile. The fixture
  may orchestrate processes from the safe parent but each executable performing
  network I/O must have exact-socket sandbox. Bound every poll and retain logs
  only within own temporary test artifacts. No success on incomplete cleanup.

```text
cd backend
env -i PATH=/usr/local/bin:/usr/bin:/bin TMPDIR=/tmp PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python tests/_heartbeat_process_fixture.py verify
```

The wrapper launches pytest through its generated exact-socket sandbox before
any test I/O. A plain unsandboxed service process does not satisfy this gate.
Never widen to working Unix sockets. Verify mode forwards exact pytest exit code
and reports cleanup failure as nonzero even if assertions otherwise passed.

- [ ] Step5 if existing implementation passes, report existing-behavior acceptance
  honestly; do not invent a RED code defect. Negative controls are assertions,
  not historical implementation failures. If a real defect appears, preserve
  exact RED and stop for controller source-scope amendment, then TDD fix/recheck.
- [ ] Step6 run adjacent existing process_heartbeat, health and scheduler_default_off
  tests in their established denied environment. Compile only newtest files,
  scoped Ruff newfiles, git diff --check. Record exact counts/exits/naturalshutdown,
  no skips and actual cleanup; no full-release claim.
- [ ] Step7 isolated self-review and bounded commit `test: verify process heartbeat on disposable Redis`.
  Handoff observed modes/contracts and operational limits; independent review
  before closure. No further feature or deployment activation.

## Preflight

One task owns the fixture, launcher and report. Exact socket ownership and normal
shutdown are part of acceptance, not convenience wrappers. Existing fake unit
tests supply precise callback race coverage; live tests add real TTL/process
evidence without replacing them. Spec distinguishes API reader from publishers
and process liveness from dependency/business readiness. No schema prerequisite.
