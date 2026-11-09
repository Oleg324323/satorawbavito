# T1 → T4: immutable Review run storage

2026-09-09. Implemented from clean base
`3337c09b87a9fdadb2dd6f37336d6edc8b37974d`, following the accepted
`2026-09-09-review-run-binding-design.md`. Candidate `20260909_0068` follows
the rechecked sole `20260909_0067`. Independent controller acceptance and exact
consumer READY handoff remain separate from this implementation report.

## Physical contract

`review_sync_runs_v2` gains five nullable columns without defaults/backfill:
`account_binding_schema_version SMALLINT`,
`account_binding_external_account_id TEXT COLLATE "C"`,
`account_binding_credential_ref TEXT COLLATE "C"`,
`account_binding_payload BYTEA`, `account_binding_checksum TEXT`.

All five NULL means legacy/unbound. Bound rows require schema version 1, nonempty exact
external ID, exact payload and lowercase SHA256; credential reference is nullable
and NULL differs from empty or whitespace. Every binding field is immutable from
INSERT, including running/empty runs and attempted promotion of legacy runs.

Pure immutable SECURITY INVOKER helpers with fixed `pg_catalog,public` search path:

```text
public.review_binding_ascii_string(text) -> text
public.review_run_binding_bytes(integer,integer,text,text,text) -> bytea
# organization, account, marketplace, external ID, nullable reference
```

Bytes are the six-key sorted compact ASCII JSON object (schemaVersion inside),
matching T4's `45e2ccb` fixture exactly. This is independent of Orders' positional
array/strip/length grammar. The CHECK verifies reconstructed bytes AND SHA256.
Native PostgreSQL TEXT cannot represent NUL; both fixture NUL cases explicitly
exercise native `22021` rejection. No Review source-body constraint is added;
0065 lossless body/source/coverage history remains unchanged.

Bound INSERT requires READ COMMITTED, locks the exact organization/account/provider
row FOR UPDATE, immediately rejects missing `FOUND`, then reads fresh canonical
metadata in a separate volatile statement. UPDATE only compares OLD/NEW binding
fields; it requests no account lock. Error messages are fixed:

```text
review_run_binding_invalid              23514
review_run_binding_mismatch             23514
review_run_binding_immutable            23514
review_run_binding_isolation_invalid    25000
review_run_binding_downgrade_bound       55000
```

## Privileges and rollback

New-column INSERT extends non-PUBLIC direct grantees only when their effective
rights cover the complete previous 16-column run INSERT allowlist. Inherited
membership remains intact. Partial INSERT, reader and UPDATE-only roles gain no
new INSERT authority. Pure helpers are granted to existing INSERT/UPDATE CHECK
evaluators, including a distinct relation owner. No parent/account, sequence,
identity or trigger-helper authority is manufactured. New function default grants
and grant options are cleared atomically; old relation/column/function/default
ACLs remain unchanged. The runtime script adds only these five INSERT columns
and pure-helper EXECUTE. Existing sequence USAGE-only behavior remains.

Existing org-only FORCE RLS remains: same-org multi-account visibility is explicit
compatibility, not allowed-account authorization. Exclusive privileged DDL/ACL
maintenance is a prerequisite; the relation lock does not fence concurrent GRANT.

Downgrade locks the run relation ACCESS EXCLUSIVE and sets `row_security=off` as
a refusal defense before all DDL. Any populated binding field blocks downgrade;
a genuinely hidden bound row also refuses (`42501` under FORCE RLS). Keep the
expanded schema for binary rollback once binding exists; never erase/unbind data.

## Verification and limitations

Initial RED on real 0067: valid ordinary unbound run passed; new-column and helper
calls failed `42703`/`42883` (2 failed, 1 passed; natural exit 1; 3.37 s). The fixture
is byte-identical to `45e2ccbb165e6ddcc383311789b84b5d93ef2933`; both Git blob IDs are
`c8f8970e1a222a932a67338066e4ccf8a1a2ecc2`.

Fresh complete feature suite: 131 passed in 66.67 s, natural exit 0, across three
new modules, no skips/xfails. All 16 allocated databases and 21 roles had exact absence
verified after cleanup. An earlier identical full run had 130 passes plus one 60 s
Alembic fixture-setup timeout; it was not accepted, and the entire unchanged suite
was rerun successfully. Required five adjacent modules: 325 passed in 159.45 s,
natural exit 0, no skips/xfails. Their 58 databases and 21 roles had exact cleanup absence verified.
Compileall, scoped Ruff, fixture equality and git diff --check exit0; Alembic heads
reports sole `20260909_0068`. New modules comprise 86 migration, 42 RLS and 3 ACL tests.

Commands (own backend/.venv, documented scrubbed sandbox launcher in the full report):

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_review_facts_schema.py tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0068_review_run_binding.py tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py
.venv/bin/python -m alembic heads
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check alembic/versions/20260909_0068_review_run_binding.py tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py
git diff --check
```

The required preflight-critic file is unavailable. An isolated implementer critic
reviewed actual SQL/tests, fixed the distinct-owner privilege regression and
preserved the inherited FK limitation with base/current evidence. This self-review
does not replace the controller's independent review and fresh acceptance.

Native concurrency evidence includes both lock winners for external-ID changes
and NULL↔empty references, observed `pg_blocking_pids`, and a deterministic missing
row→creation race. The latter temporarily pauses only the owned disposable
function after PERFORM with an assignment preserving FOUND; the exact original
function is restored in finally. Holder transaction release precedes executor join.

An inherited limitation was reproduced on both 0067 and 0068: a second UPDATE of the
same run in one transaction can recheck the existing scoped FK with FOR KEY SHARE
and wait behind a separately locked account (`55P03` at the deliberately short
test lock timeout). Single UPDATE/no-op controls succeed while that account is
locked. This patch adds no UPDATE account FOR UPDATE; it does not claim every
multi-UPDATE transaction is account-lock-free or deadlock-free.

Downgrade malformed-data fixtures are explicitly privileged and disposable-only:
remove the CHECK, temporarily substitute RETURN NEW to construct one-field data,
then restore and verify the exact original function definition/owner/ACL BEFORE
testing downgrade. No triggers are disabled. Empty/populated preservation cycles
use no such instrumentation and retain all selected driver-returned Orders and
Review rows, including NUL bytes, watermarks, run items, IDs and timestamps.

Tests use only owned random disposable PostgreSQL 16.15 databases/runtime roles
through the existing Unix-socket allocator, the plan sandbox, scrubbed `env -i`,
fake providers and unreachable application/Redis endpoints. `/dev/null` passfile
diagnostics are inherited from libpq; no secret was read and no skip/xfail or
process timeout was accepted as success. The alternate Python is Ruff-only.

No whole-backend or production acceptance is claimed. No consumer/ORM/router/UI,
provider, real credential, working Redis, key, flag, backfill, push or deployment
change is included. T4 still owns reservation/replay/publication, current and
ambiguity history admission, mixed/unbound rejection, safe ErrorEnvelope and
completed-rebind read acceptance. This schema does not detect identical-descriptor
ABA and does not authorize consumer activation.
