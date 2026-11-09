# T4 — immutable local Review workflow history

Independent extension on already consumed0071. No new DDL, data migration,
shared route registration, prompt/source text read, provider or UI activation.
Implementation first, final checks afterward, as requested.

`app/reviews/local_history.py` reads workflow audit for one exact account/review
under existing live reviews:read session/account guard through transaction end.
The0068 metadata-only identity binding check applies before empty/nonempty pages;
each returned audit also joins its exact scoped local receipt and verifies current
account binding. No source or request text is loaded. Selected audit bytes are
validated with the existing local-audit codec before returning its safe envelope.

GET `/api/v2/reviews/local/history` is added only to the existing dormant router.
Required query: marketplace_account_id,marketplace,review_id. First request omits
head_id/through_version, uses after_version=0 and limit(default50,max200). Response:

```text
schemaVersion = review-local-history-v1
organizationId, marketplaceAccountId, marketplace, reviewId
headId = workflow UUID, or null when no draft exists
throughVersion = captured upper version, or "0" without a workflow
events = safe review-audit-v1 envelopes, ascending contiguous aggregateVersion
nextAfterVersion = last returned version when another bounded page exists, else null
```

Continuation sends returned head_id and through_version together plus after_version.
All versions are canonical decimal strings on HTTP; integer arithmetic and SQL
bind parameters remain exact NUMERIC, including above BIGINT. The immutable head
identity and upper version are checked against current authorized state. The upper
bound does not silently advance when new actions arrive. A fresh initial request
can deliberately open a newer history view.

Cursor fields are filters, not signed authorization capabilities: every request
rechecks session, permission, selected accounts and binding. A changed binding or
revoked access cannot be bypassed by asking for an empty terminal page. No history
uses process cache, files or a previous successful permission snapshot. Missing
joined history/receipts or gaps fail closed instead of appearing as a successful
empty page. Reads perform no domain INSERT/UPDATE/DELETE; GET never marks receipts.

The frontend local Reviews adapter builds this exact path and rejects scope,
review/head/cap drift, missing/noncontiguous/duplicate audit rows, private fields,
inconsistent cursor/null states and invalid local event semantics. It never renders
an audit receipt as current approval or permission to send. This is workflow history,
not a full Review queue, account policy history or legacy moderation/prompt parity.

Self-critical pass added the metadata binding check to empty/terminal pages and
kept text out of SELECTs. No CodeRabbit; independent root integration remains a
separate gate. Frontend history+local/browser contracts:45PASS0.37s, TypeScript0,
all network denied. Backend history verification is PENDING: the planned15 history
cases and adjacent17 local-service cases have not run. Per coordinator instruction,
no PG slot is reserved and no new micro-gate is launched. These checks belong to
the final combined implementation-package queue; no PG history acceptance is claimed.

## Dormant frontend transport in the same implementation package

`frontend/src/features/wb-reviews/canonicalLocalReviewsClient.ts` consumes only the
existing local context/commands and this package's history paths. It does not
register routes, introduce rollout flags, switch any screen or call providers.
It bypasses shared automatic refresh/retry logic, uses bearer authentication,
no-store and redirect:error, and accepts only validated application/json200.
Arbitrary error bodies are neither exposed nor logged. Responses distinguish
invalid request, unauthenticated, no access, not found, conflict, unavailable,
invalid response and stale. Ready data is validated by the existing typed adapter.

One client belongs to one membership/session/account/review epoch. The integrating
UI must supply an epoch-sensitive isCurrent callback and dispose old clients on
every selection/logout change, including A→B→A. Every async boundary rechecks it;
same-channel reads also reject superseded requests even when fetch ignores abort.
Context verifies the returned membership as well as org/account/review. No query
cache, localStorage persistence, shared result store or legacy/demo fallback exists.

Preparation captures validated immutable command bytes behind a client-owned
opaque handle; caller object mutation cannot alter an already prepared command.
Only explicit submit transmits it. Retrying that same handle preserves its bytes
and IDs. No automatic retry, UUID regeneration or auto-approve exists. Any failed
POST conservatively reports commandOutcome:unknown; abort/network/invalid result
never imply rollback. A409 requires fresh context and a new human decision before
creating changed intent. Disposal blocks future requests; old results are stale.
Backend live authorization remains authoritative, never the callback or client.

Client test source is included for endpoint/auth/cache policy, safe HTTP errors,
malformed and cross-scope responses, stale reads/disposal, exact explicit retries,
client-owned handles,409 and history read-only behavior. Those NEW client tests
and a fresh TypeScript/build gate are PENDING and must run in the combined gate.
The45PASS/TypeScript0 above predate this transport and do not verify it.

Remaining integration: T1/root shared route registration and backend policy;
full Reviews queue/moderation/prompt parity before UI replacement; durable send
and in-app services after accepted physical contracts. No release readiness,
full architecture completion or feature activation is implied by this package.
