# Review Facts lossless forward representation

Status2026-09-09: T1 local architecture decision, not implemented or activated.
Preserve admitted NUL and exact semantic identity/content, with one additive
forward schema slice. No edits to0063/0064, real data/backfill, provider calls or
domain normalizer files. T4 owns codecs and repository integration.

## Evidence and authority

- Actual0063 stores all open source strings as PostgreSQL TEXT and coverage as
  JSONB with `jsonb_typeof(coverage)='object'`. Both representations reject NUL
  that successfully normalized Review facts/coverage can contain.
- Exact T4 `316eaf8a1c2f8b4d83498c7976db22bf0e115674` proves safe rollback of a NUL
  text ingest and a succeeding non-NUL control, not lossless parity.
- Exact `a836560dd6dd3b2d8a8a60db0d67cee0943c12f1` characterizes admitted NUL in
  source_status/source_schema_version/normalization_version/source_run_id and
  identifies coverage stream names. No ASCII/version-label grammar exists.
- Actual `_external_id` also admits NUL in external_review_id/product_id; JSON
  checksum encoding escapes it. These fields are included in this design, not
  silently treated as safe PostgreSQL identifiers.
- An unpaired surrogate differs: checksum/manifest strict UTF8 encoding cannot
  produce canonical fact/coverage evidence, but source_run_id is excluded from
  checksum and reserve_run can reach persistence without it. T4 explicitly chose
  `review-run-key-v1` Unicode-scalar admission before SQL, preserving NUL. Exact
  T4 `268ea1d52aa152a708760b40a8ddeb0c1f7189c1` implements and documents factory/
  DTO/reserve validation and standalone external-identity/get validation before
  SQL. Its full handoff was read:15actualRED→135focused/405combinedPASS17.35s,
  with attributed actualPG proof and no skips. Domain code stays T4-owned and
  must accompany the future repository codec. No surrogatepass/replacement or
  hidden SQL-driven domain rejection.

## Choice and alternatives

Retain old TEXT/JSONB columns; add nullable BYTEA alternatives with strict UTF8.
Do not rewrite all old data or change existing content/manifest checksum inputs.
In-place TYPE conversion would break old binaries immediately; escaped sentinels
inside TEXT/JSONB would create a new string grammar and risk identity/equality
ambiguity. The chosen additive scheme has explicit mixed-reader restrictions and
requires decoder-capable rollback once new writes start.

| Relation | Old field | New BYTEA field | Pair semantics |
| --- | --- | --- | --- |
| review_facts | external_review_id | external_review_id_utf8 | exactly one, nonempty |
| review_observations | external_product_id | external_product_id_utf8 | at most one, nonempty if present |
| review_observations | text | text_utf8 | at most one; empty is valid |
| review_observations | source_status | source_status_utf8 | at most one, nonempty if present |
| review_observations | source_schema_version | source_schema_version_utf8 | exactly one, nonempty |
| review_observations | normalization_version | normalization_version_utf8 | exactly one, nonempty |
| review_sync_runs_v2 | source_run_id | source_run_id_utf8 | exactly one, nonempty |
| review_sync_runs_v2 | coverage | coverage_utf8 | exactly one, valid JSON object |

Drop NOT NULL only on the old members of required pairs. Existing old CHECKs
remain and accept NULL by normal SQL CHECK semantics; new pair constraints ensure
required values cannot disappear. Every new nonnull scalar BYTEA must be strict
UTF8, with nonempty checks only where the existing domain requires it. Optional
null/null is None; b'' is distinct from None and is permitted for text. No dual
nonnull row, even if representations happen to decode equal. No length cap,
normalization, trimming, hash identity or new unbounded B-tree key.

## Validation helpers

`public.review_strict_utf8(bytea) -> boolean`: IMMUTABLE, STRICT, fixed qualified
search_path, SECURITY INVOKER. Scan original BYTEA for literal00 separators and
validate each intervening chunk with built-in `convert_from(chunk,'UTF8')`.
Catch only expected encoding errors into false; unexpected SQL failures abort.
Never concatenate chunks or delete NUL before validation: C2 00 A0 must fail even
though C2 A0 alone would be valid. Empty and all-NUL strings are valid UTF8. This
uses PostgreSQL's maintained decoder instead of a handwritten UTF8 state machine.
The stored byte sequence is never modified or decoded wholesale into NUL-bearing
PostgreSQL TEXT.

