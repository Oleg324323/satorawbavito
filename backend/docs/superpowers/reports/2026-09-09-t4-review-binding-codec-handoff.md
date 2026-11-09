# T4 → T1: Review historical binding pure codec READY

Implements the exact descriptor representation from1a3d344, following T1's direct
request. No physical schema, SQL admission or runtime service has changed.

## API

Module `app.reviews.historical_binding`:

- `ReviewBindingDescriptor(organization_id, marketplace_account_id, marketplace,
  external_account_id, credential_ref, schema_version=1)`: frozen, slots,
  repr-hidden scalar descriptor. Exact positive INT4 IDs, strict integer1 schema,
  exact string wb/avito, nonempty Unicode-scalar external ID, nullable Unicode
  scalar ref. No trim/normalization; null, empty and whitespace are different.
- `encode_review_binding_descriptor(descriptor) -> EncodedReviewBinding`:
  repr-hidden frozen canonical_bytes/checksum. Revalidates descriptor fields.
- `decode_review_binding_descriptor(raw, *, checksum) -> ReviewBindingDescriptor`:
  exact bytes only, mandatory lowercase64hex SHA256, ASCII JSON, duplicate-aware
  decode, exact six keys/types/scalars and byte-for-byte canonical re-encoding.
- Invalid representation raises only `ReviewBindingDescriptorError` with fixed
  `REVIEW_BINDING_DESCRIPTOR_INVALID`. No raw metadata/error payload in args/repr.

JSON names remain exactly schemaVersion, organizationId, marketplaceAccountId,
marketplace, externalAccountId, credentialRef. This is Review sorted compact
ensure_ascii=True encoding, not Orders positional bytes or Review source-body UTF8.

## SQL parity reference

Machine-readable literal fixture:
`tests/fixtures/review_binding_descriptor_v1.json`. Six vectors include original
ASCII null/empty/changed-account hashes, max INT4, Cyrillic, supplementary Unicode,
combining mark, edge spaces, control short escapes, DEL, slash/backslash/quote,
and separate NUL/non-NUL references. Node independently encoded and hashed all6;
independent critic also matched all6. Fixture values are synthetic metadata.

Physical admission remains T1's responsibility. Pure scalar NUL is valid here;
existing account VARCHAR cannot represent NUL. Do not silently strip/normalize
or infer SQL admissibility from successful pure encoding. The two NUL vectors
are pure-codec reference cases, not successful account INSERT expectations.
Other physical column constraints likewise require explicit admission rules.

For equivalent normalized columns, construct/validate the exact descriptor only
when the row is explicitly bound, and compare all decoded fields to the fresh
guarded binding. Missing historical stamp stays unbound: never fill it from
current metadata. Checksum equality alone is not authority or a full binding
comparison. Do not expose descriptor/reference in public responses/generic audit.

## Verification

- Initial RED: missing module, pytest collection exit2.
- Focused:61PASS0.07s, no skips. Tests include altered keys/order/whitespace/
  escape spellings, bool/float/exponent IDs, NaN/Infinity, invalid UTF8/ASCII,
  surrogate scalar rejection, deep JSON safe errors, checksum/type errors,
  frozen repr boundaries and revalidation of deliberately corrupted instances.
- Adjacent pure suite:283PASS0.48s, naturalexit0, no skips/warnings. Files:
  test_review_binding_codec, test_review_storage_payloads, test_review_storage_golden,
  test_review_decision_contract, test_review_send_recovery, test_review_lossless_codec,
  test_review_canonical_contract, test_review_canonical_wb_fetch.
- Empty-environment Python/Node runs used T1's existing all-network-denied sandbox
  `.superpowers/sdd/2026-09-09-review-shadow-rollout/task-1-sandbox.sb`.
- Scoped Ruff/compileall/diff0 after fixing six new test-style warnings.
- Independent read-only critique: scoped PASS, no important defects; no CodeRabbit.

No DB or whole-backend suite was needed/run for this pure slice. No provider,
production, credentials, flags, migration, schema or active consumer changes.

## Still open

T1 physical immutable-from-INSERT design/revision, SQL parity/admission/FK/RLS/ACL;
T4 reserve/replay/publication and current/ambiguity reader integration;
completed-rebind and both race-order tests. Existing historical gap remains OPEN.
Public canonical reader and operational sync activation remain blocked. The codec
does not introduce an epoch or detect an unobserved A→B→A identical descriptor.
