# T2 — independent progress / dependency handoff

## CURRENT — original requirements, local readiness and owners

### Final foundation/P0 package — source handoff

The root's `2026-09-09-architecture-foundation-delivery.md` now defines this
delivery. The older stage table below inventories the original larger requirements,
not additional P0 work or a claim they are complete. All recent source listed here
is **IMPLEMENTED / UNVERIFIED**; no new gate count is implied by consolidation.

P0 connections are in the domain branch ancestry:

- Approval worker `722e791c64d44adb23fe90bdf41de10f5b3e8da6` composes exact T1 job
  roots/markers/receipt/outcome; test composition `95246b1b7dfe0b8f8a9c62e09f3e218af542f1f4`
  uses the concrete dedicated resolver. It has no actual provider transport,
  registration or competing writer. Fifteen authored PG scenarios remain pending.
- Account-state participant `168a61b9ad5af66dda59c95e38073e26b11e66a2` uses actual
  0075 (`9960548b47021880f3a511d09facd508c9c7270c`): typed assignment/liquidation
  revisions, replay, CAS and audit. It is not an authenticated committed workflow.
- Current-source participant `28093f68196a75e0503253979290ff26f7991ef7` uses actual
  0077 (`b21f2166886ade7637b1b9594b5b28c1e38d3e80`): immutable run/binding/page/facts,
  finalize+publication root CAS and typed current/latest/page reads.
- Daily participant `f01939c6fddf704f78fc0be068a173f02a6948fa` composes that current
  source participant and actual 0078 (`eb079c6da56df5d90c3c6b7cbde86ab43ee87edd`):
  eligible received parents, native full count-diff/evidence, terminal decision,
  parent-linked daily revision/head CAS and typed history. No manual DB audit.
- Existing immutable calculation context/service `5e6107687a7d148875775eece0881a0b5240d3ea`,
  state command codecs `21d458039e419fbe6c8b407312a2570a88a75dea`, original approval
  kernel and accepted bounded override service are retained, without formula or
  current-writer rewiring.
- Delegated shared credential source (already imported separately by T1) includes
  dedicated resolver `00961055428365f83d7dd05bd07f46786709c975`, transaction primitives
  `874782ddeed534e4f4bfd9024037853fb5197ac4`, active resolver `e2d51a5` plus required
  timing fix `fb2ae0ec686aa59a2c8e69dc3fdf96dca753ee42`, and persisted readback
  `59bf12b3c5e028ab1262f19f3709a0a363f190b2`. Shared store ownership is T1, not open
  for further T2 edits. Preserve T1's later guarded-management ancestry.
- Bounded Avito error source `1213202` and `7b32749ee39a26ffd824572a9f65bb95f652325c`
  is included; no further standalone diagnostics work is proposed for P0.

Real P0 remainder: root integration of these immutable source commits plus final
T1 foundation ancestry (including inert0079), consolidated imports/migration/
restricted-role/credential/CAS/replay/crash gates and fixes of actual failures.
T2 has no active test process/reservation/cleanup and has not launched duplicate
backend/PG runs. Recent modules still require central compile/lint and actual PG
verification; earlier pure/PG counts elsewhere in this report do not verify them.

P1 stays closed: org-settings authority; assignment/liquidation/read/review/apply
permissions and negative-margin confirmation; source worker/publication authority
where absent; daily self-review/invalidation/document safety; provider history and
abandonment/recovery contracts; dated mapping/source/economic contexts and freshness;
financial/KTR rules; operational writer retirement/cutover. No actual transport or
public writer is registered where those contracts are missing. Final profit fields
remain null. P2 Production assembly/printing and KIZ/matcher remain deferred/excluded.

This is a foundation delivery handoff, not full original-TZ acceptance. No provider,
production, operational role/flag, real credential, deploy or push action was taken.

Dormant worker source now composes the exact T1 executor through `9b30eb2cb59b38d18e859773b9b9607406832bb5`
(import merge `331c9f80ff7aecbfedf99dc8507d10ddaf8ce45e`). Both imported0073 and
this composition are **IMPLEMENTED / UNVERIFIED**, not schema acceptance or Stage3
completion. `wb_repricing_worker.py` uses immutable stored bytes, handle-derived
membership, physical claim/reserve/dispatch roots, injected single-POST transport,
receipt-only uncertainty recovery and explicit trusted outcome closure. Accepted
upload is not an applied price. Existing dispatch marker prevents duplicate POST;
an in-flight duplicate is not automatically classified as abandoned. There is no
real transport, scheduler, ACK integration or current-writer registration.

`test_wb_repricing_worker_postgres.py` contains15 pending synthetic PG cases:
two real claim sessions, committed-marker/no-held-lock fake I/O, paired resolver
physical login observation, receipt/restart/closed duplicate, raw error sanitization,
rollback, marker/receipt commit uncertainty, pre-dispatch and post-acceptance crash,
revocation, foreign scope, stale version and mismatched applied receipt. No PG run
started. Root explicitly deferred it to centralized whole-package verification;
final compile/lint/consumer regressions also pending after latest source edits.
Initial Ruff reported6 diagnostics; imports were formatted, transport/publication
exception sanitization annotated and receipt validation narrowed. No clean final
lint claim. Independent collection literals at `a8581cb5f8cd7c3ee3233ae8812fd70e800481f5`
did receive final offline **10PASS0.10s**, exit0, before queue deferral.

Remaining implementation dependencies are concrete: T1 typed settings/assignment/
liquidation physical write tuples and source/current/daily/evidence tables plus
publication authority; immutable mapping and dated economics/source facts; provider
history/reconciliation authority and abandoned-worker recovery contract; shared
one-writer fence/cutover. T1 plans `d4fa2c6`/`63c4949` are not delivered DDL. Org-wide
settings mutation permissions and final profitability rules require explicit owner
decisions. Neither policy is invented; final financial fields remain null. Until
these contracts arrive, no further independent service implementation is identified
from the current requirements. This is a dependency inventory, not a claim that
all stages are done. Rollback removes dormant source/tests; no data rollback.

Stage4A source package for T1 now includes complete org algorithm settings/three
basket-default and liquidation command encoding at
`app/modules/wb_repricing_state_commands.py`. Latest exact field/byte contract is
appended to the existing settings-state-schema-request; five pinned synthetic SQL
prerequisite vectors are in `tests/fixtures/wb_repricing_state_golden_v1.json`.
Implementation first, then final **232PASS0.18s**, naturalexit0, across new state
commands + existing assignment and override commands under env-i/offline sandbox.
Compileall/Ruff/diff0. No intermediate test loop, no live sources/DB/DDL/auth defaults.
Own final critical pass checked exact field families, Decimal bounds, scoped/version
binding, immutable children and liquidation terminal/confirmation rules. Real SQL
parity, new state repositories/CAS/audit and one-writer fence remain unimplemented;
T1 owns requested tables, and missing immutable mapping/source facts still block
reproducible canonical context publication. Codec is not permission or evidence of
the user's true ownership. Rollback is source/tests/docs-only, with no persisted rows.

Latest process amendment: complete an implementation package first, then its final
checks; no repeated intermediate test/review loops. Independent calculation service
now exists in `app/modules/wb_repricing_calculation.py`: detached recursively frozen
context, explicit clock/guard inputs, scoped versioned cache key and actual extracted
P_MIN/P_MAX, basket, plan-fact/interval, liquidation, clamp and rounding operations.
The existing repricer is not rewired. This does not complete Stage4 or authorize
prices: canonical context assembly from dated DB facts, mapping/source validation,
full apply guards, night-median execution, writer fence and settings persistence
remain separate required work. A reference is provenance, not authenticated evidence.

