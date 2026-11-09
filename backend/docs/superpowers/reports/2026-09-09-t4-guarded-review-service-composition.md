# T4 guarded Review composition — IMPLEMENTED / UNVERIFIED

Source-first package. No tests/test execution, imports, compilation, lint,
review, PG/Redis/provider/network, role setup, route registration or activation.
No full architecture or release-readiness claim. Central acceptance remains
required for all code described here, including the previously unverified0074.

## Exact consumed platform source

Full Review authority handoff/contract/store/659-line authority implementation
read, plus shared guard diff and dedicated resolver handoff. Actual prerequisites
consumed unchanged (no migrations/role scripts executed):

```text
5e3bcae →c11ed3c  Orders shared fence parent
7d50ee3 →b548c7a  Repricer/executor shared fence parent
9b30eb2 →428c668  concrete physical executor check
b70f2d2 →3bf577f  dedicated paired resolver
297fcfd →b6b6343  concrete Review sender/executor/reconciliation roots
```

These are T1 source dependencies, not T4 shared-auth/config changes. Earlier
complete0072/0073/0074 chain remains as documented in the repository handoff.

## Actual service composition

`app/reviews/send_service.py`: `ReviewSendService(commands, executor,
reconciliation)` requires the exact actual T1 classes as keyword dependencies.
No configured factory/policy/key/provider or default authority is created.

Every callback calls `handle.require_participation(session, exact ReviewAction)`
before SQL. T1 owns physical commit, final seal and ambiguous-COMMIT errors.
Creation forwards exactly `handle.authority_values` to the sole T4 insert path;
no duplicate authority insert or actor-from-body occurs.

For new creation and claim/renew/reclaim/dispatch, T4 rechecks under retained
account serialization:

- exact canonical bytes versus typed intent and immutable owner/review/draft/
  decision/checksum references;
- current unambiguous fact/observation/checksum and answer eligibility;
- current workflow draft/revision/decision and actual approved decision actor
  equal to the approver authenticated by T1's sorted multi-member guard;
- selected policy and exact head UUID/version/selection audit witness (including
  ABA), policy/template/model generation metadata and canonical hashes;
- immutable draft text checksum and the existing `ReviewDraftRevision` binding
  checksum, plus DB-clock draft≤approval≤now.

Immutable/source queries use SELECT, never FOR UPDATE/FOR SHARE requiring
ungranted history UPDATE capabilities. No user permissions are manufactured:
T1 handles the real current sender/approver memberships and sessions. Original
creation replay remains T1's authenticated immutable replay and skips the new
creation participant/eligibility, preserving its original result.

Public service methods currently implemented:

```text
create(authenticated_actor, intent, policy, authority_expires_at)
claim(locator, expected)
renew(locator, expected)
reclaim(locator, expected)
mark_dispatch(locator, expected, resolved_credential)
block(locator, expected, reason)
mark_ambiguous(locator, expected)
close_ack(locator, expected, evidence)
append_ack(locator, expected, evidence)
observe_ack(locator, expected, observation, verifier_version, retain_only=False)
publish_reconciliation(authenticated_actor, read_authority, evidence)
```

Arguments are keyword-only. These are trusted server entrypoints, NOT unregistered
HTTP commands accepting user-provided evidence/reasons. `mark_dispatch` returns
the committed one-use T1 dispatch object plus private redacted approved text only
after physical commit. The holder still must consume `before_provider_io` before
a provider operation. A stored marker does not regenerate that object.

Closing uses actual dedicated executor handles, without resurrecting origin
session, approver permission, source eligibility or captured credential. Safe
send_blocked/send_ambiguous events are inserted in the SAME source transition
root, using the exact immutable send audit version/ID. Reconciliation publication
uses the actual fresh-user capture and one-use handle; incomplete read inserts
evidence only, never a membership-authored worker-only send.ambiguous.

`observe_ack` builds evidence from an exact trusted `ReviewAnswerObservation`,
immutable command text checksum and DB observation time. It derives exact/
different/incomplete, not from an adapter-supplied outcome flag. Late/incomplete
ACK can be explicitly retained without lifecycle authority. Canonical lower-level
evidence methods remain trusted adapters only; a version label is not provenance.

## One POST opportunity, no operational provider bootstrap

`app/reviews/send_driver.py` contains `ReviewAnswerObservation` with redacted
repr/str and refused generic copy/pickle, an injected `ReviewAnswerTransport`
protocol, and `send_once(service, locator, expected, transport, verifier_version)`.
There is NO concrete WB/Avito client/URL, fake production fallback, configured
transport, task, background loop or import-time invocation.

The caller must supply a trusted transport (a synthetic fake in final local
acceptance). The sequence is queued-only→claim→paired credential resolve→current
domain checks→commit marker→consume one-use before-provider fence→ONE POST→record
observed answer. A duplicate queued delivery loses CAS; a resumed stored marker
does not enter this driver. Proven pre-marker domain changes may block locally.
Provider exception/malformed result becomes ambiguous, not blind retry.

Live ACK conflict triggers one durable readback; for the SAME attempt/token it
may retain ACK only and mark still-leased state ambiguous, never fabricate fresh
GET timestamps or repeat POST. Terminal/ambiguous states remain preserved.
Other errors, especially REVIEW_READBACK_REQUIRED/unknown COMMIT, propagate for
explicit durable recovery. There is no generic retry loop or assumption that
an aborted response rolled back. Failed final before-provider fence leaves the
committed marker for explicit recovery; it does not authorize cancellation/reset.

The distributed DB-check→remote-call gap remains; this code does not promise
exactly-once external execution. Concrete transport verification, synthetic fake
crash/restart evidence, queue scan and operational bootstrap remain pending.

## New local producer, old0071 writer unchanged

`local_service.execute_local_review_with_notifications` is an explicit NEW
entrypoint using existing local Review auth/rollout root. A genuinely new draft
publication and approval_required event commit together. Exact prior command
replay returns the existing result and DOES NOT backfill an event for old0071.
Existing execute_local_review and its router/callers are unchanged. No old writer
trigger, global rollout expansion or new registered route is installed.

Notification repository adjustment for actual closing composition: publishing
fixed safe historical events can follow account disconnection/rebinding under
the trusted closing guard. It checks physical owner/source FK, but does not make
old source identity current. read_visible/mark_visible still require connected
account and per-event current identity binding. Otherwise a notification insert
could prevent recording a durable post-marker outcome. This narrowly supersedes
the earlier repository handoff's pending closing/event composition note; it is
not a relaxation of current-user read/reconciliation authority.

## Known remaining interfaces and verification

T1 acknowledged two exact follow-ups, not yet consumed in this package:

1. Sealed `ReviewReadAuthority.intent` from the SAME locked capture snapshot, so
   trusted reconciliation GET can use DB-bound externalReviewId/textChecksum
   without an unguarded lookup, HTTP-supplied target or process-cache ownership.
2. Authenticated original-sender queued-only cancel root/action with fresh current
   session/reviews:send/account access and exact CAS. No executor impersonation or
   guessed rights for another operator.0074 already has the domain transition.

T4 will consume actual committed signatures before adding those orchestration
paths. No placeholder authorization is substituted. Recipient read/mark root and
capability/HTTP/UI integration, complete Review queue/moderation/prompt parity,
preferences/transport policy and durable generation before publication remain
separate unfinished scope. All source packages require the combined synthetic
service/Unicode/golden/RLS/role/concurrency/restart/rollback/frontend gates.
