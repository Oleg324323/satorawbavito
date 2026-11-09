# T3 Orders / Production: requirement tracking

Branch `codex/arch-t3-operations`; исходная база `c88a474`.
Общая очередь не активирована. Legacy writers/prototype сохранены.

## Foundation Common-Run Repair Package

The root foundation-delivery plan narrows the current milestone; the broader
historical matrix below is not a claim that all original requirements are done.
Repair base is exact common `ff6ca4a`, including the shared JSONB metadata fix.
Root's first common run at c4d1b9a naturally exited1 with5426 pass/135 fail/448 error;
no shutdown workaround, force exit, timeout increase or skip is warranted.

Seven test/harness files repair the assigned concrete causes:

- Five empty-database tests now reuse candidate.cluster/disposable_database,
  including owned Unix-socket allocation and exact cleanup, rather than bind a
  TCP port inside the accepted OS sandbox. Actual upgrade/downgrade/schema/prompt
  assertions remain; no application/live database or broader role grants.
- Six legacy HTML compatibility cases load existing TypeScript with createRequire
  anchored to the declared frontend package. Python uses existing frontend_directory
  for both source and dependency identity. No install, new renderer or browser action.
- Closed diagnostic tests assert fixed endpoint/count/status instead of removed
  URL/provider keys and per-chat identifier maps; positive business DTO assertions
  remain. The logger test temporarily restores only that logger's disabled and
  propagation state, avoiding unrelated application-entrypoint test contamination.
- Two uncertain-commit assertions use the real content-free job readback dictionary,
  rather than assuming an authenticated StoredOrdersJobView. No runtime widening.

Focused command: existing T1 Python, OS deny-all-network plus existing secret-file
denials, release_gate.safe_environment, plugin autoload disabled, explicit frontend
directory pointing to the existing integration frontend, pytest no-cache/syscapture:
test_avito_orders.py, test_avito_chats.py, test_orders_avito_adapter_characterization.py,
test_orders_production_html.py. Result: **40 PASS, 2 inherited warnings, 7.90s,
natural exit0**. No provider/DB/service access. Compileall and diff check exit0.
Ruff from backend cwd initially found three import-order issues; import-only fixes
then final six-file Ruff exit0. No broad formatting or diagnostic contract rollback.

Migration and job-publication PG reruns remain pending the root-coordinated slot;
T1 holds it. T3 has no active PG process, reservation or cleanup. Do not count the
prepared five migration/two readback cases as newly passing. JSONB-related returns
and credential API failures belong to the already imported shared fix, not another
local metadata patch. Token-store cases are exclusively T1-owned.

Two other assigned IDs are exact existing baseline entries: avito_repricer_worker
pending-approval case (baseline line6) and one_c_cash_flow PNL attachment (line8).
The former supplies empty strategy assignments; the latter invokes legacy PNL
without authoritative financial-source evidence. Neither permits inventing
automatic assignments or a finance attachment workflow. Retain as explicit
legacy/P1 failures for root disposition; no test deletion, skip or baseline edit.

The subsequently assigned Python3.11 bounded PG run naturally exited1:
**15 PASS / 1 FAIL, 17.35s**. All eleven job-publication cases, including both
uncertain-commit readbacks, and four0019 compatibility cases passed. Six owned
databases and one runtime role were verified absent by the allocator cleanup.
The full-head roundtrip failed before connecting: rendering the SQLAlchemy URL
changed the exact Unix host to percent-encoded %2Ftmp, rejected by Alembic's
existing ConfigParser interpolation. The test now passes its original allocator
URL from Config, retaining exactly the same database/socket. Shared Alembic code
and sandbox are unchanged. This one corrected roundtrip awaits a newly authorized
slot; no automatic rerun was started. Slot explicitly returned to T1/root.

After the next explicit T1/root handoff, the single corrected head-roundtrip at
source89f008c passed: **1 PASS / 6.96s / natural exit0** on approved Python3.11,
env-i and unchanged Unix-only sandbox/allocator. Actual empty upgrade0001->0079,
downgrade0079->0078 and re-upgrade0079 all passed, followed by the test's head and
prompt-column assertions. Allocated orders_test_aefbee8e0a8547fb8da749d393118d2f
was verified absent on cleanup. Prior fifteen PG passes remain separate evidence,
not a new combined16-pass claim. Slot handed directly to T2 per root queue; no
active/reserved T3 resource remains. Full integrated acceptance remains root-owned.

## Current Requirements Matrix (2026-09-09, supersedes all history below)

Implementation baseline: `35ea34e77d80920b0d8779a6b888e6f1d8e7bad0`.
`implemented` means committed code exists; `released` means its owner supplied an
exact reviewed READY commit; `consumer-accepted` requires the receiving service's
own integration gate; `final-integrated` requires the consolidated root candidate.
None of these authorizes production activation. Test counts are evidence for their
named scope, not completion percentages or closure of a whole product stage.

Latest user decision relayed by root: **Production assembly/printing is DEFERRED**,
not DONE. This includes work items/assignment, batches/calendar, frozen sheets,
XLSX/PDF/stickers, print archive and delivery receipts. Preserve committed code,
schema/history and existing functionality; no new P1-P4 implementation, renderer
recovery or feature tests. Required compatibility regressions remain allowed.
Orders ingestion/storage/read/API/source progression remain ACTIVE. Deferred
Production and excluded KIZ are not blockers for integration of the active scope.

