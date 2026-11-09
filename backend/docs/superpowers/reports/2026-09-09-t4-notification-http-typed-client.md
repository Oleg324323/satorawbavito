# T4 notification HTTP + typed client — IMPLEMENTED / UNVERIFIED

Continuation after f2eed7b. No tests, compilation, imports, lint, review, PG,
provider calls or runtime activation performed. Central verification remains
pending for this package and the preceding storage/authority/service lineage.

## Exact scope: a list of explicit IDs, not inbox discovery

Current approved contracts authorize an explicit nonempty unique set of visible
event IDs, fixed reviews:read and one's own current membership receipts. This
package exposes precisely those operations. It does NOT claim discovery/list-all,
pagination, unread counters, bulk-future marks, preferences or legacy UI parity.

New `app/review_notifications_http.py` exports a router FACTORY:

```text
make_review_notifications_router(
  service_dependency=<trusted ReviewNotificationService dependency>,
  max_request_bytes=<explicit positive deployment budget>,
  max_visible_ids=<explicit positive deployment budget>,
)
```

No instantiated global router, shared configuration, Engine construction, main
registration or default budgets are introduced. Service allowlist remains exact
org/account/provider and empty by default. Current authentication is obtained
through actual actor_from_request; all DB access uses the actual recipient root.

- GET `/api/v2/reviews/notifications/visible`: exactly marketplace_account_id,
  marketplace, and repeated event_id values in intended display order. No duplicate
  scalar keys, duplicate UUIDs, empty set or unknown filters.
- GET `/api/v2/reviews/notifications/capabilities`: same query and live checks.
  Returned booleans describe ONLY that exact checked set; mutation checks again.
- POST `/api/v2/reviews/notifications/receipts`: closed JSON object with
  schemaVersion=review-notification-action-v1, organizationId,
  marketplaceAccountId, marketplace, eventIds, action=read|dismiss. Organization
  must equal the authenticated actor. No recipient, timestamp or capability body.

Body decoding rejects duplicate keys, non-JSON constants, compressed input,
wrong content type, unknown keys and oversized streamed bodies. Exact limits must
be supplied by trusted bootstrap; this implementation does not pick product policy.
Fixed-code no-store failures:400 invalid,401 unauthenticated,403 denied,404 missing,
409 conflict/disabled,503 unavailable/configuration/readback-required. Unknown
receipt commit or response-serialization outcome is not reported as rolled back.
No raw exception/request body is echoed by these handlers.

## Response envelope (actual service composition changed additively)

The previously unregistered recipient service now returns an owner envelope
constructed INSIDE its same authorized root. No second membership lookup is used:

```text
schemaVersion, organizationId, marketplaceAccountId, marketplace,
recipientMembershipId, eventIds
```

Visible adds items=[{event,receipt|null}], schemaVersion
review-notification-visible-v1. Receipt mutation adds action and items=[receipt],
schemaVersion review-notification-receipts-v1. Capabilities adds canRead,
canMarkRead, canDismiss (all true after exact successful validation),
schemaVersion review-notification-capabilities-v1. Items preserve requested ID order.

Event and receipt values retain their approved existing contracts; receipt wrapper
has value+version. HTTP sourceVersion and receipt version are decimal STRINGS,
while persistence canonical bytes remain exact integer-coded and unchanged.
There were no registered callers of the newly added recipient service to switch.

## Dormant typed frontend

New `canonicalReviewNotifications.ts` under features/notifications validates
closed wire shapes, exact org/account/provider/current recipient/ID order,
canonical UUIDs, decimal-string versions, timestamp calendar fields, receipt/action
correlation and fixed safe notification copy for the three Review kinds. It does
not display raw provider errors/content or merge these events into legacy types.

New `canonicalReviewNotificationsClient.ts` is created per authenticated
membership/session/account epoch, with explicit maxVisibleIds/maxResponseBytes.
Caller must supply isCurrent and dispose on epoch change, including A→B→A.
One supersession sequence spans reads/capabilities/mutations so a late read cannot
overwrite a newer mutation. Request IDs are copied before asynchronous work.

Requests use no-store, credential omission, bearer header, redirect error and
abort signals. Only exact200 JSON is accepted; response stream bytes are bounded.
No shared cache, browser storage, refresh, implicit retry, optimistic receipt mark,
automatic request on import or UI switch. Mutation failures conservatively carry
outcome=unknown; caller must explicitly read back before deciding a new action.
Visible-read failure never marks notifications. This client does not prove auth.

## Exact remaining contract decisions — not invented

1. Notification discovery: sort key/direction, cursor/snapshot identity, concurrent
   append behavior, read/dismiss filters/count meanings, treatment of historically
   rebound source events, retention and deployment budgets. Explicit visible-ID
   lookup is implemented; discoverability is not. No guessed offset/high-water API.
2. Provider recovery: existing Avito create_answer returns answerId but NO observed
   text; submitted draft text cannot be substituted into ACK evidence. Existing
   Avito list exposes moderation/published/rejected/unknown; accepted send-completion
   meaning and safe targeted fresh-read/pagination must be explicit.
3. WB source adapter currently retains answer presence, not durable answer identity;
   legacy request_send says mutation path is not wired. The local sources inspected
   here do not establish the accepted provider answer identity/read/write contract.
   This is NOT evidence that the official API lacks such a contract. A focused
   confirmation against accepted provider documentation and representative fixtures
   is still required before implementing the transport; do not synthesize an answer
   ID from review ID or a local hash to claim success.
4. Queue fairness/polling/backoff, concrete paired-credential transport bootstrap,
   notification preference CAS/external-delivery policy, shared registration and
   full legacy Review moderation/prompt parity remain separate tasks.

Root was sent these exact gaps. Existing code/routes/data/schema and production
remain intact; no provider documents/network were consulted or actions executed.
The source-only inspection covered app/avito/reviews.py (create result, list answer
status and legacy conversion), app/reviews/canonical_wb_fetch.py and the
request_send section of app/reviews/service.py. It was not an exhaustive official
API investigation. Avito read/pagination and status acceptance likewise require
focused confirmation; missing local wiring is not proof of missing upstream API.

## Central acceptance additions — pending, no results claimed

HTTP exact/duplicate/unknown fields, streaming size, response loss after receipt
commit, wrong org/member/session/account, own-recipient envelope, live revocation,
corrupt server bytes, large exact versions. Frontend wrong owner/recipient/IDs,
NULL receipt, independent read/dismiss merge, unsafe display text, calendar errors,
response budget, A→B→A/logout/dispose, superseded GET after POST, unknown outcome,
no retry/auto-mark/cache/legacy fallback. Include actual same-root PG service and
concurrent receipts in the central integration queue; pure DTO tests alone are
not RLS/atomicity verification.
