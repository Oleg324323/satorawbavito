# Lossless Review consumer implementation plan

> **For agentic workers:** Use executing-plans and test-driven-development inline.

**Goal:** Consume accepted0065 without changing semantic identity, checksums or manifests.

**Architecture:** Decoder-first commit adds all eight ORM pairs and strict readers,
keeping legacy writers. A separate writer commit emits bytes-only scalar/coverage
fields and retains mixed old/new replay. No current route/task is enabled. Guarded
same-fetch service composition follows only after this storage acceptance.

**Tech Stack:** Existing SQLAlchemy/PostgreSQL/pytest; no dependencies or migrations.

**Spec:** T1 lossless handoff atc68cd31660ea52a1a003cf5809f14692a2f2d9f9, fully read;
T4canonical_contract/ingestion_contract and guard acceptance30f1f15.

## Constraints

Exact schema merged in the existing T4 worktree only. No production/backfill,
provider actions, flags, shared auth/schema changes or SQL-driven stripping.
Future operational migration requires exclusive privileged DDL/ACL maintenance.
After any byte writes rollback retains a decoder-capable binary on expanded schema;
downgrade is not data conversion. Tests use owned Unix-only disposable PostgreSQL.

## Task 1: decoder-first compatibility

Files: new app/reviews/lossless_storage.py, tests/test_review_lossless_codec.py,
tests/test_review_lossless_repository.py; modify canonical_orm.py/canonical_repository.py.

- [ ] Pure RED tests for `decode_scalar_pair(row, key, required=False, allow_empty=True)`:
  legacy/bytes/null/empty/NUL/combining/emoji preserved; dual/missing required,
  malformedUTF8/wrongtype/surrogates denied with REVIEW_STORAGE_INVALID.
- [ ] Add `encode_scalar_pair` returning `{key: None, key+'_utf8': bytes_or_None}`;
  strictUnicode, no trimming. Tests use literal expectedbytes, never encoder-built expectations.
- [ ] Coverage decode/encode uses compact sorted ensure_ascii=False allow_nan=False JSON;
  reject duplicate JSON object keys and malformed/nonobject/invalidsemantic coverage.
  Reuse snapshot_manifest((), coverage, 'partial', {}) for semantic admission, not SQL jsonb.
- [ ] ORM maps all8new BYTEA columns, old required columns nullable. Decoder chooses
  `is not None`, never truthiness. Identity queries use exact
  `func.coalesce(table.c[key+'_utf8'], func.convert_to(table.c[key], 'UTF8')) == key.encode('utf-8')`.
- [ ] Decode every selected run/fact/observation pair before use. Hydrate actual
  NormalizedReviewFact from joined immutable observation identity/source-run metadata
  and recompute existing content checksum; mismatches fail safely, no representation hash.
- [ ] ActualPG RED→GREEN reads direct synthetic byte rows and ordinary legacy rows;
  validates null/empty and exact checksum. Existing writers remain legacy in this commit.

## Task 2: bytes-only dormant writers

Files: canonical_repository.py; lossless repository tests; replace old limitation
test_review_nul_storage_boundary.py with successful lossless acceptance.

- [ ] RED same actual repository roundtrip with NUL in all7scalar fields and coverage
  stream name; expected original semantic checksum and manifest unchanged.
- [ ] Write scalar pairs bytes+NULL, optionalNone null/null, coverage serializedbytes+NULL.
  Reserve/replay uses decoded source-run keys; terminal comparison uses validatedcoverage.
- [ ] Prove mixed old/new exact identity replay, no duplicate runs/facts, new-session
  reads, changedcontent/CAS, tenant/account isolation, longkeys/combiningdistinctness.
- [ ] Exercise same dataset under accepted root publication guard with explicit
  authority and no savepoints; assert rollback and old-to-new compatibility. No actualfetch claim.
- [ ] Run new+old Review/guard/schema suites, Ruff/compile/diff; independent critic,
  fix findings, exact evidence/limits and decoder-capable rollback artifact in handoff.

No catch-and-commit in root mode. Public/authenticated service, shared session
principal capture, same-fetch hook and audit are separate from repository parity.
