# Browser Evidence Envelope V1

Orders owner implementation at the commit containing this document. T1 requested
the new strict input contract; T4 confirmed shape compatibility for a NEW producer.
The legacy browser snapshot is not this contract and must not be cast or backfilled
into it. No producer, token-only guard, route, schema or scheduler is activated here.

## Exact Wire

UTF-8 JSON bytes, maximum 1,048,576 bytes. This is only a transport cap, not an
approved operational quota, token lifetime or abuse policy. Every field below is
required, including explicitly nullable fields. Extra or duplicate object keys,
non-finite JSON numbers, empty arrays and malformed values are rejected.

```json
{
  "schema_version": "avito-browser-evidence-v1",
  "idempotency_key": "11111111-1111-4111-8111-111111111111",
  "captured_at": "2026-09-09T12:34:56.123456+03:00",
  "orders": [
    {
      "external_order_id": "000-synthetic-order",
      "raw_status": "synthetic-unknown-status",
      "items": [
        {
          "external_item_id": "000-synthetic-listing",
          "stable_order_line_id": null,
          "occurrence_index": 0,
          "quantity": 3
        },
        {
          "external_item_id": "000-synthetic-listing",
          "stable_order_line_id": null,
          "occurrence_index": 1,
          "quantity": 1
        }
      ]
    }
  ]
}
```

IDs/status are exact nonempty strings with no edge whitespace, control characters
or Unicode surrogate code points. Leading zeros and case are retained. Nullable
listing/line IDs require at least one nonnull value. A stable order-line ID wins;
otherwise the existing pure contract composes order ID, listing ID and the explicit
occurrence index. The decoder never derives occurrence from array position, title,
size/color or payload hashes. Duplicate order identities or source-line keys fail.
Quantity is an integer in 1..2147483647; occurrence is 0..2147483647. Boolean and
floating-point lookalikes are rejected. Array order does not change a line identity,
but a reordered body changes the request checksum and conflicts under the same key.

The idempotency key is a canonical lowercase, hyphenated, nonnil UUID. Capture time
uses RFC3339 `YYYY-MM-DDTHH:MM:SS[.1-6 digits](Z|+HH:MM|-HH:MM)` with a valid offset;
unknown-offset `-00:00` is rejected. It is an untrusted browser capture instant,
NOT a provider event/update time. Even a future capture cannot advance a projection:
`effective_at` stays null, source coverage stays partial. No clock tolerance is
invented. Server receipt time is supplied independently as an aware datetime.

There are no body org/account/provider fields, client-selected adapter/mapping
versions, completeness/terminal/total assertions, canonical statuses, URLs, buyer
names, addresses, phone numbers, descriptions, chat text, tokens or arbitrary
metadata. A return/cancellation is only its explicit raw marketplace status in
the same order record. Separate or partial-return quantity semantics are not
inferred; unknown raw statuses remain unmapped with canonical null.

## Decoder And Sink Boundary

`app/orders/browser_envelope.py` exports:

```python
decode_avito_browser_envelope(
    payload: bytes, *, organization_id: int,
    marketplace_account_id: int, observed_at: datetime,
) -> DecodedBrowserEnvelope
```

Org/account are validated positive INT4 values supplied by trusted token/account
resolution, never authority carried in the body. The pure decoder itself does not
authorize them. Output is frozen: UUID idempotency key, parsed and original capture
instant, canonical payload checksum, `OrderManifest`, and derived `source_run_key`.

- Fixed source kind `avito-browser`, adapter `avito-browser-envelope-v1`, source
  contract `avito-browser-evidence-v1`; pure status mapper version is unchanged.
- Manifest is always `partial`, expected count null, page1 nonterminal. Empty input
  does not establish an empty account. The client cannot promote it to complete.
- `source_snapshot` is `avito-browser-evidence-v1:<checksum>`. Checksum is SHA256 of
  ASCII canonical JSON (sorted keys, compact separators, escaped Unicode, finite
  numbers); object key order and whitespace do not matter, array order does.
- Opaque `source_revision` is explicitly an application capture descriptor with
  version and original captured_at string. It is NOT a fabricated provider revision.
- Run key is `avito-browser-envelope-v1:<canonical idempotency UUID>`, additionally
  scoped by existing org/account/source SQL keys. Retrying the same body with a new
  server receipt time retains the semantic manifest checksum. Changed body under
  the same key must conflict, not create another run or replace evidence.
- Every failure is a typed `BrowserEnvelopeValidationError` with fixed message
  `orders_browser_envelope_invalid`; payload text and parser details are not echoed.

The existing `publish_orders_manifest` can persist the resulting partial manifest
only with its real USER-session principal plus expected ingestion-token authority.
Its token-only replacement is T1-owned and not fabricated here. T1 must revalidate
token expiry/revocation/issuance binding and account access in the commit transaction,
then call the Orders-owned durable evidence sink. Never promote this manifest or
construct a fake user session to bypass the current signature. A decoded object is
not an authorization certificate; the trusted boundary must call the decoder itself.

## Producer Gap

Legacy `AvitoOrdersBrowserSnapshot` allows absent order/item IDs/status/time,
zero quantity, and has no explicit stable occurrence evidence. It also accepts
personal/chat metadata that this contract rejects. Existing extension compatibility
therefore remains BLOCKED until a producer can supply the exact required evidence
and T1 provides its token-only commit guard. Missing values must not be guessed.
This is an additive contract, not a silent change to the working legacy endpoint.

## Verification

Implementation-first package, then final pure gate: **183 PASS in 0.17s, exit 0**
across browser-envelope, identity/status, ingestion and read contracts. macOS
sandbox denied all networking and file writes; environment was cleared, external
pytest plugins, bytecode, disk capture/logging and cache disabled. No DB allocator,
provider or operational data was used. Ruff, compileall and diff checks passed.
Final self-critical pass checked scope ownership, exact field rejection, duplicate
keys/identities, capture versus provider chronology and partial-only output.

Synthetic fixture: `tests/fixtures/orders/avito_browser_envelope_v1.json`.
Canonical payload SHA256:
`2e7ee26192a6925cae0e68436e42d7da2153363952692751355c389b63e9ff7a`.
For trusted org91001/account91101, semantic manifest checksum:
`5faed75f639ec3bb7662353f2030c1589632f2a2e1b9e222d4dba76bebfdde39`.
These are producer interoperability fixtures, not authorization certificates.
Token-only durable ingestion and producer acceptance are not claimed by this gate.
