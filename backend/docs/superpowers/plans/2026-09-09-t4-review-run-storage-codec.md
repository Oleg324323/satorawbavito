# Review run storage codec implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Decode and encode the exact five-column0068 Review binding representation without treating legacy rows as bound.

**Architecture:** A pure storage adapter composes the already accepted descriptor codec. It validates normalized columns against the decoded canonical bytes and checksum, returning an explicit legacy sentinel only when every binding column is present and NULL. No database, authorization, repository publication or public response is added by this slice.

**Tech Stack:** Python, existing dataclasses/Mapping/pytest; no dependencies or SQL execution.

**Spec:** `backend/docs/superpowers/reports/2026-09-09-t4-review-historical-binding-request.md`; T1 exact4d95b24 `backend/docs/superpowers/reports/2026-09-09-t1-review-run-binding-schema-handoff.md` read via Git; physical migration0068 read completely.

## Global constraints

- Default-off behavior and all operational flags remain unchanged.
- No old row is relabelled/backfilled from current account metadata.
- Exact NULL, empty and whitespace credential references remain different.
- Do not expose descriptor/credentialRef in public errors, logs or repr.
- Native SQL NUL admission remains separate from pure Unicode representation.
- T1 alone owns migrations/shared config/registration/ACL/RLS.
- Small pure checks may run offline; PG/browser/build gates use the shared queue.

### Task 1: Exact pure storage adapter

Execution checkpoint: implemented and independently reviewed; actual missing-module
RED, final208purePASS, scopedRuff/diff0. See same-date codec report for exact evidence
and limits. This task does not mark the later repository acceptance complete.

**Files:** Create `backend/app/reviews/run_binding_storage.py` and `backend/tests/test_review_run_binding_storage.py`.

**Interfaces:** Consumes `ReviewBindingDescriptor`, `EncodedReviewBinding`, `encode_review_binding_descriptor` and `decode_review_binding_descriptor` from `app.reviews.historical_binding` unchanged. Produces:

```python
def encode_review_run_binding(descriptor: ReviewBindingDescriptor) -> dict[str, object]: ...
def decode_review_run_binding(row: Mapping[str, object]) -> ReviewBindingDescriptor | None: ...
```

- [ ] Write a literal positive vector, not a serializer roundtrip alone:

```python
def test_exact_nullable_reference_vector():
    descriptor = ReviewBindingDescriptor(1, 11, "wb", "seller-A", None)
    row = encode_review_run_binding(descriptor)
    assert row["account_binding_payload"] == b'{"credentialRef":null,"externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}'
    assert row["account_binding_checksum"] == "71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60"
    assert decode_review_run_binding({"organization_id": 1, "marketplace_account_id": 11, "marketplace": "wb", **row}) == descriptor
```

- [ ] Add all-five-NULL→None; each missing binding column must raise the existing fixed `ReviewBindingDescriptorError`; each partial column must fail. For a bound row, remove/change each owner/normalized field, flip canonical bytes/checksum, supply bool schema/owner, bytes instead of text, invalid scalar and wrong checksum type. Compare exact None/empty/space and Unicode vectors. Additional ordinary run fields are allowed because callers pass real row mappings.
- [ ] Run only the new pure test with scrubbed environment, network-denied sandbox and pytest cache/plugin autoload disabled. Observe import/attribute RED before adding runtime code.
- [ ] Implement two functions. Encoder uses the existing canonical encoder, returns only five named0068 columns. Decoder first requires Mapping and all five keys; returnsNone only if all five values areNone. Otherwise constructs a normalized descriptor from physical owner/provider and normalized values; calls the existing strict byte/checksum decoder; raises the same safe error if descriptors differ. Catch only malformed-input KeyError/TypeError/ValueError into that fixed error; never stringify row values.
- [ ] Run new pure tests plus `test_review_binding_codec.py` and existing lossless scalar/coverage codec tests. Ruff only scoped changed paths and diff-check. Independent read-only critic checks literal vector/missing-vs-NULL and error leakage; fix important findings and rerun.
- [ ] Commit only adapter, its tests, this plan and a precise handoff/report. Do not include unrelated in-progress frontend work.

## Explicit next gate, not implemented by this plan

The actual repository consumer still needs schema chain0066 (`dbf8d31`),0067
(`d87e575` plus mandatory `c4e3e37`) and0068 (`4d95b24`) accepted without importing
unrelated platform changes. Read all exact owned test/grant diffs before integrating.
Then a separate executable consumer plan must cover ORM fields, captured
credentialRef through guarded reserve-before-fetch, replay comparison before I/O,
current/ambiguity/watermark/history validation, all-unit safe409, no rewind or
cross-cabinet merge, completed rebind regression and actual PostgreSQL rollback/
two-session tests. A pure decoder PASS is not any of those acceptance claims.

## Plan self-review

This slice intentionally supplies storage representation only; its independent
acceptance is meaningful without pretending to close the historical reader gap.
Every physical column comes from the exact accepted0068 contract. There are no new
schema numbers, permissions, lifecycle epochs, credential payloads or source rules.
User has already instructed autonomous execution: continue inline without asking
for another execution-method choice.