Final package check after implementation: new34 cases plus existing102 legacy
characterization = **136PASS0.71s**, natural exit0, offline sandbox/plugin. New
parity cases compare actual old/new outputs including explanations, interval history,
float precision boundary, price clamps, plan metrics and liquidation; original
characterization continues checking other unchanged guards. No RED-first claim for
this package: user explicitly changed the implementation/check order. Compileall0;
initial Ruff reported9 remaining diagnostics after2 safe fixes; final narrow legacy
arithmetic/timestamp and deliberate invalid-input annotations are documented in code.
No runtime flags, existing formula functions, provider adapters, migrations or shared
wiring changed. Rollback removes the detached service and tests; no data rollback.

Updated 2026-09-09 after root process-audit direction. This is the **only current
requirement status table**. Everything under HISTORY below is dated evidence, not
current readiness. Counts prove only named tests, never completion of a stage.

Scope amendment from user via root,2026-09-09: shared WB/Avito Production assembly
and printing is **DEFERRED, not DONE** (workitems/assignments/batches/schedule,
assembly sheets/XLSX/PDF/labels/print archive/delivery receipt). T3 owns its exact
deferred readiness/dependency inventory; T2 neither resumes its implementation nor
searches missing renderer/calendar sources. Preserve already committed code/schema
and required compatibility regression, but Production completion is not a gate for
remaining-scope acceptance/integration/push. Orders ingestion/storage/read/API/source
progression and all T2 repricer/settings/economics/sources requirements remain active.
KIZ/matcher stays excluded. Activation/provider/flag restrictions and heavy queue
are unchanged. Existing0069 compatibility may be tested without resuming P1–P4 work.

`implemented` = code exists at the stated SHA; `released` = bounded owner handoff;
`consumer-accepted` = downstream owner actually accepted that exact contract;
`final-integrated` = common candidate verified by root. The latter is **not yet
established for T2**. Production activation is NOT AUTHORIZED, separately from local
implementation/testing; no provider calls, flag changes, deploy or push in T2.

| Requirement | Actual implementation / released input SHA | Verified evidence and acceptance | Remaining / exact owner |
| --- | --- | --- | --- |
| 1 Writers, identity bridge, repository contract | `769f3cf44742622824233eefe8f81d4e360f8484`, `ccaed3410e32c50b3604f02e5b1171fd7243ce49`, `45f94a39627ebe2916903e9e5e0930844f3daaf5` | Inventory/explicit identity bridge/scoped contract implemented; T1 approval0066 consumed by merge `1464fcdfaab15b053678dd4f6d0337d958dab7f7`; valid action keys unchanged | T2 current writers still disconnected; account/membership bridge is not authorization or writer cutover |
| 2 Durable approvals | `bd3bb60a0baadc8d7683c8efd8bf4eff444c9643`, legacy import `9c7e5baa6dc3b3b2f113671b03db6c301942b1e7` | Actual PG participant + committed user-session create/claim/reject/block; recorded93PG cases include CAS/races/scope/restart/rollback/import; locally accepted0066 only | T2 full Protocol/worker-authorized outcome service and current-flow integration incomplete; T1 accepted worker authority/schema needed, then T2 service acceptance |
| 3 Jobs, attempts, receipt, crash recovery | Participant at `bd3bb60`; job-domain request `3c99f1eddb4750faa2be0ce79806c2d6f26cbc7e`; raw receipt codec `43e2cab31707192f2a30ab9b00ca7e56c6b11c4d` | Durable attempt/marker/outcome primitives tested;95synthetic receipt cases. Job/receipt domain inputs released to T1, not DDL or worker-service READY | T1 exact jobs/receipt DDL + trusted worker/closing resolver; T2 actual dispatch/reconciliation/crash matrix through fake-provider service, duplicate delivery proof, one action owner. No external exactly-once claim |
| 4 Normalized settings / removal of globals | Codec `bfc6c0e`, vectors `a6b9fd1`, characterization `f3c61d7`; permissions consumed `5294fdb`; access `fcaf22984b6658095ca28a3c4b618c3d56361f32`; accepted T1 override0070 `54386d4a789b0b74ae30710035defe949ea44b6a` merged `1e56b57915fa0be3c067f2ae79d3984752678bbe`; dormant service in accompanying slice | Actual43PG PASS10.62s: access19 + service24 including read/replay/CAS/rollback. SQL prerequisite consumed; bounded service locally verified, not downstream/final integration or writer cutover | Org settings/assignments/liquidation/context rows and mapping versions remain T1/T2 work. Existing globals untouched; one-writer fence/cutover NOT done |
| 5 Price snapshots / stock snapshots / daily / typed reads | Price `4e14e21ce79ebda29144d17db0b4a798f4bd2ec4`, stock `f7a5d62548d86f39c588c531c143936f0af8e55a`, chronology fix `901c1796e9a0ab04c0a4c7256e19bcce64dbaef1`; DDL request `907ec7b46ea5b6427385c7e80a204cb80f4adcad` | Immutable typed adapters/manifests/grain/presence and UTC chronology implemented;104scoped parser cases. Not actual publication or historical stock persistence | T1 source DDL; T2 atomic publication/current-head CAS, stock daily revisions and typed DB reads, fake collection/restart tests. FBS exact identity contract and source freshness policy unresolved; today's stock is not historical evidence |
| 6 KTR and source revision policy | `94b540f592a0fed3a29ce476fdb3dbd7ce58a604`; stock evidence request `0edde8bcc735999eaec0361620af47eee0b2c8a0`, codec/vectors `0c8fca9c5f8bdf62e630398cb1bd663acfb2cee0` | SourceDiff classification and stock count-only bytes implemented;69pure codec/diff cases. T1 accepted8vectors as prerequisite, not SQL/auth/publication proof; exact requests delivered | Owner source evidence: KTR artifact/effective bands + local/all-orders grain; T1 evidence DDL/auth, T2 complete diff+parent-linked daily correction service. Finance rrd/fingerprint limitation preserved; scheduler off |
| 7 Final profitability | Decision package `94b540f592a0fed3a29ce476fdb3dbd7ce58a604` | Synthetic alternatives delivered; no financial rule selected, no final-profit implementation/acceptance claimed | Financial owner approval + complete source evidence, then T2 canonical service/golden tests. netProfitKopecks/profitClass/abcCode remain null; no guessed allocation/account/backdate/rrdId/hash adjustment |
| 8 Test debt, rollback and common integration | Current code candidate `f3c61d7704813790245ec73096c753ee5dd93915`; scoped fixes/commands below | Named unit and owned-disposable PG evidence only; inherited lint deltas disclosed. T4 independently reran102characterization cases with OS network deny. No full backend/combined platform acceptance | Root one common integration milestone after T1 platform release; T2 consumer regression on accepted dependencies, remaining assigned failures and rollback verification. No further abstractions/characterization without a concrete service need |

### Independent finance readiness regression (2026-09-09)

Historical root-assigned IDs are not fresh baseline failures. Local scoped probe
reproduced two readiness fixture failures (missing current finance metadata) and
the obsolete unscoped P&L expectation. Source tests now preserve the existing
cache-only blocked contract; positive parsing/reconciliation coverage remains.
Retry delay assertions use the existing injected sleeper, never actual sleeps.

A separate real guard defect was reproduced: the daily-date union fallback of
`_range_source_ready` accepted legacy/wrong-basis finance caches even though the
covering-range path rejected them. New six-case regression: **4FAIL2PASS1.08s**
before the fix; the fallback now applies the identical existing basis predicate.
Explicit `require_current_finance_basis=False` onboarding compatibility remains.
No formula, basis constant, source fetch, writer activation or schema change.

