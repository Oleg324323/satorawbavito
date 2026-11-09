# T4 recipient, queue and reconciliation composition — IMPLEMENTED / UNVERIFIED

Source-only continuation of a13a6f4. No tests, imports, compilation, lint, review,
PG/Redis, role setup, transport calls or operational actions were run. No release
or full parity claim. Final centralized acceptance is mandatory. Source reads
and unchanged prerequisite import are not runtime verification.

## Actual dependency, unchanged

T1 cc8203530e0c363a80abacb2f3042600ab20e5c7 imported as43fa81d: exact three
contract/authority/handoff paths, no conflict or consumer edits to shared source.
Its full diff and handoff appendix were read. Sealed same-snapshot intent and
original-sender cancellation now exist; they are no longer missing prerequisites.

## Implemented entrypoints (all dormant, keyword-only)

`ReviewSendService.cancel(authenticated_actor, locator, expected)` participates
first in actual CANCEL guard, uses captured historical account binding and
performs audit-first send.cancelled/USER_CANCELLED with the actual current
membership. T1 admits only original user/member, fresh current reviews:send
session and exact queued/no-attempt CAS, seals and returns committed readback.
There is no other-operator override, creator impersonation or terminal replay.

`reconcile_once(service, authenticated_actor, locator, expected, transport,
verifier_version)` uses actual capture_read; the external review target comes
ONLY from that capability's canonical sealed intent. One injected read_answer
call occurs outside the DB root with the exact paired resolved credential.
Malformed/failed/missing answer is incomplete evidence, never absence proof or
another POST grant. No provider client, retry loop or task registration exists.
Transport implementations must prohibit hidden retries and preserve answer text.

`ReviewSendService.observe_reconciliation(...)` derives exact/different/incomplete
from the actual observation and captured intent checksum. Its publication callback
first requires RECONCILE, then creates canonical evidence using the captured read
UUID/start time and publication DB clock. T1 rechecks current operator/session,
account/credential, same intent/attempt/version and final physical root. Incomplete
evidence is append-only; it does not manufacture worker-only ambiguous transitions.
Publication failure/unknown COMMIT propagates without automatic repeat GET/write.
Existing lower-level evidence entrypoint remains trusted-only, not an HTTP DTO.

`scan_ready_commands(executor_session_factory, identity, organization_id,
marketplace_account_id, marketplace, limit)` returns canonical immutable enqueue
payloads joined to exact current queued command/version/no-attempt. It owns a
fresh Engine-bound PostgreSQL root, verifies the actual dedicated executor login
and physical root, sets exact account/tenant context, checks stored bytes/hash and
scope, then rolls back/closes before returning. It does no DML, provider call,
credential fetch or broker ACK. Foreign/dirty sessions are rejected, not closed.
Duplicate discovery is intentional; actual claim CAS elects execution. This is
not a completeness/snapshot service or authorization token. Fair scheduling,
unclaimable-oldest starvation policy, polling and retry/backoff remain unimplemented;
an explicit batch limit is required and no operational default is invented.

`ReviewNotificationService(engine, allowlist=frozenset())` owns fresh physical
roots. Trusted bootstrap supplies exact (org, account, provider) tuples; body
parameters cannot enable rollout. Fixed reviews:read, actual ActorContext/current
membership/session, exact connected account binding and public publication guard
authorize only the three existing Review event kinds and one's own receipts.
No notification permission/profile or access to another recipient is invented.

Methods read_visible/mark_visible/capabilities require explicit event_ids. Every
ID is checked for exact scope and current source binding before any receipt
write. Recipient membership is derived server-side. Mark read/dismiss is sorted,
all-or-nothing and preserves the other field through the existing repository.
Read/capabilities perform no receipt writes; capabilities apply only to the
currently checked exact ID set and never replace mutation-time authorization.
Unknown receipt commit/cleanup outcome returns NOTIFICATION_READBACK_REQUIRED;
no raw SQL/provider exception chain, token, source text or automatic retry.

## Deliberately not implemented here

Notification discovery/list, HTTP/frontend wiring, preferences storage; concrete
WB/Avito transports and bootstrap; queue poller/fairness/restart acceptance; full
Review list/policy/moderation/prompt parity and final integration. Shared config,
schema/migrations/grants, providers, production, existing registered routes and
legacy writers are unchanged. All flags remain off; no external sends or exports.

## Central acceptance additions (NOT RUN)

- Original user's new-session cancel; other member/user denial; concurrent claim,
  stale/repeated/terminal cancel; exact actor/reason and final-root sabotage.
- Fresh GET exact captured target/checksum despite old origin expiry; current
  operator/credential/binding loss; altered intent; exact/different/incomplete
  answer; GET error; publication commit uncertainty; no repeated POST/GET.
- Scanner wrong login/SET ROLE, scope tampering, corrupt bytes/hash/version,
  absent/joined-current intents, duplicate/reclaimed intents, same physical root,
  foreign session ownership and rollback/close failures.
- Recipient read-only SELECT, default-off/scoped allowlist, revoked session/member,
  wrong tenant/account/provider/recipient, stale source binding, mixed visible IDs,
  concurrent read/dismiss, no-op physical version, atomic rollback and unknown COMMIT.

Earlier codec or local/frontend results do not verify any of this new package.
