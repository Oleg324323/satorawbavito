# T4 — requirement → commit → evidence → remaining work

## Current authoritative status — all eight stages

Frozen platform-milestone input: **77abab0ae4a5bef1544bccfbeb92e728d8b7c4e6**.
It contains consumer27c227e and HTTP835a82b; frontend acceptance uses the same SHA.
Later documentation/parity work does not silently replace this integration input.
`released` below means a local owner handoff, never deployment. `consumer-accepted`
means the stated local check only. No row is final-integrated or production-active.

| Requirement | Actual implementation SHA / state | Verified evidence | Remaining local work / owner | Production activation |
|---|---|---|---|---|
| 1. Typed ABC/P&L cutover | 7cd1fd9/028168b/81ee4b9; implemented, local consumer-accepted, not final-integrated | Typed pagination/account/formula/null guards; full frontend at77abab0:430/0/0 | T1/root shared integration and exact freshness contract; T4 keeps default-off rollback and no invented profit | Not authorized; separate approved canary/deploy |
| 2. Review Facts + same-DTO parity | 27c227e immutable0068 consumer;835a82b canonical-only HTTP;c87af78 parity. Later single-fact read is implemented locally, separate from frozen77; exact commit in Git | Binding44PASS; earlier225/2 retained. Parity4PASS/combined61PASS. New read14+HTTP/binding/shadow final71PASS18.28s; diagnostic BIGINT fixture failures disclosed in HTTP handoff. No consolidated platform milestone result yet | T1 shared registration; T4 list/queue, remaining workflow services and frontend; root milestone review. Local explicit retry proven, automatic failed-run/ambiguous-COMMIT recovery not implemented. Platform0068 is released, not missing | Off; original legacy shadow activation is not implemented or authorized |
| 3. Policies/drafts/decisions | Actual0071 local repository/live-session service/dormant HTTP/fake-manual preparation implemented; unchanged T1 chain consumed through4f196cd. Source SHA in local-services-send-in-app handoff commit | Combined92PASS2warnings7.92s after Numeric-bind fix; original91/1 and diagnostic disclosed. Receipt/source/epoch/CAS/races/rollback and revocation covered; final DB-clock rerun in handoff | T1/root registration and independent integration; T4 full list/history/moderation/prompt parity, durable pre-publication orchestration and process-restart acceptance. Local v1 is not full legacy feature parity | Off; no real LLM/send required or authorized |
| 4. Send/recovery | Actual0074 participant + T1 roots297fcfd→b6b6343 and cc820→43fa81d; source/policy/approval callbacks, closing, queued-only one-POST driver, original-sender cancellation, captured fresh-read orchestration and read-only ready scanner implemented UNVERIFIED | Earlier287purePASS covers codecs only, NOT new repositories/authority/services/drivers/scanner; no new gate | Concrete synthetic transport acceptance, uncertain commit/restart and fair queue scheduling; trusted bootstrap/transport and UI boundary; broad authority/0074 DDL and sealed intent/cancel no longer missing | Live sends off; no provider bootstrap or final acceptance |
| 5. Notifications | Actual0074 participant + atomic producers + own-recipient live-session root + dormant exact-visible-ID HTTP factory/typed frontend client implemented UNVERIFIED; account allowlist empty by default | Earlier287purePASS covers codecs only; new HTTP/client/service/RLS/concurrent merge acceptance PENDING | Discovery pagination/filter/binding policy, preferences CAS extension, bootstrap/registration/full UI parity and external delivery policy remain; explicit-ID reads/marks are no longer missing | No Telegram/provider actions; old0071 writer unchanged/no replay backfill |
| 6. Other canonical frontend consumers |77abab0 existing fixes +430/0/0; dormant local Reviews adapter and new Avito browser-evidence producer validator, no UI/extension switch | New browser25+Reviews10=35PASS0.42s/TypeScript0; actual frontend→T3 decoder2PASS/checksum parity. No full frontend/new UI parity implied | T2 repricer/finance/sources and T3 Orders routes/authority; T4 switches. Shared Orders filter semantics absent. Legacy browser producer lacks stable occurrence; new validator does not fabricate it. Production deferred | Default-off; no operational authority inferred |
| 7. Rollout/retirement preparation |e4818e3 runbook; preparation implemented, execution not performed | Null/version/pagination/rollback criteria and build-time redeploy limitation documented | Root/T1 integration artifact and authorized observation inputs; T4 maintain runbook against accepted contracts. Legacy retained until actual parity | Canary/deploy/retirement explicitly not authorized by local test work |
| 8. Assigned debt + final handoff |77abab0; frontend debt consumer-accepted, architecture final handoff incomplete |Actual full430PASS0FAIL0PENDING/133.91s; T2 independent102PASS0.70s; not full backend green | Root/T1 one consolidated integration milestone, then remaining vertical services and final combined/backend gates; independent final review remains | No release approval inferred from test counts |

Local Reviews amendment and exact parity limits are recorded in
`2026-09-09-t4-canonical-review-http-handoff.md`. Tests cannot close a row whose
required service chain or integration is absent. The table above is the only
current stage-status source in this file.

### Final available-source checkpoint after b557de1 (not acceptance/release)

Coordinator accepted b557de1 as completion of the CURRENTLY AVAILABLE T4 source
implementation, not full original scope or release readiness. No additional
speculative feature/policy work is authorized by that checkpoint. Exact remaining
mapping of the original eight stages:

