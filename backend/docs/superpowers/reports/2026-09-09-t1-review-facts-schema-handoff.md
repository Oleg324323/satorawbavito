# T1 → T4: Review Facts schema prerequisite

Date: 2026-09-09. Local additive prerequisite only; controller review and exact
commit acceptance remain separate. No activation, provider call, legacy write,
backfill, rollout flag, send or notification capability is introduced.

## Revision and ownership

The sole prior head was `20260909_0062`; the new revision is
`20260909_0063_review_facts.py`, revision ID `20260909_0063`. It is self-contained
and creates exactly four relations: `review_sync_runs_v2`, `review_facts`,
`review_observations`, `review_sync_run_items`. T4 retains ownership of
`app/reviews/canonical_orm.py`, repository, semantic ingestion and collectors.
Use the exact commit containing this handoff as the schema/grants prerequisite;
the controller task report supplies its full SHA after commit creation.

The accepted source is T4 `4a7d66999e8d8deedbe6ac5d44a23d9a960c9044` and T1
acceptance `d38e923`. The controller subsequently approved the physical exact-text
uniqueness amendment and READ COMMITTED insertion requirement described below.

## Exact data and keys

All four relations carry INTEGER `organization_id`, INTEGER
`marketplace_account_id`, and VARCHAR(16) `marketplace` constrained to `wb|avito`.
Each has a composite FK to canonical `marketplace_accounts` with all three owner
columns. All internal references also include organization/account/provider;
all FKs use ON DELETE RESTRICT. Internal IDs are UUID, versions/revisions/order
are BIGINT, timestamps are TIMESTAMPTZ, opaque external IDs are TEXT and checksums
are lowercase SHA-256 VARCHAR(64).

Runs have global PK `sync_run_id`, global positive UNIQUE `run_sequence`, scoped
UNIQUE `(owner,sync_run_id)` and `(owner,sync_run_id,run_sequence)`. Facts have PK
`review_id`, scoped UNIQUE `(owner,review_id)`, and a paired scoped run/sequence
watermark. Observations have PK `observation_id` and scoped UNIQUE keys on
`(owner,review_id,revision)`, `(owner,review_id,observation_id)` and
`(owner,review_id,observation_id,content_checksum)`. Run items use PK
`(owner,sync_run_id,review_id)`, scoped ordinal uniqueness, and the exact
observation/checksum FK. A → B → A can therefore retain three evidence revisions.

Facts' current and ambiguous pointers reference the same review and owner.
`source_order_state='ambiguous'` is paired with a non-null ambiguity pointer;
`current` is the default for an explicitly supplied owner identity. Nullable
text, rating, product ID and `can_answer` are retained without invented values.
Coverage must be a JSON object; typed keys, value types and safe `error_code`
vocabulary remain T4 validation responsibilities.

## Exact opaque-text uniqueness amendment

The current normalizer accepts unbounded external review IDs and source run IDs.
Real PostgreSQL rejected synthetic valid 8 KiB incompressible keys with SQLSTATE
54000 when the complete text was used in B-tree UNIQUE indexes. No business length
limit, truncation or hash identity was introduced.

Only `(owner,external_review_id)` and `(owner,source_run_id)` therefore use logical
uniqueness enforced by BEFORE INSERT triggers, rather than physical B-tree UNIQUE
constraints. The trigger locks the exact canonical account with `FOR UPDATE`,
then performs an exact `COLLATE "C"` text comparison using a fresh READ COMMITTED
snapshot. Exact duplicates raise SQLSTATE `23505` with the safe primary message
`Review identity already exists` and logical constraint name
`uq_review_fact_external` or `uq_review_run_source`. Neither input is echoed.
All UUID-scoped UNIQUE FK targets remain physical constraints.

There is no hash or collision bucket: the existing owner-leading indexes bound
the account scan, but comparison cost grows with that account's stored facts or
runs. Inserts serialize on the canonical account and may contend under high load.
A future performance amendment can add a nonunique digest lookup while retaining
exact equality; this prerequisite makes no throughput claim.

The two insertion triggers reject REPEATABLE READ, SERIALIZABLE and other
non-READ COMMITTED levels with safe SQLSTATE `25000`. An unchanged account row
lock cannot refresh a repeatable snapshot; accepting such inserts would permit
an exact-duplicate race. No existing account row is mutated just to obtain a
serialization conflict. Runtime needs SELECT/UPDATE on the exact account to
take the lock; the existing actual runtime script already grants these rights.

T4 must use READ COMMITTED for new run/fact insertion, lock the canonical account
before identity/replay decisions, and use an exact account-scoped SELECT plus
INSERT/RETURNING. These logical keys are **not ON CONFLICT constraint targets**;
`ON CONFLICT ON CONSTRAINT uq_review_fact_external/uq_review_run_source` is not
supported. Handle a 23505 with rollback/savepoint recovery and a fresh authorized
read, not an automatic retry of the business command. Lock multiple accounts in
deterministic order if a future service introduces multi-account transactions.
Orders 0062 and its existing ON CONFLICT contract are untouched.

## Transactional invariants and runtime ACL

Insert identity with null pointer/version zero, insert the observation, then
advance the pointer and version to one in the same transaction. The deferred
constraint trigger reads the final row by full identity at commit. A committed
half-created fact, a null final pointer or final version zero is rejected.
Every fact UPDATE must increment version exactly once; identity/owner/external ID
and first observation time are immutable. T4 must additionally include the
expected version in its WHERE predicate and reject a zero-row CAS result.