| Requirement | Actual implementation SHA / state | Evidence | Remaining / owner |
|---|---|---|---|
| 1 Discovery, synthetic identity/parity inventory | `4323821`, `f4d6d55`; implemented | Stage1 and UI/DB parity reports; current contract/returns/XLSX/Stage1 rerun 90 PASS, exit0 | Production source gaps recorded for DEFERRED resumption, no active recovery request / root + T3 |
| 2 Initial ingestion, replay, partial manifests, immutable owner binding | `9d1f88b`, `8010112`, `33c95c1`, `4c8b4c6`; Orders0067 consumer-accepted | Actual0067 seven-file198 PASS; guarded browser USER-session8 PASS; append-only facts and initial publication | Changed facts still reconcile instead of advancing current projection; source ordering/completeness contract and fake-source revision-to-read acceptance / T3 + source owner |
| 2 Cancellation/return lifecycle | Pure contract and persisted cancellation/return evidence in `9d1f88b`; implemented, not full lifecycle parity | Status mapping and publication tests | Orders partial-return/current progression and reconciliation / T3; work/sheet cancellation handling DEFERRED with Production; WB Statistics never proves fulfillment |
| 2 Known-order Avito status progression | `f4c6a286030f15ba063980d10dc517a7ddad5da6`; implemented, locally accepted, not activated | 20 runtime-role cases plus48 adjacent and64 pure PASS; durable rejected evidence and two-session CAS | Trusted source-client/job wiring, account traversal and changes beyond status remain separate; no provider chronology claim |
| 2 Strict browser raw evidence decoder | Implemented in the commit containing `2026-09-09-orders-browser-envelope-contract.md`; fixed v1 partial-only manifest | Final183 pure PASS, exact synthetic fixture/checksums, no auth/body scope or client completeness | NEW producer must supply explicit IDs/status/quantity/occurrence/capture; token-only guard/sink binding awaits T1, no fabricated user principal |
| 3 Stored order-level deadline read | Follow-up implemented in the commit containing this checkpoint | Final combined69 runtime-role tests PASS;101 pure PASS. Exact org/account/order/current-observation predicates, stable ordering and immutable historical snapshot | No deadline calculation, provider schedule mapping or item-specific binding inferred; source writer and source semantics remain explicit dependencies |
| 3 Catalog resolution, snapshot pagination, cache-only read | `866d1a1`, `da7e017`, `33c95c1`; local consumer-accepted for stated scope | Scoped Catalog/projection/read gates; v3 provenance and decimal-string BIGINT wire | Exact filters and shared registration / T3 + T1; T4 UI consumer integration; readiness remains unproven, no production activation |
| 4 Physical P1 work items, receipt/history, account RLS | DEFERRED; T1 `d57c54418762583c41829dabd80969775b4fdb56` + mandatory `bb7a958d99f1458d7e10877406eaf5dfe03ae6bc` released, NOT T3 consumer-accepted | T1 exact READY: feature173/covering447/review; prerequisite0068 `4d95b24b549f0b1f773095ec583a85f18172fdbc`; head0069 | Preserve additive schema; no new service/CAS feature work until resumed / root + T3 + T1 |
| 4 Permission/actor/replay policy and P1 service | DEFERRED; approved policy `842bbed`, codecs `bd1b9d8`, `35ea34e`; preparation | Committed consumer plan; strict result/command114 pure PASS; T1 main shared-permission265 PASS reported | Preserve implemented policy/code; service, source proof, CAS/audit/two-session acceptance remain unfinished, not active blockers |
| 5 Batches/groups/calendar/selected frozen sheets | DEFERRED; `142cf46` partial HTML characterization; P2 request `0aa2c48` | Existing characterization/request retained | Resume requires source/rules and P2 persistence/version-set assembly; no further source recovery now |
| 6 Existing Avito XLSX | DEFERRED new work; existing `f013dec`, `66c2420` preserved | XML/value/quantity/formula-text and deterministic retry tests; latest scoped90 PASS | Frozen-sheet/artifact integration unfinished; only required compatibility regression now |
| 6 A4 PDF, 120x75 and 58x40 ordinary stickers | DEFERRED; no complete renderer recovered | Source package retained as resumption record | Missing renderer/fixtures and separate format gates; do not search or reconstruct now |
| 7 Archive/reprint/delivery | DEFERRED; P3/P4 request `0aa2c48`, no completed service | Immutable revision/intent/ambiguity requirements retained | Source/storage/service/retry acceptance unfinished; not an active Orders blocker |
| 8 Active Orders integration; Production cutover deferred | Orders candidate `35ea34e77d80920b0d8779a6b888e6f1d8e7bad0`; NOT final-integrated | Exact candidate handed T1/root, legacy functionality preserved | Root active-scope consolidated integration and latesthead compatibility; Production parity/writer-fence/retirement stays DEFERRED, not prerequisite |
| KIZ/separate matcher exclusion | T3 `4374bb5`; T4 owner cleanup (separate branch) | Backend runtime scan has no KIZ subsystem; ordinary return matching and barcodes preserved | No KIZ source or allocation dependency. Historical compatibility records remain; frontend integration belongs to root/T4 |

Current missing input is collected once in
`2026-09-09-t3-production-source-recovery-package.md`. Operational credentials,
production canaries and physical printing are NOT prerequisites for safe local
service development. No current T3 heavy process, reservation or pending cleanup.
The package now also records root-authorized official Avito OpenAPI lookup:
required `hasMore` (not `total`), no documented ordered revision/stable order-line
guarantee. Root subsequently approved the bounded known-order status-only
application policy, implemented in `app/orders/avito_status_refresh.py` and
tracked by `2026-09-09-orders-avito-status-refresh.md`. This is not provider
chronology or account completeness. Local runtime-role acceptance: 20 PASS in
8.76s; adjacent publication/read/HTTP/browser regression: 48 PASS, two known
warnings, in 20.22s. Exact owned database/role cleanup was verified in both gates.
Final independent scoped review found no further significant defects; fresh pure
source/identity checks passed 64 tests in 0.42s. This follow-up is recorded in the
commit containing this checkpoint; the frozen baseline above does not include
it and root integration remains separate. Valid rejected observations remain durable;
only accepted predecessor lineage is excluded from divergence. Existing work
items prevent status promotion. Full-account traversal, old-baseline enrollment,
trusted credential-bound client/worker activation, exact filter semantics and
shared route registration remain separate missing inputs or integration work.
Production portions of that package and the first-consumer plan are retained for
resumption only. The Avito source-semantics investigation concerns ACTIVE Orders;
it does not authorize new work items, sheet generation or print actions.

## Final Deadline Read Package

Implemented first, then verified under the updated user workflow. Existing
`order_deadlines` order-level rows for the exact current observation now reach
the frozen read view using the existing `DeadlineEvidence` wire shape. Source and
computed instants, original rule/version, timezone and evidence source are retained;
no new schedule calculation, provider mapping, readiness or schema was introduced.
Rejected-observation deadlines are not attached to the current projection. Existing
snapshots do not change when a later deadline is recorded. The source writer and
item-specific observation binding are not fabricated by this read-only consumer.