Fresh combined lightweight gate: **19PASS23deselected1.03s**, natural exit0:
`tests/test_reports_sources_runtime.py tests/test_repricer_finance_readiness_guard.py
tests/test_repricer_tasks.py -k 'reports_sources_runtime or partitioned_finance or
rnp_daily_baskets_ready or onboarding_readiness or report_snapshot_source_ready'`.
Used isolated env, fake mode/in-memory SQLite, OS sandbox and
`-p tests.repricer_offline_plugin`; no actual tasks/provider calls or PG allocator.
Three test files Ruff0; compileall0. Runtime Ruff1: same29 inherited diagnostics
as HEAD, no new diagnostic. Read-only critic found no defects; no independent
test run implied. This is a bounded stage8 fix, not full-suite baseline closure.
Rollback: revert this code/test slice; no persisted facts or schema to roll back.

### Cold onboarding legacy test / containment contract (2026-09-09)

T1 assigned exact `test_wb_repricer_bff.py::test_manual_cold_full_sync_enqueues_onboarding_task`
and explicitly delegated the single SAFE_MARKER update in shared
`test_background_test_isolation.py`. Current route already uses an API background
thread and `api-bg-onboarding-{uuid}`; no runtime dispatch behavior is changed.
The test now stubs status diagnostics before any cache/secret access, captures exact
thread arguments/name/daemon and rejects an unexpected Celery dispatch. The inline
runner is synthetic, not the actual onboarding task. Historical node ID retained.

Fresh isolated old-ID RED **1FAIL2.03s**; corrected test **1PASS2.43s**. Then actual
child containment old-marker RED **1FAIL2.13s** showed precisely
`INNER_STATUS=0 COLLECTED=1 FAILED=0 ERRORS=0 SENTINEL_REACHED=0`; updated only its
expected marker. Combined domain+containment **2PASS4.03s**, naturalexit0, two
inherited Starlette deprecation warnings. Scrubbed child environment, actual-entrypoint
sentinel, exact stdout/stderr assertions and natural shutdown remain unchanged.
Same OS/offline parent fences as above; no provider/PG/job or baseline-list edit.
Ruff of the legacy BFF test remains72 inherited diagnostics, same as HEAD; shared
containment code has only the marker change. Compile/diff verification recorded
with this slice: compileall/diff exit0. Read-only critic found no defects and did
not run tests. Test-only rollback; no data/schema/runtime rollback needed.

### Report daily-source fixture compatibility (2026-09-09)

Fresh exact historical acceptance case reproduced **1FAIL1PASS55deselected1.88s**:
the fixture lacked the already-required `retailAmount`/`v3` finance metadata.
Corrected both positive and daily-detail-negative fixtures, preserving rejection
for daily count0; added three explicit missing/old-version/wrong-basis negatives.
No router/source/formula changes. Focused report readiness group **5PASS55deselected
1.59s**, naturalexit0, two inherited Starlette warnings, same offline fences.
Compileall/diff0; legacy test Ruff1 same20 inherited diagnostics as HEAD.
Read-only critic found no issues and did not run tests. This proves scoped fixture
compatibility, not full report/finance acceptance.

T1 explicitly accepted the shared containment delta in combined `e3d46fadb4e37738502882a8703eb51b00b380bb`
after reading it, attributing T2 test evidence. Root notified: do not import its
marker alone into a branch retaining the old domain test. Root admission remains
separate from this owner acceptance.

### P&L cache-only TTL fixture and combined local gate (2026-09-09)

Historical `test_sprint_d_reports.py::test_pnl_report_response_is_cached_for_two_hours`
first failed on finance reread count2vs1 (missing fixture basis), then on TTL86400vs7200.
Current runtime already sets fresh/stale TTL24h/24h, with no usable stale window;
runtime TTL, formula and source behavior are unchanged. Added synthetic range-list
stub (no DB fallback), current basis fixture, retained historical node ID/documented
24h contract. Through3h assert exact cached response/no reread; at25h assert rebuild,
one reread, preserved revenue and replacement timestamp. Actual RED1FAIL2.12s then
RED1FAIL1.71s → **1PASS1.47s**, two inherited warnings. Critic no issues/no tests.
Compile/diff0; scoped legacy Ruff1 same2 inherited UP017 as HEAD.

Final combined offline command under the same isolated env/sandbox/plugin:

```text
python -m pytest -q -p tests.repricer_offline_plugin
tests/test_reports_sources_runtime.py tests/test_repricer_finance_readiness_guard.py
tests/test_repricer_tasks.py tests/test_wb_reports_bff.py
tests/test_sprint_d_reports.py::test_pnl_report_response_is_cached_for_two_hours
tests/test_wb_repricer_bff.py::test_manual_cold_full_sync_enqueues_onboarding_task
tests/test_background_test_isolation.py
-k 'reports_sources_runtime or partitioned_finance or rnp_daily_baskets_ready or onboarding_readiness or report_snapshot_source_ready or report_daily_sources_ready or test_pnl_report_response_is_cached_for_two_hours or test_manual_cold_full_sync_enqueues_onboarding_task or background_test_isolation'
--tb=short
```

Actual **29PASS76deselected4.83s**, naturalexit0, two inherited Starlette warnings.
Selector also covers the two report-router RNP readiness cases. No fullsuite/baseline
list or root integration claim, no provider/real job/production/PG allocator.
Report readiness fix `5f97bdf83f0444591653da9619da8de091f644c8` is test-only.
Rollback of this TTL slice is test/docs-only, without data/schema changes.

Additional bounded P&L fixture group: negative-revenue historical ID actualRED1
IndexError1.69s → preserved original negative-money assertions; missing-ads-token
ID actualRED1 absent module attribute1.62s → assert removed live import stays absent,
poison actual adapter and assert zero calls even if exceptions get caught. Current
basis and synthetic range/cache-write stubs avoid implicit storage. Positive and
fallback caches actualRED2 empty rows1.60s; after metadata4PASS1FAIL1.54s exposed
obsolete commission expectation20k versus actual18k. Existing local history
`58c32d83b9bce69ce1957e48e45b24001cc107c6`, helper159–166 and field mapping1186
already prefer reported commission: explicit reportedCommissionRows1 now pins18k
and legacy computed54,700 profit; fallback retains20k. No new canonical financial
rule or production formula change. Final five P&L cases **5PASS17deselected1.50s**,
natural0/two inherited warnings. Finance owner decision for canonical final profit
remains pending; this is compatibility coverage, not business approval.
Final rerun after the UTC-only lint correction: **5PASS17deselected1.52s**, natural0;
compileall/diff0. Read-only critic found no issues and did not run tests.

Lint correction: earlier TTL entry measured2 UP017 before the final25h assertion,
but committed17b0172 added a third. Current slice replaces only that new occurrence
with `UTC`; actual scoped Ruff now2 original UP017 again. No blanket formatting.

### Stock grouping / WoW cache-only legacy contracts (2026-09-09)

Stock grouping test safely isolated legacy history helper before calling renderer;
that helper otherwise mutates process memory and backfills past days from current
stock. No runtime history change or evidence claim. ActualRED1FAIL1.61s only on
obsolete `marketplaceStockUnits` column expectation. T4 read mounted StockTableShell
and backend contract: actual columns totalStockUnits/wbStockUnits; marketplace
remains row/KPI data. Changed assertion to WB column, preserving raw80/100/20,
KPI80/100, warehouse grouping and missing-KTR assertions; captured history arguments.

WoW historical live-builder test actualRED1 absent import1.66s, then combined
1FAIL1PASS1.64s cache-only versus live expectation. Actual current fallback reads
scoped current/previous cached source and ads snapshots. Test pins those calls,
org and date pairs, forbids actual live adapters and ABC/repricer row fallback,
retains numerical orders/sales/delta assertions; metadata stage names now match
existing cache-only runtime. No source/formula/job/provider implementation changes.
Final pair **2PASS1.75s**, natural0/two inherited warnings. Compileall/diff0,
Ruff1 same20 inherited diagnostics; read-only critic no findings/no tests.

T1 requested Review0071 smoke slot after this pair naturally completed; T2 ACK:
no active heavy/test/reservation/cleanup, only preparation while their slot held.
Separate remaining WoW task ID statically traces to existing background ABC/funnel
composition, not this cache-only fallback. Do not conflate their contracts or run
the legacy task with unmocked dependencies. No blanket rerun or baseline edit.

