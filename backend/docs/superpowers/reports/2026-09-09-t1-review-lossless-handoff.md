# T1 → T4: Review Facts lossless schema handoff

Additive revision `20260909_0065` follows actual `20260909_0064`. Domain codecs,
ORM/repository behavior, checksums/manifests, user publication guard and activation
remain T4-owned. This is a schema prerequisite, not production or repository parity.

**Operational prerequisite:** run migration only during exclusive privileged
DDL/ACL maintenance. Actual two-session PostgreSQL testing showed that an owner
`GRANT INSERT` can cross the PUBLIC-ACL preflight while ACCESS EXCLUSIVE relation
locks are held. Those locks stabilize data/DDL, not privileged ACL changes. No
catalog/global locks or operational ACL remediation are introduced. This slice
ran only in random disposable synthetic databases; it performed no deployment.

## Representation and decoder contract

| Relation | Legacy field | New BYTEA field | Pair requirement |
| --- | --- | --- | --- |
| review_facts | external_review_id | external_review_id_utf8 | Exactly one; nonempty |
| review_observations | external_product_id | external_product_id_utf8 | At most one; nonempty bytes if present |
| review_observations | text | text_utf8 | At most one; empty bytes valid and distinct from None |
| review_observations | source_status | source_status_utf8 | At most one; nonempty bytes if present |
| review_observations | source_schema_version | source_schema_version_utf8 | Exactly one; nonempty |
| review_observations | normalization_version | normalization_version_utf8 | Exactly one; nonempty |
| review_sync_runs_v2 | source_run_id | source_run_id_utf8 | Exactly one; nonempty |
| review_sync_runs_v2 | coverage | coverage_utf8 | Exactly one; valid JSON object |

All legacy columns/checks/data remain. Only the five required legacy fields drop
NOT NULL; new pair checks preserve requiredness. Optional legacy product/status
checks are not retroactively tightened. All seven new scalar columns validate
strict UTF8, including unchanged NUL, Unicode, combining sequences and unbounded
identities. No trimming, normalization, sentinel, hash identity or byte length cap.

`public.review_strict_utf8(bytea)` uses PostgreSQL's decoder on separate byte
chunks between literal NUL positions; it does not join chunks or drop bytes.
Empty/all-NUL inputs are valid; a UTF8 sequence split by NUL is invalid. It catches
only `character_not_in_repertoire`. `public.review_coverage_json_object_utf8(bytea)`
decodes serialized JSON bytes to PostgreSQL `json`, never JSONB, and requires
`json_typeof(...)='object'`. It catches only encoding/JSON syntax exceptions.
Escaped NUL inside nested strings is admitted; malformed UTF8/JSON, raw NUL and
non-object roots are rejected. Both functions are IMMUTABLE, STRICT, SECURITY
INVOKER with fixed `pg_catalog,public` search_path.

Readers must reject dual nonnull or missing required pairs before hydration;
select BYTEA with `IS NOT NULL`, strict-decode unchanged bytes, otherwise consume
the legacy value. New scalar writers write bytes and NULL the legacy field;
optional None writes null/null. Coverage writers retain T4's canonical compact,
sorted-key, ensure_ascii=False/allow_nan=False JSON encoding. Validate the decoded
semantic object and existing content checksum/manifest; representation fields
must not enter hashes. SQL object admission does not validate coverage semantics,
duplicate names, canonical key ordering, manifest equality or user authorization.

## Exact identity, lifecycle and grants

The two branches of `review_exact_identity_guard()` compare:

```sql
COALESCE(external_review_id_utf8, convert_to(external_review_id,'UTF8'))
COALESCE(source_run_id_utf8, convert_to(source_run_id,'UTF8'))
```

T4 lookup/replay must use the corresponding same expression. Existing account
FOR UPDATE, READ COMMITTED restriction and fresh full-equality statement after
the wait remain. Logical SQLSTATE23505 conflicts remain `uq_review_fact_external`
and `uq_review_run_source`, with fixed message `Review identity already exists`.
No index over the complete unbounded identity and no ON CONFLICT shortcut is added.