Final six-file runtime-role gate: **69 PASS, 2 existing deprecation warnings,
21.32s, exit 0**. All six exact owned databases/runtime roles were verified absent
after cleanup. The preceding run was **68 PASS / 1 FAIL, 18.58s, exit 1**: an older
cancellation-history test asserted over every order in its module-shared database,
including a new independent reconciled order. The assertion now selects its own
identity and requires both rows and unchanged statuses; no runtime/permission
behavior was weakened. Pure read/HTTP/source/identity contracts: **101 PASS,
0.51s, exit 0**. Ruff, compileall and diff checks passed. Final local critical pass
checked exact scope predicates, unchanged wire, observation lineage and immutable
snapshot behavior; consolidated root acceptance remains separate.

The remaining active inputs are collected in the source-recovery package's current
section. T4 explicitly confirmed no agreed canonical filter engine contract; T1's
trusted Orders job resolver is in implementation, not yet an executable dependency.
The finite selectors in source amendment `2f0422076ac0b365d2e97432b9ce57d6aa6b350f`
describe page acquisition only, not a complete synchronization claim.

## Legacy Cache Baseline Probe (current follow-up)

At `f4c6a286030f15ba063980d10dc517a7ddad5da6`, the unchanged historical
`tests/test_avito_orders.py::test_avito_orders_endpoint_ignores_blocked_cache_and_refetches`
passed alone: **1 PASS, 2 existing deprecation warnings, 1.77s, exit 0**.
This does not establish full-suite resolution; `ops/legacy-test-failures.txt`
remains unchanged. The test stubs actor/credentials/orders client/cache, but
does not stub public-color lookup or return-inventory DB enrichment. Thus its
authorization and enrichment behavior are not acceptance evidence.

Reproduction used a cleared environment, disabled plugin autoload/bytecode/cache,
and macOS sandbox denial of ALL networking and file writes plus secret-file reads.
No DB allocator, provider, printing, operational export or file mutation ran.
Initial pytest FD capture could not create its temporary file and exited before
collection; switching to in-memory capture and disabling logging fixed only the
harness, without relaxing containment. No runtime or test source was changed.

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/sandbox-exec -p \
  '(version 1) (allow default) (deny network*) (deny file-write*) (deny file-read* (regex #"/\\.env($|[./])")) (deny file-read* (regex #"/\\.(pgpass|pg_service.conf|netrc)$"))' \
  /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/backend/.venv/bin/python -m pytest \
  -p no:cacheprovider -p no:logging --capture=sys -q --tb=short \
  tests/test_avito_orders.py::test_avito_orders_endpoint_ignores_blocked_cache_and_refetches
