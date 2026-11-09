# T2: source-grain regression and durable dispatch acceptance

## Implemented without schema

`tests/test_wb_source_grain_contract.py` adds 16 synthetic characterization cases
against the existing pure revision comparator; no production code is changed.
The same nmId retains distinct offer/chrt and warehouse identities under all six
row permutations. A zero change affects only the exact identity. Missing, null,
and explicit zero fixtures hash distinctly. Partial runs remain incomplete even
with a review reference, with removed identities diagnostic only. A synthetic
120800-kopeck change remains unexplained without reviewed evidence; neither hash
nor external identity is rewritten. This does not resolve the real historical
discrepancy or authorize creating its missing rrdId.

These tests operate AFTER normalization. They do not prove endpoint grain,
pagination completeness, adapter hashing, persistence, or historical stock reconstruction.
The JSON hashing helper belongs only to synthetic fixtures, not production policy.

## Durable dispatch crash matrix — acceptance specification, NOT RUN

The existing `PriceApprovalRepository` Protocol lacks an attempt reservation and
dispatch-marker command. It is not yet a complete worker service API. Resolve this
with accepted T1 schema before wiring manual or scheduler callers. A claim alone
must never stand in for a durable dispatch marker.

| Crash boundary | Durable evidence | Recovery | Forbidden behavior |
| --- | --- | --- | --- |
| Before intent commit | No approval/attempt | Recreate authorized intent with original scoped action key; unique conflict must compare payload | Sending before intent or recomputing checksum to bypass conflict |
| After intent, before dispatch marker | Intent and possibly applying claim; no committed marker | Resume only through exclusive transactional attempt/dispatch ownership; membership/resolver checks still required | Treating a lease timeout as proof that a prior sender is dead |
| After marker commit, before known provider acceptance | Dispatch may have happened | Preserve ambiguous state and reconcile; unavailable outcome is not evidence of rejection | Clearing marker or automatic second POST |
| After provider acceptance, before local result commit | Marker; upload ID may exist only outside DB | Mark/recover ambiguous; reconcile by persisted upload ID or authoritative provider history when available | Assuming a missing local upload ID means provider did not accept |
| After result commit, before queue ACK | Closed approval plus result/audit | Duplicate delivery reads terminal result and ACKs without dispatch | Reopening approval or making another POST |
| Duplicate delivery racing original worker | Shared scoped approval/attempt and CAS state | Only successful first dispatch-marker owner may invoke the fake provider | Calling provider on zero updated rows or on marker replay |

The pre-marker resume case requires a transaction predicate against durable state,
not a timer-only decision. A resumed worker and original worker racing to dispatch
must compete for the same immutable marker. The loser does not call the provider.
After marker commit, a stalled original process may still act: recovery must not
authorize a replacement send. This intentionally sacrifices automatic progress
when non-acceptance cannot be proved. Exactly-once external effect is not claimed.

## Required transaction boundaries

1. Intent insertion: scoped ownership, immutable payload/checksum/action key and
   creation audit commit together. Reused key with changed payload is a conflict.
2. Claim: one UPDATE matches org/account/approval/pending/expected_version. Actor
   membership belongs to the same org and is authenticated/authorized by the caller;
   bridge validation alone is not authentication. Transition and audit commit together.
3. Attempt/dispatch: immutable attempt identity and request binding, exclusive
   dispatch marker and audit commit before the fake provider is invoked. A marker
   already present is not fresh authorization. No database lock spans network I/O.
4. Result: matching scoped attempt, applying version, upload ID and safe outcome,
   transition and audit commit together. A transaction failure is explicit; no
   memory/file fallback. Closed rows remain unchanged on replay/conflict.

Current pure kernel treats ambiguous as terminal. Reconciliation therefore needs
an explicit future append-only resolution/attempt contract; do not silently add
`ambiguous -> applied` to the existing kernel to accommodate worker convenience.

## PostgreSQL proof obligations for the next accepted schema

All below require a disposable local DB with applied accepted migrations. FakeSession,
in-memory dicts, Protocol conformance and sequential immutable values are not substitutes.

- Two independent sessions synchronize before scoped claim. Exactly one UPDATE
  wins; one conflict; one fake provider invocation after committed dispatch ownership.
- Synchronize two workers after intent but before marker; prove a single marker
  winner. Repeat with stale version and duplicate Celery delivery.
- Inject failures at every crash boundary above. Record row counts, versions,
  attempts, audit entries and fake call count after restart in new sessions.
- Raise during audit insertion: approval/attempt changes roll back too. Raise
  during result commit: do not re-send, retain/recover uncertainty.
- Cross-org and two accounts in one org with colliding approval/external IDs:
  no lookup or mutation leaks; enforce composite FK and FORCE RLS under non-owner role.
- Unique org/account/action key: identical replay is stable; different payload
  conflicts. Backfill preserves all original IDs/status/money/hash/key.
- Shadow has zero claims and zero provider calls. Resolver receives internal
  org/account IDs; queue and audit contain no tokens or raw exceptions.
- A legacy one-writer fence is a rollout prerequisite, not proven by the new
  repository tests. Do not increase workers/replicas before it is verified.

T1 schema, local migration acceptance, authenticated resolver and one-writer rollout
remain dependencies. This document requests no schema/config changes in T2.

## Verification

```sh
python -m pytest -q -p tests.repricer_offline_plugin \
  tests/test_wb_source_grain_contract.py tests/test_wb_source_revision.py \
  tests/test_wb_repricing_price_inputs.py
```

Executed with the local wave1-integration Python from backend: **77 passed, exit 0**.
No new behavior was implemented, so these are characterization tests, not a red-green
bug-fix claim. Manual critic pass keeps the absent durable proof, provider-source
authority, reconciliation API and one-writer fence explicit.
