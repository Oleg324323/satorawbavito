# Production P1 permission boundary

Authority: coordinator-root's explicit local architecture decision delivered2026-09-09 to T1 and T3. This registers capabilities, not operational grants or activation. T1 owns shared permissions/guard; T3 owns the Production service and its actual command/replay audit.

## Contract

`app.cabinet.permissions` exports immutable frozensets:

```python
PRODUCTION_PERMISSION_KEYS = frozenset({"production:read", "production:create", "production:assign"})
PRODUCTION_READ_PERMISSIONS = frozenset({"production:read"})
PRODUCTION_CREATE_PERMISSIONS = frozenset({"production:read", "production:create"})
PRODUCTION_ASSIGN_PERMISSIONS = frozenset({"production:read", "production:assign"})
```

No changes to `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, existing permission strings or profile normalization. Admin/owner/settings_editor/legacy production aliases receive no implicit Production capability. Existing explicit membership grants are unioned with profile grants exactly as today. Registration is separate from assignment; no user rows, defaults, migrations, provisioning or grant-management endpoints change.

Trusted services select the exact operation constant; request data, role labels, idempotency keys and receipts cannot select permissions. Existing `acquire_publication_guard(session, *, principal, required_permissions, accounts, authorities)` remains the only shared transaction fence. Its pre-I/O contract additionally rejects requirements containing production:create or production:assign without production:read, with existing safe code `publication_context_invalid`. It does not narrow unrelated existing permission requirements or treat an unknown string as a granted permission.

T3 read operations use READ. Create and its replay use CREATE. Assignment/change and its replay use ASSIGN. Caller supplies actual authenticated `UserSessionPrincipal`, exact frozen `ExpectedAccountBinding`, and empty authorities for credential-independent Production commands. No fake user, background principal, credential fetch or provider call is introduced. Permission sets are not authorization by themselves.

The unchanged guard obtains a real Engine-owned PostgreSQL READ COMMITTED root, user/membership/session/account locks, live profile+explicit-grant union and exact account allowlist. It revalidates before writes and at final commit. T3 must invoke it before reading a receipt and before domain locks, keep the root through the operation, and return only after successful commit. Replays need current operation+read permissions even if the original command previously succeeded. A later revocation may serialize after an already-locked operation; a committed earlier revocation must deny it. No claim that a pending revocation retroactively cancels a completed transaction.

## Explicit limits

Current P1 storage0069 supports creation and non-NULL manual assignment only. The production:assign capability may authorize a separately approved future unassign operation, but this slice adds no unassign, schema changes, public writer, automatic mapping, print, scheduler, flags or operational grants. Physical schema is not authentication. Existing source-chain/current-binding checks remain T3's separate obligation.

Shared tests prove fixed constants, profile non-escalation, pre-I/O malformed requirement rejection, actual user/session/grant/account fences and rollback of synthetic writes. They do not claim actual T3 receipt or domain audit behavior; T3 acceptance must distinguish create/assign/replay and prove denial before receipt disclosure. No invented audit executor.

## Acceptance and rollback

Pure RED first: missing exported constants and create/assign-only requirements reaching SQL under the existing guard. GREEN must include immutable exact constants, all unchanged profiles/aliases, missing read, operation mismatch and safe canary errors. Actual disposable PostgreSQL tests cover explicit-grant union, both providers, foreign tenant/account, inactive membership/session, grant revocation before acquisition and during final validation, and two lock winners for a real concurrent revocation. Allowed operations commit synthetic proof rows; denied operations leave none.

Use only the owned local Unix PostgreSQL allocator, scrubbed environment and existing secret/IP-denying sandbox under the team's serialized heavy slot. No real credentials, providers, working Redis, production, network, deployment, push or flag activation. No baseline expansion, skip, SQLite RLS substitution or widened timeouts.

Rollback is code-only while consumers remain dormant; if consumers already import these constants, keep the additive constants and revert their wiring first. Never restore a writer that bypasses current authorization. Exact shared READY is issued only after independent review and fresh verification, not from this design.