```

Run from this worktree's `backend` using the approved sibling T1 virtualenv.
The blocked-cache branch explicitly bypasses blocked results at
`app/routers/avito_orders.py:879`, then calls the injected order client at line884.
The probe provides no reason to alter that branch or weaken permissions. Missing
test isolation and possible suite-order/environment dependencies remain distinct
from an asserted cache defect. Production renderer tests were not resumed.

## Historical Evidence (not current status)

All headings, tables, pending/blocked statements, test counts and next-step notes
below describe their original checkpoint. In particular, old "P1 DDL not READY"
and "permission requires owner policy" statements are superseded by the current
matrix. Do not use the historical sections as a second current backlog.

## Scope override пользователя, 2026-09-09

КИЗ/Честный знак и отдельный recovered matcher исключены из продукта по прямому
указанию пользователя. Isolated matcher harness/report удалены: runtime imports
или consumers отсутствуют. PHP runtime tests не запускались. Исторические строки
ниже про КИЗ/source recovery описывают прежний scope и больше не являются gates.
Avito return matching, Catalog resolution, WB/Avito order identities, XLSX и
обычная печать не относятся к удаляемому КИЗ subsystem и сохранены.
Backend app scan не обнаружил kiz/chz/КИЗ/Честный знак runtime references.
Frontend cleanup выполнен владельцем T4 в его ветке, включая `8d4a5b9`;
общая интеграция остаётся отдельным gate. T3 frontend не менял.
Backup/user data и исторические Git commits не удалялись.

| Этап / requirement | Реализованные commits / проверки | Remaining / blocker |
|---|---|---|
| 1 Discovery / parity / schema request | `4323821` Stage1; `3a30b16` offline/XLSX; `f4d6d55` returns/XLSX edge characterization | Полного production prototype нет; только частичные исходники. КИЗ/matcher исключены, не blocker |
| 2 Identity / normalized ingestion | `9d1f88b` atomic publication; exact replay/CAS; actual0064 and now0067 consumer acceptance (198PASS below) | Source progression deliberately reconciles; real adapter completeness/paired-fetch activation and worker authority still require proof |
| 3 Catalog / read API | `866d1a1` Catalog/assembly/cursor/HTTP; `da7e017` decimal-string BIGINT; 0067 immutable provenance and v3 snapshot gate | Router unregistered; legacy unbound/v2 rejected; source/adapter mixed coverage needs contract; no filter engine or production readiness. Mutable audit result receipt is not claimed immutable |
| 4 Production work items / commands | Pure assignment preconditions + canonical receipt byte codec; `0aa2c48` exact creation/witness/accountRLS amendment | T1 confirms Production DDL not READY; actual CAS/concurrency NOT_RUN; command permission requires owner policy |
| 5 Batches / groups / frozen sheets | `142cf46` actual HTML synthetic characterization; `0aa2c48` P2/P3/P4 storage requests | Полный legacy schedule/grouping source не восстановлен; Orders read snapshot не равен Production sheet |
| 6 XLSX | Renderer characterization: inline text, leading zeros, Cyrillic, quantities, 1000 rows; `f013dec` rejects XML-invalid controls | Frozen sheet adapter/артефакт persistence требуют Production contract |
| 6 A4 PDF | Source gap описан | Нет полного renderer/fixtures, не реконструировать по screenshots; multipage/48+ rows NOT_RUN |
| 6 Stickers 120x75 / 58x40 | Source gap documented; no substitute renderer invented | Нужны исходники обычных WB/Avito renderers; selected unit identity/barcode/quantity parity NOT_RUN. КИЗ/matcher не требуются |
| 7 Archive / delivery | Requirements and safety boundaries preserved; КИЗ excluded | Immutable historical artifact and delivery receipt/ambiguity require source+Production schema prerequisites |
| 8 Cutover / retirement | Proposed T4 wire/error handoff `f4d6d55`, no activation | Нет полной parity/reconciliation/writer fence/rollback proof; prototype не удалён; switch запрещён |

## Strict storage decoder slice

### Independent P1 result decoder

Accepted design `2026-09-09-production-assignment-storage-design.md` already fixes
the seven result fields. Added frozen AssignmentResult and exact version1 JSONB
encode/decode, strict positive BIGINT IDs/versions, positive INTEGER SKU/required,
nonnegative planned/remaining with remaining=required-planned. No planning operation
or permission follows from this representation. AssignmentCommand now rejects
values beyond the accepted physical INTEGER/BIGINT bounds before reaching SQL.
These codecs are internal Python/JSONB, NOT a numeric browser wire contract.

Actual RED64: missing result decoder and three accepted command overflows. Final
result/command-codec/text/read-contract files113PASS0.11s/exit0; scoped Ruff0,
independent read-only critic no important findings. P1 persisted witness/CAS/live
authority still requires exact READY migration and real two-session acceptance.
No new heavy gate was run while T1 owned the resource interval. Browser-token
USER-session sink tests now pass the bounded8case actual0067 gate below;
token-only issuance binding/auth/completeness remain explicitly unresolved.

Decoder follow-up: 10,000 nested empty arrays (20KB) caused uncaught RecursionError
in `deserialize_assignment_command`:1RED0.06s. Added conversion to its existing
fixed typed validation error; final114purePASS0.13s/Ruff0, read-only critic no
important concerns. Shallower initial probe already passed and is not RED evidence.
No payload/memory limit or HTTP DoS protection is claimed by this exception fix.

### Normalized browser sink acceptance

`tests/test_orders_browser_publication.py`:8PASS3.28s/natural0 after explicit T4
release, own disposable DB/role absence verified, then explicit release to T1.
Existing behavior characterization, not a fabricated RED or new auth implementation.
Complete/partial durable runs and replay, required user session, account/UUID/expiry
metadata mismatch without run insertion, revoked-token replay denial and final-hook
revocation rollback all pass. Final-hook denial checks exact authority error, not
generic SQL permission failure. This is same-transaction revalidation, not a new
two-writer revocation proof. Read-only critic found no important false positives.

Committed service interface remains:
`app.orders.publication_service.publish_orders_manifest(session, *, principal,
account, authorities, manifest, source_run_key) -> OrdersPublicationResult`.
Principal is actual `UserSessionPrincipal`; account is `ExpectedAccountBinding`;
browser authorities include `ExpectedIngestionToken` with exact
`avito.browser_snapshot.write` scope. Own clean root transaction uses fixed
`sync:run` and live guard. `OrderManifest` is normalized domain input, NOT the
browser HTTP body. Body owner override policy, token secret verification,
issuance-time frozen account binding and bearer-only principal are not supplied
by this sink. Org/account/provider drift rejects rather than overriding identity.

Manifest completeness is structural: one scoped account/source/adapter/snapshot,
unique orders, contiguous pages with final terminal marker and exact declared
distinct-order count. Partial stores facts without projecting/removing absent
orders. No browser/provider completeness is inferred from bearer or supplied flag.
Immutable0067 binding and original replay result remain; changed evidence reconciles.
Outcome fields:run_id,state,replayed,reconciliation_count, returned only aftercommit.
Typed validation/conflict or sanitized PublicationGuardError propagate; HTTP error
translation remains endpoint-owner work. No memory/cache success fallback.

Tests use synthetic token metadata/verifier only, not a real bearer or account.
No new route, auth mechanism, provider/network call, operational export or activation.

### Independent XLSX retry stability

Existing Avito XLSX package used render-clock ZIP timestamps. Synthetic two-clock
test first confirmed all member XML equal but package bytes different (1failed,
0.51s); renderer now uses explicit fixed ZIP epoch and DEFLATE per member.
No headers, cell values/types, row ordering, quantity fallback, styles or print
settings changed. Same-runtime repeat output is byte-stable; cross-Python/zlib
identity is not promised. Focused XLSX/returns/Stage1:44PASS0.30s/exit0, scoped
Ruff/compile/diff0, independent critic no important findings. No output file was
written; only synthetic in-memory rendering. Artifact persistence, renderer version
registry and frozen-sheet input still require P2/P3 contracts. Rollback of this
renderer-only change restores old timestamp metadata, not any business data.

### Remaining execution boundary after independent fixes

This lists remaining requested product work, not a claim that every possible bug
has been eliminated. Existing pure identity/status, receipt codecs, read contracts,
normalization evidence, date validation and known renderer characterization have
executable coverage. More invented models would not substitute for these inputs.

| Remaining | Exact missing prerequisite / owner | Existing local evidence |
|---|---|---|
| Live Avito ingestion / worker publication | Proven complete provider page manifest and worker principal authority; fetch account binding must travel with response; no provider execution allowed | `2026-09-09-orders-job-source-binding.md`, `2026-09-09-orders-domain-binding-amendment.md`; current service handles normalized guarded user manifests only |
| WB operational readiness/deadline/sticker ingestion | Separate WB fulfillment source contract, not supplier/orders statistics | Stage1 discovery and `app/modules/orders.py`; statistics unknown/cancellation-only mapping remains deliberate |
| Automatic changed-fact progression | Proven ordering/version/coverage semantics; opaque revision or clock alone insufficient | `app/orders/publication_service.py` records reconciliation instead; pure comparator is not progression authority |
| Full order filters / activation | Exact typed filter semantics and shared router registration/permission decision | `2026-09-09-orders-read-http-handoff.md`; query checksum is not a filter implementation; T4 owns frontend |
| Persistent assignment / quantities / CAS / two-writer proof | Exact READY P1 DDL plus command permission; T1 f76ad06 is plan only, queued after Review binding | `2026-09-09-production-workitems-schema-request.md`, `2026-09-09-production-first-writer-amendment.md`; pure command codec already implemented |
| Production batches / groups / scheduling / selected sheets | P2 storage plus full source calendar/grouping rules including weekend, cutoff and coverage behavior | `2026-09-09-production-frozen-artifacts-schema-request.md`; actual HTML partial source characterization is not full rule authority |
| Frozen XLSX artifact / A4 PDF / both sticker sizes | Immutable sheet contract + P3 artifact storage; full A4/sticker renderer source missing | Stage1/UI-DB parity reports; Avito XLSX source exists and is tested, HTML labels are not complete unit/barcode renderers |
| Archive / reprint / delivery intents and ambiguous receipts | P3/P4 storage plus proven provider acceptance/receipt contract; historical revisions must remain immutable | `2026-09-09-production-frozen-artifacts-schema-request.md`; UI send click is not success |
| Cutover / prototype retirement | Complete per-format parity, reconciliation, writer fence, rollback and explicit retirement authorization | Current matrix stages5-8 blocked; do not remove prototype or revive whole-JSON writer |

Report filenames above are under `backend/docs/superpowers/reports/` unless stated
otherwise. KIZ/label matcher is excluded, not a dependency. No schema number is
reserved by T3; follow the exact T1 READY handoff rather than assuming P1 is0068.

### Independent instant-order correction

Pure contract audit found same-ZoneInfo datetime comparisons use local wall time
across a repeated hour. Four actual RED cases: two observation directions, valid
nonempty coverage wrongly rejected, reversed instants wrongly accepted (4failed,
64passed,0.15s). `AccountCoverage` bounds and `compare_observations` now compare
UTC instants without replacing source values, inventing a calendar rule or granting
projection progression. No computed deadline or weekend schedule was introduced.
Final nine-file pure/serialization/legacy regression:240PASS1.82s/exit0. Scoped
Ruff/compile/diff0; independent critic reports no important defects.
Actual0067 publication/assembly regression:18PASS99.07s/exit0, own disposable
database/role absence verified. No provider or operational actions.

### 0067 consumer acceptance (supersedes preparation notes below)

Merged exact T1 feature and mandatory fix through
`ff91356830c14cb494d55d4f5ec28b18a60e88cb`; decoder preparation is
`8010112ed61ead1c345630da38618dd0ebc231ed`. No T1 migration was edited.
New publication INSERT carries all immutable binding fields from the outset.
Replay validates those fields before reading its result receipt. Assembly validates
every selected coverage/current source run directly, never using audit as provenance.
Current projection sources must also be complete published manifests, not staging.
SQL repositories use actual columns; no speculative shared ORM change was needed.

New frozen marks are `orders-view-v3`; audit-era v2 snapshots fail closed even when
the live binding digest matches (latest, explicit and cursor). HTTP shape/query,
cursor v1 and decimal-string BIGINT are unchanged; HWM stays opaque to consumers.
Old data is retained, not backfilled/relabelled. Rollback must retain expanded0067
and a decoder-capable binary; do not downgrade bound rows or reactivate audit-era
readers. Permission, provider completeness, A-to-B-to-A epoch identity and Production
eligibility are not established by the immutable fingerprint.

TDD: missing persisted fields RED; audit checksum deletion RED after correcting
test JSON versus JSONB operator. Initial fixture setup lacked helper privileges;
fixed by applying actual runtime role script in disposable0067 fixture. Historical
0064 repository fixture remains unchanged. Critic found staging admission and v2
acceptance; reproduced 4 failures in12.59s plus pure v2 RED, then fixed. Independent
re-review reports no further important defects.

Final seven-file gate: **198 passed, 2 known dependency warnings, 85.53s, exit0**:
`test_orders_bindings.py`, `test_orders_publication_service.py`,
`test_orders_read_assembly.py`, `test_orders_read_service.py`, `test_orders_http.py`,
`test_orders_run_binding_migration.py`, `test_orders_run_binding_rls.py`.
Earlier combined attempt:183PASS/10setupERROR, migration subprocess60s timeout;
not counted green and timeout was not increased. All allocated databases/roles
were confirmed absent on cleanup, including failed attempt. Scoped Ruff, compileall,
diff check, Git fsck:0; sole Alembic head0067. This is not whole-backend acceptance.
Scrubbed environment, disabled pytest plugin autoload, inherited Unix-only sandbox;
`/dev/null` passfile warnings retained, no application DB or network access.

Next executable consumer depends on exact Production P1 DDL; f76ad06 is a plan,
not READY. Missing renderer/schedule/WB fulfillment source gates remain separately
blocked. No schema/config registration edits, provider/production actions, physical
printing, operational exports, frontend changes, KIZ, push or activation.

`deserialize_observation` и `deserialize_read_row` в `app/orders/serialization.py`:
exact envelope/version, exact nested fields, strict scalars/arrays, canonical UTC
timestamp encoding, reconstruction через existing validated frozen models.
Отвергает unknown version, bool/float/string version, extra/missing fields, forged
source line key/account/status mapping, invalid resolution и mismatched read item.
Нет fallback на current Catalog или provider. JSONB object input, не JSON text
parser: duplicate JSON keys должны отвергаться до JSONB, если будет внешний JSON
ingress. Этот decoder не подключён к API и не устанавливает authority SQL FK.

Формат v1 связан с существующими mapping versions. При будущем изменении mapper
нужен явный versioned decoder/mapping registry; нельзя silently remap historical
status или переименовать v1 fields. Повреждённая история требует reconciliation,
не скрытого repair. Права проверяет будущий repository на каждой странице.

TDD RED: отсутствующий `deserialize_observation`, pytest exit2. GREEN: 27 decoder
cases; combined scoped offline regression **232 passed, 2 прежних dependency
warnings**, exit0. Ruff проходит. Compileall/diff проверяются перед commit.
Self-review: проверены schema versions, nested domain validation, отсутствие
mutation входного JSON и mutable Catalog/network imports. Full backend baseline,
DB/CAS/RLS/renderer visual parity НЕ заявляются.

```sh
python -m pytest -p tests.orders_offline_plugin -q tests/test_orders_decoding.py \
  tests/test_orders_serialization.py tests/test_orders_contract.py \
  tests/test_orders_stage1_characterization.py tests/test_orders_xlsx_characterization.py \
  tests/test_orders_offline_guard.py tests/test_avito_orders.py tests/test_avito_returns.py \
  tests/test_orders_ingestion.py tests/test_orders_read_contracts.py \
  tests/test_orders_avito_adapter_characterization.py tests/test_orders_returns_characterization.py