| Original stage | Actual source checkpoints | Pending checks | Concrete missing input / next owner |
|---|---|---|---|
| 1 ABC/P&L | 7cd1fd9/028168b/81ee4b9, integrated frontend milestone77abab0 | Final integrated backend/frontend freshness/null/pagination parity and approved canary | T1/root accepted freshness + integration artifact; final financial policy is not T4's choice |
| 2 Review facts/shadow |27c227e/835a82b/c87af78 and later single-read source recorded above | Integrated RLS/rebind/revocation/partial source and restart acceptance | T1/root registration/rollout; queue/discovery/filter contract, not guessed completeness |
| 3 Policies/drafts/decisions/history |ae7db1d local services, dc6a72d workflow history + typed client, a13a6f4 atomic new-draft producer | New history15+adjacent17 and frontend transport cases were authored but not run; integrated process restart/CAS and legacy parity | Full moderation/prompt/model provenance and list/filter parity mapping, durable preparation orchestration; old legacy preserved |
| 4 Send/recovery |dc6a72d repository, a13a6f4 guarded services, f2eed7b cancel/fresh read/queue; actual T1 prerequisites b6b6343/43fa81d | Actual0074 + authority + service combined PG; cancellation races, final fences, fake transport unknown COMMIT/restart/late ACK; queue login/root/duplication | Confirmed WB/Avito read/write/answer identity/status semantics and fixtures; trusted transport/bootstrap/fair scheduling and explicit lease/budget policies |
| 5 Notifications |dc6a72d persistence, a13a6f4 producers, f2eed7b recipient root, b557de1 exact-ID HTTP/typed client | Actual RLS/current recipient/commit + concurrent read/dismiss; HTTP wire/strict input/stream budgets; frontend epochs/supersession/unknown outcome | Discovery sort/cursor/filter/count/retention/rebinding policy, deployment budgets, preferences CAS and external destination/delivery policy; root registration/UI integration |
| 6 Other frontend |77abab0 legacy regression milestone; dc6a72d local Reviews client;8d29886 new Avito producer boundary; b557de1 notifications client | Fresh combined frontend and backend DTO fixtures against final integration SHA | Actual T2/T3 service/HTTP/capability contracts. T3 reported8735915 adds no new frontend contract. Production assembly/printing remain deferred |
| 7 Rollout/legacy retirement |e4818e3 runbook + current dormant modules | Authorized canary, observed rollback/restore and actual full parity evidence | Root release artifact/allowlist approval and deployment inputs; no retirement now |
| 8 Final debt/integration |Current T4 branch through b557de1 + this bounded hygiene/ledger follow-up | Central serialized integration/PG/fullsuite/build and independent review, without CodeRabbit | Coordinator resource slot and accepted integration SHA; baseline backend debt is not proven resolved by T4 static checks |

Actual light verification after available-source completion: frontend `tsc -b
--pretty false` natural exit0. Initial Ruff5-file check found26 diagnostics,
including import/UTC style, FastAPI dependency defaults and intentional outer
safety catches. Scoped follow-up uses Annotated dependencies, automatic import/
UTC fixes, and per-boundary documented BLE001 exceptions to preserve safe unknown
commit/transport cleanup semantics. Final same5-file Ruff: All checks passed,
exit0; git diff --check: exit0/no output. This is NOT test/PG/API/transport acceptance. No heavy test,
fullsuite, build, role creation or resource reservation was started by T4 here.

Provider gap is precisely a gap in the accepted integration inputs examined, NOT
a claim that official APIs lack functionality. Inspected local sources were
Avito review create/list/answer DTOs, WB single-page canonical fetch, and legacy WB
request_send. No official provider docs or network were consulted in this phase.
Final focused provider review must establish identity/status/read/write semantics
against authoritative contract and fixtures before activation; submitted text must
never be relabelled as observed answer evidence.

### In-progress implementation package after8d29886 — verification pending

Local immutable workflow history now has backend service/dormant HTTP, bounded
version pagination and a typed frontend consumer. The same package adds a dormant
HTTP client for local context/history/commands with epoch/supersession guards and
explicit exact-byte command retries; no UI switch or route registration. This
narrows rows3/6, but does not close full queue, policy history or legacy parity.
See `2026-09-09-t4-review-workflow-history.md` for exact files and boundaries.
History backend15+adjacent17 and new frontend transport tests/fresh TypeScript
are PENDING. Earlier45frontendPASS/TypeScript0 cover pre-transport state only.
Per coordinator, implementation is assembled first; no micro-gates/PG reservations
are active. The combined verification queue remains a release prerequisite.

### Explicitly deferred by user — Production assembly and printing

Shared WB/Avito work items/assignments, batches, schedule, picking/print sheets,
XLSX/PDF/stickers, print archive and delivery receipts are deferred, not DONE.
T4 retains the existing UI and completed compatibility protections at frozen
77abab0; its new canonical Production adapter/switch is not implemented or
consumer-accepted. T1's implemented0069 atd57c544 and all existing owner code/
migration history must remain; this statement does not assert T3 P1 service
acceptance. Exact backend readiness remains in the T1/T3 current ledgers.
Resumption needs accepted backend service/HTTP/authority contracts and the
missing source/parity inputs recorded by T3. Do not continue new implementation,
P1–P4 tests or missing-renderer/calendar research while deferred. Preserve existing
behavior and required regression compatibility for already accepted changes.
This deferral does not block Orders ingestion/read/API, Reviews, Notifications,
T2 work or integration/acceptance of the remaining scope. KIZ/matcher stay excluded.

