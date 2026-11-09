# T4 → T1: exact metadata admission, not a SQL-driven string ban

Inspected actual canonical_contract.py, repository, accepted0063 and existing
normalizer fixtures. `_nonempty_string` requires a nonempty str equal to strip();
it has no ASCII/version-label grammar, length cap or NUL ban. The current
external_review_id/external_product_id validator is separate and unchanged.

| Field | Actual producer/admission | Checksum identity | Bounded choice |
|---|---|---|---|
| source_status | WB source_status/sourceStatus; Avito stage/source_status; optional opaque value | Included | Preserve accepted provider evidence losslessly, never strip or translate status. |
| source_schema_version | WB/Avito source_schema_version/sourceSchemaVersion override; otherwise provider-version constant | Included | Preserve accepted historical version values; no existing safe-label rule to enforce retroactively. |
| normalization_version | Factory uses current constant; NormalizedReviewFact constructor/hydration admits historical strings | Included | Preserve existing DTO contract; do not assume factory constant is a database allowlist. |
| source_run_id | Trusted caller supplies opaque nonempty run key; reserve_run checks same nonempty/strip rule | Excluded from content checksum; exact account-scoped replay key | Preserve exact identity. No UUID conversion, hash-only identity, truncation or silent NUL removal. |

`test_review_metadata_boundaries.py` freezes12 synthetic cases (four fields ×
standalone NUL, interior NUL, non-ASCII/combining/emoji), through actual normalizers
or historical DTO construction and actual checksum. This is admission evidence,
not proof PostgreSQL0063 can store these strings. All four current TEXT columns
still need a representation decision. No new runtime rejection added by T4.

If the platform later chooses stricter machine-label/run-key admission, it needs
an explicit versioned domain contract and migration compatibility review first.
The present forward text_utf8 proposal can stay text-only, but must not claim
whole ReviewFacts representation parity. A lossless metadata/identity amendment
may be separate; exact replay semantics and immutable scoped ownership must remain.

Text amendment compatibility: encode text with strict UTF8 and decode the same
bytes before constructing the semantic DTO/checksum. Choose BYTEA on IS NOT NULL,
not truthiness: null/null=None, b''=empty string. Existing TEXT-only rows stay
readable; dual nonnull rejected. New bytes-only rows cannot be read by the old
TEXT-only repository without losing content, so decoder deployment must precede
writer activation; rollback must keep a decoder-capable binary. Do not backfill
real rows or rewrite0063 in this slice. T4 will amend its dormant repository only
after committed forwardDDL is accepted.

Adjacent admission to assess separately: coverage.streams[].name uses nonempty/
strip validation too, but coverage persists as JSONB (which cannot represent NUL).
Do not infer complete coverage parity from fixing observation text. This document
does not broaden SQL implementation scope or waive any of these remaining gates.

Verification:12 new characterization cases +38 existing canonical contract cases,
**50 passed in0.20s**, exit0; Ruff0, independent critic scoped PASS. Initial test
fixture lacked mandatory createdAt; corrected the synthetic fixture only, not the
normalizer. No schema/runtime/provider change. Larger adjacent run is separate.