python -m ruff check app/orders/serialization.py tests/test_orders_decoding.py
python -m compileall -q app/orders/serialization.py tests/test_orders_decoding.py
```

SQL candidate не является installed migration. Ни application DB, ни Redis,
provider, production, реальные print/export, deploy или push не использовались.
## XLSX invalid-character rule change

Отдельное явное изменение legacy behavior в `app/avito/orders_picking_xlsx.py`:
`_cell` отклоняет запрещённые XML 1.0 codepoints до создания ZIP: NUL/недопустимые
controls, surrogate range, U+FFFE/U+FFFF. Разрешённые tab/LF/CR и Unicode ranges
не очищаются и не нормализуются. ValueError содержит постоянное сообщение без
исходного текста. Это не новый renderer и не schema work.

TDD: 4 RED failures (XML parse errors/Unicode encoding error вместо predictable
rejection), затем GREEN. Старые characterization assertions повреждённого XML
заменены на acceptance безопасного отказа; исторический отчёт `f4d6d55` остаётся
доказательством предыдущего поведения. Количества/layout/headers/inline formulas
не менялись. HTTP error envelope не меняется здесь: T1 router owner должен
согласовать отображение renderer ValueError перед UI activation. Без такого
binding запрос может завершиться generic 500, но не успешным corrupt XLSX.
Rollback возможен отдельным revert этого fix; он вернёт известный corrupt-output
риск, поэтому не рекомендуется как способ продолжить печать malformed данных.
Все проверки только synthetic in-memory rendering, без physical print/export.
Combined offline regression после изменения: **234 passed, 2 прежних warnings**,
exit0. Ruff/compileall проверены для renderer и characterization file. Полный
visual XLSX/Excel layout acceptance не заявляется.

После exact T1 ready commit приоритет переключается на Orders persistence.

## Orders evidence repository / accepted dependency

Merged exact verified T1 schema `f9c7401d946063465e0576a689eb247c368eed73`
with its prerequisite lineage, no merge conflicts or manual shared-file edits.
Active0062 dependency accepted by root; не запускалась против application DB.

`app/orders/evidence_repository.py` now implements transaction-participating
order-level fact storage via parameterized SQLAlchemy Session queries. It checks
scope and current tenant context, locks run FOR UPDATE, rejects terminal/source
binding drift, creates scoped order identity, stores immutable normalized evidence
with semantic replay check, and links receipt to run membership. Same run/different
evidence is rejected. New run replay returns original persisted observation receipt.
No commit, provider, secret access, authorization helper, snapshot publication,
item projection, cancellation inference or route registration in this repository.
Caller must roll back on errors and hold fresh auth/account locks.

TDD RED missing repository import, then5 actual PostgreSQL tests PASS: rollback,
terminal/source drift, same-run changed fact/scope rejection, cross-run replay,
two simultaneous sessions/Barrier yielding one immutable fact. Fresh random
disposable DB and runtime NOSUPERUSER/NOBYPASSRLS role; reused T1 bounded fixture
with deliberately broad DML grants (tests trigger defense, not exact final ACL).
Network denied by sandbox except local Unix PG socket. Fixture verified DB/role
cleanup. No working DB table read, only maintenance metadata/create/drop own DB.
Ruff/compileall passed; DB08/revocation and full publication remain NOT_RUN.

Root fixed trusted service permission decision: user-initiated publish requires
`sync:run`; persisted reads require `cabinet:read` and live scope. These are service
constants, never request strings. Existing membership+profile union retained;
worker denied. Guard implementation is T1-owned, still awaited for service wiring.

Production schema dependency separately committed `8a8722f`:3relations with CAS,
successful receipt and immutable assignment audit; does not require renderer source.

## Sealed snapshot repository

`app/orders/snapshot_repository.py`: caller-transaction freeze/read over0062.
Before INSERT header, freeze validates complete read model and binds each row to
stored observation, account-qualified item identity, quantity, current version and
resolution. Deterministic account/external-order/source-line order; projection SHARE
locks held through caller commit. Header plus all payload rows inserted in same tx;
existing deferred DDL seals positions/count. No commits or own sessions.

Read fetches ONLY snapshot header/rows (no mutable Catalog/projection joins), checks
exact query checksum/account scope, expiry when present, position bounds, payload
versions and scoped row metadata. Returns frozen rows, high-water mark, published
time, validated account coverage/aggregate and numeric next_position. Position is
repository-only, NOT an authenticated/signed HTTP cursor or enduring permission.
No default retention lifetime invented; service activation must decide cursor policy.
Empty account scope is rejected for storage; service returns empty without header.

TDD RED missing module and separate missing coverage result assertion; GREEN3
snapshot PostgreSQL tests (historical read after current mutation, stale freeze,
query/account mismatch). Combined evidence+snapshot8PASS14.18s, fresh disposable
DBs/runtime roles; cleanup verified. Ruff/compileall exit0. Tests use T1 Unix-only
sandbox profile and bounded fixtures. Full concurrent snapshot-vs-projection race,
projection writer/CAS, authguard/DB08, API/cursor and retained-snapshot expiry gates
remain incomplete. No claim that this is already GET/api/v2/orders.

Guard design reviewed exact `1406686de74b52e1d7c92c3d9975d55b778c0cc6`:
metadata locks/atomic credential binding/session lock/final commit validation agree
with caller-owned repository transactions. Do not call a resolver with a separate
session under these locks. Guard is still a design dependency, not installed here.
Self-review retained coverage in read output and checks frozen payload against DB
identity/version rather than trusting only JSONB object shape. Projection/observation
source authority remains service responsibility; snapshot storage cannot prove it.

### Parent projection composition amendment

Root requested parent-only status/version race. Actual two-session RED reproduced
stale ready freeze after cancellation while item version stayed1. freeze now requires
`parent_versions: dict[ExternalOrderIdentity,int]` for exactly its scoped orders;
under existing parent SHARE lock compares version plus raw/canonical status and
mapping state/version with selected evidence. No observed_at ordering or version
guessing. Snapshot assembler must capture these parent versions alongside source
selection; changed parent forces conflict/retry. Historic committed snapshots stay
immutable; their read never compares against current parent.
Two real connection PIDs, Barrier/Event, parent-only cancellation and version-only
change both rejected. Combined snapshot/evidence10PASS8.89s, disposablecleanup
verified. Ruff import-order corrected before commit. This closes the stale-parent
storage gate, not yet authoritative source/high-watermark selection in service.
Known0062 long-key B-tree defect remains separate T1 amendment prerequisite; no
arbitrary cap or hash-as-identity introduced in domain.

## Parent projection CAS primitive

`app/orders/projection_repository.py` set_parent(run_id,observation_id,expected_version)
uses the caller transaction/tenant context, staging-run FOR UPDATE, exact scoped
membership, stored observation decoder/checksum/source binding and SQL
`UPDATE ... WHERE org/account/order/version ... RETURNING version`.
Updates raw/canonical/mapping fields, effective source time and last-seen run;
no observed_at ordering, no provider/raw body, no own commit or session.

This is a repository primitive, NOT a source progression decision: trusted service
must classify replay/out-of-order/reconciliation before calling it, authorize under
T1 guard and atomically commit all publication effects. Calling this primitive for
an arbitrary older fact would be misuse; it does not claim automatic lifecycle
monotonicity. No route calls it. Item projections/status-history/coverage/final-run
publication remain separate unfinished service work.

TDD RED missing module ->3PGGREEN including two real sessions competing for one
parent version (one winner/one conflict); combined evidence/snapshot/CAS13PASS16.22s.
Added caller-rollback regression: focused4PGPASS5.34s. Fresh random disposable DB,
runtime role and cleanup verified, Unix-only sandbox. Ruff/compileall exit0.
Self-review checked exact scoped joins and that incoming request cannot override
stored status. Full auth/revocation/source-progression/long-key gates not claimed.

## Exact-key repository preparation

T1 approved design `87ef04884e5636dbed704ee3f54f2e95fb9959a7` removes raw TEXT
arbiters in forthcoming0064. Evidence append now rejects RR/SERIALIZABLE, locks
the scoped account before run/domain locks, then performs fresh C-collated exact
order/evidence lookup and insert-if-absent. Semantic replay verification and
bounded membership ON CONFLICT remain; no hash identity or length cap.

RED: two isolation cases DID NOT RAISE on old code. GREEN: eight evidence tests,
including two actual sessions with pg_blocking_pids proving account-lock wait;
the waiter establishes a snapshot before blocking and sees the winner's newly
committed exact fact. Final evidence/parent/snapshot regression17PASS9.67s on0062,
own disposable DB/role cleanup verified. Ruff (backend cwd) and compileall exit0.
Manual critic: account lock is serialization, not authorization; caller still
needs T1 shared guard before entering, and must not hold domain locks first.
Actual0064 migration/long-key gate remains NOT_RUN until T1 delivers its exact SHA.
No schema, shared auth, routes, providers, production or frontend changed.

## Item projection primitive

`OrdersProjectionRepository.set_item(run,observation,line,resolution,expected_version)`
uses the same decoded scoped order-level membership as parent CAS. Shared account
lock precedes run/item locks under RC. None means insert only if absent; explicit
version requires CAS. Stored line identity/quantity/effective time cannot be
overridden by request. Repeated listings retain distinct explicit occurrence keys.
Identity drift fails; no physical deletion or inferred absent-item cancellation.
Catalog IDs remain references with account-scoped Product/Offer FKs; resolution is
an explicit trusted caller decision, not title matching. No readiness field written.

RED2 missing set_item -> GREEN; final parent/item/evidence/snapshot21PASS9.03s,
including two actual sessions/PIDs competing for item version (one winner, one
conflict), quantity3->5, repeated lines, missing line, rollback, and same-org other
account Product rejection. Cleanup verified, Ruff/compileall/diff exit0.
Manual critic: not an operator assignment endpoint; manual override commands still
require Production CAS/idempotency/audit service. No-op replay, out-of-order source
selection and atomic status/coverage/run publication remain service obligations.
Long-key acceptance still waits exact0064 and its PG gate; no original0062 changes.

## Actual0064 consumer acceptance

Exact T1 implementation `0dbb85d8ccda4d528b4fc9b37d539a6e664cbabf` and test correction
`d44c971148ab1e5d185adcc079c8d2ab9aced813` merged with prerequisite lineage in
`2d9a40059f39ecf2b1b2d4f7123d8ff749e2126c`, no conflicts or manual shared-file edits.
The earlier long-key NOT_RUN entries above are historical; this gate is now PASS.

All three repository suites now use actual0064 pinned disposable fixtures. New
consumer tests retain >4096-byte synthetic order/adapter/line values exactly,
replay without changed IDs, create a new observation for changed evidence, keep
same external ID in two accounts distinct, and roll back order/item/evidence
together. The existing physical account-wait test now runs with short and long
keys; both sessions have established snapshots before the waiter is released.
A separate latest-head fixture applies the actual runtime role script and verifies
narrow Orders privileges before evidence replay and parent/item projection calls.
No latest-head assertion is baked into the historical0064 fixture.

Fresh combined command (same sanitized Unix-only prefix documented above):
`python -m pytest -q -s --tb=short tests/test_orders_exact_repositories.py tests/test_orders_evidence_repository.py tests/test_orders_projection_repository.py tests/test_orders_snapshot_repository.py tests/test_orders_exact_text_migration.py`
Result: **106 passed in37.92s**, exit0, no skips; 26 consumer cases plus80 migration
cases. All own disposable DB/role cleanup assertions passed. Ruff over app/orders
and four consumer files, compileall, diff check and git fsck --no-dangling exit0.
Self-review read actual171-line migration and checked account-before-domain locks,
C-comparison predicates, bounded membership arbiter and scope preservation.
This closes the0064 repository compatibility dependency, NOT publication/auth,
complete source coverage, worker authority, Production parity or application activation.

## Guarded persisted read service

Consumed T1 paired-fetch `7bfb631d30426dd39a4f4dae7aab4754155b6a62`, guard0900f8e
and mandatory finalizer fix `4860c53df7e12543c7f8afb7569ada773bea37d3` by clean merge.
`read_orders_snapshot` owns a fresh root transaction on the supplied unused Session,
uses fixed cabinet:read and no credential authority, delegates to immutable snapshot
read, then returns only after revalidation and guarded Session commit succeeds.
Does not commit a caller's existing transaction. Physical SQLAlchemy COMMIT errors
are sanitized. Principal must come from authenticated session/canonical membership;
the passed principal object is not authentication. Exact account bindings and live
scope are checked on every page. Current T1 guard requires connected accounts;
no active marketplace credential is required to read stored facts.

RED missing module; fresh GREEN34PASS15.94s: eight read-service cases plus26
repository cases. Covers no-credential read, Session reuse, membership/permission/
scope/logout revocation between pages, final-commit mutation rejection/rollback,
and actual two-session logout lock wait (distinct PIDs, pg_blocking_pids observed).
Own disposable cleanup verified, Ruff/compileall exit0. Self-review corrected the
snapshot ID annotation to int and confirmed result cannot escape before commit.
Not an HTTP route/signed cursor/authentication adapter; no provider/business-state
write, worker authority, current queue activation or Production parity claimed.

## Executable HTML recovery, not mock authority

Read actual `frontend/public/vella-production.html`, last local source revision
`58c32d83b9bce69ce1957e48e45b24001cc107c6`; T4 supplied newer cleanup8d4a5b9
and generator `frontend/scripts/generate-vella-production-snapshot.mjs`. Generator
was not run, frontend was not edited. New backend-only Node/TypeScript AST harness
executes original function declarations, never page startup; synthetic localStorage,
UI boundaries and rejecting fetch replace browser/external actions. No real prints,
downloads, stored user data or provider calls. Python HTMLParser extracts scripts.

Six executable cases PASS4.84s: group flattening preserves ordering/quantity;
120x75 and58x40 produce one escaped-text sticker regardless quantity3, with no
barcode/QR SVG/image/canvas; unknown status normalizes to ready; missing status
passes ready filter; override storage key is only source/date with whole-object
last-write-wins; rejected send still stores demo sent. These are characterizations,
NOT approved canonical rules. They prove why source execution alone is not parity.

Evidence: HTML17434 export calls missing server renderer;17465 send catches failure
and marks sent;17795 storage key;17861 release-time display fallback08:00;18062
normalizer;18165 group flattening (consumes groups, does not derive schedule/group
rules);18190 fallback row ID;18857 sticker markup. No proven Europe/Moscow/weekend
backend grouping rule or multipage A4 renderer is recovered by these functions.
Canonical Orders raw/unmapped behavior remains unchanged; no ready fallback copied.

Command: same OS sandbox, scrubbed environment; add NODE_PATH pointing at existing
T4 frontend/node_modules and run `python -m pytest -q tests/test_orders_production_html.py`.
Uses existing project TypeScript parser, no installs/new dependencies. Node VM is a
test mechanism, not a security boundary; OS sandbox denies external networking and
secret files. Physical print explicitly throws. Further canonical work proceeds
independently; real format/barcode/render parity gates remain distinct.

## Atomic normalized manifest service

`publish_orders_manifest` owns one fresh caller-supplied Session transaction under
fixed sync:run. Nonempty declared fetch authorities must match source type; the
guard verifies exact live principal/account/credential or browser-token binding.
No fetch inside transaction. Run-key lookup is exact/account serialized; replay
checks immutable run metadata, semantic membership evidence and stored audit receipt.
Run, immutable observations/statuses/lifecycle evidence, initial item/parent
projections, coverage, terminal state and authenticated-user audit commit together.
Partial manifests store evidence/status only, without changing current projections
or deleting/cancelling absent orders. Request period bounds remain NULL, not an
invented provider/date window. Item count counts observed lines, not unit quantity.

Projection authority is deliberately bounded: the first complete normalized fact
may establish a projection. Exact evidence replay is a no-op; every changed fact
without a proven source ordering policy records reconciliation rather than regressing
current state. Cancellation/return statuses produce immutable evidence events,
never physical deletes. This is NOT yet automatic lifecycle progression or a
Production eligibility decision; read assembly must expose reconciliation blockers.
The complete-manifest assertion still requires a real adapter completeness contract;
no live source handler or scheduler is activated. No missing field becomes ready.

RED missing service import; initial GREEN exposed a test-fixture teardown column
mistake (one test passed, four setup/teardown errors). Inspected actual credential
schema, corrected to revocation_reason_code/operator_revoked, cleanup confirmed.
Final focused9PASS3.59s; combined six Orders consumer files43PASS20.16s, no skips.
Two actual sessions/PIDs publish one run/audit; same key/different manifest conflicts;
rotated generation denies; final guard failure rolls back all publication effects;
WB statistics raw/canonical NULL remains unmapped. Earlier8case combined42PASS18.16s.
Ruff/compileall/diff exit0, all own disposable resources cleaned. No schema/routers,
provider calls, real marketplace actions, external print/export or production writes.

## Immutable run decoder preparation (2026-09-09)

T1 delivered 0067 feature d87e575f8fec32815dbeb459157339f8ad25220f plus mandatory
fix c4e3e373f97be19171f80ebdad2ce919dd7c84dc. Its full schema handoff was read.
`app/orders/bindings.py:validate_run_binding` now validates its five immutable
fields against the exact independently guarded single-account binding. Legacy
unbound, partial fields, changed ownership/provider/reference, aggregate payloads,
noncanonical bytes and invalid version/checksum reject with a fixed domain error.
All four single-account golden vectors pass, including Unicode and nullable refs.
No audit JSON fallback exists in this helper. It is not yet connected to storage.

TDD: missing function produced 19 failures (exit 1); the initial test matrix also
contained one vacuous nullable-reference case, removed before implementation.
Final focused command `python -m pytest -q tests/test_orders_bindings.py
tests/test_orders_contract.py tests/test_orders_http_contracts.py`: 97 passed,
0.75s, exit 0. Scoped Ruff and compileall: exit 0. Independent read-only critic:
no important defects; suggested golden-positive coverage added and rerun.

Available next: actual 0067 merge and publication/replay/read assembly acceptance,
including forged mutable audit and unbound-history denial. Existing consumers
still use audit provenance: that gate is NOT closed by this pure preparation.
Blocked separately: Production P1 exact DDL/permission (f76ad06 is only a plan),
missing full frozen renderer/schedule sources, WB fulfillment source authority,
and parity/cutover gates. HTTP wire unchanged. No database, provider, production,
physical print/export, frontend, schema, KIZ, push or activation changes here.
