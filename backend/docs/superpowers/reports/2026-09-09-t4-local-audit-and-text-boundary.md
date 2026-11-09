# T4 → T1: local audit representation and source-text storage boundary

## Local encoder (dormant, not persisted audit)

`app/reviews/storage_payloads.py:encode_review_local_audit` implements only the
five local events of 8f4a6c4/b17638f: policy.created, policy.selected,
draft.published, decision.approved, decision.rejected. No send/worker events.
The closed dictionary is typed by runtime validation and produces the existing
EncodedStoragePayload bytes/checksum. No auth, DB writes, routes or schema.

Exact envelope keys: schemaVersion, organizationId, marketplaceAccountId,
marketplace, eventId, aggregateId, aggregateVersion, eventKind, occurredAt,
actorKind, actorMembershipId, commandId, draftId, policyId, decisionId,
attemptId, beforeState, afterState, reasonCode. No omitted nullable keys or
extra metadata. Owner explicitly includes marketplace (wb|avito), as in other
Review canonical payloads; T1 must use these exact bytes, not a two-key owner
variant. Org/account/membership are positive PostgreSQL INTEGER-range values;
aggregateVersion is a positive Python integer, including >BIGINT. Policy and
generation serializers' preexisting integer admission is unchanged.

Local actor is membership only; reason always null. Required references and
head initial/version transition matrix match b17638f. Policy-created aggregate
equals policyId. UUIDs are canonical nonzero strings; timestamp is exact UTC6.
All other references are explicit null. The encoder cannot validate actual
stored before-state, source ownership, membership freshness, distinct aggregate
types, immutable witness chains or transactionality: those remain DB/service gates.

Synthetic fixture `tests/fixtures/reviews/local-audit-v1-golden.json`: decode outer
JSON once, then UTF8-encode canonicalUtf8Json. Lossless JSON parsing gives typed
input. SHA256 literal76c1f81b4cad8eb1fddfbad5f3d7a9705cb9f5e30db58a6398413e67a6412a17.
The earlier storage-v1-golden fixture remains an immutable historical snapshot
that correctly said audit was not implemented at 5d4ac9a; this file supersedes
that limitation for encoding only, not audit persistence.

TDD: first collection failed because encoder did not exist; implementation then
47 cases passed, plus frozen bytes/hash. Independent critic identified an impossible
transition from decision_current to new version2: earliest decision is version2,
so transitions from it require version>=3. Three new cases reproduced48PASS/3FAIL;
fixed guard brings local tests to51. Combined local51 + previous106 =157 cases.
Fresh result: **157 passed in0.13s**, exit0; scoped Ruff/diff0, independent
fix re-review PASS. No DB witness proof.

## Review Facts source text: confirmed unresolved boundary

`canonical_contract._optional_text` retains NUL; accepted0063 observation text is
PostgreSQL TEXT. Disposable migrated real-runtime-role test confirms ingest fails
safely with REVIEW_STORAGE_UNAVAILABLE: savepoint leaves no fact/runitem and does
not mark the run complete. Same identity/run/transaction with a plain text control
succeeds. `test_review_nul_storage_boundary.py`: **1 passed in2.52s**, exit0.

This is a rollback characterization, NOT a test proving lossless NUL storage.
Replace it with round-trip acceptance when T1 supplies a forward-compatible
representation decision. No truncation/stripping, rewriting0063, shared migration
or application data mutation by T4. T1 was directly notified; a delivery receipt
does not imply acceptance or an implemented fix. Shadow activation remains gated.
Tests used the existing approved sandbox and fresh disposable resources/cleanup;
no provider requests, production, host-service changes or existing application DB.