# Historical ledger — all following checkpoints are superseded snapshots

The headings and tables below retain their original dates/claims as evidence;
they are not current dependency or completion status. Use only the table above
for planning. In particular, historical missing0068/guard and frontend failures
must not be repeated as current blockers.

## Current complete frontend gate: 430PASS / 0FAIL / 0PENDING

One fresh serial fullfrontend run completed naturally0 in133.91s, JSONsuccess
true. This supersedes old405/4 and all intervening scoped-only debt checkpoints.
No skips/test-timeout expansion. Report:
`frontend/docs/superpowers/reports/2026-09-09-t4-full-frontend-acceptance.md`.
Includes22e3e30/322aa89/cfe4104/b32ec42 and whitespace-only final test indentation.
T4 independently reran T2f3c61d7 calculation characterization102PASS0.70s under
OSnetworkdenial; no financial-policy approval or T2 writes. Heavy interval
explicitly released to owners, T1 permissions retry next. Remaining T4 local
storage/service and other canonical frontend activation paths still require
exact DDL/HTTP/authority contracts, not a speculative API. No backendfullgreen,
productioncanary, release or completearchitecture claim; coordinator finalreview
remains. Historical checkpoints below retain their original limits.

## Latest: remaining generic-filter/Digest cases pass in scoped gate

Test-only reconciliation: explicit twelve-row DOM fixture uses real filtering,
waits for delayed listener binding and supplies the P&L operational source tag.
Retired local Digest controls are not recreated; existing renderer scenarios
retain point/tooltip/overflow coverage. Final two cases2PASS12.91s, pure
aggregation5+stock25=30PASS0.72s, TypeScript0/diff0. Partial-bucket mutation caught.
Two diagnostic fixture failures are disclosed in
`2026-09-09-t4-filter-digest-test-reconciliation.md`. No runtime changes or
canonical policy claims; fresh full frontend suite remains required.

## Latest acceptance: stabilized report case now passes

The previously failing stabilized legacy report case passes1/31.21s after
obsolete Ads label reconciliation and replacing the retired RNP demo-history
action with no-demo-row assertions. Actual React RNP source explanation in
the Comment column is covered by populated cache test; all three cache/session
scenarios pass3/6.92s. Ads+historical marking compatibility2/4.57s; TypeScript0.
No runtime changes in this batch. Earlier45s timeout narrowed to exact5s
missing-locator failure before correction; no test timeout widened. Remaining
generic-filter/Digest broad debt stays open; whole-suite not rerun/inferred.
See `2026-09-09-t4-legacy-report-case-reconciliation.md` for scope and evidence.

## Latest: active Ads search characterized, unused-path hypothesis rejected

Actual React Ads search by supplied SKU/campaign: 1PASS3.48s, TypeScript0,
all traffic intercepted and browser cleanup complete. Test-only addition;
runtime/source snapshot unchanged. Prototype-selector hypothesis was disproved
by mounted React inspection and deliberately disabled old demo renderer; all
speculative helper edits removed. Old giant test still fails, no full-suite
green inferred. See frontend report `2026-09-09-t4-ads-search-verification.md`.
Explicit heavy-slot release sent to T1/T2/T3. T2 continues uncovered legacy
calculation characterization; T3 continues independent Orders/Production scope.
Neither requires waiting for T1 as a whole; exact missing DDL/authority contracts
still gate their respective integration slices.

## Current checkpoint: Review0068 consumer scoped acceptance; frontend debt continues

Review consumer final focused44PASS11.78s, including six actual account-lock race
cases, first-fact UPDATE rollback witness, corrected lossless JSON fixture and
publication adapter. Broader227case actual225PASS2FAIL62.94s retained: both failures
were race test assuming platform permits empty credentialRef; existing platform
rejects it, new tests assert that early denial and add valid nonempty ref. No
runtime weakening, all owned cleanup/absence verified. Ruff/compile/diff0;
final review main-agent critical pass (not independent finalapproval after agent
usage limit); root integration review remains. Full report
`2026-09-09-t4-review-binding-consumer.md` states remaining activation boundaries.

Frontend Week actual segment-search RED→GREEN, P&L null/zero/suppliedtax proof,
Stock adjacent browser combined3PASS7.19s, tsc0/hashbytesmatch. Old broadcase passes
P&L after obsolete tax-base label correction; next failure Ads “Все SKU” versus
actual “Все товары”, later assertions still not executed. No fresh fullsuite or
new build claim. Explicit T4 heavy release delivered; T3 next.

## Prior current checkpoint — historical evidence

Independent Digest hidden-empty1RED→GREEN and Stock exact7/60 numeric3RED→GREEN
completed. Main combined28PASS5.90s/natural0/browsercleanup, tsc0/diff0,
independent critic scopedPASS. Details and limits: frontend report
`2026-09-09-t4-digest-stock-boundaries.md`. No new full-suite/build claim.

