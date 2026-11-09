# T4 — lossless0065 dormant writer acceptance

Decoder-first rollback checkpoint: **dab11ec** on expanded0065. Exact schema
c68cd31 was accepted and merged74bde41. This slice writes all8paired fields as
BYTEA + SQL NULL; optionalNone stays null/null and empty text is empty bytes.
JSONB mapping uses none_as_null=True so coverage=None means SQL NULL, not JSON null.
No old observation or identity is rewritten. Existing checksum/manifest construction,
immutable evidence, account serialization, CAS and exact COALESCE lookup are retained.

## Evidence

- All8pair actual repository tests: initial3RED on old NUL source-run writes;
  GREEN after writer switch. Text variantsNone/empty/NUL+combining+emoji; product,
  review/run identity, metadata versions/status and coverage name also contain NUL.
  Raw stored pairs, original normalized checksum, terminal manifest and new-session
  exact replay are verified, not just the codec's returned values.
- Mixed legacy fixture contains a valid TEXT identity, complete old run and old
  observation. Replay preserves old runUUID; new source produces byte observation
  revision2 on the same reviewUUID. Original TEXT identity/observation remain intact,
  and exact mixed query finds one fact.
- Historical NUL failure characterization intentionally failed after support arrived;
  replaced with actual durable NUL roundtrip, terminal completeness and item count.
  Separate rollback/CAS tests are retained. Three direct test SQL queries now use
  COALESCE exactbyte comparison: old TEXT-only count could falsely report zero.
- Actual guard consumer runs ASCII and NUL keys for successful publication/new-session
  read and whole-root rollback on later command conflict. No SAVEPOINT or catch-and-commit.
- Final **622 passed in38.86s**, exit0/no skips:461 Review/adjacent+161 shared guard.
  Owned Unix-only disposable PostgreSQL/runtime-role fixtures verify exact cleanup;
  inherited /dev/null warning unchanged. Scoped Ruff/compile/diff0. Independent
  writer review found no important defects. Not the full application suite.

Command: same16-file scrubbed Unix-only run in decoder report, additionally
tests/test_publication_guard.py and tests/test_publication_guard_postgres.py.

## Rollback and remaining work

Rollback after byte writes MUST retain a decoder-capable binary (dab11ec or later)
and expanded0065. Old TEXT-only artifacts and downgrade are unsafe; migration
correctly refuses any byte-backed data. No production maintenance/DDL/ACL authority
is granted by this test result, and no deployment/backfill occurred.

This closes representation compatibility for the dormant repository, not the whole
Reviews stage. Next: authenticated same-fetch shadow composition, actual Review
revocation/publication races and read API. Policy/draft/approval, send and notification
DDL/services remain distinct stages. No real provider/decryption/fetch, live send,
production, current service activation, flags, printing/export, push or schema edits.
