# Canonical SKU-override permission constants

Coordinator-root explicitly approved this bounded policy on 2026-09-09; T2 accepted it. This is a new canonical contract, not an assertion that legacy SKU routers already enforce it. T1 supplies shared constants; T2 owns the authenticated override repository/service and its acceptance tests. No operational grant, route activation or physical schema is included.

## Shared contract

Add these exact exports to `app.cabinet.permissions`:

```python
WB_SKU_OVERRIDE_READ_PERMISSIONS = frozenset({"settings:read"})
WB_SKU_OVERRIDE_REPLACE_PERMISSIONS = frozenset({"settings:read", "settings:write"})
```

Independent get/history uses READ. Replace and exact-command replay use REPLACE, including the read prerequisite because mutation/replay returns state or a receipt. Trusted service code selects the set; request input, role label, command UUID or an existing receipt never selects or substitutes for authorization. No `price:send` or `team:write` alternative is admitted.

Do not change existing permission strings, `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, normalization or explicit-grant union. Do not modify the existing publication guard: applying a blanket settings:write-implies-read rule to unrelated legacy mutations would broaden this canonical-only policy. Existing Production permission constants and their separate pre-I/O check remain unchanged.

## Consumer obligations, not implementation in this slice

T2 must call the existing `acquire_publication_guard` with actual authenticated `UserSessionPrincipal`, server-fixed requirements and exact frozen `ExpectedAccountBinding`, before receipt disclosure and before domain locks. Hold the same PostgreSQL root through current mapping checks, revision/head/audit mutation or replay, and final commit revalidation. Return only after successful commit. No background principal or credential dependency is introduced for a user-driven settings command.

Required T2 real-consumer tests: read-only/write-only/both/neither permission matrix; different organization and account; inactive membership/session; revocation while waiting and final revalidation; denied requests disclose no existing receipt and commit no mutation; valid exact-command replay adds no revision or audit. Merely verifying the constants cannot prove these service behaviors. Source mapping, exact canonical payload, CAS and schema readiness are separate gates.

## This task's acceptance and rollback

Pure tests first reproduce absent exports with actual dynamic attribute lookup, then verify exact immutable sets and the required read-plus-write relationship. Existing Production constants/profile/alias and generic publication-guard pure tests must still pass. No PostgreSQL, Redis or provider process is needed to verify these two declarations; no RLS or domain authorization claim follows from a pure result.

Only the permission module, one new pure test and a handoff report change during implementation. No dependency, router, task, frontend, config, migration, grant or user data change. Use the own worktree virtual environment and existing secret/IP-denying sandbox with a scrubbed environment. No production, real credentials, network, flags, backfill, push or deployment.

Rollback remains additive while consumers are dormant. Once T2 imports the constants, revert that consumer wiring before removing exports; do not replace the canonical writer with a permission-bypassing legacy writer. Exact shared READY follows independent review and controller verification; it is not an API or schema activation approval.