T1 exact prerequisites imported unchanged locally: dbf8d31→117dbc6,
d87e575→0439652, mandatoryc4e3e373→f330020,4d95b24→b8b7128.
T4 actual head0068/runtimegrants migrated successfully in ownfixture. Original
two behavioral REDs reproduced6.23s/natural1: unstamped reservation and oldcabinet
read. Consumer runtime now stamps INSERT, validates exact owner reference and
contributing run provenance including history/replay, maps safe conflict. Main
targeted18newcases+facts/lossless/shadow:80PASS1FAIL12.53s/natural1, exact5DB5role
cleanup. Failure was existing duplicate-JSON fixture remaining unbound, masking
its intended JSON-invalid assertion; fixture now stamps at INSERT, rerun pending.
Pure metadata/storage/descriptor137PASS0.34s, Ruff0. New account-lock-race tests
prepared, not yet executed; final PostgreSQL/critic acceptance and activation
remain open. No fullgreen inference or consumer commit yet.

## Previous frontend checkpoint — historical evidence

T1 Review0068 is now READY exact4d95b24b549f0b1f773095ec583a85f18172fdbc,
superseding earlier not-ready entries below. Main read complete handoff/migration/
three new test modules and grant diff; fixture Git blob matches45e2ccb. T1 reports
actual131PASS with naturalcleanup/independentacceptance. T4 pure storage adapter
04a5f66 adds exact normalized/bytes/hash validation, explicitallNULLlegacy and safe
errors:208purePASS0.22s/networkdenied/Ruffdiff0/criticPASS. No schema integration or
repository acceptance inferred; current/historical publicread still blocked.

Current continuation:

- Final frontend evidence before the last test-only readiness patch:
  full405PASS4FAIL0pending, four earlier sourcefailure
  IDs closed; three old broadsourcecases remain plus reportLoading logout test
  readinessrace. Exact session assertion now polls, separatefocused4PASS0FAIL.
  No inferred406/3/fullgreen claim. FinalruntimeTypeScript/Vite0, Stock15focused
  PASS, Orders3PASS/Listings2PASS/overview2PASS, purelegacyownership6PASS;
  critics scopedPASS.
  Slots were individually admitted/serialized and explicitly released to all owners.
  No active ownheavy gate. Digesthiddenempty and Stock exact7/60 boundaryparity
  remain identified independent follow-ups; manager/Week/aggregation rules not
  invented. Reports retain intermediate RED/timeouts and exact limits.
- RNP actual cache/session proof committed37ac6c9: three new scenarios pass in
  main serial run; only obsolete explanatory-copy assertion removed.
- Listings actual stale rows/seven KPI/open detail: main2RED→2GREEN; load clears
  prior response/selection, abort guard retained. Final TypeScript/Vitebuild0
  includes Orders and Stock runtime changes. Independent critic scopedPASS.
- Broad first376/9 includes5additional timeout IDs; serial380/5 resolves those,
  leaves4old IDs plus actual overview error→logout readiness race. Exact seven
  KPI assertion now waits for state transition; focused and final broad both pass.
- Orders actual browserRED: duplicate legacy Orders/returns reads and unexpected
  production-skus read alongside React; all intercepted. Narrow Avito ownership
  guard implemented, pure actual-function test5PASS1FAIL→6PASS; standalone/WB
  current/archive paths preserved with stubs. Generated snapshot refreshed.
- Orders HTTP503 hidden by onboarding: reuse existing visible error/loading UI;
  empty-row onboarding policy preserved. Actual3browserPASS and build0.
- Stock metadata/label/unknown-as999 defects reproduced and fixed; actualfilter
  browser plus14purecases15PASS. Zero-stock/null-days criticfinding also closed.
  Missing manager/promotion/source fields are not invented. Exact7/60boundary
  alignment and hidden Digest empty message remain separate follow-ups.
- T4 independently reran T3 ingestion/read-contract/XLSX group88PASS under denied
  networking. T2 and T3 remaining dependency matrices read; no speculative storage
  or operational activation to substitute for missing contracts/sources.

Root temporary heavy-resource queue honored: all own existing gates naturalexit
and browser cleanup; every owner directly notified slot free. T1 subsequently
completed/released P1RED; T4 admitted focused4case ended naturally and released.
Every next heavy gate still requires coordinated admission. Pure VM checks use
no real network, DOM renderer/export, browser or DB.
Detailed evidence:frontend/docs/superpowers/reports/
2026-09-09-t4-rnp-listings-state-verification.md.

## Earlier checkpoints — historical evidence, superseded by the current section

Previous independent frontend batch:373PASS/7FAIL/0pending versus358/16, exact
nine old failed IDs closed, zero new failures; finaltsc/diff0. Actual overview
period/modal/loading/empty/error/logout, protected secondary DOM node identity
and exception restoration, Ads/Stock event scopes with intended mutation REDs,
legacy Reviews explicit two-row synthetic fixture preserving all guards/settings
and initial empty-runtime proof. Equivalent threshold-copy and valid negative
strategy input tests corrected without runtime/formula changes. Full report:
frontend/docs/superpowers/reports/2026-09-09-t4-independent-frontend-batch.md.
Separate RNP cache test in progress, not part of the above evidence.
User explicitly requested all available T2–T4 work: existing T2/T3 resumed,
both delivered further independent commits and were asked to continue remaining
safe owner scope. T4 independently verified T2 receipt/override/assignment228PASS
and buyer-price DST38PASS under all-network-denied sandbox. T3 reports0067consumer
33c95c1 ready/HWMv3; adapter keeps HWMopaque; no frontend/APIactivation inferred.

