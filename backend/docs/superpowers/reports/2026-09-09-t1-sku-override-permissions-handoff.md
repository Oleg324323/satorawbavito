# T1 SKU override permission export handoff

Date: 2026-09-09. Clean dispatch BASE: `e6d97c2ee97fbc320ba98c945a768a2b9a631a0e`.

## Contract delivered

`app.cabinet.permissions` now exports exactly:

```python
WB_SKU_OVERRIDE_READ_PERMISSIONS = frozenset({"settings:read"})
WB_SKU_OVERRIDE_REPLACE_PERMISSIONS = frozenset({"settings:read", "settings:write"})
```

READ is the fixed requirement for independent SKU-override get/history. REPLACE is the fixed requirement for replace and exact-command replay; it includes the read prerequisite because mutation/replay returns state or a receipt. Trusted T2 service code must select these constants; request input, role labels, command UUIDs, and receipts must not select or substitute authorization.

## T2 obligations and gates

T2 remains responsible for actual authenticated `UserSessionPrincipal`, server-fixed requirements, exact frozen `ExpectedAccountBinding`, and `acquire_publication_guard` before receipt disclosure and domain locks. The same PostgreSQL root must span mapping checks, revision/head/audit mutation or replay, and final commit revalidation; successful responses return only after commit.

Required T2 matrix: read-only/write-only/both/neither permissions; different organization and account; inactive membership/session; revocation while waiting and final revalidation; denied requests disclose no existing receipt and commit no mutation; valid exact-command replay adds no revision or audit. Separate readiness gates remain source mapping, exact canonical payload, durable CAS, schema/RLS, and activation. These pure export tests do not prove service authorization, RLS, mapping, CAS, or activation.

## Scope and preservation

Only the two additive constants, one pure focused test, and this handoff report changed. Existing permission strings, `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, normalization, explicit-grant union, and publication guard behavior are unchanged. No dependency, router, task, frontend, config, migration, grant, user data, production, network, provider, database, Redis, flag, push, or deployment work was performed.

## Verification

Initial RED used dynamic lookup and failed at assertion time with three `AttributeError` failures for the absent exports. The focused test then passed with the constants present. The combined gate completed with `66 passed in 0.49s`; compileall and `git diff --check` exited 0. Alternate Ruff from backend cwd reports the pre-existing `I001` in `app/cabinet/permissions.py`; comparing the BASE file produces the same single `I001`, so no new lint diagnostic was introduced.

Commit: recorded in the execution report after commit finalization.
