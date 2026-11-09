# T4 → T1: existing storage serializer golden fixtures

Synthetic fixture: `tests/fixtures/reviews/storage-v1-golden.json`.
Source implementations: policy/generation serializers `4b349ec` + `92ba34c`;
decision binding `98d1331` (unchanged by this fixture commit).

For each `canonicalUtf8Json`, parse the outer fixture JSON, then encode the resulting
string directly as UTF-8: these are the exact expected bytes. The stored SHA-256
is a frozen literal, not recomputed into a new expectation by the test. To obtain
typed serializer input, JSON-decode that same string with a **lossless integer**
parser (Python json works). Reordering dictionary keys must not change the bytes.
Do not round-trip through JavaScript Number or use PostgreSQL jsonb::text as a
substitute canonical encoder. This JSON file needs no domain imports to consume.

Cases:

- Policy version `9223372036854775808` (> signed BIGINT) is accepted by current code.
- Fake generation omits previousDraftId; adding explicit null is invalid.
- Manual edit requires a non-null canonical UUID previousDraftId; null and omission
  are separately invalid. Fake/manual cases are independent, not two rows sharing
  one generation ID in a combined import. UUIDs and checksums are synthetic anchors.
- UTC timestamps retain exactly six fractional digits; the fixture uses distinct
  nonzero microseconds. Missing fractions are invalid under the existing serializer.
- Decision binding has a >BIGINT draft revision and a leading-zero Unicode opaque
  external ID. `owner`, `draft`, `source`, `policy` arrays in its canonical JSON are
  the exact typed constructor inputs; createdAt is supplied separately.
- Draft text includes composed é, decomposed e+combining accent, emoji, quote,
  newline, tab and NUL. `textUtf8Hex` and `textSha256` describe raw text bytes,
  not JSON-string bytes. No normalization, NUL removal or integer truncation.
  Text remains excluded from repr and from decision-binding JSON (hash only).

**Local audit is not mislabeled as implemented.** Its envelope exists in the
`8f4a6c4`/`b17638f` proposal, but no current runtime audit serializer exists yet.
The fixture explicitly records `not_implemented`; audit canonical-byte fixture
requires the bounded typed encoder/test slice, not a fabricated existing result.
No schema/DDL, policy values, ownership or send authority is implied by these bytes.

Verification: 8 new golden/negative cases plus existing58 serializers and40 decisions:
**106 passed**, exit0. The fixture is a compatibility freeze against existing code,
not a new behavior implementation; no runtime source changed. No real data or external
actions. T1 may consume it read-only without importing/copying domain implementation.