T1 direct dependency ACK: committed spec+implementation plan at
17028f5667599c0ef2ca2f2536d3334426354afd fully read via git show. T1 explicitly
consumed T4 codec45e2ccb and six literal vectors. No important domain mismatch;
four native SQL-positive/two NUL-negative cases, immutable-from-INSERT, exact
bytes/hash, narrow grants and unchanged orgRLS are specified. Candidate0068
remains conditional/unreserved; DDL NOT READY. No schema copy, registration,
activation or service acceptance inferred. Direct ACK sent to T1. Actual
repository reserve/replay/current+ambiguity/completed-rebind gates remain open.

Latest Avito stats test-only checkpoint: actual React date inputs keep draft
separate from applied period, Apply performs exact secondGET, logout no extraGET.
Unique in-memory disabled-Apply mutation fails at secondGET assertion as intended.
Replaced only obsolete stats date DOM-ID source assertions. Full358PASS/16FAIL/
0pending versus356/17: one old failure closed, zero new failed IDs. Finaltsc/diff0,
independent critic no important defects. Empty-response proof, not populated
KPI clearing/refresh/multi-account/backend parity. Report under frontend/docs/
superpowers/reports/2026-09-09-t4-avito-stats-period-test.md. No runtime changes.

Latest test-only Stock/Week checkpoint: actual React page proves four scoped
loading→empty/error→session-expired cases with one intercepted GET, no mock rows
or additional API calls. Replaced only two obsolete progress-component source
bans. Focused9PASS; full356PASS/17FAIL/0pending versus350/19, exact two source
failures closed and zero new failed IDs. Initial 1s readiness timeout reproduced
under load, corrected to bounded15s; final focused and broad runs pass new cases.
TypeScript/diff0; independent critic found no important defects. Runtime, HTML,
schema and flags unchanged. Report: frontend/docs/superpowers/reports/
2026-09-09-t4-stock-week-state-tests.md. Remaining17failures unwaived.
T1 candidate Review-run-binding design fully compared against1a3d344/45e2ccb;
domain direction accepted directly to T1, not DDL readiness or activation.

T1-requested Review binding purecodec READY: historical_binding.py frozenexact
descriptor +sortedASCIIJSONencoder +mandatorychecksum strictdecoder,6literal
Unicode/control/null-emptyvectors matched byindependentNodeencoder/hash andcritic.
61focusedPASS/283adjacentpurePASS0.48s/no skips, networkdenied, Ruff/compile/diff0.
Report2026-09-09-t4-review-binding-codec-handoff.md. ExactreadySHA sentdirectlyT1
forSQLparity, notrootper-slice. NoSQL/servicewiring; pureNULacceptance is notVARCHAR
admission; immutablehistory andpublicread/operationalactivation gates remainOPEN.

Latest test-only strategy-scope follow-up to c114a18: obsolete dependencyliteral
replaced byactualReactinactivepage APIassertion, precisein-memorymutation detects
unexpected GET/strategies/catalog. Finalfull350PASS/19FAIL/0pending versus349/20,
oneperformance sourcecaseclosed/nonewIDs; TypeScript/diff0, independentcriticPASS.
No runtimechanges; doesnotproveallproductrouteperformance. Localreport same
2026-09-09-t4-repricer-stats-route-test.md, remaining19failuresunwaived.

Latest frontend test-only checkpoint: actual VellaHtmlParityPage route/effects/
generatedHTML browser test replaces2obsolete source-string assertions, retaining
all otherguards. RealinitialGET/scopedperiodGET/authnullrow+KPIclearing verified.
In-memory route/eventguard mutations bothfail for intendedbehavior. Finalfull
349PASS/20FAIL/0pending versus345/21: one sourcecase closed, nonewfailedIDs.
Includes final Orderswhitespacefix tests. Finaltsc/diff0, independentcriticPASS;
no runtime/sourceHTML/generated changes. Reportfrontend/docs/superpowers/reports/
2026-09-09-t4-repricer-stats-route-test.md. Twentyfailures remainunwaived.

Dormant Orders wire preparation868728e:
canonicalOrders.ts strict typeddecoder and URLbuilder, nofetch/UIactivation/print
inference. T3da7e017 wire unchanged throughab272ee, directlyconfirmed. ActualT3
Pydantic JSON golden matches; independentcritic found2P2s, actual4RED fixed precise
microseconds/WBmandatoryevidence. Additional Python/JSwhitespace mismatch2RED fixed
without identitynormalization, critic independentlymatched Python Unicodewhitespace.
Finalfocused115PASS(59Orders+56ABC), broadpre-whitespacefix345PASS/21samefailedIDs/0pending
versus288/21. TypeScript/build passed pre-finaltextboundary; finaltsc exits0 as well.
No activatedclient or queue; sharedregistration/historybinding/queryregistry/rollout
and Productioncapabilities remain gates. Fullreport frontend/docs/superpowers/reports/
2026-09-09-t4-orders-wire-adapter.md. No root per-slice message requested.

Frontend311458a prevents stale Repricer first/page2 success/error from changing
newer results after reload/disposal. Follow-up5256f77 clears stale KPI/deltas/filter
summary during loading/error/logout. Actual browser RED→GREEN, final6focusedPASS;
full288PASS/21samefailedIDs/0pending (previous282/21 before both slices), TypeScript
and Vitebuild0, independentcriticPASS. Reports2026-09-09-t4-repricer-stats-response-isolation.md
and2026-09-09-t4-repricer-stats-aggregate-clearing.md are under frontend/docs/superpowers/reports.
Synthetic actualbridge tests, not wholeReact/auth lifecycle or production proof.
Root requested local-matrix checkpoints only, no per-slice integration/messages.