WoW task case subsequently safely reproduced absent-import **1FAIL1.51s** before
task invocation. Repaired only the test to actual existing background ABC route:
strict org/current/previous calls through synthetic ABC, period stats and funnel;
no token forwarded, no repricer fallback for populated ABC. Rules and cache I/O
stubbed, real pure mapper/shell preserved; exact background-abc metadata and saved
previousRange checked. Direct `.run` is a synchronous synthetic test, not a queued
or production job. Final task+fallback+stock **3PASS1.51s**, natural0/two warnings.
Compile/diff0, same20 inherited Ruff diagnostics, critic no findings/no tests.
No durable/auth/queue claim. T1 next single ACL RED ACK given only after natural
completion, no active T2 tests/reservation/cleanup.

Latest stage8 fixture evidence (not a new service): three ABC cache cases repaired
with current finance metadata and explicit synthetic sales-funnel observations,
preserving every existing financial/ranking/date assertion. No business rule change.
Original period-cache case1FAIL1.44s→1PASS1.47s; other two actual2FAIL1.75s→3ABC
PASS in the combined probe. HTTP P&L positive cases originally2FAIL (blocked without
evidence), while the three ABC cases passed: bounded5case **2FAIL3PASS1.99s**.
Prepared exact default-period synthetic finance/ads cache inputs, retaining actual
test login, permissions and original assertions. After fresh T1/T3/T4 admission:
**5PASS17deselected3.85s**, natural0/two inherited warnings. Initial peer release
mistyped elapsed1.82s; immediately corrected to actual3.85s, no count/exit ambiguity.
Unused/uninvoked RNP fixture preparation was removed, not included as completed work.
Critic found no issues in these five fixtures; HTTP GREEN subsequently confirmed.
No runtime/source/flags/schema change or canonical final-profit approval. This is
test-process memory/synthetic cache compatibility, not durable PostgreSQL proof.
Exact selector in `tests/test_sprint_d_reports.py`:
`pnl_finance_viewer_gets_financial_fields_and_preliminary_state or pnl_supports_operative_and_final_states or abc_report_uses_repricer_period_cache_without_own_report_cache or abc_report_net_profit_uses_full_finance_formula or abc_report_uses_covering_repricer_daily_cache`.

### Canonical SKU policy — one compatibility record

Root explicitly approved: independent get/history require fixed `settings:read`;
replace and exact-command replay require **both** `settings:read` + `settings:write`
before reading receipt or acquiring domain locks. A write-only custom principal is
now denied by the new canonical API, even if a legacy write path would allow it.
This is intentional compatibility impact, not inferred historical behavior. Existing
profiles/aliases, profile+explicit-grant union, operational grants and legacy routes
remain unchanged. No price:send/team:write fallback or client-selected permission.
Fresh authenticated user/session/membership/exact account scope and final commit
revalidation apply to each call/replay; idempotency key grants no authority.
T1 released exact shared constants at `47ca9513244ea29f21aba47527cbd8807d31caed`;
T2 consumed via cherry-pick-x `5294fdb0860092b20c1fcc9c71b781b8666518a3`.
Context-only conflict resolved by adding only the two SKU sets: T2 did not yet have
adjacent Production constants, which were neither imported nor deleted by this slice.
Actual pure exports+override codec **81PASS0.07s**, compileall/diff0. This confirms
the exports, not authenticated service or DB authorization; those remain required.
Required service matrix: read-only/write-only/both/neither, cross-account, revoke
after wait, denied mutation and receipt non-disclosure. No repeat policy approval needed.

Verified access-only consumer slice (not a repository/service release):
`app/modules/wb_repricing_override_access.py` and `tests/test_wb_override_access.py`
use the actual released permission exports, existing publication guard and existing
Catalog offer rows. Separate read and replace entry points; mapping SHARE locks
follow auth/account locks and persist through the caller-owned root. Actual final
**19 PASS / 5.59s / natural exit0** on accepted0066 with non-owner runtime and OS
network-deny sandbox, including observed mapping/revocation waits. Scoped Ruff,
compileall and diff-check exit0. Exact temporary DB
orders_test_df4ce14e8bad420794bf40e270aedd59 and role
repricer_runtime_f0c44acef0814f2f9153aabfb978d300 cleanup absence verified.
Existing libpq /dev/null passfile warnings retained. Read-only critic found no
defects in the domain-error fix; PostgreSQL concurrency proof is this actual run.

Historical RED steps for this slice (superseded by final19PASS above):
Missing-module collection RED verified; scoped Ruff/compile0. First admitted actual
PG gate stopped with **14 PASS / 1 FAIL, 3.40s, natural exit1**: shared guard safely
normalized the unsupported domain mapping code to publication_persistence_failed.
Fixed locally with a separate fixed-message OverrideMappingUnresolvedError, leaving
the shared guard/allowlist unchanged. Exact owned DB orders_test_a941c45710c94013a90988060802ad43 and role
repricer_runtime_c2c7919ec28545cfb91e6bb2e7b4ba1d cleanup absence verified; no pending
cleanup. This is no substitute for the pending
override repository/receipt/CAS/audit implementation and is excluded from frozen
integration candidate f3c61d7. No new schema or general-purpose repository added.
Second admitted gate: **18 PASS / 1 FAIL, 9.97s, natural exit1**; mapping blocker
regression passed. The membership-race observer incorrectly matched FOR UPDATE,
where the accepted guard uses with_for_update(read=True), i.e. FOR SHARE. Corrected
that observer only; no timeout or guard change. Exact temporary DB
orders_test_5729daa46d174241b2f6c65cd2538fb0 and role
repricer_runtime_dadeca7a32b64e75929cdfe5e512b7e9 cleanup absence verified.
Independent follow-up: all six invalid-authority cases now explicitly observe zero
Catalog queries, not merely an eventual exception. Future service ordering is BOTH
live authorization -> exact scoped receipt lookup -> current mapping lock only for
a new revision. The combined replace helper is not a replay entry point; historical
replay must not require a surviving current mapping. This boundary was sent to T1.

### Stable consumer candidate for the common milestone

New accepted prerequisite, separate from the frozen common milestone:
T1 SKU storage `54386d4a789b0b74ae30710035defe949ea44b6a` (0070 after0069)
consumed with full ancestry by merge `1e56b57915fa0be3c067f2ae79d3984752678bbe`.
Only merge conflict was permissions context after prior SKU-export cherry-pick;
the final permissions, all migrations and runtime SQL match T1 exactly (diff0).
Release annotation `6e35c06ba3d66790c0b0f7091d5d22f5c9ad5469` consumed as
`1362dab`; full independent review read: Approved,0Critical/0Important/2Minor
(targeted FK tests can be masked by deferred orphan; inherited passfile warnings).
No independent T2 schema rewrite, ORM/config/flag change or deferred Production work.
Postmerge actual access baseline19PASS; new service test then failed after real0070
bootstrap because the service module was absent (19PASS1FAIL9.94s/natural1).
Minimal first-write implementation now passes its actual PG test:1PASS4.20s/natural0,
physical commit visible from another connection across revision/head/audit. Both
gates used the accepted scrubbed Unix allocator and network-deny sandbox; exact owned
DB/role absence was verified and heavy slots released. Bounded service now locally
verified by the43-case gate below; no current writer wiring or full stage4 acceptance.

### Dormant authenticated SKU override service — bounded release

Implementation commit `a14581534ec439f88e27811dc901babf308b42dd`; T1 subsequently
read the actual377-line service and access delta, verified exact schema/runtime/
permission ancestry, and accepted the bounded interface. This downstream interface
acceptance attributes T2's43PG evidence; T1 did not claim a separate service test run.
It is not root final integration, whole-stage completion or writer activation.

