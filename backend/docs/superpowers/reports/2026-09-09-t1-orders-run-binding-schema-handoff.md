# Orders immutable run account binding — schema handoff

2026-09-09. Local schema implementation on `codex/arch-t1-platform`,
base `434ff5f23a116126c362d65089a2407f6ddef947`. Independent review and T3
consumer acceptance remain required. This is not deployment or activation.

## Persisted contract

Forward revision `20260909_0067`, parent `20260909_0066`, adds five nullable
columns to `order_sync_runs`, without defaults or backfill:

| Column | Type |
| --- | --- |
| `account_binding_schema_version` | SMALLINT |
| `account_binding_external_account_id` | TEXT COLLATE "C" |
| `account_binding_credential_ref` | TEXT COLLATE "C" |
| `account_binding_payload` | BYTEA |
| `account_binding_checksum` | TEXT |

All five SQL NULL means legacy unbound. Bound rows require schema 1, exact
external ID, payload and lowercase SHA256; reference is legitimately nullable.
External ID is nonblank 1..128 Unicode codepoints; reference is NULL or nonblank
1..255. Nonblank matches Python `strip()` whitespace, while preserving all text,
including leading/trailing whitespace, controls, combining marks and Unicode.

Per-run bytes are exactly the positional ASCII representation
`[organization_id,[[marketplace_account_id,provider,external_account_id,credential_ref]]]`.
Version is outside those bytes. The CHECK compares reconstructed bytes as well
as SHA256, rejecting alternate lexical forms even when their checksum matches.

The three immutable SECURITY INVOKER helpers use fixed `pg_catalog,public`:

```text
public.orders_binding_text(text,integer) -> boolean
public.orders_binding_ascii_string(text) -> text
public.orders_run_binding_bytes(integer,integer,text,text,text) -> bytea
```

The fixture is byte-for-byte from
`1e25e71eaa2a3ac00115759ca9a93ab3d9bbe8e3:backend/tests/fixtures/orders/account_binding_golden_v1.json`.
The four single-account vectors prove SQL byte/checksum parity. The unchanged
two-account vector is rejected for a run with matching first-account ownership.
Additional boundary expectations use Python stdlib JSON, not copied domain code
or a claim that T3's producer ran in this worktree.

## Writes, locking and authorization boundary

New bound INSERTs require the canonical account lock lookup to find a row in
READ COMMITTED, immediately rejecting a miss before the fresh read, and use a
separate volatile SQL statement to read fresh provider/external/reference
metadata after waiting. Missing/mismatched metadata raises fixed
`orders_run_binding_mismatch` (23514). Existing 0064 account-first identity
uniqueness remains intact. Non-RC inserts retain `orders_isolation_invalid`
(25000). Invalid helper inputs raise `orders_run_binding_invalid` (23514).

All five binding fields are immutable from INSERT, in staging and terminal
states, including NULL→bound and bound→NULL. The fixed error is
`orders_run_binding_immutable` (23514). This UPDATE trigger never locks an
account, avoiding run→account lock inversion. Existing lifecycle guards remain.

Existing FORCE organization RLS is unchanged. Missing/wrong organization is
denied, while same-org second-account SELECT remains possible under the old
policy. Live per-account read and publication authorization are T3/guard duties;
immutable provenance does not authorize a SQL writer or establish a provider's
truthfulness or historical A→B→A epoch identity.

New functions lose PUBLIC/default-grantee execution and grant options. Only
existing non-owner INSERT/UPDATE table or column grantees receive pure helper
execution. SELECT-only readers retain their column limitations and need no new
helper rights. Neither trigger-only function is granted to runtime. The runtime
script adds only this helper ACL block; no old table or default ACL is widened
by this migration.

## Verified evidence

Admission in the assigned worktree: exact branch/base, clean owned paths,
sole 0066 and absent 0067 before editing. Final graph: sole 0067; a synthetic
future successor is accepted by the feature ancestry test.

Actual pre-DDL RED on 0066: **2 failed / 1 passed, 3.06s, exit 1**.
Failures were UndefinedFunction (42883) and UndefinedColumn (42703); ordinary
old unbound INSERT passed. One initial PATH mistake caused three setup errors
before any database was allocated; corrected to installed `/usr/local/bin`.