T3 accepted dormant read wire866d1a1 + bigint-string correctionda7e017:
GET/api/v2/orders, snapshot_id and row_version decimal strings. T4 independently
ran10pure wiretests. Typed frontend preparation may proceed without activation;
public integration remains dependent on approved immutable sourcebinding service
and single-owner rollout. No invented filtering/capability/print semantics.

Historical binding request/witness1a3d344 supersedes any broader read readiness:
actual1PGcase3.08s demonstrates oldbody after completedaccountrebind under freshguard.
This is an OPEN defect, not safe-read PASS. T1 receives perrunimmutable binding and
unbound/mixed/currenthead rules; publicreads and operationalactivation blocked until
accepted persistence+consumer verification. SharedHTTP errors are error.code in
app/main.py, not standalone detail.code; T1 owns real registration/envelope tests.

Latest bounded result: canonical-only HTTP router/service/raw WB adapter ready for
T1 default-off registration, NOT activation. Coordinator superseded unsafe legacy
dualconsumer for this path: no legacy write/read/cache use. Actual signed auth,
pairedcredential/scope/revoke/replay/rollback/outage and rawlossless source acceptance;
final900PASS2warnings49.16s/no skips, scopedRuffcompile/diff0, independentcriticPASS.
See2026-09-09-t4-canonical-review-http-handoff.md for exact wire/errors/limits.
Public canonical reads, dualconsumer legacy isolation and later stages stay open.

Selector434ff5f accepted/cherry-picked9981e72,94PASS2warnings2.81s/newRuffcompile/diff0.
All flag defaults remain off. New actual wiring gate: legacy feedback storage and
reads have no org/account owner; adding arbitrary account DTO dualwrites is unsafe.
Coordinator received exact source evidence and bounded compatibility/scoping decision
request. No HTTP edit; older pending-selector statements below are superseded.

Fixture follow-up supersedes the blocker immediately below: acceptedT1 34b5f62
cherry-picked79f83b9; actual mixed collection with Review ORM and all original files
plus fixture regressions passes349tests27.69s and349tests31.46s in reverse order,
no skips/naturalexit0.20setup errors closed for this gate, not a full backend claim.
No unaccepted approvals ancestry imported; Review selector remains pending/off.

Physical guard8338ef7 now accepted from T1 and merged2dc09de; static integration
criticPASS. Expanded334-test acceptance is blocked by20credential SQLite setup
errors (314passed): global metadata includes Review JSONB after collection. Minimal
same-test alonePASS/importedReviewERROR reproduced and sent to T1 fixture owner.
Not waived; full mixed run must be repeated after fix. Review selector still pending.

Latest bounded acceptance: 19 new actual PostgreSQL shadow cases; combined641PASS
48.76s, no skips, Ruff/compile/diff0, independent scoped criticPASS. Details in
2026-09-09-t4-review-shadow-composition.md. Same received WB DTO composition, paired
credential authority, committed reservation and both real session-revocation lock
orders are now covered. Actual manual HTTP wiring, T1 physical guard follow-up and
Review-specific default-off selector remain gates; scheduler and Avito stay unwired.
This supersedes the older statements below that composition/race tests are missing,
not the broader stage-completion requirements.

All eight original stages remain in scope. A pure contract, fixture or dormant
repository is not an activated service. No production/deploy/push/live provider,
send, print/export action or rollout activation in this continuation.

| Stage | Delivered evidence | Still required before stage completion |
|---|---|---|
| 1. ABC/P&L | 7cd1fd9/028168b/81ee4b9 typed default-off adapter; 8d68560 actual SSR auth regression. Original focused75 and build/typecheck passed; later focused auth65. Null/formula semantics unchanged. | Backend freshness policy and separately authorized hosted canary/E2E; no release approval. |
| 2. Review Facts/shadow | Exact T1 56b5fa5/0063 integrated64f256f, fixture/style followups825326d/5f1f539; eaf15c6 dormant ORM/repository. Fresh314PASS (35 repository +48 schema +231 adjacent), real disposable PostgreSQL/runtime role, cleanup verified. Account lock/RC, replay manifest, CAS, immutable source history, late-run ambiguity. | Real shared publication/auth guard, same-fetch shadow service hook, actual account revocation/publication race and restart integration; public read API. Repository is not authorization and no route is wired. |
| 3. Policies/drafts/approval | 98d1331 pure decisions; 8f4a6c4/b17638f exact head/audit proposals; 4b349ec/92ba34c serializers; 5d4ac9a golden vectors; 316eaf8 local audit encoder/golden,157 combinedPASS incl51 audit,58 serializer,40 decision,8 golden tests. Independent critic PASS after impossible head transition fix. | T1 accepted local DDL, durable repositories/head CAS, fake generation service, authenticated actions, atomic audit and database races/restart. Encoding alone is not durable audit. |
| 4. Send/recovery | e4818e3 recovery, b17638f exact attempt/lease/result matrix, canonical send payload serializer. Existing35 recovery +8 provider lifecycle cases included in adjacent checks. | Durable command/idempotency/attempt/marker/evidence/audit/enqueue repositories, real credential and worker authority, fake-provider crash/restart integration. Live send remains prohibited. |
| 5. Notifications | 98d1331 safe projection; 9284fe0 read-only existing preferences;31 projection/21 SQLite tests (not RLS proof). | Event/receipt persistence, visible-ID mark-all and fresh actor checks, transactional producer/outbox, cache isolation and delivery storage. External delivery also needs verified destination/receipt/policy contract. |
| 6. Other frontend consumers | Frontend ownership retained; approved ABC only. Safe marking cleanup1d0700d/6171d72/02cbd9a/0ff9941/8d4a5b9 preserves historical status/problem filter/barcode fallback and core WB/Avito matching; shared snapshot regenerated by existing pipeline. | Approved authenticated T2/T3 HTTP/capability contracts then adapters/state/race/409 UI and single-writer cutover. Do not invent endpoints from repository proposals. Production parity/legacy owner retirement not established. |
| 7. Rollout/retirement | e4818e3 canary/rollback runbook; default-off, build-time redeploy requirement and separate write-action authority retained. | Approved budgets/window/artifact and explicit canary authority; legacy retirement only after actual parity and writer rollback. |
| 8. Debt/final handoff | Actual-rendering/browser fixes;5d92b78 expense-help,0aec110 runtime-source guard with RED/GREEN and mutation. Fresh full frontend279PASS/24FAIL/0pending versus278/25: zero new IDs, one resolved. Typecheck0, full npm build0 (large-chunk/plugin-time warnings); generator hash unchanged, timestamp-only incidental diff restored. Critic PASS. | Remaining24 failures, integration/full backend baseline, complete ready-set review; current checkpoint is not final architecture handoff. |

