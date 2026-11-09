# T4 → T1: review-run-key-v1 and external key Unicode boundary

Explicit domain decision for the unified additive codec: logical keys are Unicode
scalar strings, represented with strict UTF8. Python surrogate code units are not
accepted identifiers; do not encode them with surrogatepass or lossy replacement.
This is a bounded validation amendment, not a blanket PostgreSQL TEXT restriction.
NUL remains allowed in run/review/product keys. Preserve nonempty/unchanged-strip,
numeric-like external-ID rules, leading zeroes, combining sequences, emoji, case
and unbounded lengths. No byte normalization, ASCII-only grammar or hash identity.

`validate_review_source_run_id` applies the review-run-key-v1 rule at both factories,
NormalizedReviewFact hydration and repository reserve before any SQL. Failure is
typed `invalid_string:source_run_id`, mapped to REVIEW_STORAGE_INVALID by repository.
Run key is excluded from fact checksum: do not bump normalization version or alter
existing fact checksums for this admission fix. Stored0063 TEXT cannot already hold
surrogates; no real-data backfill is performed or needed for this rejection alone.

`_external_id` applies the same strict scalar requirement with existing field-specific
safe error codes. ExternalReviewIdentity construction and repository get_fact now
agree; raw read keys are validated before SQL. Normalized external fields previously
failed later as UnicodeEncodeError; standalone identity previously admitted them.
Positive integer conversion and all valid Unicode/NUL cases remain unchanged.

Separate uncovered NUL representation requirement: actual `_external_id` admits NUL
for external_review_id and external_product_id. Four new factory/checksum cases
confirm this; T1's unified codec must include both columns, not assume the external
validator makes TEXT safe. Source metadata/coverage from a836560 remain unchanged.

TDD:15 new surrogate boundary cases fail before implementation (factory/identity
admitted invalid values or repository touched SQL); existing16 metadata/NUL cases
pass. After implementation selected metadata+canonical+storage golden/serializer
**135 passed in0.52s**. Final combined scoped suite **405 passed in17.35s**, including
actual disposable PostgreSQL repositories/schema and adjacent Reviews/Notifications;
no skips. Ruff/diff0 and independent critic PASS. No schema/HTTP/provider
actions. UTC alias/isinstance cleanup is semantic-neutral; two existing deliberate
catch-all DTO/tzinfo boundaries retain behavior with explicit narrow lint rationale.
