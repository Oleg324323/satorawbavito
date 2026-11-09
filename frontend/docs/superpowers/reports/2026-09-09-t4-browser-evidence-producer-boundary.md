# New Avito browser evidence — frontend boundary

Consumes exact Orders owner4e010e216d1d9b875e563d6cccce96b20215a04b:
`backend/app/orders/browser_envelope.py` and its committed contract/fixture.
This is a NEW producer interface, not conversion of a legacy snapshot.

Implemented `src/features/orders/avitoBrowserEvidence.ts`:

- Strict versioned shape with explicit nullable listing/line identities, explicit
  occurrence index and positive bounded quantity. No automatic IDs, time, status
  mapping, completeness, customer data, organization/account or credentials.
- UTF-8 serializer returns bytes; caller retains exact body/idempotency UUID for
  retries. It does not perform capture, upload, account resolution or token lookup.
- Duplicate order/source-line identities fail. Stable line identity wins over
  listing/occurrence exactly as the backend contract; fallback occurrence is
  required input, never array position. No claim that old producer supplies it.
- Exact strings are rejected rather than normalized. Python whitespace parity
  is explicit (including NEXT LINE, excluding BOM); Unicode scalar values are
  preserved, unpaired surrogates/control/edge whitespace rejected.
- Capture validates actual calendar/explicit offset but is not provider chronology.
  Future capture is not silently rewritten or promoted to complete coverage.
- 1,048,576-byte cap is the agreed transport cap, not operational abuse/TTL policy.
- It does not compute a raw-wire checksum: T3 normalizes decoded JSON to its
  canonical ASCII representation; whitespace/key order can differ on the wire.

Implementation first, then tests: browser25 + localReviews10 =35PASS0.42s,
all network denied. TypeScript passed. An additional real Node serializer→actual
T3 Python decoder pipe passed both the literal fixture and BOM case; literal
payload checksum matched2e7ee26192a6925cae0e68436e42d7da2153363952692751355c389b63e9ff7a,
coverage stayed partial, expected count null, provider effective_at null.
The first ad-hoc probe used a nonexistent `completeness` attribute after successful
decode/checksum; corrected probe asserts the actual `coverage_state` field.
No test fabricated backend identity, provider timestamps or complete coverage.

No serverless/extension route changed, no UI enabled, no provider/network/print/
export action occurred. Actual legacy producer compatibility is still blocked
until it supplies stable occurrence/line evidence and T1 token-only authority is
consumed by a registered durable sink. Schema, legacy data and Production remain
unchanged. This adapter does not close that producer/activation/parity gap.