Run identity, owner, source invocation, request checksum, ordering token and
start time cannot change. Running rows can update/finalize only within the
status/completeness/timestamp/manifest constraints. Any UPDATE of a terminal run
is rejected. Observation and run-item UPDATE/DELETE/TRUNCATE are blocked by both
runtime privileges and protective triggers. Trigger messages contain no text or
provider payload. Schema owner operations remain privileged maintenance actions;
the migration does not introduce a runtime restore bypass.

All four relations ENABLE and FORCE organization RLS with both USING and WITH
CHECK on transaction-local `app.organization_id`. Missing/wrong organization
cannot read or mutate the scoped rows. RLS is not allowed-account authorization.

Runtime is a non-owner NOSUPERUSER/NOBYPASSRLS role. On facts it receives
SELECT/INSERT/UPDATE; on observations and run items SELECT/INSERT; on runs
SELECT/UPDATE plus this explicit INSERT column list:

```text
sync_run_id, organization_id, marketplace_account_id, marketplace,
source_run_id, request_checksum, status, completeness, started_at, completed_at,
observed_count, manifest_checksum, coverage, error_code
```

`run_sequence` is GENERATED ALWAYS AS IDENTITY. There is no table-level run
INSERT grant and no INSERT privilege on that column. T4 must omit the column,
including explicit DEFAULT entries, and use `RETURNING sync_run_id,run_sequence`.
Explicit values, `OVERRIDING SYSTEM VALUE`, sequence UPDATE/setval and run sequence
UPDATE are denied. The identity sequence grants USAGE only, without grant options.
No new relation grants DELETE/TRUNCATE/REFERENCES/TRIGGER or DDL rights.

The expand migration intersects every existing new-object grantee's inherited
ACL with permitted rights before commit, including PUBLIC; broad run INSERT is
converted to the column list. SELECT-only grantees are not widened. Old object
ACLs and global defaults are not modified by the migration. The actual runtime
script checks all required FORCE RLS flags before writes and applies broad grants
and exact overrides in its existing atomic transaction.

## Downgrade and remaining service gates

Downgrade locks all four relations before checking emptiness with row_security
off. Any nonempty relation blocks removal. A non-bypass caller whose RLS view is
empty is refused rather than allowed to erase hidden rows. Empty removal drops
the circular pointer FKs, tables and trigger functions in dependency order,
without CASCADE. No archival/removal authorization is implied.

T4 still must prove fresh membership and selected-account authorization, account
disconnect/rebinding checks, typed coverage/error validation, complete manifest
and count agreement, terminal replay comparison, monotonic evidence revisions,
CAS rollback, provider freshness, watermark advancement, ambiguity admission and
clearance, partial coverage behavior, account lock ordering, and no provider I/O
inside database transactions. SQL run order is not provider freshness proof.
No ORM/domain/service, wheel-registration or activation acceptance is claimed.

## Verification

### Accepted follow-up

Schema commit `56b5fa50b2de41a4f3277392148012233952188c` independently passed
148 PostgreSQL tests in the controller (43.12s) and coordinator (48.80s).
Review identified a remaining historical-fixture/current-script coupling.
`b9cb131c0fd234ca12941f46b4ade5899c20f50e` removes that duplicate script call
from the pinned0063 test and adds a structural target/call regression; latest-head
ACL/ordering acceptance remains intact. Its focused regression was RED then GREEN;
the same four-file command below now yields **149 passed, zero skips, 34.93s**.
`e396b08556b6a47bdf4bbf6ddf6eb9008c34c5a7` only sorts0063 imports.
Scoped three-file Ruff using the coordinator-approved static tool, own-venv
compileall and diff checks pass; independent scoped review accepts both fixes.
Runtime SQL is unchanged. This supersedes the initial missing-Ruff note below.

The source-run concurrency case directly proves an INSERT waiting inside the
exact-text trigger. The review-ID branch also serializes through its earlier run
creation; it proves transactional duplicate protection, not an independently
waiting fact INSERT. Strengthening that branch remains a nonblocking test note,
not a claim of additional executed proof. All allocated resources were removed.

### Initial implementation evidence

Final matrix: **148 passed, zero skipped, exit 0, 35.02 seconds**: 47 Review
tests plus all 101 retained Orders candidate/integration/contract tests. This
includes empty→head migration without stamp, populated 0062→0063 preservation,
empty 0063→0062→0063, actual runtime-script atomic failure/RLS guards, expand-time
ACL intersection, server-only ordering, scoped references, exact long text,
final-row deferred checks, CAS, and evidence/terminal immutability. Both concurrent
READ COMMITTED inserts establish a read and demonstrably wait on the account lock
before one commits and the other receives exact 23505. REPEATABLE READ and
SERIALIZABLE insertion refusal are both exercised.

Bounded Review feature roundtrips target revision 0063 explicitly; graph checks
require one head with 0063 in its ancestry. Actual shared runtime-script fixtures
use the latest head, so future migrations do not weaken or invalidate the
historical feature proof. Orders feature fixtures remain pinned to 0062.

From `backend`, the exact final test command was:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-facts-schema/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_review_facts_schema.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py
```

`compileall -q` for Alembic and both changed test files, sole-head graph inspection
and `git diff --check` passed. The own venv has no `ruff` module; optional Ruff
invocation failed with that missing-module message and no tool was installed.
libpq prints its expected `/dev/null` is not a plain password-file warning under
the deliberately disabled password-file setting; test execution and cleanup pass.

Tests use only own fresh random database/roles on PostgreSQL 16.15
(Homebrew), through the exact local Unix socket `/private/tmp/.s.PGSQL.5432`, with
environment scrubbed, IP networking and secret-file reads denied by sandbox.
Every disposable resource has try/finally cleanup and exact absence checks.
The native initdb fixture and full backend suite are not newly certified here.