Latest verification superseding the counts in the stage rows above:

- Reviews268ea1d: valid Unicode/NUL identities preserved, invalid surrogate keys
  rejected with typed safe errors before SQL on write and read. Fresh405 combined
  backend tests pass (real disposable PostgreSQL plus adjacent contracts), no skips;
  scoped Ruff/compile/diff0. Local audit remains an encoder, not durable storage.
- Frontend4fd9156 actual ABC bridge empty/error/auth behavior, ff7038a Avito schedule
  cancel browser case,9099f32 RNP period/session scoped runtime fix with actual RED.
  Full282PASS/21FAIL/0pending, zero new failure IDs versus282/22. TypeScript and Vite
  build exit0; existing chunk/plugin warnings. All21 failures remain unwaived.
- T1 explicitly confirmed reading exact5d4ac9a,316eaf8,a836560,268ea1d. The unified
  eight-pair lossless design405033a and implementation plan1d8d493 were read fully;
  actual0065 migration and T4 decoder/repository acceptance are not yet available.
- T1 fetch-only authority7bfb631 handoff read: paired credential/binding is not
  publication authorization. Full shared guard remains pending; neither this API
  nor the proposed0065 are claimed installed in this worktree.

The earlier pending-guard statement is superseded: exact T1 guard4860c53 and paired
fetch7bfb631 lineage are now merged83ef4a4. T4 root-transaction compatibility30f1f15
passes253 tests (new9+repository35+schema48+sharedguard161), Ruff/compile/diff0 and
independent review. Repository uses explicit command_savepoints=False under guard;
caller must roll back the full root on every error. No current service is wired.
This closes guard/repository compatibility, not authenticated same-fetch shadow
composition, audit or a Review-specific two-session revocation acceptance gate.
Actual0065c68cd31 subsequently controller-approved and merged74bde41. Decoder-first
dab11ec and writer0ce0517 close lossless representation acceptance: fresh622PASS38.86s
(461Review/adjacent+161guard), no skips, Ruff/compile/diff0, independent reviewPASS.
All8pairs preserve NUL/Unicode/null-empty and semantic hashes; mixed legacy identity
is not rewritten; actual guard publication/rollback covers NUL keys. DeepJSON
RecursionError safe handling was reproduced and fixed during decoder review.
Rollback requires decoder-capable dab11ec+expanded0065, never oldTEXT-only/downgrade.
Next same-fetch shadow composition remains separate. T1 was directly requested a
Review-specific default-off org/account policy; collector flags are not reused.
No SQL migrations or shared auth are authored by T4. Next independent task:
remaining evidence-backed frontend test debt; durable wiring waits for real guard/DDL.

Current native PostgreSQL access is no longer blocked by the historical ENOMEM:
coordinator authorized Unix-only fresh disposable tests; no existing application DB
or host service changes. eaf15c6 report contains exact commands/cleanup and limitations.
Source-text NUL mismatch is now reproduced316eaf8: normalizer accepts it,0063 TEXT
storage rejects it;1 realPG test proves safe rollback plus plain-text control, not
lossless parity. T1 notified for forward schema decision; no stripping or schema edit.

## Historical snapshot at 9284fe0 — retained as history, not current readiness

Snapshot на `9284fe0`, worktree clean. Координатор: задача
`01a082e2-4420-70a3-b4f4-2c4ad061e587`; начальная передача подтверждена tool.
Stage completion оценивается по исходному заданию T4, не числу pure modules/documents.
GitHub push/integration выполняет координатор после общего gate.