Post-release compatibility gate on the same0070 ancestry: existing
`tests/test_wb_repricing_postgres_repository.py`,
`tests/test_wb_repricing_approval_commands.py`,
`tests/test_wb_repricing_legacy_import.py` **93 passed in14.84s, natural exit0**.
Same exact scrubbed sandbox invocation below with these three test paths; no skips,
no changed tests and no failure delta in this bounded group. Exact owned cleanup
absence verified for all three databases/roles:

- `orders_test_283b990aa8df4ba1ab5c916547f8265a` / `repricer_runtime_c4bd82cee6c34845a46ed858aec699c3`;
- `orders_test_10b2c1008d474543bb58ef3e7b6c2acc` / `repricer_runtime_2354ed388ab6466b983948bdef14f836`;
- `orders_test_2e81894f764d4f0d94305290579e8031` / `repricer_runtime_7e3143e4b58f41bebbc57866fa4175b5`.

`app/modules/wb_repricing_override_service.py` owns a fresh PostgreSQL physical root
per call. `SkuOverrideService(engine,max_request_bytes=...)` requires a PostgreSQL
Engine and explicit trusted byte budget; methods are `replace(change,principal,
binding,now=aware_datetime)`, `get_current(OverrideScope,principal,binding)` and
`history(OverrideScope,principal,binding,limit=...,before_revision=...)`. History is
descending, cursor-exclusive and returns an immutable tuple; trusted caller owns
the page/resource budget. No HTTP, provider, worker or legacy adapter is connected.

The private SQL write participant deliberately has no public unguarded repository
surface. Read/current/history use fixed READ; replace/replay fixed BOTH. Authenticated
membership must equal command actor. Mapping-only helper grants no authority and
is used only after the same root's command guard and missing receipt. All receipt
queries include org/account/SKU/command; reads reconstruct the immutable command
from actual typed columns and verify canonical bytes/hash. NUMERIC is decoded as
Decimal/int, never float. Current head/version/time must agree with its revision.

```
live principal/session/member + exact account lock
  -> full-owner command lookup
     -> matching actor+bytes: original immutable revision -> final auth -> COMMIT
     -> new command: mapping SHARE -> scoped head/expected-version
        -> revision INSERT -> head INSERT or scoped version CAS -> audit INSERT
        -> final live auth -> physical COMMIT -> return

holder locks exact account
  worker A --wait chain--> holder
  worker B --wait chain via A or directly--> holder
release holder -> one revision1 winner -> other sees version1 -> conflict
```

Actual accepted0070 runtime tests: **43 passed in10.62s, natural exit0** (19 existing
access +24 service). Coverage: physical commit visibility; full-null first row;
later revision and historical replay after mapping deletion; historical reads after
deletion; exact2**80 money/long signed Decimal/false/zero roundtrip; bounded history;
same-key changed payload/version/actor conflict; stale new command; read/write/both/
neither permissions before receipt reads; independent command namespace for two WB
accounts in one org; audit-statement and physical-commit failure rollback; final
live-session revocation rollback; two actual concurrent Sessions with observed
transitive owned wait chains and one CAS winner. No sleep-based winner assumption
or timeout widening. Temporary resources were exactly removed/absence verified:

- `orders_test_3fced08fcaf74c3d9bb1ff711c1a770e` / `repricer_runtime_4ab4add211714eb8a75e5f389c3bc560`;
- `orders_test_5a0a488a631f4aa29e28ad3b3c1b8003` / `sku_override_runtime_3047b427ea784432874bd514374d13bc`.

RED history retained: missing service after bootstrap19PASS1FAIL9.94s; firstwrite
1PASS4.20s; historical replay after remapping1PASS1FAIL4.30s; then34PASS6FAIL/
3deselected14.24s (five missing-read-API failures and a too-narrow direct-blocker
observer). Read API implemented; observer changed to actual transitive blocking
graph with the same5s deadline. The last43-case gate is the final evidence, not a
claim those earlier failed gates passed. Inherited libpq passfile warnings remain.

