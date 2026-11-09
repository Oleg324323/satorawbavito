# Self-published process heartbeat implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Add default-off worker/beat-owned shared freshness without making API manufacture a heartbeat.

**Architecture:** An app-scoped consumer bootstep fences the installed Heart signal. A PersistentScheduler subclass publishes after successful ticks. An independent read-only helper performs one bounded MGET; API dependency readiness stays separate.

**Tech Stack:** Existing Python, Celery and redis-py; pytest with fake clock/client and installed framework hooks. No new dependency.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-process-heartbeat-design.md`.

## Global Constraints

- No production, working DB/Redis, providers, .env, real credentials, Docker, deploy or flags activation.
- No edits to repricer_tasks.py, review_tasks.py, provider clients, frontend or schedules.
- Default disabled; enabled configuration is explicit and fail-closed. No production TTL, identities or timeout values are selected.
- API reads only; no secret/keyring access, raw errors, hostnames, key inventory or payload in responses.
- Preserve existing readiness payload when disabled and dependency-derived status when enabled.
- No full-suite, live Redis TTL or worker-mode coverage claim from fake tests.

### Task 1: Policy, owner hooks and read-only health integration

**Files:**
- Create `backend/app/infra/heartbeat.py`: validated policy, bounded wire/client, publisher, bootstep, scheduler, reader.
- Modify `backend/app/config.py`: only explicit nonsecret heartbeat settings and parsing.
- Modify `backend/app/infra/celery_app.py`: one factory registration hook, no task/schedule edits.
- Modify `backend/app/infra/health.py`: optional separate freshness output, unchanged dependency status.
- Create `backend/tests/test_process_heartbeat.py`: synthetic unit/framework-hook regression.

**Interfaces:** Exact settings, signatures, wire and lifecycle invariants in the spec are binding. `read_process_freshness(settings, *, client_factory=None, clock=None)` returns only redacted aggregate states. `install_process_heartbeat(app, settings)` configures only the given app and never connects. Publisher owns lifecycle; API cannot call its publication path.

- [ ] Write focused RED tests before implementation. Begin with the actual disabled contract:

```python
from app.config import Settings
from app.infra.heartbeat import read_process_freshness

def test_disabled_reader_never_constructs_client():
    def forbidden_client(*args, **kwargs):
        raise AssertionError("disabled heartbeat constructed a client")
    assert read_process_freshness(
        Settings(), client_factory=forbidden_client
    ) == {"status": "disabled"}
```

- [ ] Add explicit synthetic-policy tests covering every field/type constraint in the spec; malformed enabled value must not become disabled. Test missing owner identity, duplicate/oversized IDs, booleans as numbers, nonfinite/future timestamps, unknown marker fields, bounded parsing and no raw canary disclosure. Fake client records SET/EX/MGET/close; injected epoch clock controls stale→missing behavior. No fake object may open a socket.
- [ ] Add installed-framework hook tests before wiring: construct real Heart with fake timer/eventer after installation; prove online/periodic/offline discrimination, exact Heart/app/PID matching, reconnect, --without-heartbeat, idempotent registration, all shutdown hooks and queued/in-flight publication fencing. Test real PersistentScheduler subclass with temporary shelf or patched storage: no lazy/init publication, successful tick-only publication, blocked/failed tick aging, explicit sleep cap, close preservation and Service finally cleanup. Patch dispatch so no task can reach a broker.
- [ ] Run RED under this worktree's OS network-deny profile and scrubbed environment. Record missing module/interface failures, then behavioral failures as each unit is introduced. No SQLite substitute or live services.

```text
cd backend
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-08-marketplace-credentials-encryption/offline-local.sb .venv/bin/python -m pytest -q tests/test_process_heartbeat.py
```

- [ ] Implement only the specified contract in `heartbeat.py` and config. Dedicated lazy client uses explicit `Retry(NoBackoff(), 0)` and configured socket timeouts. Key count is bounded; reader uses one MGET and closes local client. Publisher generates incarnation on lifecycle start; locks publish/stop and never deletes a shared marker. Do not alter global Redis helper.
- [ ] Wire the app hook after Celery configuration inside the cached factory:

```python
from app.infra.heartbeat import install_process_heartbeat

# Inside get_celery_app(), after existing app.conf.update(...):
install_process_heartbeat(app, settings)
return app
```

- [ ] Wire the health reader after existing dependency aggregation. Preserve `ready` and all disabled fields; enabled freshness cannot make a failed dependency ready. Add off-state exact payload tests and on-state missing/stale/unavailable/invalid_configuration aggregates; assert no SET from health/liveness paths.
- [ ] Run GREEN plus inspected hermetic `tests/test_health.py`, `tests/test_scheduler_default_off.py` and the two existing positive scheduling cases. Do not run unreviewed broad suites or suppress baseline failures.
- [ ] Run compileall on changed files and `git diff --check`; inspect exact file scope. Commit one bounded feature change and record full SHA, RED/GREEN commands/exits, lifecycle coverage and operational limitations in the task report. Independent task review precedes next task.

## Self-review

Coverage: explicit policy/wire limits, lazy bounded Redis, worker lifecycle, persistent beat, read-only health, redaction and disabled compatibility each have a task step and RED matrix in the spec. No migration or domain dependency is introduced. No production policy or provider call is required to test this local slice. Framework compatibility, DNS deadlines, clock synchronization and overlapping logical identities remain explicit operational limitations, not hidden success claims.