`public.review_coverage_json_object_utf8(bytea) -> boolean`: IMMUTABLE, STRICT,
fixed search_path, SECURITY INVOKER. Strict-convert the serialized JSON bytes to
TEXT, cast to PostgreSQL `json` (not jsonb), require `json_typeof(...)='object'`.
Catch only expected encoding/JSON syntax errors. JSON's ASCII `\u0000` escape
must parse without materializing the decoded value as PostgreSQL TEXT; prove this
against disposable PostgreSQL before acceptance. Raw literal NUL in JSON input,
malformed/truncated JSON, array/scalar/null roots must fail. This preserves the
actual0063 JSON-object guarantee, not just arbitrary UTF8 or syntactic JSON.

The SQL check does not claim canonical whitespace/key order, duplicate-name
rejection, manifest equality or semantic coverage validation. T4's existing
snapshot_manifest/typed decoder remains required before replay or publication.
No JSONB coercion or extraction of NUL-bearing nested strings is used in DDL.

## Exact identities, lifecycle and immutability

Both branches of the actual shared function `review_exact_identity_guard()`
must compare semantic bytes:

```sql
COALESCE(new_field_utf8, convert_to(old_field, 'UTF8'))
```

The run branch uses source_run_id; the fact branch uses external_review_id.
Keep existing account FOR UPDATE, READ COMMITTED-only admission and a separate
fresh full-equality SQL statement after the wait. Preserve both original logical
23505 names `uq_review_run_source` and `uq_review_fact_external`. Old-TEXT/new-BYTEA
representations of one semantic key conflict; different accounts remain separate.
No digest accelerator or full-unbounded-BYTEA index is added.

Replace `review_run_guard()` to include both source_run_id members in immutable
identity; replace `review_fact_guard()` to include both external_review_id members.
No representation swap is allowed after INSERT. Keep all existing owner/time/
version/status checks and scoped FK targets. Immutable observation triggers already
cover new columns. Fact UUID/revision/pointer/source sequence/checksum contracts
are unchanged. Coverage remains mutable only through the existing running→terminal
run lifecycle, not an independent successful cache or alternate business store.

## T4 codec and service contract

Defensively reject dual nonnull and required null/null before hydration. Choose
BYTEA by IS NOT NULL, not truthiness; strict-decode unchanged bytes, otherwise use
old value. Apply exact existing domain validation and semantic checksum/manifest
comparison. `external_review_id` and source_run_id exact repository lookup must
use the same semantic byte expression as SQL uniqueness, not old text alone.

New scalar writer writes strictUTF8 bytes and NULL old field; None writes both
NULL only for optional fields. Coverage writer uses existing compact/sorted-key,
ensure_ascii=False/allow_nan=False canonical JSON encoding, and reader validates
the parsed semantic object with T4's existing coverage contract. Representation
fields do not enter content/manifest hashes. Never reinterpret an escaped string
twice, normalize combining text, strip NUL, replace unencodable content or hide a
decode/DB error with old-store/cache success.

Source-run Unicode-scalar validation and standalone external identity read
validation reject surrogate input with typed safe errors before SQL through
T4's explicit268ea1d boundary, not an invented T1 normalizer change.
The SQL storage contract alone cannot authenticate user/session/allowed-account
authority; publication guard remains necessary for writes and replay returns.

## Migration, RLS and ACL

Install additive columns/pair/validation checks and replace three named trigger
functions in one forward migration at actual next free head. Existing rows are
not rewritten/backfilled; CHECK validation may scan the affected relations and
DDL locks can wait. Do not call it zero-cost or online deployment proof.

All three affected tables retain ENABLE/FORCE RLS and existing org policies/FKs.
Existing table SELECT/INSERT/UPDATE privileges retain their exact allowed shape;
run INSERT is column-scoped and must gain only source_run_id_utf8/coverage_utf8,
never run_sequence. At upgrade, account for existing grantees of table and column
rights, including role inheritance; extend only a grantee already able to write
the corresponding old run column. Do not grant new business table operations or
rewrite unrelated/default ACLs. SELECT-only roles must not become writers.

