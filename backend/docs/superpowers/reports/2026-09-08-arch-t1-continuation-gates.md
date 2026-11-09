# Terminal 1: continuation gates and verification prerequisite

Date: 2026-09-08. Branch: `codex/arch-t1-platform`.
Original base: `c88a474695569af905b294906ba604d61f3de62f`.
Continuation starts at Stage 1 commit
`2a2b765457f603d62235231dfb6dec63235cd307`.

## Implemented in this continuation

The release runner inherited Redis/Celery connections, rollout flags, arbitrary
application configuration and interpreter injection settings. Its old secret-name
denylist was insufficient. `safe_environment()` now inherits only process-launch
essentials and supplies fake provider modes, disabled external-write switches,
unreachable loopback DB/Redis defaults and loopback proxies explicitly. A disposable
migration database still replaces the database default only for migration steps.

Frontend discovery now defaults to the frontend adjacent to this runner's backend,
not a directory outside its worktree. The explicit `SATORNA_FRONTEND_DIR` override
remains supported.

This is a bounded Stage 6 prerequisite, not a completed Stage 6 release gate.
Proxy variables do not prevent clients with `trust_env=False`, native libraries or
subprocesses from using the network. A complete deny-by-default test network boundary
and shutdown-hang investigation remain required before another broad regression run.
No full suite was run in this continuation.
Docker lifecycle commands still need a separately verified local daemon/context
boundary; this patch does not make invoking the complete release runner safe against
an inherited remote Docker configuration. No Docker command was run here.

## Coordination snapshot (not integration approval)

| Terminal | Observed committed HEAD | Concurrent local work observed |
|---|---|---|
| T2 | `769f3cf44742622824233eefe8f81d4e360f8484` | Untracked source-revision module/test |
| T3 | `4323821d2763291659b4ea1ba6ac5501c20b2109` | Modified orders tests; untracked offline guard and XLSX tests |
| T4 | `81ee4b96236b0f1f0c7f5510839a35b47e5a2371` | Untracked Reviews Stage 2 schema request |

Only committed handoffs were reviewed as requests. Uncommitted work was neither
read as an approved contract nor changed. These worktrees are not an exact-ready,
stopped four-terminal integration set. No integration branch or cherry-pick was made.

T1's shared-file ownership and resolver imports/kinds/safe errors remain specified
in `2026-09-08-arch-t1-stage-1-credential-api-handoff.md`.

## Decisions and dependencies still required

1. **Extension and mixed-version cutover:** the approved design's owner-decisions
   section and the Stage 1 handoff require finite production TTL, pre-lookup IP and
   post-verification token quotas/windows, maximum body size, revoke/reissue UX and
   an explicit old-job drain/fence policy. No values or policy were supplied by
   “continue all stages.” Do not infer them from synthetic test fixtures. Local
   configurable/default-off implementation is possible, but does not approve
   activation or compatibility handling. Owner input was requested.
2. **Key lifecycle/deployment:** host reader identities/path/modes, recovery custody
   and service/login placement remain owner decisions. No key mount, working-key
   rotation, real mapping/backfill, backup access or cleanup is authorized here.
   Synthetic CLI/restore work remains permissible separately from those operations.
3. **T2 schema review:** the committed request identifies three approval/attempt/audit
   tables and requires organization AND account RLS. Before freezing DDL, coordinate
   exact attempt states, immutable dispatch identity representation, safe-code
   vocabulary and lifecycle consistency checks with the owner; the request describes
   these partly in prose. Existing shared tenant context is organization-only, so an
   account-context contract must accompany schema acceptance. Do not silently replace
   the requested account RLS with organization-only RLS.
4. **T3 schema review:** the committed request covers ten tables and Catalog anchors.
   Agree exact enum/check vocabularies and replay identities where the request says
   “allowed” or “unique replay identity” without enumerating them. Production work,
   sheets, printing and KIZ remain blocked on the missing prototype/matcher sources
   documented by T3. This does not prevent a separately agreed Avito ingestion slice.
5. **T4 and source-diff:** await committed Reviews schema request and committed T2
   identity-level revision policy. Do not adopt concurrently written draft files.
6. **Integration:** exact ready commits and stopped-owner confirmation are required.
   Existing backend 105-failure and frontend 29-failure baselines are not green suites
   and must not be expanded. The known Python shutdown hang is not fixed by this patch.

No migration was issued or reserved. Fresh `alembic heads` still reports exactly
`20260908_0061`; historical migrations and the empty-DB 0019 repair are unchanged.

## Verification

Commands below run from `backend`, using only this worktree's isolated `.venv`.

- RED: `env -i PATH="$PATH" LC_ALL=C .venv/bin/python -m pytest -q --tb=short
  tests/test_release_gate.py`: exit 1, **3 failed, 6 passed**. Failures reproduce
  inherited Redis settings, absent safe connection defaults and wrong frontend root.
- GREEN: `env -i PATH="$PATH" LC_ALL=C .venv/bin/python -m pytest -q
  tests/test_release_gate.py`: exit 0, **9 passed**.
- `python -m compileall` for the changed runner/test and `git diff --check` pass.
- `env -i PATH="$PATH" LC_ALL=C .venv/bin/python -m alembic heads`: exit 0,
  one head `20260908_0061`; this command does not connect to a database.

Baseline delta: four focused tests added to five existing release-runner tests.
No broad baseline delta, migration roundtrip, RLS, frontend build, wheel import,
Celery broker inspection or restore rehearsal is claimed for this continuation.

No application flag was enabled. No consumer, schema, runtime grant, domain-owner
file, frontend, serverless or deployment configuration changed. This continuation
used no GitHub, production, working DB/Redis, real credential or provider action.

Stages 2–6 are **not complete**. Resume with owner-policy resolution and coordinated
schema/resolver acceptance; production encrypted-only switch, plaintext cleanup and
legacy retirement remain separately authorized operations.
