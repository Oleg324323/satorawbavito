# T4 storage amendment review (read-only)

Compared committed T4 amendment
`8f4a6c4251a31da819e998e5ddc797e1a3c219be` only against T1 feedback
`d38e923` and the prior T4 request
`e4818e33f0353fe912598386af0ef062d285bde6`. Review Facts acceptance is
unchanged and out of this review. No schema, migration, DB, API, or uncommitted
domain file was inspected or changed.

## Verdict

The amendment resolves most earlier **domain** blockers and is internally
conservative at the network boundary. It is sufficient to split local work into
Review local storage, Review send storage, and account-scoped in-app
notifications. It is **not yet a complete exact DDL contract** for the combined
Stage 3–5 surface: Review audit matrices have a few remaining ambiguities, and
external notification delivery still depends on platform-owned destination,
provider-receipt, policy, and system-event contracts.

Nothing reviewed proves an installed schema. PostgreSQL bootstrap remains
unverified/blocked by the separately reported ENOMEM condition.

## T1 feedback disposition

| T1 request | Status after `8f4a6c4` | Evidence / remaining boundary |
|---|---|---|
| Exact command/attempt/delivery states and mutable-vs-immutable model | Substantially resolved | Review command and attempt matrices are explicit; attempts are mutable lifecycle rows with append-only audit (`amendment:8-56`). Delivery and delivery-attempt matrices are also explicit (`175-198`). |
| Policy/provenance payload and canonical bytes | Resolved for local v1 | Exact key sets, literals, JSON canonicalization, hashes, modes, timestamp encoding, and replay conflicts are defined (`60-80`). Retention is correctly left owner-approved and blocks production activation, not local storage (`82-85`). |
| Send operation/idempotency/active uniqueness/reconciliation | Resolved | One operation kind, exact request fields/bytes, UUID idempotency, same-key comparison, active/sent/conflict uniqueness, and evidence-based ambiguous resolution are defined (`89-112`). |
| Domain audit and enqueue/outbox semantics | Enqueue resolved; audit partial | Audit/event/reason vocabularies, envelope, actor classes, uniqueness, replay, and enqueue identity are supplied (`116-140`). Per-event before/after/reference/reason matrices remain incomplete below. Physical outbox remains correctly owned by T1. |
| Notification payload/preferences/receipts/mark-all/channel/delivery policy | Split result | Review account events, explicit-list mark-all, receipt merge, authoritative preferences owner, and the Telegram-only channel restriction are resolved (`144-170`). External delivery dependencies remain intentionally separate (`171-173,200-230`). |

## Lifecycle consistency

The main safety invariants are consistent:

- Review post-dispatch uncertainty is `ambiguous`; access revocation, timeout,
  5xx, crash, opt-out, or lease expiry cannot clear the marker or authorize a
  resend. Only append-only verified evidence may produce `sent|conflict`
  (`18-29,45-56,105-112`).
- Pre-dispatch expired lease reclaims atomically: old attempt becomes
  `abandoned`, command becomes `queued`, pointer clears, and a later claim creates
  a new attempt. Marker history is never erased (`26-29,45-56`).
- Marker writes are fenced by current token + command version + current attempt
  and live DB lease; no lock spans the network. Every successful lifecycle write
  advances command version; failed CAS/replay does not (`10-14,45-56`).
- Delivery follows the same conservative split: only an unmarked attempt may be
  abandoned/retried; any marked uncertainty becomes terminal `ambiguous` for
  this wave (`175-198`).

Two exact lifecycle details still need a committed sentence before strict DDL/
repository checks:

1. State which Review attempt states permit `send.lease_renewed`. “Lease applies
   in ambiguous” plus the generic renewal rule could allow indefinite renewal of
   an ambiguous attempt and delay user reconciliation. If renewal is intended
   only while `claimed`/pre-marker, say so; otherwise define the bounded
   ambiguous behavior.
2. A dispatch marker changes attempt `claimed -> dispatched` while command state
   apparently remains `leased` but its version increments. Confirm this
   state-preserving command update and audit tuple (`leased -> leased`), including
   the expected command version consumed by the result CAS.

The required command↔current-attempt alignment (for example ambiguous points to
an ambiguous marked attempt; blocked never points to a marked attempt) is
semantically clear. Enforcing it with deferred triggers/composite keys versus the
transactional repository is a T1 physical-schema choice, not a T4 decision.

