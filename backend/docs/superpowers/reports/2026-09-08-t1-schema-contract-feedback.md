# T1: schema prerequisites and domain decisions

Local review, 2026-09-08. This is coordination feedback, not installed schema,
runtime approval, or a release-ready integration set. No revision is reserved by
this document. T1 allocates revisions from the actual single head immediately
before implementation. No production/provider/Redis/working-DB access was used.

## Reviews facts: accepted for bounded schema implementation

Reviewed T4 request `4a7d66999e8d8deedbe6ac5d44a23d9a960c9044`,
`2026-09-08-t4-reviews-stage-2-schema-request.md`.

T1 will implement the four requested relations, composite owner/provider FKs,
forced organization RLS, immutable evidence protection, server-issued run ordering,
and guarded empty-only downgrade. A deferred fact-completeness check must inspect
the final row at commit, allowing atomic identity/observation/pointer creation.
Run ordering cannot be caller-selected, including through identity overrides or
sequence mutation privileges. Existing broad runtime default grants are part of
the security test, not an assumption that later grants will repair exposure.
Targeted runtime revokes follow broad grants/default-ACL handling and must be
tested under the actual NOSUPERUSER NOBYPASSRLS role, including expand-time access.

T4 owns `app/reviews/canonical_orm.py` and its repository after the committed DDL
handoff; T1 will not create competing domain ORM/service code. T4 must then prove
manifest/count consistency, terminal replay comparison, current-pointer/watermark
CAS, source ambiguity, fresh membership/allowed-account authorization and rollback
under real disposable PostgreSQL. Schema acceptance alone does not establish these
service properties or enable a collector/send capability.

## T2: approval/attempt/audit contract needs a committed amendment

Reviewed initial repository request `769f3cf44742622824233eefe8f81d4e360f8484`
and continuation through `ee7bd6e565e7a7bbf4363a2b591fdeccc2f4d9d7`.
The latter's `2026-09-08-t2-source-grain-and-dispatch-acceptance.md` explicitly
confirms that `PriceApprovalRepository` lacks attempt reservation/dispatch-marker
commands. No subsequent approval/repository contract change resolves that gap.

The clarified transaction phases are intent+audit, claim+audit,
attempt/dispatch-marker+audit, result+audit. Claim is not dispatch authorization.
Only the exclusive committed marker winner may call a provider; uncertainty after
the marker is ambiguous, not permission for another POST. No lock spans network.

Before T1 freezes the three-table migration, T2 must commit:

1. Exact reserve/dispatch commands, who supplies `attempt_id`, and returned values.
2. Attempt states/nullability/transitions, version meaning (pending V versus
   applying V+1), cardinality, dispatch-key formula/scope, and timestamp/result fields.
3. Authoritative canonical apply-request bytes or typed immutable representation,
   checksum input, size/redaction, and same-key/different-payload comparison.
4. Audit event vocabulary, actor/null rules for worker outcomes, creation and
   reject/block coverage, before/after values, and bounded metadata schema.
5. Stored safe-error compatibility: command failure accepts eight codes, while
   `PriceApprovalSnapshot` accepts any grammar-valid code. SQL must not silently
   reject a snapshot the domain accepts. Specify whether ambiguous rows remain
   terminal without resolution in this wave or define append-only reconciliation.

T1 owns shared transaction-local organization/account context and runtime RLS/grant
implementation. T2 need not implement those shared files to answer domain questions.
The helper sets context, not authorization; service membership/account checks remain
mandatory. Identity-level source-diff policy is received, but its reviewed-evidence
input is not proof of provider completeness or a scheduler enablement decision.

## T4: later send/notification storage is a separate incomplete request

Reviewed `e4818e33f0353fe912598386af0ef062d285bde6`,
`2026-09-08-t4-send-notifications-schema-request.md`. This does not block the
accepted review-facts slice above. Before later storage writers, T4 must commit:

1. Exact command/attempt/delivery state and nullability matrices, distinguishing
   persisted states from pure recovery proposals. Choose mutable lifecycle rows
   versus append-only attempt events; "immutable history" cannot describe mutable
   lease/marker/result columns without that distinction.
2. Policy/provenance content schema, canonical checksum input and replay semantics;
   incorporate owner-approved retention policy rather than choosing its values.
   No default policy or unknown actor/account will be invented by T1.
3. Send operation kind, idempotency/request binding, active uniqueness predicate
   including the rule after sent, and reconciliation of terminal ambiguity.
4. Domain audit/outbox events, payloads, idempotency and atomic replay rules. T1
   selects and implements shared physical schema, naming, grants and safe reuse;
   a generic event bus will not be introduced to fill the gap.
5. Notification event payload/version and reference semantics, authoritative
   preferences source, receipt/mark-all choice, recipient/destination/channel
   contract, incorporating owner-approved finite delivery retry policy rather than
   selecting production values. T1 will implement the corresponding
   org-wide/account database policy once the authorized scope contract is exact.

Production TTLs, abuse limits, delivery policy, retention and observation windows
remain owner decisions. Their absence prevents activation, not independent local
tests or schema work whose contracts are already exact.

## Integration and ownership

Observed branch heads are not ready-commit approvals. Integration waits for all four
owners to stop and provide exact ready commits; prerequisites precede domain commits
and frontend in a separate integration worktree. Semantic conflicts return to owners.
T2 retains repricer_tasks, T4 review_tasks/frontend/serverless, T3 Avito order client;
T1 retains avito_orders router during token/credential cutover and shared infrastructure.
This follows the shared-file ownership manifest in
`2026-09-08-arch-t1-stage-1-credential-api-handoff.md` and the user's integration gates.
No flags, legacy readers/writers, provider actions or production state change here.

Verification for this report: local committed-source comparison and independent
read-only contract review. No migration, concurrency or provider test is claimed
by this document; those results belong in each implementation handoff.