Initial feature gate: **115 passed, 56.05s, exit 0, no skips/xfails**. Tests cover
all malformed partial-null shapes, wrong schema/hash/bytes, text boundaries,
immutability in three states, both observed physical account-lock winner orders,
fresh metadata after wait, unchanged sealed stamp after rebind, no account lock
on UPDATE, runtime RLS, narrow table/column/default ACLs, actual runtime script
and rollback on injected script failure. Empty and all-eleven-table synthetic
unbound upgrade→downgrade→upgrade preserve old values/counts/schema/ACLs.
Visible bound, FORCE-RLS-hidden bound and explicitly constructed partial bound
fixtures all refuse downgrade without drops or deletion.

Required seven-file adjacent gate: **374 passed, 172.20s, exit 0, no skips/xfails**.
Scoped Ruff, compileall,
fixture `git show | cmp`, and `git diff --check`: exit 0. Alembic heads: sole 0067.
PostgreSQL client: 16.15 Homebrew. Final feature run verified absence of all nine
own disposable databases and twelve generated roles after engines were disposed.

Exact commands, intermediate failures and complete resource evidence are in
`.superpowers/sdd/2026-09-09-orders-run-binding/task-1-report.md`.

## Execution and remaining limits

Every database command used the existing candidate allocator and random own
databases/roles over the allowed local Unix socket. Scrubbed environment and
the complete plan-owned sandbox denied IP and known secret paths. No application
database, working Redis, providers, production, credentials, `.env`, flags,
dependencies, domain Python, push or deployment was used. Inherited libpq
`/dev/null is not a plain file` warnings remain; the passfile was not changed.

PostgreSQL TEXT/UTF8 and psycopg reject NUL/unpaired surrogates before the helper
in supported execution. SQL INTEGER/SMALLINT casts occur before typed functions;
out-of-range or uncastable inputs can therefore fail natively. Intentional SQL
casts can coerce inputs before the helper; T3 must retain its strict domain input
validation. Native CHECK/FK/type errors can contain database detail, so consumer
sanitization is still necessary; fixed-error canaries cover custom errors.

Self-review checked the brief, exact SQL, fixture provenance and real test
evidence. It corrected test query percent escaping, matched the multiaccount
negative to its real first-account ownership, made runtime-script setup follow
head, and exercised limited-column UPDATE. The configured `preflight-critic`
skill was absent at its path and in installed skill/plugin trees; an isolated
manual critic pass was used without a reviewer subagent, as required here.

T3 must add ORM/repository/decoder/live-guard/API acceptance and handle unbound
history fail closed. Do not backfill from current accounts or mutable audit data.
Rollback retains expanded schema and a decoder-capable binary. Downgrade locks
`order_sync_runs` ACCESS EXCLUSIVE, refuses any non-NULL binding field with
`orders_run_binding_downgrade_bound` (55000), and uses `row_security=off` only
as a fail-closed visibility defense. Never clear bindings to force rollback.

## I1 correction before release

Independent review found that the locking PERFORM's FOUND result was discarded.
A newly committed account could become visible to the later fresh SELECT even
though the lock lookup had missed it. The unreleased 0067 now checks NOT FOUND
immediately after FOR UPDATE and raises the same fixed mismatch error before
any metadata read. The fresh READ COMMITTED read remains after successful locking.

A coordinated regression temporarily instruments only the installed guard in its
own disposable database: an advisory-lock assignment pauses immediately after
the row-lock lookup and preserves FOUND. It checks FOUND did not change and that
the row lookup missed; an independent creator commits matching account metadata
during the observed `pg_blocking_pids` pause. This reproduced original admission
as **1 failed, 3.44s, exit 1**. After the three-line guard fix it reports
**1 passed, 3.70s, exit 0**, and the restored uninstrumented guard admits a normal
subsequent insert when that account is visible. This is an instrumented scheduling
proof, not a claim to reproduce the narrow timing window without instrumentation.
The original function definition is restored in finally; production has no pause.

Final covering gate after I1: **116 passed, 32.21s, exit 0, no skips/xfails**.
Compileall, scoped Ruff and diff checks: exit 0. All eleven databases/fourteen
roles across fix RED, focused GREEN and covering GREEN were cleaned and verified
absent. Existing account-lock winner and unbound/immutability/ACL/downgrade
controls still pass. The seven-file adjacent gate above predates this local fix;
the controller explicitly required only the two covering files for the fix.
No older revision, grants, domain code or activation changed. I1 awaits focused
independent re-review; inherited passfile warning M1 remains separately recorded.
