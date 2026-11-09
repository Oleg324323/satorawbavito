# T2 stock daily/evidence — actual0078 source composition

**IMPLEMENTED / UNVERIFIED**, not an authenticated/committed service or Stage5
completion. Consumes actual T1 `eb079c6da56df5d90c3c6b7cbde86ab43ee87edd`, full295-line
physical handoff, exact runtime column grants and original stock evidence codec.
No tests/test authoring, review, imports, compile/lint, PG/Redis or provider calls
run. All final gates remain pending by source-first order.

## Bounded persistence participant

`app.modules.wb_stock_daily_postgres.StockDailyTransaction(session, request,
business_date, *, max_evidence_bytes, max_observations)` composes the actual0077
`CurrentSourceTransaction` root/account context. Resource caps must be supplied by
trusted bootstrap, with no fabricated default. The existing participant verifies
clean same Engine-bound physical READ COMMITTED root and account-before-domain lock.
This is not principal, permission, document-sanitization or worker authority.

- `create_initial(source_run_id,command_id)` performs full owner/command replay
  first, then invokes actual `wb_daily_run_eligible` against complete0077 parents
  and every page's same Moscow receipt date. New UUID only after replay. Immutable
  revision1 and head1 are inserted together, DB audit/timestamps untouched.
- `propose(command_id,before_daily_revision,after_run_id,proposer_membership_id,
  evidence_document_bytes,reviewed_evidence_reference)` compares scoped immutable
  replay first; loads exact parent daily revision and two eligible real source
  runs, computes complete count-only row diff from persisted native stock rows.
  Presence/missing/null/zero are retained by original codecs. Added/removed/changed
  detection compares original encoded row bytes, not aggregate totals. Nullable
  chrt remains nullable; lexical identity order is the original Python codec.
  Proposal UUID is generated only after command lookup; proposal+all diff rows
  share the top-level root. Original document bytes/hash and exact reference are
  preserved, not treated as verified WB evidence or proof of sanitization.
- `decide(evidence_id,command_id,outcome,reviewer_membership_id,proposal_checksum,
  reason_code)` compares scoped command replay and immutable proposal checksum;
  one immutable accepted/rejected decision. Changed command intent or second
  decision conflicts; no reopen/expiry/four-eyes policy is invented.
- `apply(evidence_id,decision_id,command_id,actor_membership_id,reason_code)` replays
  scoped command first, reads immutable proposal+accepted decision, checks both
  runs' same-day eligibility, then locks exact mutable head. Parent must still
  equal proposal.before_daily_revision; new revision is parent+1 and stores exact
  after_run/checksum/decision, then explicit full-day parent/version CAS. A lost
  CAS must roll back the caller's whole root; no savepoint swallow/upsert/fallback.
  Acting member is stored separately, not inferred from historical reviewer.
- `current`, bounded `history`, `get_evidence`, `get_decision` return immutable typed
  snapshots. Evidence decoding reconstructs canonical proposal bytes/hash from
  typed children. Evidence wrapper repr is redacted, but proposal/document access
  stays inside the trusted backend; no general API serializer is supplied.

Runs/current heads and raw observations remain immutable and are not changed by
correction. No manual daily audit INSERT: existing head triggers create typed
reciprocal witnesses. Source-count/full-diff SQL verification, same-root xmin seals,
accepted evidence consumption uniqueness and graph constraints remain authoritative.
SQL NUMERIC revision/count values bind as Decimal, never BIGINT/float narrowing.

## Closed public authority boundary

No route, worker, scheduler or committed service registers these primitives.
Public mutations stay unavailable: fixed read/propose/accept/reject/apply permissions,
initial worker origin, self-review/four-eyes, acceptance invalidation, explanation
sanitization/limits/retention are missing. No Orders sync/override permission reuse.
Positive membership IDs and syntactically safe reason codes are ownership/storage
inputs only. Caller must authenticate and enforce approved semantic reason/document
policy before entering this participant, then retain final live guard until commit.

Today's received run cannot revise an earlier date even with accepted decision.
Missing days stay unknown, source_observed_at remains unknown, and reviewed
reference is not a provider signature. Final-profit/KTR/source-business rules are
untouched. No durable outbox, notification or automatic correction was invented.

## Transaction failure and final gates

Participant statement errors use existing fixed source persistence/conflict codes;
daily contract/head/eligibility failures use fixed stock_daily codes. Deferred
constraints and physical commit belong to future root owner, which must safely map
errors and never return provisional records as committed. Every exception requires
whole-root rollback; no inner commit/rollback/retry or File/memory fallback exists.

Pending centralized verification: all8 pinned codec↔SQL vectors; actual-role dual
RLS; complete received parents and cross-midnight/last-page date cases; missing/null/
zero/native nullable grain and net-zero full diffs; omitted/additional/swapped
children; immutable same-root sealing; exact command replay/restart/no-newUUID;
two-reviewer and two-consumer CAS races; correction parent/evidence consumption;
resource bounds/UTF8/NUL and sanitized-source canaries; head/audit/commit rollback;
negative permissions and live revocation once policy exists; typed history/readback;
hidden downgrade and full migration cycle. No PASS or operational readiness claim.

Rollback removes dormant participant/report, not immutable history; no actual
data/source/flag/provider mutation was performed during this implementation.
