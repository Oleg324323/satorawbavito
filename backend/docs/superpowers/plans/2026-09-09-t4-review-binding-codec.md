# Review historical binding codec implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline with TDD. User authorizes continued independent work without per-slice approval pauses.

**Goal:** Deliver the exact pure descriptor codec/decoder requested by T1 for its future SQL parity gate.

**Architecture:** A separate Review-specific immutable scalar descriptor and canonical ASCII JSON codec. No Orders positional codec reuse, SQL, repository/service wiring or provenance admission. A valid descriptor is representation, not authority.

**Tech Stack:** Python standard library, pytest; no dependencies.

**Spec:** docs/superpowers/reports/2026-09-09-t4-review-historical-binding-request.md at1a3d344. T1 direct request confirms normalized immutable-from-INSERT columns direction but no DDL READY.

## Global constraints

- Exact keys/schemaVersion1, positive INT4 owner IDs excluding bool, marketplace wb/avito.
- ExternalAccountId nonempty Unicode scalar string, no trim/normalization; nullable credentialRef distinguishes null/empty.
- Sorted compact ensure_ascii=True JSON ASCII bytes, no newline/BOM; SHA256 over exact bytes.
- Decoder rejects duplicate/extra/missing keys, bad types/scalars/checksum and noncanonical spellings by re-encoding.
- Pure NUL is allowed; existing account VARCHAR cannot store NUL. Physical admission stays a separate explicit T1 gate. Do not narrow or normalize the pure contract.
- No auth, public exposure, DB/migrations/DDL, runtime wiring, provider calls, flags or legacy relabel.

### Task 1: Typed codec and literal vectors

Files: app/reviews/historical_binding.py; tests/test_review_binding_codec.py;
tests/fixtures/review_binding_descriptor_v1.json; completion report in docs/superpowers/reports.
Interfaces: ReviewBindingDescriptor frozen repr-hidden scalar dataclass;
encode_review_binding_descriptor(descriptor) -> EncodedReviewBinding;
decode_review_binding_descriptor(raw, *, checksum) -> ReviewBindingDescriptor.
Error: ReviewBindingDescriptorError with only REVIEW_BINDING_DESCRIPTOR_INVALID.

- [ ] Write failing exact ASCII3-vector tests, Unicode/control roundtrips, null/empty/space distinction, INT4/type/scalar boundaries. Example externalAccountId `seller-A`, null ref encodes exact requested bytes and hash71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60.
- [ ] Run isolated pytest to observe missing implementation RED.
- [ ] Implement scalar validation, sorted ASCII JSON encoding and mandatory strict checksum/duplicate-aware decoder. Re-encode decoded descriptor and require byte equality before returning.
- [ ] Add literal Unicode/control canonical bytes with independently calculated Node SHA256; reject alternate escapes/whitespace/key order, exponent/float/bool IDs, invalid bytes, deep malformed JSON and checksum drift. Verify each semantic field affects identity and no metadata leaks through repr/error.
- [ ] Run focused and adjacent pure Reviews suites under network-denied sandbox, Ruff/compile/diff, independent critique. No DB needed and no broader isolation readiness claimed.
- [ ] Record exact ready SHA/tests/limits in local matrix and send directly T1 for SQL parity. Root receives no per-slice report.