Exact final PG command, from this worktree's backend (exit0):

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/t2-repository-offline.sb /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -m pytest -q -s tests/test_wb_override_access.py tests/test_wb_override_service.py --tb=short
```

Scoped backend-cwd Ruff/compileall/diff-check are the static gates. A root-cwd Ruff
invocation inferred app as third-party and reported I001 in the new test; no import
behavior changed to satisfy that alternate cwd. Read-only critic inspected current
service/access/tests and found no safety defects; its initial stale FOR UPDATE
observation was explicitly withdrawn after rereading actual FOR SHARE source.

Rollback is code-only while dormant. No automatic downgrade of populated0070 and
no runtime memory/file fallback. This is not current-flow integration, one-writer
fence, complete normalized settings, immutable mapping context, source persistence
or full backend regression acceptance. The frozen root candidate remains unchanged
until an explicit new consumer milestone is agreed. Production/flags/provider
actions were not run; imported T1 ancestry is preserved, not independently rewritten.

Immutable T2 code candidate: `f3c61d7704813790245ec73096c753ee5dd93915` on
`codex/arch-t2-economics`, base `c88a474695569af905b294906ba604d61f3de62f`.
Exact accepted0066 prerequisite: `dbf8d31d9fc0b9035b9c8a84727e70713956afe0`,
merged with its platform/guard ancestry by `1464fcdfaab15b053678dd4f6d0337d958dab7f7`.
This includes the0066 consumer and released pure source prerequisites; no speculative
imports of upcoming override constants/tables. Accepted compatibility currently means
0066 plus its merged guard prerequisites, **not** the later combined platform.
Root may use this candidate after T1 platform release for one shared migration/import/
contract/consumer gate; root owns integration and authorized push. T2 will update the
candidate when an actual new accepted repository/service dependency is consumed,
not for each test/docs commit. Stage2/3/4/5 remain incomplete after this milestone.
Read-only `git diff --exit-code 9c7e5ba f3c61d7` confirms the participant/user-command
modules and all three PG acceptance test files byte-unchanged; merge-base verifies
0066 ancestry, both exit0. These are snapshot checks, not a new platform test run.
Next work priority is actual worker/outcome/jobs/receipt and normalized override/source
repositories/services using exact released T1 contracts, not extra generic codecs.

## HISTORY — prior snapshots and per-slice evidence (not current status)

## T1 SQL encoder golden-vector dependency

`tests/fixtures/wb_repricing_sql_golden_vectors_v1.json`: eight synthetic vectors
generated by exact45f94a3 domain serializers (those three modules are byte-unchanged
at this delivery). Actual API is `CanonicalApplyRequest`, not a function named
build_price_apply_request. Each vector stores typed input scope/request, UUID4,
canonical request ASCII text/byte count, request checksum, action/dispatch keys and
pure provider-body bytes as ASCII text. No provider request was sent.

Coverage: explicit optional NULL; all optional IDs/minimum populated; Cyrillic;
decomposed/precomposed combining text independently; supplementary emoji;
quote/backslash/slash; permitted interior approval controls including DEL; business
integers above BIGINT. Org/account/catalog remain positive internal INTEGER-range IDs.
All requests fit4096 bytes (218..335). Fixture itself is ASCII JSON: load outer JSON
once, then encode `canonical_request_ascii` as ASCII; do not JSON-encode it again.
Use exact integer JSON decoder (Python json.loads); JS Number would lose big integers.
SQL encoder should compare to these literal bytes/hashes, not regenerate expected
results through SQL under test. Fixture is never regenerated automatically in tests.

RED fixture missing →GREEN parity regression; kernel/repository/dispatch adjacent
228PASS. Existing negative Unicode corpus remains in test_wb_repricing_postgres_text.py.
This supplies parity vectors requested by T1 design7e9085c, not DDL acceptance.

Branch `codex/arch-t2-economics`, base `c88a474695569af905b294906ba604d61f3de62f`.
Worktree `satornawb-main/.worktrees/arch-t2-economics`. No push/deploy/production,
provider requests, working DB/Redis/state, schema/config/shared wiring/flags changes
in these slices. Existing pure approval ownership/action keys preserved. Tests use
synthetic values; cost/economics legacy tests use ephemeral in-memory SQLite, **not**
a substitute for PostgreSQL approval acceptance. Offline plugin fence is Python-level,
not an OS/native networking sandbox. No native PostgreSQL calls in this delivery.

## Exact ready commits since root's 6ee8485

| Commit | Scope / evidence |
| --- | --- |
| `4e14e21ce79ebda29144d17db0b4a798f4bd2ec4` | Seller-price raw parser/immutable pages/complete manifest; initial54 tests; corrected below |
| `45f94a39627ebe2916903e9e5e0930844f3daaf5` | Kernel/repository/dispatch reject PostgreSQL-unrepresentable text;72 new tests,227 adjacent; updated approval schema request |
| `907ec7b46ea5b6427385c7e80a204cb80f4adcad` | Two independently reviewed exact bounded DDL requests: normalized state4A and prices/stocks/daily5 |
| `c11d07b3d623c5e534bac84166714377d3312a3f` | Safe DecimalException decode boundary;2RED→56parser+16root syntheticPASS |
| `02591f3c249fbb32082e802b27ca7a058fff83e6` | Pure scoped CollectionRequest canonical bytes/checksum;29 tests |
| `f7a5d62548d86f39c588c531c143936f0af8e55a` | WB warehouse stock parser/grain/complete manifests/receipt-daily eligibility;42 tests |

Exact changed files per commit: `git show --stat --name-status <full SHA>`. New modules:
`app/modules/wb_price_snapshots.py`, `wb_source_requests.py`, `wb_stock_snapshots.py`;
new tests respectively `test_wb_canonical_price_parser.py`,
`test_wb_source_collection_request.py`, `test_wb_stock_snapshots.py`, plus approval
`test_wb_repricing_postgres_text.py`. Approval change touches only pure kernel,
repository Protocol/identity bridge and dispatch domain; no actual writer connection.

## Historical requirement snapshot — superseded by CURRENT above

| Stage | Completed evidence | Still required / actual dependency |
| --- | --- | --- |
| 1 writer inventory / identity / contract | 769f3cf + ccaed341 +45f94a; composite scope, exclusive marker, safe UUID/text, unchanged valid hashes; accepted T1 approvals0066 dbf8d31 merged by1464fcd | No UUID-only repository; current writers still disconnected |
| 2 durable approvals | Actual scoped PostgreSQL transaction participant plus committed user-session create/claim/reject/block; real runtime-role tests, two-session CAS, replay, rollback, restart, crossorg/account, immutable terminals; owner-only synthetic legacy import preserving full snapshot | Full repository Protocol not complete: worker-authorized outcome service still pending; no current-flow cutover |
| 3 jobs/crash | Pure matrix plus durable reserve/dispatch marker/outcome participant, observed PostgreSQL dispatch lock race and one synthetic effect | Accepted worker/job authority PENDING per T1; no worker impersonation, scheduler/provider wiring or reconciliation activation. No external exactly-once claim |
| 4 globals/settings | 80 legacy characterization cases; exact normalized4A schema request907ec7b; pure four-strategy AssignmentChange canonical command bytes/checksum | T1 rows/heads/audit, concrete repository/context wiring and one-writer fence; additional strategies/context mapping must be typed before cutover |
| 5 prices/stocks/daily | Price and warehouse stock adapters,request hashes,immutable complete manifest assembly,missing/null/zero,all sizes,receipt-date gate; targeted legacy source fixes | T1 separate source DDL, DB publication/read services, current-head CAS and daily revision persistence; Catalog mapping/source freshness policy, FBS skus/chrtIds contract unresolved |
| 6 KTR/source revisions | SourceDiff classifications + synthetic closed-day revision evidence tests (94b540f/ee7bd6e) | Verified local/all orders grain, reference effective ranges/gaps/overlap evidence; unknown remains null, scheduler off |
| 7 final profit | Decision package94b540f with numerical alternatives | Explicit financial owner approval and complete source evidence. No invented OPEX allocation/backdate/rrdId/checksum change; netProfitKopecks/profitClass/abcCode remain null |
| 8 test debt / handoff | Targeted source/cache/RNP/economics regressions repaired; new scoped group below | Not full backend parity and not DB durability; no weakened guards/removed tests |

No claim that whole T2 scope or the complete repository/service protocol is ready.
Accepted T1 approval schema0066 `dbf8d31d9fc0b9035b9c8a84727e70713956afe0`
and its reviewed platform prerequisites were merged without conflicts by
`1464fcdfaab15b053678dd4f6d0337d958dab7f7`. State4A and source requests remain
independent dependencies, not queued behind complete repricer.

## Verification

From this worktree `backend`, runtime:
`/Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python`.

```sh
python -m pytest -q -p tests.repricer_offline_plugin \
 tests/test_wb_stock_snapshots.py tests/test_wb_repricing_postgres_text.py \
 tests/test_wb_source_collection_request.py tests/test_wb_canonical_price_parser.py \
 tests/test_wb_price_size_preservation.py tests/test_wb_repricing_dispatch_contract.py \
 tests/test_wb_repricing_repository_contract.py tests/test_wb_repricing_approval_domain.py \
 tests/test_wb_repricing_price_inputs.py tests/test_wb_source_revision.py \
 tests/test_wb_source_grain_contract.py tests/test_wb_stock_page_validation.py \
 tests/test_wb_price_units.py tests/test_repricer_calculation_characterization.py \
 tests/test_economics_internal_identity.py tests/test_economics_policies.py \
 tests/test_abc_pnl_costs.py tests/test_rnp_runtime.py \
 tests/test_rnp_source_period_isolation.py tests/test_repricer_bff_cache_contract.py \
 tests/test_repricer_cache_store.py