`review_fact_guard()` and `review_run_guard()` additionally freeze new identity
members. Representation swapping after INSERT is forbidden. All original owner,
timestamp, version and terminal-state conditions remain. Existing observation
immutability covers the new fields. UUIDs, scoped FKs, revision/current pointers,
watermarks, sequence issuance and ENABLE/FORCE RLS are unchanged. Coverage updates
remain within the existing running→terminal lifecycle. Org RLS does not replace
same-org allowed-account/session authorization at publication.

Migration preflight inspects table and column ACLs on all three affected tables.
PUBLIC INSERT/UPDATE is rejected before DDL with SQLSTATE55000 and fixed message
`review_lossless_acl_unsupported`; PUBLIC SELECT-only remains supported. It does
not revoke existing PUBLIC writes. Existing named run-column writers gain only
their paired new column, preserving privilege and grant-option shape. Table
rights naturally include new columns; inheritance remains on existing grantees.
Current writers, including separate table owners, receive required helper EXECUTE.
PUBLIC/helper default grants to unrelated readers are removed only on the two
new helpers. Existing/default ACLs and old function ACLs remain unchanged.

The actual runtime SQL script adds source_run_id_utf8/coverage_utf8 to the run
INSERT allowlist and grants both helpers after broad grants within its existing
transaction. No run_sequence INSERT, setval, delete/truncate, policy/trigger or
function mutation authority is added. Latest-script tests are separate from the
historical feature fixture pinned to0065; ancestry gates accept a future successor.

## Rollout and rollback

Deploy and verify decoder-capable readers before enabling byte writes. Old
TEXT-only binaries cannot faithfully read these rows. After any byte writes,
rollback keeps a decoder-capable application on the expanded schema.

Downgrade takes fixed-order ACCESS EXCLUSIVE locks on runs, facts, observations,
sets local row_security=off as a refusal defense and checks every new column with
genuine all-row visibility before DDL. Any byte-backed row refuses SQLSTATE55000
with `Review lossless downgrade blocked: byte-backed data`; FORCE RLS hidden rows
cannot masquerade as empty. Old-only downgrade restores actual0063 function bodies
and required NOT NULL exactly, removes only new constraints/columns/helpers, and
does not CASCADE, convert or delete data. CHECK validation may scan data and DDL
locks may wait; no zero-cost/online rollout claim.

## Verification and limits

Final authorized adjacent suite: **390 passed in187.59s**, exit0, natural
completion, no skips. This includes161 new schema/runtime cases and229 unchanged
adjacent Review/Orders cases. Own-runtime compileall, scoped Ruff and diff checks
exit0. Evidence is recorded in the plan-owned `task-1-report.md`, including exact
command prefix, RED/GREEN results, temporary identifiers and cleanup absence.

Actual0064 representation controls: seven TEXT insert paths reject admitted
synthetic NUL with SQLSTATE22021; escaped NUL coverage rejects with22P05. Every
case has a successful old-row control and savepoint rollback. Separate missing
revision RED is not passed off as representation evidence. New tests cover all
eight semantic roundtrips, UTF8/JSON boundaries, optional None/empty distinction,
exact mixed identity conflicts in both directions, long/NUL/combining keys,
different accounts, two actual guarded INSERT waits, immutable identity swaps,
runtime/tenant/FK/ACL denials, grant options/inheritance/default-reader exclusion,
PUBLIC refusal, no old-row rewrite, exact function restoration, byte-blocked and
FORCE-RLS-hidden downgrade, and the privileged GRANT coordination limitation.

Existing `/dev/null` passfile warnings remain disclosed. Native initdb was not
claimed: approved existing Unix maintenance PostgreSQL allocated/dropped only
random owned disposable databases/roles, with exact catalog absence assertions.
No real environment/secrets/providers/Redis/network, package installation, push,
production migration, backfill, flags or decoder activation occurred.

Independent controller review is still required before ready-SHA release to T4.
