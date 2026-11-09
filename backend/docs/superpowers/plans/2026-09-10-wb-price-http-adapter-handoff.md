# T2 price HTTP adapter — bounded implementation, activation closed

## Reuse and boundary

`app/modules/wb_repricing_http.py` implements the existing worker's
`post_price_once(body, credential) -> OriginalPricePostResponse`. Existing
canonical request bytes, rounding, receipt parser, credential binding and
worker/transaction owners are unchanged. Legacy `RealWbApiClient` is not suitable
for this boundary: it decodes responses, retains token on the instance and turns
transport exceptions into raw error text. The new adapter uses installed httpx
directly with an ephemeral client, bounded streaming, trust_env false and no
redirects or retries. Only synthetic MockTransport calls were executed.

## Current official evidence

Checked the rendered [WB product documentation](https://dev.wildberries.ru/docs/openapi/item-management)
on 2026-09-10, including expanded historyGoods response schema:

- POST `/api/v2/upload/task` operates on product price/discount; sizes have a
  distinct `/api/v2/upload/task/size` operation. 208 is an existing upload, not a
  reason for another POST. A receipt still does not prove applied price.
- GET `/api/v2/history/tasks`: 3 means completed without errors; 4 cancelled;
  5 completed with some errors; 6 completed with all errors. Status 1 belongs
  to future promotion buffer operations, not this reconciliation method.
- GET `/api/v2/history/goods/task`: uploadID plus paginated historyGoods;
  limit at most1000 and offset. Row status2 means updated, status3 means error.
  Product nmID and nullable sizeID are different identities. Raw errorText is
  discarded after deriving a boolean; currency must be RUB for this kernel.
- Shared Prices/Discounts category: base entitlement4/hour, interval900seconds;
  higher documented entitlements600ms. This adapter conservatively asks for900s
  and never infers entitlement from JWT. Each GET is a separate admitted call.

## Bootstrap and safe pacing

Constructor: positive internal organization_id/account_id, explicit
`admit_request(org, account, method, path, minimum_interval_seconds) -> True`,
response budget1..4MiB, optional injected httpx transport/clock.

Required `record_cooldown(org, account, next_not_before) -> True` durably extends
the same category deadline monotonically (MAX, never shorten). Called immediately
on received headers, before reading/parsing the body for BOTH POST and GET. This
preserves Retry-After even for POST429, broken body streams and malformed JSON.
False or exception fails closed; it does not authorize another POST. Bootstrap
must provide a real committed callback, never a permissive placeholder. A DB
outage can still prevent persistence; operators must not interpret this explicit
failure as successful cooldown recording. Existing dispatch stays uncertain.

Admission MUST reserve an account-wide durable category budget before network
I/O and coordinate with all price reads/writes. A permissive callback in tests
is not a production limiter. No in-memory pacing, loops or sleep are installed.
`read_history_once` returns next_not_before (at least900s); HTTP errors preserve a
safe deadline including Retry-After. Caller must persist that deadline before
the next GET, cap its polling job, and revalidate read authority. This adapter
does not create that job or choose its lifetime. Unknown outcomes never permit
a POST retry. Existing worker marker/authority gates remain mandatory for POST.

## Supported payload and outcome proof

Exactly one product row, exact keys nmID/price/discount; integer values only.
Stored bytes are sent unchanged. sizeID/minPrice are rejected before HTTP:
the existing canonical request may encode these, but the fixed product endpoint
is not evidence they will be honored. No field dropping or endpoint rewriting.

`matches_single_product_application` compares the canonical request with two
account-owned observations AND the caller-supplied original durable receipt ID:
same uploadID, completed status3, total=success=1, offset0, page limit>1, exactly
one row, rowstatus2, no error, exact nmID/article/RUB/rounded provider price and
discount. Size-scoped, incomplete, conflicting or partial evidence never yields
true. False is NOT evidence of nonacceptance and NEVER permission to resend.
This pure matcher does not persist or transition approval. Caller must load
receipt ID from the owned stored attempt, persist evidence and close under the
existing transaction guard; client-supplied receipt IDs are not authoritative.
Multi-product/multi-size results require a separate completeness slice.

## Precise remaining dependencies sent to T1/ROOT

1. Trusted stored approval accessor `(authenticated_actor, internal_account_id,
   approval_id)` returning existing `RepricerApprovalBinding`, with exact org /
   account / approval scope, immutable bytes/checksum/action key preserved.
   Candidate existing owner: `repricer_job_store.approval_binding(row)` inside
   guarded service; do not expose SQL helper or UUID-only lookup to the router.
2. Existing `RepricerAuthorityPolicy(reference, version)` approved by ROOT plus
   stable approval-bound `authority_expires_at`. No newly invented TTL. Same
   approval replay must not mint a new deadline; session expiry remains upper
   bound. Existing `RepricerJobCommands.create` validates/captures these facts.
3. Actual account-wide quota reservation, readback job attempt cap and expiry,
   stored evidence publication/closing composition, legacy one-writer fence.
4. Before supporting size/minPrice, endpoint-bound canonical format plus
   receipt-path contract must be reviewed; existing action keys must not change.

No new DDL, shared wiring, flags, policy values, provider executions or activation.
The adapter exists and is tested; full runtime/reconciliation is NOT claimed done.