```

Ruff runtime `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check` and
`python -m compileall -q`: kernel/repository/dispatch/price_snapshots/source_requests/
stock_snapshots modules plus four new/updated tests listed above; both exit0.
`git diff --check`, staged diff checks exit0. Scoped test results recorded below;
counts are not added to infer a broader suite. Fresh combined command above:
**662 passed in2.24s, exit0**, no pytest warnings. Earlier independently scoped source
group556PASS and economics/cache group106PASS were also run separately;662 is an
actual combined run, not only arithmetic. Historical full-suite delta remains unproved.

Independent critique closed text UUID exception leakage, unsupported FBS transport,
schema nullability/audit/paused-slot ambiguities, collection integer serialization,
stock owner range and daily datetime overflow. Every code defect gained a failing
test before correction. Stock final three findings rechecked locally by fresh tests.

## Accepted approvals persistence slice

Owned code: `app/modules/wb_repricing_postgres.py`,
`app/modules/wb_repricing_commands.py`; tests:
`tests/test_wb_repricing_postgres_repository.py`,
`tests/test_wb_repricing_approval_commands.py`. Existing pure kernel, current
repricer, migrations/config/shared wiring are unchanged by this T2 slice.

`ApprovalTransaction` is explicitly an **uncommitted** scoped transaction
participant, not authentication and not send permission. `UserApprovalCommands`
owns its clean Engine-bound root and returns after physical commit only. The latter
uses accepted live PublicationGuard with fixed `price:send`, authenticated membership
derived from its principal, exact account binding and no request-selected permission.
It exposes no reserve/dispatch/outcome or worker API. Guard-through-commit behavior
also depends on accepted T1 guard tests, not solely the tests added here.

RED: missing participant module/API (collection exit2); missing attempt-ID boundary
6 failing cases, missing durable attempt2 failures (exit1). GREEN: corrected required
canonical UUID4 and absent-row conflict. User service RED: absent module (exit2).
Fresh combined real PostgreSQL run: **75 passed in12.77s, exit0**. The runtime role
is nonowner/NOBYPASSRLS, actual migrations run into disposable databases; fixture
verified exact database and role cleanup. Explicit null pgpass produces benign
libpq `/dev/null is not a plain file` warnings; no real credential files read.

Reproduction from backend (same interpreter as above): scrub environment with
`env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null
NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1`,
then `/usr/bin/sandbox-exec -f ../.superpowers/t2-repository-offline.sb python
-m pytest -q -s tests/test_wb_repricing_postgres_repository.py
tests/test_wb_repricing_approval_commands.py --tb=short`.
The local untracked test-only sandbox profile allows default, denies network,
then allows outbound Unix socket `/private/tmp/.s.PGSQL.5432` only, and denies
reads matching `(^|/)\.env($|\.)` or `(^|/)(\.pgpass|\.netrc)$`.
This is not an application/config change. No provider TCP access or production DB.

Fresh unchanged pure approval/identity/dispatch/Postgres-text/golden-vector group:
**228 passed in0.19s, exit0**. Ruff, compileall and diff checks are scoped to the four
owned Python files. Independent critic's missing-ID blocker fixed/rechecked;
final read-only review found no further blockers. Full backend baseline delta and
privileged backfill preservation are not claimed by these results.

Worker-authority amendment committed `a4de0b8a653857779391bb054ac06cde7d0ac184`
and sent directly to T1. Known upload ID with ambiguous requires a separate durable
receipt/evidence boundary: current domain/0066 prohibit upload ID on ambiguous.
Worker/provider/scheduler remain dormant pending that accepted contract.

### Independent synthetic backfill continuation

`LegacyApprovalImportTransaction` is a separate owner-only caller-committed surface;
runtime role denied before import. Explicit positive internal catalog/member IDs
must exactly match snapshot identities; unresolved legacy strings remain blocked.
No files read, rekeying, inferred IDs, repaired hashes or historical time changes.
All seven statuses, long logical ID, NUMERIC values above BIGINT, original timestamps,
replay/conflicting payload and transaction rollback are tested. Current old rows
are still immutable/non-dispatchable by0066; this is not a real data backfill run.

RED: absent importer API, collection exit2. Fresh combined three-file PG group
(repository, user commands, `tests/test_wb_repricing_legacy_import.py`):
**93 passed in11.94s, exit0**, exact three disposable databases/runtime roles cleaned.
Importer has18 cases. Scoped Ruff/compileall exit0. Independent critic found no
importer blockers; worker receipt limitation clarified in the existing amendment.
The historical note above about missing backfill proof is superseded only for these
synthetic cases, not a production migration/backfill or full backend baseline.

### Independent Stage4A assignment command codec

`app/modules/wb_repricing_assignments.py` +
`tests/test_wb_repricing_assignment_command.py` fix the full assignment command
encoding documented in the existing settings-state schema request. Strict internal
INT4 scope/member, UUID4 command, exact decimal version, four supported strategies,
explicit clear/null, interval compatibility and immutable aware UTC instant. Unknown
strategy/config is not defaulted. No registry/global reads, formula changes, clock,
repository or current-state mutation. A checksum is neither auth nor proof of DB CAS.

RED missing module exit2 →56 pure cases pass. Fresh combined codec+unchanged old
calculation characterization: **136 passed in1.05s, exit0** under offline plugin.
Scoped Ruff/compile/diff exit0; independent read-only critic found no codec issues.
This fixes assignment bytes only, not settings/overrides/liquidation serializers.

T1 additionally requested exact receipt schema evidence; the separate
`2026-09-09-t2-provider-upload-receipt-schema-request.md` traces available WB22/WB23
fields, lossy `_as_int`, mixed HTTP/task status, incomplete details pagination and
receipt-vs-applied semantics. No provider jobs run. Receipt DDL, strict initial-ID
adapter projection and worker/job reference remain pending; audit vocabulary and
finite separate-clock semantics are explicit, no guessed retention/permission/TTL.

### Dormant decoded upload-ID projection

After T1 accepted470d410 as domain input (not schema READY), added
`app/modules/wb_repricing_upload_receipt.py` with47 synthetic offline tests in
`tests/test_wb_repricing_upload_receipt.py`. Original decoded POST response only;
no float/bool truncation, invalid alias fallback, unknown transport or raw error
reflection. It returns only ID, never applied state or retained response payload.
No current adapter/client or provider execution is connected.

RED missing module exit2; nested explicit error2FAIL exit1 → corrected GREEN.
Fresh projection+assignment+dispatch group **165 passed in0.52s, exit0**; scoped
Ruff/compile/diff0. Independent critic47PASS and no material findings. Pure helper
does not establish raw JSON duplicate-key validation, trusted transport provenance,
job authority, committed dispatch proof or receipt persistence; those remain gates.

Worker dependency ownership clarified after critique: resolver belongs to T1, but
T2 can propose domain job rows/lifecycle independently. The worker amendment now
contains exact proposed `wb_repricing_jobs` UUID4 binding, composite approval/origin
references and creation audit. Lifecycle remains derived from0066, not a second
mutable state machine. Receipt gets a proposed concrete scoped job FK. Existing
ORM/memory/Orders proposal are not misrepresented as accepted price-worker authority;
trusted executor/closing resolver remains T1-owned and pending. No production or
shared schema changes and no fabricated worker principal.

### Independent raw receipt JSON boundary

`extract_post_upload_id_from_bytes` now accepts original immutable UTF-8 bytes,
requires an explicit trusted response-byte budget, rejects duplicate/escaped-equal
keys and malformed/non-finite JSON, and preserves integer lexemes beyond BIGINT
and Python's decimal-string digit limit. Decimal/exponent tokens never become IDs.
Only canonical ID is returned; no raw payload/checksum is retained. The decoded
entrypoint remains available but cannot establish raw JSON uniqueness by itself.

RED missing raw API exit2 → **95 combined raw+decoded cases PASS in0.06s, exit0**
(48 new raw cases). Independent critic reran95PASS; scoped Ruff/compile/diff0.
The existing transport does not call this helper: trusted POST provenance, job
authority, marker-commit proof and receipt DB publication remain separate gates.
T4's available/blocked matrix was sent before implementation, not used as a stopping
point; no frontend or active T1 schema/resolver work was duplicated.

### Independent full-row SKU override command codec

Added dormant `wb_repricing_overrides.py` and61 synthetic tests. Full immutable
nullable row, strict internal ownership/member IDs, UUID4 command, expected version,
lossless money/Decimal canonical bytes and SHA-256. No new percentage bounds or
cross-field formula rules; existing formula guards are unchanged. Explicit trusted
byte budget rejects compact exponent expansion. No current override registry read
or write, no persistence/auth claims and no dependency on unaccepted schema.

RED missing module: collection exit2. GREEN focused56PASS; five extra decimal
position cases added. Fresh combined override/assignment/decoded+raw receipt run:
**212 passed in0.88s, exit0** under offline plugin. Scoped Ruff (two mechanical
test literal fixes), compileall and diff checks exit0. Independent critic reran
the original56 cases, exit0, and found no defects. Existing schema request records
the exact codec boundary; Stage4 persistence, immutable calculation context and
one-writer fence remain unimplemented gates, not implied by these tests.

Follow-up contract pinning: fixed v1 all-NULL golden checksum, all14 override
fields individually participate in the hash, and AST import allowlist prevents
accidental runtime/client dependencies. Fresh combined group **228PASS in0.16s**;
override suite now77 cases. Ruff from `backend` exits0. A root-cwd Ruff invocation
reported I001 because import-root inference differs; the documented backend-cwd
invocation passes without source changes. This is a scoped check, not whole-repo lint.

### Real independent input-guard defect: DST elapsed TTL

Strict-boundary review found a real defect in dormant `wb_repricing_price_inputs`:
adding TTL/comparing datetimes in the same timezone used wall-clock semantics.
Synthetic New York fall-back accepted an expired observation as fresh and accepted
a future fold as current; spring-forward expired a still-fresh observation early.
RED **3FAIL32PASS**, exit1. Root cause fixed by normalizing comparison instants to
UTC before elapsed-second TTL arithmetic, while retaining original observation.
UTC overflow fails closed; timezone/freshness policies and TTL values are not invented.

Six regressions (DST3, UTC boundary2, provenance/equivalent offset1). Independent
critic **38PASS**, no findings. Fresh combined price-input/stock/price-parser/source-
revision/unchanged legacy calculation group **245PASS54.76s**, exit0. Compileall and
diff0. Ruff exit1 has exactly the same5 diagnostics as pre-change HEAD: TRY004,
I001, UP017, C408x2; new test lines use UTC and introduce no additional diagnostics.
This is not a full-backend baseline claim. No current repricer wiring or formula edits.
Rollback is a code-only revert of this bounded fix; no data/schema/backfill exists
for this dormant guard. Activating it still requires accepted canonical inputs and
the existing approval/economics/one-writer gates.

### T1-requested stock evidence codec and literal SQL vectors

Implemented the existing0edde8b §3 row/diff/proposal wire contract, no new policy
or abstraction outside that request. New `wb_stock_evidence_codec.py`, focused
test and eight literal vectors (`wb_stock_evidence_golden_v1.json`), with full inputs,
ASCII bytes/count/hash. Native identity separated from count-only payload; missing/
null/zero, BIGINT, lexical sorting, all delta categories, Unicode/escaping covered.
Proposal derives hashes/counts, rejects duplicate/empty evidence and invalid owners.
Explicit encoding budget is caller-supplied, not invented permission/retention policy.

RED missing module exit2 →40 focusedPASS0.05s, including actual fixture parity.
Read-only critic found fixture-test gap during work (closed by non-regenerating test)
and preallocation order (fixed). No critic test processes during T1 resource slot.
No PostgreSQL/integration/browser/build/fullsuite run; scoped lightweight verification
only. Complete-run/evidence auth/parent-linked CAS/daily eligibility remain separate
unproved gates. Existing source hashes/compare_runs/formulas/flags untouched.
Final lightweight codec+existing SourceDiff run **69PASS0.07s**, exit0; scoped
Ruff/compileall/diff checks exit0. No accepted SQL parity or DB gate implied.

### Source page chronology: same-zone DST regression

Independent strict-boundary review found the same wall-clock comparison issue in
both canonical price/warehouse-stock run assemblers. RED6FAIL0.08s: each rejected
valid increasing fall-back instants, accepted reversed actual instants, and accepted
a UTC-unrepresentable receipt. Fixed only comparison locals to UTC, safe domain
overflow rejection; original pages/times/raw checksums/manifest algorithm unchanged.
Added `tests/test_wb_source_page_chronology.py`, preserving page object identity and
comparing manifest checksum against an equivalent UTC representation.

Fresh lightweight new chronology+existing price/stock parser group **104PASS0.10s**,
exit0; scoped Ruff/compileall/diff0. Read-only critic no findings, no test process.
No heavy slot used, no actual source request, schema or current publication/wiring.
Rollback is a code-only revert, no migrated facts. New schema dependencies requested
directly from T1; uncommitted0069 Production work is unrelated, not consumed as a
repricer/source READY handoff. Continue independent work while awaiting exact acceptance.

### Next schema selection: SKU overrides / dedicated parity vectors

T1 selected bounded SKU overrides only from existingbfc6c0 request, not whole4A;
this can unblock a scoped transaction participant without worker authority or
source/context wiring. T2 provided exact request/codec/pinned-test SHAs, explicitly
not claiming a dedicated fixture existed at that point. Then supplied the requested
six literal vectors in `wb_repricing_override_golden_v1.json` covering fullowner,
14fields, NULL/false/zero, signed/fraction Decimal normalization,2**80 money/version,
longprecision, modes andINT4 boundaries. Existing encoder byte-unchanged.

Actual missing-fixture RED1FAIL77deselected → focused **78PASS0.07s**, exit0; test
loads pinned expected bytes/hash/count and never regenerates.8192 syntheticbudget,
no operational policy. Native NUMERIC limits and separate settings-write/live mapping
auth must remain explicit; these vectors are SQL parity inputs, not SQL proof.
Scoped Ruff/compileall/diff exit0; read-only critic no findings, no extra test process.

### Independent override-to-calculation characterization gap

Added22 test-only cases to existing `test_repricer_calculation_characterization.py`:
explicit pMin/alias/zero inheritance, fixed costs/pick-pack/expense precedence,
nonpositive denominator fallback, pMax/RRP/margin priority, minutes/hours/global
interval fallback and input/global immutability. Original repricer code is unchanged.
These pin actual legacy behavior, not approved financial rules or new API defaults.

One synthetic precision boundary is explicit: `2**53+1` passes direct pMin exactly,
but the existing COGS path crosses float and produces one kopeck less. This must not
be hidden by future integer storage/codec exactness claims. No business formula or
checksum is adjusted to repair it in a state-migration slice; no real data involved.

Fresh focused **22PASS0.57s,80deselected**, naturalexit0; no claim the entire102case
file ran at this revision. Compileall/diff0. Ruff exit1 same3 inherited diagnostics
as pre-change HEAD (I001,C408,UP017), no new diagnostics. Fixture blocks network,
approval/apply/storage entry points and restores monkeypatched globals. No PG,
browser/build/fullsuite gate or heavy-slot reservation. Test-only rollback removes
the new cases; no runtime/schema/data rollback needed.
Final single-module unit run **102PASS0.62s**, naturalexit0 (old80+new22 actually
run together). Read-only critic found no defects, did not start test processes.

### RNP absent-advertising response contract (independent stage 8)

Fresh admitted three GET-only historical probes: **3FAIL2.53s**, natural exit1;
interval released, no cleanup or provider call. PNL viewer has no source fixture;
BFF RNP correctly returns waiting-source shape; v1 RNP exposes a distinct runtime
defect: missing ads cache returns `blocked` without the WB-02 blocker required by
the existing response validator. SQLite thread-close diagnostic also appeared in
the legacy HTTP harness; this is not a PostgreSQL persistence test.

Narrow runtime fix preserves WB_ADS_CACHE_EMPTY and adds WB-02, both on fresh
empty sources and legacy cached blocked/unknown responses. No validator weakening,
formula change, source activation or inferred financial approval. Regression actual
RED1FAIL0.68s → GREEN1PASS0.62s; additional cached blocked/unknown cases actual
RED2FAIL1PASS0.72s → **3PASS0.62s**, natural exit0. Cached blocker list is not mutated.
Command: isolated env-i fake/in-memory SQLite, sandbox-exec/offline plugin,
`pytest -q -p tests.repricer_offline_plugin tests/test_rnp_missing_ads_contract.py
--tb=short`. Focused compileall, test Ruff and diff-check exit0. Three historical
positive HTTP cases remain open, not counted as repaired by this regression.

Subsequent separately admitted same-three GET gate with explicit synthetic daily
and period baskets/ads observations and existing PNL fixture: **3PASS1.86s**, exit0,
two inherited deprecation warnings. Actual cache parsers, readiness and HTTP login
remain exercised; only BFF rule decoration is isolated for its shape-only contract.
Original numeric/permission assertions retained. Ads lineage assertion now names
actual `wb-ads-cache` / `repricer ads period cache`, not a live fullstats call.
New fixture compile/Ruff and diff-check0; read-only critic found no defects.
These three historical nodes are repaired, not the whole report suite. Synthetic
fixtures are not tenant isolation, financial policy or durable persistence proof.
Runtime blocker fix rollback is its isolated commit; this subsequent test-only
fixture slice requires no schema/data/runtime rollback. No shared wiring changed.