| Требование | Commit / артефакт | Проверка | Незавершённое |
|---|---|---|---|
| 1. Canonical ABC/P&L frontend | 7cd1fd9,028168b,81ee4b9 | focused75; typecheck/build0; immutable base193pass29fail →261pass29fail, exact same failure IDs | Hosted canary/browser E2E, missing backend freshness policy; no release approval |
| 2. Review Facts + shadow | 4a7d669 request, accepted T1 d38e923; existing canonical_contract.py | existing normalization38 cases в текущем combined173 | DDL, canonical_orm/repository, same-fetch shadow hook, actual PG replay/manifest/watermark/source-order/CAS/RLS/outage/restart |
| 3. Policy/draft/approval | 98d1331 pure decision, 8f4a6c4+b17638f exact payload/head/audit proposals | 40 decision tests + lifecycle subset; revoked/stale/scope/checksum predicates | Canonical serializers, immutable rows/head publication CAS, fake generation service, authenticated actions, atomic audit, PG races/restart |
| 4. Send/recovery | e4818e3 pure recovery; b17638f exact lease/result/evidence/audit | 35 recovery tests, current-read boundary/live lease/ambiguity; 8 provider lifecycle | Durable idempotency, exclusive attempt/marker, worker enqueue and credential resolution, actual provider-fake crash/restart/CAS; no live send |
| 5. Notifications | 98d1331 safe projection; 9284fe0 read-only preferences | 31 projection +21 SQLite preferences tests | Immutable events, per-member receipt/upsert/visible-ID mark-all, atomic outbox, recipient/cache isolation, delivery storage/attempts, verified destination/receipt contracts |
| 6. Other frontend consumers | Существующий ABC adapter; новые domain endpoints ещё не approved | Stage1/bd6139f проверены; другие capability cutovers не заявляются | T1 credential/token policy; T2 repricer/finance/sources HTTP schemas; T3 Orders/Production HTTP commands/read/export/receipt contracts; adapters/UI/default-off flags после parity |
| 7. Rollout/retirement | e4818e3 frontend canary-rollback-runbook | read-only critic, explicit NO-GO, build-time rollback and old tabs | Approved release budgets/window/artifacts, отдельно разрешённый canary; legacy retirement только после actual parity |
| 8. Test debt/handoff | bd6139f clock fix, все bounded handoffs | fresh frontend261/29 →264/28, exact0new1resolved, skips0; typecheck0 | Остальные28 frontend failures, integration full suite/baseline, real generator pipeline по контракту, final ready set |

Latest scoped combined T4: **173 passed, exit0**, zero external-action attempts under
Python audit hooks. 21 SQLite tests доказывают query/projection, не PostgreSQL RLS.
Полный backend suite для T4 continuation не запускался; исторические baseline failures
не автоматически утверждённый release allowlist. Frontend runtime после Stage1 не менялся.

## Exact requests координатору и владельцам

### T1 — prerequisites в порядке возможности

1. Принятый Review Facts DDL по `4a7d669`, actual revision от единственной head, scoped
   FKs/RLS/runtime privileges и committed test handoff. T4 после проверки пишет
   `app/reviews/canonical_orm.py` + repository, не competing T1 domain ORM.
2. Review local policy/draft/decision/head DDL по `8f4a6c4` и `b17638f`.
   b17638f уточняет head initialization, distinct head/draft versions и per-event audit;
   независимый critic прошёл, acceptance T1 ещё не подтверждено.
3. Send command/attempt/evidence + domain audit/enqueue atomic persistence по тем же
   matrices. No provider/production policy defaults. Shared transaction-local context,
   grants и physical outbox принадлежат T1; T4 получает готовый интерфейс/DDL.
4. Account-scoped notification events/receipts DDL можно выпускать независимо от Telegram.
   Explicit visible-ID mark-all, integer membership, composite owner FKs. Preferences
   остаются в lk_user_preferences; read-only adapter9284fe0 уже готов. Writer требует
   T1 extension existing owner's version/CAS, не новой preferences table.
5. External Telegram позже: verified versioned destination binding, scoped provider
   receipt, immutable delivery policy relation/ref. Org-wide events отдельно требуют
   platform registry. Missing policy/binding блокирует активацию, не in-app storage.
6. Disposable PostgreSQL environment: T1 report9438186 сообщает bootstrap ENOMEM.
   T4 не меняет sysctl/shared memory/чужие процессы. Координатор определяет разрешённую
   remediation/isolated runtime; новый PG запуск только после безопасной готовности.

### T2

Нужны committed approved HTTP schemas/routes/permissions/capabilities для repricer,
finance/sources: exact read pagination/snapshot/error contracts и command expected_version/
idempotency/409 semantics. Observed HEAD ee7bd6e; незакоммиченный reserve/dispatch amendment
не импортирован. Pure repository proposal не служит источником придуманных HTTP endpoints.

### T3

Нужны committed approved Orders/Production HTTP read/commands + status mapping, print/download/
delivery/backend receipt distinctions и parity evidence. Observed HEAD2f1724a. T1 currently
интегрирует Orders candidate; незакоммиченная0062 не imported и не названа ready. Legacy
serverless snapshot writer отключается только при agreed single-owner cutover/rollback.

## Готовые bounded commits и следующий шаг

Текущий ready для review набор — T4 история от c88a474 до9284fe0, включая docs; это не
разрешение общего merge и не stop/ready declaration всей задачи. После каждого следующего
slice координатор получает exact commit/tests/gaps. Независимый следующий шаг — canonical
serializers для уже описанных policy/generation/send bytes, чтобы repository повторно
использовал проверенную реализацию. Это не заменит DDL/DB/HTTP acceptance.
