# T4 — decoder-first0065 acceptance

Exact T1c68cd31660ea52a1a003cf5809f14692a2f2d9f9 controller-approved and merged74bde41.
T4 adds all8BYTEA mappings, strict pair/coverage readers and exact mixed-representation
COALESCE identity/run lookups. Existing writers deliberately remain TEXT/JSONB.
This checkpoint requires expanded0065; it does not run against an unmigrated0064.

Pair decoding rejects dual values, missing required representation, malformedUTF8
and wrong types. IS NOT NULL preserves empty bytes versus null. No Unicode/NUL
normalization, truncation or hash identity. Coverage JSON rejects duplicate object
keys and invalid semantic coverage before replay, including duplicate stream names.
Joined immutable observation+fact+source-run data reconstructs NormalizedReviewFact;
existing semantic content checksum is recomputed before returning observation data.
Representation fields do not enter content/manifest hashes. Owner predicates apply
to all joined relations; UUID/scoped schema constraints remain intact.

Evidence:

- Purecodec initial24RED missingmodule; actualreader4RED after correcting test-only
  complete-unit fixture (T1 schema helper assumes fixed item checksum; own helper
  uses matching observation/item checksum). Final pure36 and actualreader5 cases.
- Actual new bytes rows read with NUL identities/source keys, text null/empty/Unicode;
  mixed lookup reuses existing run UUID. Incorrect stored content checksum and
  duplicate JSON keys admitted by SQL are refused safely.
- Independent critic found deepJSON RecursionError leakage: main1RED reproduced,
  narrow exception sanitization fixed, pure36GREEN and independent fix reviewPASS.
- Fresh combined **455 passed in23.81s**, exit0, no skips. Existing repository35,
  schema48, rootguardconsumer9 and adjacent Review/Notification contracts included.
  Actual owned disposable DB/runtime role at latest head; inherited exact cleanup
  verified by fixture. /dev/null passfile warnings retained. Ruff/compile/diff0.

Reproduction uses the scrubbed Unix-only command prefix in
2026-09-09-t4-review-guard-acceptance.md with test_review_lossless_codec.py,
test_review_lossless_repository.py, test_review_facts_repository.py,
test_review_publication_repository.py, test_review_facts_schema.py,
test_review_storage_payloads.py, test_notification_preferences_read.py,
test_review_send_recovery.py, test_review_provider_lifecycle.py,
test_review_decision_contract.py, test_notification_contract.py,
test_review_canonical_contract.py, test_review_storage_golden.py,
test_review_local_audit_payload.py, test_review_nul_storage_boundary.py,
test_review_metadata_boundaries.py.

Next commit switches dormant writers to bytes and proves all8pair roundtrip/hash,
legacy/new replay and guarded rollback. Current NUL writer limitation still exists
intentionally and its old rollback characterization still passes. No actualshadow
hook, API/authentication composition, audit/send, provider, backfill or deployment.
Future bytes-writer rollback must retain this decoder-capable artifact or a later
compatible binary on0065. Never downgrade byte-backed data or use oldTEXT-only code.
