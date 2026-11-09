# SKU-override permission constants implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. TDD and verification-before-completion apply.

**Goal:** Export the two approved canonical SKU-override requirement sets without altering existing consumers or permission grants.

**Architecture:** Add two immutable constants to the existing permission module, verify them with pure tests and hand the precise import contract to T2. Existing publication-guard semantics are not changed.

**Tech Stack:** Existing Python, pytest and SQLAlchemy; no dependencies or services added.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-sku-override-permissions-design.md`.

## Global Constraints

- Only the T1 arch-t1-platform worktree; one implementation agent at a time. Dispatch after the preceding Production permission task passes review.
- Do not change existing permission strings, `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, normalization or explicit-grant union.
- Do not modify the existing publication guard: applying a blanket settings:write-implies-read rule to unrelated legacy mutations would broaden this canonical-only policy.
- No dependency, router, task, frontend, config, migration, grant or user data change. No production, real credentials, network, flags, backfill, push or deployment.
- Use the own worktree virtual environment and existing secret/IP-denying sandbox with a scrubbed environment. No PostgreSQL, Redis or provider process is needed for this task. The approved alternate interpreter is lint-only.
- T2 owns real-consumer read/write/replay authorization, mapping and durable CAS tests. Pure constant tests are not RLS, service authorization or schema proof. No skips, baseline expansion or invented historical code failure.

## Task 1: Export the two canonical requirement sets

**Files:** modify `backend/app/cabinet/permissions.py` (two constants only); create `backend/tests/test_sku_override_permissions.py`; create `backend/docs/superpowers/reports/2026-09-09-t1-sku-override-permissions-handoff.md`.

**Consumes:** existing `app.cabinet.permissions` and existing `tests/test_production_permissions.py` unchanged-profile assertions. Read the complete cited spec and these two small modules before writing tests.

**Produces:** exactly these imports:

```python
WB_SKU_OVERRIDE_READ_PERMISSIONS = frozenset({"settings:read"})
WB_SKU_OVERRIDE_REPLACE_PERMISSIONS = frozenset({"settings:read", "settings:write"})
```

- [ ] Record the actual clean dispatch BASE and read the spec. The controller supplies the same-task sandbox and report paths; never borrow another worktree's Python environment.
- [ ] Write the focused test before the constants exist. Use dynamic lookup so an absent export is an assertion-time failure rather than a module collection error:

```python
from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "name,expected",
    [
        ("WB_SKU_OVERRIDE_READ_PERMISSIONS", frozenset({"settings:read"})),
        (
            "WB_SKU_OVERRIDE_REPLACE_PERMISSIONS",
            frozenset({"settings:read", "settings:write"}),
        ),
    ],
)
def test_canonical_override_requirements_are_exact_and_immutable(name, expected):
    actual = getattr(import_module("app.cabinet.permissions"), name)
    assert type(actual) is frozenset
    assert actual == expected
    with pytest.raises(AttributeError):
        actual.add("price:send")


def test_replace_and_replay_require_read_in_addition_to_write():
    permissions = import_module("app.cabinet.permissions")
    read = permissions.WB_SKU_OVERRIDE_READ_PERMISSIONS
    replace = permissions.WB_SKU_OVERRIDE_REPLACE_PERMISSIONS
    assert read < replace
    assert replace - read == frozenset({"settings:write"})
```

- [ ] Run the new file under env-i and the supplied network-denying sandbox. Preserve actual missing-attribute failures. Do not start database fixtures or mark fixture errors as behavior RED.
- [ ] Add only the exact two constants beside the existing constants; do not add a service wrapper, permission dispatcher or global guard rule.
- [ ] Run the new file plus unchanged `tests/test_production_permissions.py` and `tests/test_publication_guard.py`. All must finish naturally; compare any existing lint diagnostics against dispatch BASE instead of formatting unrelated source.

```sh
.venv/bin/python -m pytest -q --tb=short tests/test_sku_override_permissions.py tests/test_production_permissions.py tests/test_publication_guard.py
.venv/bin/python -m compileall -q app/cabinet/permissions.py tests/test_sku_override_permissions.py
git diff --check
```

- [ ] Check exact changed paths and immutable old profile/alias maps via existing tests. Write the handoff: exact imports, READ for get/history, REPLACE for replace/replay, actual principal/account/same-root/final-validation obligations, T2 test matrix, separate schema/mapping/activation gates, measured RED/GREEN and static results.
- [ ] Self-review and commit `feat: define canonical SKU override permissions`. Report full SHA and exact results; independent review and controller verification precede READY to T2.

## Preflight

| Interface | Check |
| --- | --- |
| New constants → old profiles | Additive declarations only; existing profile/alias snapshot tests retained |
| New constants → shared guard | No change to generic or Production requirement validation |
| New constants → T2 wrapper | Fixed server selection, real principal/account and replay-before-disclosure required; wrapper remains T2 |
| Pure checks → readiness claim | Proves export contract only; no service/RLS/schema/activation claim |