Validation helpers have PUBLIC EXECUTE revoked and explicit EXECUTE only for
required current writers (owner is implicit); grant no mutation/table access to
reader-only roles. Runtime script updates new column/helper grants after broad
grants inside its existing transaction. Test actual preexisting column grants,
runtime/script idempotence, grant options and sequence prohibition. Never use
SECURITY DEFINER merely to bypass CHECK helper permissions.

PUBLIC writer preflight: actual0063 can preserve PUBLIC INSERT/UPDATE table or
column grants. There is no finite named-role extension that preserves that
arbitrary-role writer contract while denying PUBLIC helper EXECUTE. Before any
DDL, acquire ACCESS EXCLUSIVE on all three affected tables in the same fixed
order as downgrade, then reject such grants with fixed SQLSTATE55000
`review_lossless_acl_unsupported`; inspect both relacl and attacl. Preserve all
old grants/schema/data on refusal. PUBLIC SELECT alone is not a writer and does
not block this migration. Do not silently revoke old PUBLIC writes, enumerate
all current roles as a substitute for PUBLIC, or make helpers SECURITY DEFINER.
Real remediation of a detected PUBLIC writer grant requires a separate owner-
approved ACL change; no such operational change is performed by this slice.
LOCK itself is not DDL or an ACL/data mutation. Any claim that it fences concurrent
GRANT requires an actual two-session PostgreSQL test; otherwise exclusive
privileged DDL/ACL maintenance remains an explicit operating precondition. No
catalog/global role-lock expansion or hostile database-owner sandbox is promised.

Downgrade locks all three affected tables ACCESS EXCLUSIVE in fixed order and
proves all new byte columns NULL with genuine all-row visibility. row_security=off
is a refusal defense, not a bypass privilege. Any byte-backed row blocks downgrade
atomically. When every new column is NULL, required-pair checks prove old required
columns remain populated: restore original three trigger definitions/NOT NULL,
drop only new checks/columns/helpers without CASCADE. Old rows remain unchanged.
No data conversion/deletion to force downgrade; after bytes writes rollback means
a decoder-capable application on the expanded schema.

## Required acceptance

- Empty bootstrap and previous-head production-shaped synthetic old rows upgrade;
  old content/row identities/constraints/ACLs unchanged, no stamp or row rewrite.
- Every scalar field NUL at start/interior/end/alone, non-ASCII combining forms,
  emoji, long incompressible IDs, optionalNone and emptytext exact round-trip.
- Literal00 valid; C2 00 A0, stray continuation, truncation, overlong encodings,
  surrogate encodings and above-U+10FFFF fail. No helper/input fragments in safe
  external errors; unknown SQL errors propagate to safe service boundary.
- Pair conflicts/missing required values, immutable swaps, terminal mutations,
  FK/ownership violations reject; old/new exact identity duplicate race and
  distinct-account case preserve both23505 names and refreshed account wait proof.
- Coverage object with escapedNUL name passes DB object admission and T4 manifest
  decode; array/null/scalar/malformed/rawNUL fail. Semantic invalidity/duplicate
  names must fail at T4 publication, not claimed as DB-only validation.
- Runtime role/new column INSERT allowed; missing/wrong tenant, delete/truncate,
  sequence override/setval, broad inherited privilege/reader escalation denied.
- PUBLIC table/column INSERT or UPDATE preflight refuses before DDL with unchanged
  ACLs/revision; PUBLIC SELECT-only remains compatible and gains no write rights.
- Old-only data downgrade→upgrade succeeds unchanged; any new bytes or hidden
  bytes refuse without dropping schema/data. Historical feature fixture pinned;
  actual latest runtime-script test separate, one-head ancestor gate successor-safe.
- T4 actual repository ingest/read/checksum/replay tests consume ready DDL and
  replace rollback-only characterization with lossless acceptance. DDL tests do
  not substitute for T4 codec/user-publication tests or production observation.

No production activation or release parity is claimed by this design. No secret,
customer content, real operational snapshot, provider call, backfill, migration
execution or deployment occurred in preparing it.
