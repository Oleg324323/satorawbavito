# Review0068 historical binding consumer acceptance

## Scope and behavior

Dormant Reviews repository and existing guarded shadow composition only. Exact
T1 migrations0066/0067+mandatoryfix/0068 and runtime grants were imported unchanged;
sole local head0068 and byte diff against4d95b24 verified. No schema edits, backfill,
route registration, provider calls, flags, production or push.

New reservations insert the immutable five-field binding immediately, from the
captured guarded account (including exact credential reference). Existing run
replay/ingest validates stored normalized fields, canonical bytes and checksum.
All-NULL legacy rows are rejected, never relabeled using today's account.

Reads validate current and ambiguous observations plus last-source run. Ingest
also validates all contributing observation runs before consulting historical
timestamps/revisions or taking a late-run early return. Missing/foreign sources
cannot disappear through an inner join: contributing IDs are collected first and
matched against scoped runs. These queries fetch metadata, not historical bodies.
Terminal replay checks its own run and every ITEM observation's provenance and
checksum, including unchanged-body reuse. Same-binding empty replay and existing
late/ambiguous ordering remain covered by existing repository tests.

Internal `REVIEW_HISTORY_BINDING_CONFLICT` maps through existing shadow/sync
conflict handling to safe409. This is not a new public read route. The repository
is not authorization; existing user/session/publication guards remain mandatory.
Root-mode failures must escape and roll back the entire caller transaction.

## Actual evidence

- Original two behavioral REDs on actual migrated0068/runtime role:2FAIL6.23s.
  Reservation stored NULL binding; a fresh new-cabinet guard returned an old fact.
  Both own database/role cleanup and absence assertions completed naturally.
- First consumer targeted gate:80PASS1FAIL12.53s. All18 new binding cases passed.
  Existing duplicate-JSON fixture remained unbound and was rejected before its
  intended JSON-invalid check. Fixture now stamps binding at INSERT; its original
  duplicate-key payload and exact invalid-storage assertion remain unchanged.
- Expanded consumer/publication plus all three0068 schema suites:225PASS2FAIL
  62.94s, naturalexit1. All schema cases and prior adjacent behavior passed. Both
  failures were new race-test assumptions that an empty credential reference
  could form a valid ExpectedAccountBinding. Existing platform policy rejects it.
- Corrected race tests preserve that early denial and add a valid nonempty
  reference transition. Final focused binding/history/races/lossless/publication:
  **44PASS11.78s**, naturalexit0; own5DB5role cleanup/absence verified.
- Both lock winners pass for external-account changes and credential-reference
  changes. Publisher-first retains the original immutable run descriptor; a new
  authorized cabinet read rejects old history. Rebind-first blocks publication
  and leaves the separately committed reservation running with zero facts/items.
- NULL/empty/space distinction remains a storage/codec invariant, not permission
  to authorize an unsupported reference. Empty-reference race asserts the earlier
  platform rejection without weakening it or claiming a valid new read guard.
- Rollback test observes one real successful fact UPDATE before the second fact's
  provenance rejection, then a fresh transaction sees no first fact/items and the
  original reservation remains running. No catch-and-commit inside guarded root.
- Pure metadata/storage/descriptor137PASS0.34s under all-network-denied sandbox.
  Scoped Ruff and git diff checks pass. No whole-backend green claim or inferred
  corrected broad total; actual225/2 is retained, with44 focused acceptance after.

All PG gates used scrubbed environment, the existing Unix-only sandbox and fresh
random owned databases/roles; fixtures assert cleanup/absence. No shared app data
or services were changed. Existing `/dev/null` passfile diagnostics remain.

## Review and remaining boundaries

Early independent read-only review found no concrete provenance hole and required
the additional ambiguity/watermark/ITEM/rollback/race cases now present. The agent
later hit its usage limit after preparing race tests; main reviewed that file and
corrected two incorrect table names before executing it. Final review is a separate
**main-agent critical pass**, not an independent-agent final approval; root's final
integration review is still required. No reset credit was used.

Main critical pass checked exact caller binding propagation, no nullable-history
promotion, every replay/late early-return boundary, missing-join rejection,
minimal run-metadata projection, safe error mapping, genuine first-write rollback,
and both actual account-lock winners against observed tests. Platform guard
strictness for empty references is explicitly retained.

Not closed: same-descriptor ABA/lifecycle epoch, automatic recovery of abandoned
running reservations, public reader/API rollout, durable policy/draft/approval/send
and Notifications storage, live canary and production integration. The inherited
second UPDATE FK/account-lock wait characterized by T1 is not claimed fixed.

Keep expanded0068 for binary rollback. Never downgrade bound data or fall back to
an unsafe old reader to bypass history conflicts. Legacy history needs a separate
evidence-backed migration policy, not a guessed backfill.

## Reproduce final focused gate

From backend, existing installed Python and the established local Unix sandbox:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/.superpowers/sdd/2026-09-09-review-facts-schema/local-postgres.sb /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -m pytest -q -s -p no:cacheprovider --tb=short tests/test_review_run_binding_repository.py tests/test_review_historical_binding_gap.py tests/test_review_binding_publication_races.py tests/test_review_lossless_repository.py tests/test_review_publication_repository.py
```
