# T1 Review shadow rollout selector handoff

Date: 2026-09-09

Branch: `codex/arch-t1-platform`

Base: `34b5f62d9109b69870113bcb319143d95c46409b`

## Delivered interface

`app.config.Settings` now provides these non-secret, default-off fields:

```python
review_shadow_enabled: bool = False
review_shadow_account_pairs: tuple[tuple[int, int], ...] = ()
```

T4 can import and call:

```python
from app.config import is_review_shadow_enabled

selected = is_review_shadow_enabled(
    settings,
    organization_id=organization_id,
    marketplace_account_id=marketplace_account_id,
)
```

Environment inputs are `VELLA_REVIEW_SHADOW_ENABLED` and
`VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS`. The flag accepts only surrounding-whitespace
trimmed, case-insensitive `true` or `false`. Pairs are an ordered comma-separated
sequence of exact `organization_id:marketplace_account_id` positive INT4 pairs.
Whitespace around a whole pair is accepted; signs, padding, Unicode digits,
embedded whitespace, wildcards, duplicates, empty segments, extra colons, zero,
and values above `2147483647` are rejected.

Any malformed provided policy, including malformed pairs while disabled, raises
`RuntimeError("review_shadow_configuration_invalid")` with no chained raw parser
error. Missing inputs produce `False` and `()`. An enabled empty allowlist is
valid and selects nobody. Directly constructed `Settings` receive the same exact
bool/tuple/pair/INT4 validation in the predicate; invalid requested IDs simply
return `False`.

## TDD and verification evidence

The missing-interface RED was run before changing `app/config.py`:

```sh
env -i PATH=/usr/bin:/bin /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-shadow-rollout/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_review_shadow_rollout.py
```

Exit `1`: 72 expected failures. The leading failures were `Settings.__init__()`
rejecting `review_shadow_enabled` and `app.config` lacking
`is_review_shadow_enabled`; malformed environment canaries also did not raise.

Before implementation, the compatibility canary suite passed with exit `0`:

```sh
env -i PATH=/usr/bin:/bin /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-shadow-rollout/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_scheduler_default_off.py tests/test_canonical_shadow_tasks.py tests/test_infra_baseline.py
```

Result: `21 passed, 2 warnings`. The warnings are dependency deprecations from
FastAPI/Starlette and were already present.

Fresh final checks under the same `env -i` and all-network/known-secret deny
sandbox produced these results:

- `.venv/bin/python -m pytest -q tests/test_review_shadow_rollout.py` — exit `0`,
  `73 passed`.
- `.venv/bin/python -m pytest -q tests/test_scheduler_default_off.py
  tests/test_canonical_shadow_tasks.py tests/test_infra_baseline.py` — exit `0`,
  `21 passed, 2 warnings` (the pre-existing dependency warnings noted above).
- `.venv/bin/python -m compileall -q app/config.py
  tests/test_review_shadow_rollout.py` — exit `0`.
- `/usr/bin/git diff --check` — exit `0`.
- `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check
  tests/test_review_shadow_rollout.py` — exit `0`, all checks passed.
- `.venv/bin/python -m black --check tests/test_review_shadow_rollout.py` —
  exit `0`, unchanged.

The alternate lint-only interpreter is
`/tmp/satorna-backend-verify-20260908/bin/python`. The new test file passes Ruff.
`app/config.py` retains one pre-existing `I001` import-format finding: running the
same Ruff check against the base-commit version returns the identical finding.
It was not broadened into unrelated cleanup.

The scrubbed before/after snapshot remained exactly:

```text
canonical shadow: False, ()
finance shadow ingest: False, ()
advertising shadow ingest: False, ()
WB feedback send: False
repricer scheduler: False
```

## Limits

This selector is eligibility data only. It does not authenticate, authorize
`reviews:write`, validate canonical ownership, resolve credentials, permit a
provider request, register a task/router, write to DB/Redis, replace publication
guards, reuse canonical-shadow flags, or activate an operational rollout. T4 must
keep those checks independent. No environment, Compose, CI, dependency, provider,
startup, DB, Redis, GitHub, push, or operational-flag state was changed.