## Remaining exact DDL blockers

### Review local/send storage

1. **Lease identity representation:** `lease token identity` is immutable and
   fencing is exact, but its SQL/domain type, server-generation rule, and unique
   scope are not stated. T4 should choose an opaque non-client-controlled identity
   contract; T1 chooses its equivalent physical type.
2. **Attempt result/reason nullability:** both attempt and command rows name a
   result-evidence FK and safe reason, but the matrices do not state when those
   fields are required/null or whether they must equal the transition audit.
   This matters for direct provider success versus reconciliation success.
3. **Reconciliation evidence discriminator:** the prior request required
   evidence kind/version and safe outcome. The amendment supplies
   `verifierVersion` and optional answer fields but does not explicitly replace
   or enumerate `evidenceKind`/stored outcome. State whether outcome exists on
   evidence or only on the command transition, and define optional-field
   nullability for incomplete evidence.
4. **Review audit matrix:** `beforeState`/`afterState` and references are “as
   applicable”, not enumerated for every event kind. In particular define:
   immutable policy/draft/decision events' state values; aggregate identity for
   `policy.selected` and `draft.published`; `send.dispatched`'s state-preserving
   tuple; allowed actor for user reconciliation versus worker outcomes; and the
   per-event reason/null rule. The closed vocabularies themselves are ready.
5. **Head initialization:** policy-head/workflow-head initial version and pointer
   nullability/creation transitions from the prior request are not amended.
   T1 can choose column names/types, but T4 must confirm whether committed empty
   heads exist or heads are created only with their first immutable target.

### Notification external dependencies

These are separable blockers, not reasons to delay account-scoped in-app events
and receipts:

- `recipientMembershipId` is written as “UUID/ID”; it must resolve to the existing
  canonical membership integer and composite organization FK, or T4 must name a
  different identity.
- External delivery cannot receive its required FK until T1/platform publishes
  the versioned verified destination-binding contract. Raw legacy `chatId` is
  correctly rejected as the delivery identity.
- A sent attempt requires a separate scoped provider-receipt reference, but no
  provider-receipt relation/identity/checksum contract is supplied.
- Delivery policy fields are defined, but the immutable policy relation/owner,
  `(policyId, version)` target key, canonical snapshot/checksum, and backoff-list
  representation remain to be accepted. Production values may remain absent and
  must block activation rather than DDL.
- Org-wide `system_attention` remains blocked on a platform event registry and
  producer authorization. This does not block Reviews account-scoped kinds.
- Existing preferences remain authoritative. A fail-closed read-only typed DB
  adapter is independent; a settings writer waits for the T1-owned CAS extension.

For delivery attempts, T4 should also confirm the exact fencing predicate mirrors
Review send: current delivery version + current attempt + immutable lease token +
unexpired DB-time lease. States alone do not prove stale workers are fenced.

## Separable local slices

1. **Already accepted:** Review Facts DDL/repository handoff from `4a7d669`;
   amendment does not block it.
2. **Review local policy/draft/decision:** canonical serializers and immutable
   rows can proceed locally after the small head/audit clarifications; production
   retention approval remains an activation gate.
3. **Review send command/attempt/evidence/outbox:** independent of Notifications
   and external providers after lease/result/audit clarifications. Keep provider
   fake/default-off and do not claim exactly-once.
4. **In-app notifications:** account-scoped Reviews events plus explicit-ID
   receipts/mark-all can proceed independently of Telegram, preferences writes,
   system-event registry, and destination binding.
5. **Preferences:** fail-closed read-only adapter may proceed without schema;
   settings mutation is a separate T1 CAS-extension slice.
6. **External Telegram delivery:** blocked from an exact relational/FK-complete
   handoff by destination-binding, provider-receipt, and delivery-policy
   contracts; production activation additionally waits for owner-approved
   retry/lease values.
7. **Org-wide system events:** separate platform-registry slice; do not widen a
   missing Reviews account into organization scope.

## Acceptance status

`8f4a6c4` is a domain-owner proposal for T1 acceptance, not an installed contract.
T1 may accept the resolved slices independently and return exact physical schema,
RLS/grant, ORM-boundary, and migration handoffs. No PostgreSQL/bootstrap,
concurrency, restart, provider, or production result is established by this
document or review.
