# WB live path — shared coordination

Base: filipp `966dcc5ba64697b52f596a0abd755323c23e36e2`.
User scope: attachment 5b6a9f7b-0dde-4dd3-b7f4-83a288517901, read in full.
Coordinator: current task 01a08045-7a78-72d2-ad9a-34a2bc76f519.
Integration: codex/wb-live-integration, architecture-integration-filipp worktree.

## Current execution override — one terminal

The user stopped parallel execution. ROOT alone owns subsequent implementation,
schema changes, tests, review and integration; historical owner assignments below
describe provenance, not permission to resume other terminals. Preserve their
worktrees. No subagents or terminal delegation unless the user re-enables them.

The full-original-T1–T4 scope remains applicable. The smaller WB-live package is
the first useful-path priority, not a replacement denominator for total progress.
The requested 50% checkpoint must use traceable accepted requirements, not test
counts, commit counts or the historical 77% estimate. The denominator/accepted
requirement ledger is not yet complete, so no 50% claim is made.

Next-path prioritization: dedicated persistent infrastructure and the existing
WB products path precede new Orders snapshot-publisher contracts. Source inspection
and the composed test below show that products do not depend on Orders snapshots.
Do not start a new snapshot subsystem merely because that design is available.

## Owned local runtime checkpoint — 2026-09-10

### Later live acceptance evidence (supersedes the no-token/no-job state below)

User entered a WB key through the local connection form. ROOT did not retrieve
or print it. Connection had persisted, but no job existed: the current UI requires
a separate explicit "Проверить доступ и загрузить" action in the profile.
ROOT activated that existing read-only action once under the user's ongoing
request. No duplicate start, history date, price write or message send was used.

Real worker fetched/persisted content and prices. The UI initially rejected
the returned product timestamps because it required UTC suffixes, while the
backend datetime contract preserves the PostgreSQL session's `+03:00` offset.
Safe stack-only temporary diagnostics isolated `timestamp()`; diagnostics were
removed. Frontend now accepts explicit ISO datetime offsets, preserves the
original timestamp/instant, and still rejects zone-less or invalid timestamps.
Causal regression: two non-UTC cases failed before the fix; final WB validation,
API and history suite **56 passed**, direct `tsc -b` exit 0.

Actual in-app browser: **50 real product rows**, no alert after rereading;
partial prices/names are kept nullable rather than invented. API and worker
were gracefully restarted: both active, one durable job, one encrypted credential,
**2232 product identities retained** at that checkpoint (union of content/price
rows, not 2232 complete cards). Background loading remains enabled. This does not
prove complete content/prices history, final P&L, sustained throughput, full
restart/retry matrix or final original T1–T4 acceptance. The previous screenshot's
rendering failure was separately fixed in `3f73f11` with a genuine full-route
browser regression and scoped styles; misleading legacy overlay/footer/calendar
are no longer mounted on live products.

User explicitly authorized installing isolated local infrastructure. ROOT alone
installed official digest-verified Lima 2.2.0 ARM64 into
`~/.local/opt/satorna-lima-2.2.0`, with the dedicated
`LIMA_HOME=~/.local/state/satorna-lima`, profile `satorna-db`.
The checked-in `ops/satorna-local-db.lima.yaml` pins Ubuntu 24.04, 2 CPUs,
1 GiB RAM, 8 GiB sparse disk, no host/home mounts, no forwarded SSH agent or
existing public keys, no proxy environment propagation. Only API port 58000 is
forwarded to host **127.0.0.1**. Explicit IPv4/IPv6 deny rules suppress database,
Redis and other port forwarding; guest Redis uses a private Unix socket only.
Existing host PostgreSQL and backend production were not modified.

Inside this new VM: PostgreSQL 16.15 and Redis 7.0.15; empty application DB
`satorna_wb_live` migrated to `20260910_0085`, then existing
`ops/wb-live-local-grants.sql` applied transactionally. Linux/SQL identities
`wb_live_api`, `wb_live_worker`, `wb_live_dispatch` use peer authentication,
not database passwords or owner credentials. Owner is migration-only. API
snapshot writes and product-title updates were denied; dispatcher cannot read
the keyring. Newly generated local crypto/auth secrets remain outside Git in
root-managed guest files. No WB token was created, recovered or submitted.

Runtime release: `/opt/satorna-releases/739366d/backend`. Three enabled systemd
units `satorna-wb-{api,worker,beat}` run as distinct unprivileged users with
read-only system paths, private state directories, no core dumps and no raw
service-log output. Redis AOF is enabled. There is one worker and one beat.
Real price application, review sending and legacy/canonical collectors remain
disabled; only the existing read-only WB-live schedule is enabled.

Actual checks:
- Full VM stop/start: services return automatically with zero restarts;
  synthetic PostgreSQL marker survives. This is persistence/restart evidence,
  **not** a backup-restore or real WB loading proof.
- `/health/live` and `/health/ready` pass from host localhost after restart;
  database, Redis, Celery broker/result checks are `ok`. Worker `pong` verified
  separately. Readiness still reports worker/beat `not_monitored`; do not call
  the heartbeat architecture complete.
- Real HTTP through Vite proxy: synthetic registration, cabinet/me, refresh,
  logout, and revoked bearer/cookie rejection pass. One clearly named synthetic
  account remains in this isolated DB; no provider credential or job created.
- Regression fix `739366d`: Celery Unix URL must be translated for redis-py
  readiness while preserving selected database. Original failure reproduced;
  scoped health/Linux config suite **36 passed, 2 dependency deprecations**;
  Ruff and diff checks pass. Existing health assertion updated for the already
  present default-off WB fields; the health payload itself was not changed.
- Frontend WB-live API/validation/history suite: **50 passed**. Registration
  page visibly verified in Codex browser; Playwright CLI Chrome launch timed
  out, so that CLI attempt is not accepted as browser evidence.

Working launch command, from integration worktree: `./start-wb-live --vm`.
This starts the **already provisioned** VM and local Vite, waits for readiness,
uses same-origin `/api` proxy, disables MSW, never runs production snapshot
generation. URL: `http://127.0.0.1:5173/auth/register`. Ctrl+C stops only Vite.
Stop the owned VM without deleting data:
`LIMA_HOME="$HOME/.local/state/satorna-lima" "$HOME/.local/opt/satorna-lima-2.2.0/bin/limactl" stop satorna-db`.
The no-argument launcher retains the native path; it is not the accepted path
on this Mac because native initdb had failed with shared-memory ENOMEM.

Limitations: VM Python is 3.12.3; first-path imports/HTTP work, but the dormant
review sender requires Unicode 14/Python 3.11 and is **not** accepted on this
runtime. Dependencies are not a complete reproducible lock. The VM provisioning
was performed locally; the launch command is not a fresh-machine installer.
Observed VM memory after service startup: 562 MiB used / 392 MiB available
of 955 MiB, **not** peak-load measurement. Owned Lima state occupies ~2.6 GiB;
host free disk ~11 GiB. No first-page/throughput/p95 claims without actual data.

Next required live acceptance: user creates their local account and enters a
read-only WB token **in the connection form, never chat/logs**. Verify durable
connection → background batch/progress → stored products → selected-account UI
→ restart → subsequent update. Until this passes, the first live package is not
complete and package-two external operations must not be activated. Full
original T1–T4 scope and the traceable 50% checkpoint remain incomplete.

Single-terminal composed proof, base `2d127f4`: new
`test_wb_live_first_products_path.py` passed actual PostgreSQL (1 in 4.09s), scoped
Ruff. It migrates to current head, applies the real local API/worker/dispatcher
grants and connects a new synthetic seller through the actual encrypted service.
Actual dispatcher emits IDs only, actual worker uses a mocked HTTP boundary,
and fresh API engine/repository instances recover the same intent and read two
durable products in distinct signed pages. Pending prices stay null and readiness
partial; account lock is released during HTTP. Owned resources were cleaned with
fixture absence assertions. It does not prove browser/Bearer/Celery broker wiring,
OS-process/DB restart, real provider access or persistent infrastructure. Earlier
focused existing connection test passed (1 in 3.77s) on its historical 0080 fixture;
that result is not a current-head whole-path claim. No production code changed.

Persistent native startup remains blocked by demonstrated host `shmget(size56)`
ENOMEM; mmap/low-memory alternatives were already tried unsuccessfully. Existing
shared PostgreSQL trust authentication is not approved for real credentials.
No currently verified native workaround or installed container CLI is available.
Installing/provisioning an isolated runtime requires a separate explicit user
decision. Do not repeat initdb probes, modify existing services/HBA/kernel/IPC,
or run the launcher as a DB probe (it also creates keys and enables services).

## Ownership and delivery

| Owner | Exclusive areas |
|---|---|
| T1 | Alembic/schema/indexes/grants; account/credential connection, durable sync intent repository and HTTP; config/db/cabinet; local infrastructure and launcher |
| T2 | WB sync execution, provider reads, paging/checkpoints/retries, read-only periodic job discovery; existing repricer_sync/tasks as needed, new wb_live worker/provider modules |
| T3 | Account-scoped read HTTP/query services and DTOs, screen/source mapping, query/index requests to T1 |
| T4 (coordinator locally) | frontend only, settings connection and existing data screens/typed consumers |
| Coordinator | this file, integration, backend main/bootstrap/router registration and shared task entrypoint registration |

Separate branches based on base above. Completed commits only for handoff. Never modify another owner's paths without explicit transfer. T1 owns all DDL, T2/T3 request it. No migrations in other packages. Report only completed package, contract change or blocker. No CodeRabbit. No full audit or repeated full suite; complete packages first, focused acceptance afterward; heavy checks serial.

## Frozen cross-package contracts

Preserve existing account-scoped GET/PUT credential route:
`/api/v1/cabinet/marketplace-accounts/{id}/credentials/wb/wb_api`, PUT `{wbToken}`, existing DataEnvelope/MarketplaceCredentialStatusView; never return secret. Separate verified/saved credential from sync state. T1 resolves existing account discovery/create needs without organization-global fallback. Authenticated session and fresh account permission remain authoritative.

New missing sync route family (T1): `/api/v2/wb/accounts/{id}/sync` POST (empty JSON, explicit idempotency header) and GET (latest persisted state). Return `{data:{marketplaceAccountId,jobId,state,sources,updatedAt}}`; jobId UUID string or null on never-started GET; state idle/queued/running/partial/completed/failed; sources list `{source,state,processed,updatedAt,errorCode}` with nonnegative committed count, nullable UTC timestamps and safe nullable error code. No fabricated percentage/total. HTTP 403 denied, 409 conflicting action, 503 unavailable; saved key remains distinguishable from enqueue failure. Exact replay resolves same intent. T1 supplies final DTO and worker repository signatures before dependent integration; additive details must be recorded here.

Durable intent: organization + account + credential binding + source/window + idempotency identity, no plaintext in broker. PostgreSQL is authoritative; Redis is delivery, not commit evidence. One active equivalent source/window, leased recovery, bounded attempts/backoff; persist batch/checkpoint atomically, repeat-safe unique identities. Recheck current account/credential binding before provider access/publication. No locks held during HTTP. T1/T2 agree existing table reuse and concrete repository signature directly; preserve publication guards.

First useful screen: existing WB SKU/goods table; first batch goods/content/current prices as supported by current provider consumers. T3 owns `/api/v2/wb/accounts/{id}/products` bounded server read and exact existing-column DTO; page size default 50/max 200, stable deterministic cursor, allowlisted sort/filter, no per-row queries. Return data items + nextCursor + source readiness (empty/partial/ready/error); never infer complete from zero rows. T3 sends exact wire fixture to T4 and schema/index needs to T1. Preserve existing canonical ABC/P&L route and null final-profit semantics; never replace unavailable canonical fields with guessed financial values. Extend only screens supported by actual loaded sources and explicitly list remaining ones.

Frontend scopes requests/cache by session + organization + account + period/filter, aborts/invalidates stale work, deduplicates in-flight GET, bounded status polling paused when hidden/unmounted. No automatic repeat of uncertain writes, no demo/error-as-empty fallback. Only one active credential write path after verified replacement.

## Dependencies and acceptance

All owners start from these contracts now; mocks only in development/tests. T1 publishes storage/API; T2 publishes bounded source runner; T3 publishes queries; T4 consumes agreed wire. Integrate immutable commits then run one bounded end-to-end path with persistent local services. No existing remote production modifications or broad environment/service discovery. Local app setup may use dedicated persistent local resources; never repurpose another database or enable external price/message mutations. Live WB token must be supplied securely through UI by user; do not retrieve secrets from history/logs or display them. Missing token is a live acceptance blocker, not permission to fabricate data.

Measure first useful page, source throughput, main query timings, peak memory and browser response bytes; distinguish fixture measurements from live WB. Validate persistence across controlled restart of our own services. One launch command and local address required. Package 2 (notifications/settings/schedule and existing external-operation adapters/recovery) starts only after coordinator confirms real-data path; no automatic external mutation during connection check.

## Known gaps / current state

- Confirmed source: frontend settings writes legacy /cabinet/wb-token; cabinet enqueue catches Exception and returns silently; canonical credential route already exists but does not enqueue.
- Runtime fake-mode, PostgreSQL/Redis/worker availability and real-mode memory fallback require targeted T1 verification, not assumptions from prior reports.
- Persistent job/provider/read composition is not yet verified. No live credential accepted in this task yet.
- Foundation tests are historical evidence, not acceptance of this new operational path.

### Accepted implementation details

- T1 verified native PostgreSQL and Redis tools available; Docker CLI absent. Use dedicated persistent native resources, not existing application databases.
- T1 account discovery: GET `/api/v1/cabinet/marketplace-accounts?provider=wb` returns `{data:[{marketplaceAccountId,provider,externalAccountId,displayName,status}]}`. Connect: POST `/api/v1/cabinet/marketplace-accounts/connect/wb` with `{wbToken,displayName?}` returns `{data:{account:{same fields},credential:{existing MarketplaceCredentialStatusView}}}` after actual seller verification and encrypted persistence. Existing account credential PUT is replacement. Never derive account identity from the legacy `wb-primary` capability label.
- `Settings.wb_live_sync_enabled` / `VELLA_WB_LIVE_SYNC_ENABLED` default false. T2 implements `app.wb_live.tasks`: `wb_live.dispatch_pending`, `wb_live.run_batch(organization_id, marketplace_account_id, job_id)`. Coordinator registers a 30-second dispatcher tick and `vella.wb-live` routes; initial worker concurrency one. Legacy execution schedules remain off in launcher.
- First sources are `content` and `prices`; goods is their read projection, not a duplicate source fetch. History is subsequent work within package 1, not represented as already loaded.
- T2 inspected official browser documentation on 2026-09-10 at https://dev.wildberries.ru/docs/openapi/item-management. Content uses ascending updatedAt/nmID cursor, 100 cards/page. Price listing is GET `/api/v2/list/goods/filter`, limit 1000/offset, continuing until empty; POST is nmList lookup, not equivalent provenance. Existing 0077 POST evidence cannot be reused to label a GET. T1 owns any necessary codec/schema accommodation.
- Documented base price token limit is four/hour (900 seconds between calls); faster service profile requires explicit verified entitlement. Persist retry/due time and release worker instead of sleeping for 900 seconds. Content-only rows are partial, missing prices are null. Honor server throttle feedback. Static web fetches returned 498; fresh evidence is T2's official browser read, not stale search index.
- T3 product query: limit default50/max200; sort `nmId|vendorCode|title|brand`, direction asc/desc, literal `q`, exact `brand`, opaque cursor. Envelope data has integer marketplaceAccountId, items, nextCursor, readiness, sources and opaque readVersion. Items: string nmId, nullable vendorCode/title/brand/subjectId(string)/subjectName/photoUrl/contentUpdatedAt/pricesUpdatedAt, sizes and sizesTruncated. Size: string chrtId, nullable techSize, skus, skusTruncated, nullable decimal-string priceKopecks/discountedPriceKopecks. Bound first100 sizes and first100 barcodes each, flag truncation. Current-source table has no historical-period semantics. Signed cursor binds principal/account/query/source revision; one statement reads source revisions and rows at one MVCC snapshot. Changed publication returns409 `WB_PRODUCTS_CHANGED`; frontend resets pagination without a retry loop.

## Superseding user instruction — full original T1–T4 completion

The user explicitly resumed all unfinished original requirements after reading
attachment `42220e2b-3a63-4b43-a8e4-78334716fb3f`. This supersedes the earlier
package-2 implementation wait and Production deferral, not external-action
authorization. Independent implementation may proceed before a real WB key is
available; fixture proof must never be called live acceptance.

Current delivery ledger:

| Owner | Active bounded package | Acceptance / dependency |
|---|---|---|
| T1 | Notification preference runtime/0081, then history staging/publication and manual Review SQL amendment | Retry/import-path fixes integrated; sole DDL owner |
| T2 | Price HTTP adapter and override factory delivered; remaining source/runtime composition | No blind retries or invented finance/authority policy; durable shared quota still required |
| T3 | Complete Orders read/filter contract for UI, then Production commands/CAS | Preserve separate marketplace status rules and all legacy print/export behavior |
| T4 | Local Reviews drawer and first manual draft; Notifications UI integrated | Existing real source/policy required; no fake generation or provider send |
| ROOT | Shared entrypoints, native startup, assembled verification and merges | Heavy slot free after T1 preference gate; allocate explicitly, no simultaneous runs |

Imports: final native-cursor/URL fix `fba66f6` -> `d603aa7`; worker cursor
regression `edf9cd` -> `8b9ddf8`. Explicit partial-source retry and clean launcher
import fixes are integrated through `a854ecc`, independently reviewed.
T3 actual product bounds supersede the earlier paragraph: first **5** sizes;
barcodes omitted with explicit null/empty/truncated semantics, not 100 x 100.

Original requirements not yet accepted: all-consumer credential lifecycle and
Avito isolation, repricer runtime/settings/recovery, history/revision evidence,
Orders UI, Production parity/commands/batches/print/archive, complete Reviews and
Notifications flows, remaining canonical consumers, CI/debt/rollback acceptance.
Track them as implementation plus verification, never merely a merge checklist.
Existing authoritative business decisions are reused. Missing final-profit
policy is a decision blocker: retain null rather than invent a formula.
KIZ/standalone matcher and redesign remain excluded; no actual production,
price changes, message sends, printing or exports are authorized by this restart.

### Historical integration checkpoint (2026-09-10, HEAD 814a69d)

Notification list/receipt client and UI are integrated (`8f88f7a`, `a926728`,
`7f2dea7`); shared default-off API registration `3e911c0` has five focused
entrypoint tests passing (2.24s, two dependency deprecation warnings).
Schema0081 `5a4ee23` is imported, not applied to a persistent database. T1's
preference service/config/legacy-writer fence package is still pending import;
the preferences screen is therefore not an accepted end-to-end workflow yet.

First manual Review backend `cb8496a` and local drawer `814a69d` are imported.
Manual mode is initial draft only, requires an actual current policy/source,
and remains dependent on T1's additive SQL encoder amendment. No database or
full Reviews acceptance is claimed from pure tests. Helper owns only domain
code; T1 remains sole DDL owner.

History streaming source is integrated through `d48afb3`, independently reviewed.
Its staged pages are provisional until guarded EOF; published source evidence
is not the canonical Orders queue. Explicit dateFrom is mandatory. With no
active job, history initialization may also create the normal content/prices
sources and must disclose them; adding history to an active job leaves siblings
unchanged. No inferred date window or implicit cursor reset.

Dedicated local initdb remains blocked by host shared-memory allocation. The
existing PostgreSQL alternative was rejected after read-only checks found
generic trust authentication. No existing database/role, HBA, kernel setting,
IPC segment or server was modified; no persistent service or live WB key is
active. Disposable synthetic test databases are not approval to store real
credentials on that shared server.

### Historical integration checkpoint (2026-09-10, HEAD 7c93728)

- Notification preferences/config and registration are now composed through
  `74dcfb7`; 30 focused entrypoint/HTTP tests passed. The final local API role
  exercised list/read/dismiss and two recipients plus preferences: one actual
  PostgreSQL test passed in 17.90s, owned DB/roles cleanup asserted. Independent
  bounded Notifications review passed. This is not bearer/live acceptance.
- First manual Review SQL0083 `7c93728` is imported after history0082. T1's
  15-case gate covered canonical bytes, initial revision, replay/edit/approval,
  revocations, downgrade and retained SQL identities. Root's independent SQL
  review is pending; local workflow and streamed64KiB HTTP boundary were reviewed.
- Shared WB/Avito account discovery `9db095a` + registration `7e76b4c` and Orders
  UI `772069c` are integrated and independently reviewed. Metadata has its own
  default-off flag, not the WB collector flag. Root's 15 entrypoint/HTTP tests
  passed; T2's 10 actual PG cases passed. Orders remains a limited saved view.
- SKU context/config `a2714d8` and read composition `9ecb4a9` are integrated,
  with 29 focused HTTP tests and independent review passing. Write admission
  remains permanently false until an actual verified legacy-writer cutover.
- History0082 `1a4954d` and runtime `6b563d4` are integrated and independently
  reviewed. T1's four unique PostgreSQL cases include the real streaming worker
  and non-bypass-owner downgrade refusal. Staged source evidence is NOT current
  canonical Orders projection. A separate explicit sync:run projection intent
  is the approved direction; the original read subscription is not write authority.
- Production service `70acd18` adds guarded create/read/manual assignment and
  passed independent source review. T3's 16 unique PG cases and six new pure
  cases passed. It is dormant: no HTTP, local API grant acceptance, calendar,
  batches, print/export, or prototype retirement is claimed.
- Synthetic Chromium component proof `a4b5fe5` extended by OrdersUI passed on
  owner worktree. Root built the integrated frontend with canonical flags true:
  direct Vite build passed in 6.17s; existing large-chunk warnings remain. This
  is not styled parity or live-data acceptance; no snapshot generator was run.
- Scoped CI `3cc3a0d` is source/static-validated only, not executed on GitHub.
  It explicitly does not replace full release/debt/restore/rollback gates.

Work at that checkpoint: T1 sole schema/guard owner prepares durable shared price quota then
explicit Orders projection authority; T2 migrates a bounded Avito statistics
consumer without org cache/plaintext fallback; T3 prepares bounded staged-source
decoding and projection semantics; T4 adds explicit history initialization UI.
All external mutation flags remain off. Missing real WB input, dedicated local
infrastructure acceptance, final finance decisions and complete Production
renderer/calendar parity remain separate blockers, not completed tasks.

### Historical integration checkpoint (2026-09-10, HEAD 478c15a)

- Shared price quota schema0084 `d9adb37` and service `ae4df5d` are integrated
  and independently reviewed. Owner proof: 31 pure / 12 PostgreSQL tests;
  ROOT repeated the pure contract: 31 passed in 0.53s. Database-time account
  serialization, monotonic cooldown and hostile default-ACL/reapply denial are
  covered. This is admission infrastructure, not price-send authority. Actual
  adapter admission currently follows dispatch; T2 must move preparation before
  the marker with a one-use bound operation and no second quota charge. A crash
  may waste quota capacity; no durable exactly-once admission claim is made.
- Shared WB/Avito Notifications account discovery `80952a8` is integrated,
  independently reviewed and owner-tested (58 focused/client/browser cases).
  Cleanup fix `fbda7c0` has 17 focused client tests plus TypeScript passing.
  Neither Notifications nor Orders derives action permission from metadata.
  Explicit WB history initialization `ae29ccb` is also reviewed and integrated.
- Manual Review SQL0083 and pure history bridge `80b26e3` have passed bounded
  independent review. Final local API role tests `742b30f` (Reviews) and
  `22c8409` (SKU read-only) expose narrower missing grants; T1 owns their
  RED-to-GREEN fixes. Broad fixture roles are not final API acceptance.
- Avito encrypted-account statistics `5399e46` and default-off exact-pair
  composition `478c15a` are integrated. Owner proof: 43 offline / 19 PostgreSQL
  cases; ROOT entrypoint/HTTP/transport run: 48 passed in 2.42s. Independent
  review found a daily-null aggregation defect despite those tests; T2 is
  fixing it before acceptance. Final local API-role proof and validated config
  are still pending. No legacy statistics cutover, source persistence/cache,
  OAuth refresh or live provider acceptance is claimed. Future frontend load
  must be explicit, not triggered by every date-field change.
- Scoped CI `8807d30` includes quota, history decoder and new typed frontend
  consumers; YAML parsed locally. GitHub Actions has not been executed.
- T1/T3 projection direction is frozen: a separate current `sync:run` intent,
  immutable completed-history selection, typed 1000-row chunks and atomic
  receipt/progress. Historical and current credential identity/generation/
  incarnation must match. Explicit trusted policy and deadline dependencies
  are mandatory; no runtime values/TTL are invented. Partial observations do
  not establish fulfillment readiness or permit Production activation.

Active implementation is T1 minimal grants/authority, T2 Avito correction and
pre-dispatch price composition, T3 Production HTTP factory while awaiting the
exact projection handle, and T4 typed explicit-load Avito client. All new gates
remain off; the dedicated local infrastructure and actual WB-key blockers above
are unchanged. This checkpoint is not complete architecture/release acceptance.

### Current integration checkpoint (2026-09-10, HEAD 97fc255)

- Final local API Review grants `4f22de9` and SKU read-only grants `2ab1f31`
  are integrated. ROOT's assembled Review, SKU and encrypted Avito statistics
  tests passed together: **3 passed in 10.29s**, with actual limited API logins,
  synthetic owner seeds and allocator cleanup. Reviews cannot enqueue/send;
  SKU reads require no current mapping or write permission. Avito statistics
  reuses existing credential-read rights and performs no SQL mutation.
- Avito daily-completeness correction `4841499` is independently accepted:
  incomplete/null days cannot become a complete total. Explicit zero and null
  totals remain distinct. Config `b29b3ce` supplies strict default-off exact-pair
  rollout; own route does not depend on WB collection or metadata flags.
- ROOT's exact migration ancestry, Avito entrypoint and config checks passed:
  **24 passed in 1.84s**. `f3b06c3` fixes the old test that incorrectly required
  0083 to remain the newest migration forever: exactly one head, 0083 ancestry
  and its exact 0082 parent remain required. An initial broad `-k head` selector
  also selected one PG fixture, whose native initdb failed before starting a
  server; its allocated directory was empty. The corrected exact-node check
  was run independently. No existing server or database was changed by that
  setup failure.
- Typed Avito client `97fc255` has 40 owner tests and TypeScript passing.
  Canonical UI is still being implemented behind its own default-off flag;
  it will load only after explicit submission, never on every date edit.
- The shared role proof is not live/bearer acceptance, nor full migration or
  release acceptance. Existing lint debt remains: unchanged SIM117 in the
  manual-schema test and unchanged config import ordering were reproduced in
  pre-patch sources. No blanket lint waiver or all-green claim is made.

The heavy PostgreSQL slot is now T1's exact six-case general WB runtime ACL
gate. Migration0085 is reserved exclusively for T1's history projection authority.
T2 prepared-price composition, T3 Production HTTP and T4 explicit-load UI
continue independently. No runtime policy values, production actions or legacy
retirement are introduced by these approvals.

### Superseding integration checkpoint (2026-09-10, HEAD c98f020)

- Prepared price admission `202ad12` precedes the durable dispatch marker.
  ROOT verified the actual quota service plus prepared worker: **3 passed in
  8.30s**, including durable Retry-After and lost quota-commit acknowledgment.
  This is synthetic transport proof, not live activation or provider exactly-once.
- Saved Orders views now have two-table read-only local API grants `c04c609`:
  ROOT's unchanged final-role test went from RED to **1 passed in 3.69s**.
- Explicit-load Avito statistics UI `0802ac9` is integrated behind its own
  default-off flag. ROOT TypeScript and direct build passed. Native ephemeral
  Vite binding `f697013` fixed an actual parallel-browser port collision:
  two browser files / **3 tests passed in 6.87s**, with listener cleanup verified.
- Pure history projection contracts `4387c63` and dormant trusted credential
  registrar `e7a6513` are independently reviewed. They do not establish actual
  0085 SQL authority, maintenance provisioning, source transfer or rotation.
- Production command client `c98f020` passed independent source review and ROOT
  **50 tests in 703ms**, plus TypeScript. It is client-only, not mounted UI.
  Commands are single-dispatch and require explicit readback; unknown creation
  without a returned ID stays blocked because no safe lookup endpoint exists.
- Actual Production HTTP-to-service final API-role test `c1ae0e2` is RED:
  **1 failed in 8.73s**, creation returned 503 rather than 200. Synthetic DB and
  three roles were removed and absence verified. T1 owns cause diagnosis and
  any narrowly justified local role changes; broad grants are not authorized.
- CI adds already verified Orders/quota, dormant Production HTTP/client,
  registrar, pure projection and parallel Avito browser checks. YAML is locally
  parsed; no GitHub Actions execution or full release proof is claimed. The RED
  Production final-role test is deliberately not yet advertised as a CI gate.
- ROOT then ran the complete eleven-file scoped frontend command with two
  workers: **222 passed in 11.49s**, including both Chromium fixtures; all five
  owned listeners closed. The four newly added pure backend files passed
  together: **95 passed in 1.13s**, with two existing dependency warnings.
  A safe diagnostic repeat of the Production role gate confirmed SQLSTATE
  **42501** on `marketplace_order_items` (1 failed in 3.74s); it printed neither
  SQL nor parameters and verified temporary database/role cleanup.

T1 is implementing genuine 0085 projection authority; T3 implements its actual
participant, with no fake handle. T2 continues the encrypted-account Avito
Orders status preview, not a full queue or legacy replacement. Production
batches, calendar, print/export parity, credential maintenance/store lifecycle,
runtime business-policy decisions and full release acceptance remain unfinished.
No real provider request, secret retrieval, production operation or push occurred.

### Superseding integration checkpoint (2026-09-10, HEAD 06cba90)

- Production grants `3b49771` are independently reviewed and the unchanged
  final API-role workflow is now GREEN: **1 passed in 9.94s**. Narrow lock
  privileges are not source-publication rights. The catalog timestamp column
  remains explicitly classified as mutable timestamp DML, not immutable.
- Recovery GET `ee80042` finds committed work by account + original order item
  + exact source version, using current Production read authority and source
  coherence. No audit/write/retry is performed. Owner HTTP44 and service PG6
  pass; an initial native-initdb setup error was resolved only by the already
  authorized synthetic owned-database fixture, not host changes. ROOT extended
  final API-role workflow `52cd067` also checks recovery before/after assignment:
  **1 passed in 5.11s**. Temporary database/role cleanup was verified.
- Client `ea435de` exposes explicit `readbackUnknownCreate()` with no caller
  IDs, accepts only the original captured scope and source version, and retains
  its write fence on failed readback. ROOT **68 passed in 989ms** and TypeScript
  passed. The suspected trailing-newline ID defect was disproved by tests;
  `36a909d` adds regression coverage only, not a bug fix.
- Encrypted Avito Orders status preview `497c9d0` / `ae30760` and client
  `9ce84da` are independently reviewed. Owner service PG15 passed; ROOT pure
  preview + recovery HTTP **57 passed in 1.96s**, client **70 passed in 1.07s**.
  Present null/conflicting account aliases and unsupported top-level aliases
  fail closed; absent provider account evidence is explicitly credential-scoped.
  Unknown statuses/timestamps stay unknown; no quantity/price/total is inferred.
- Config `c8e01ca` and bootstrap `06cba90` register this preview only under its
  own default-off flag and strict organization/account allowlist. ROOT TDD was
  4 failed / 1 passed before implementation, then combined source/entrypoint
  checks **54 passed in 2.53s** and config/entrypoint **41 passed in 2.83s**.
  Actual final API-role test `a84324b` passed **1 in 5.87s**, including fresh
  revoked-session denial and no service DML. No live/bearer/provider proof,
  canonical queue publication, frontend mounting or legacy replacement claimed.

T1/T3 are implementing the explicitly approved per-run history decision witness
over immutable parent memberships. The database must derive its pre-state and
outcome, reject caller-authored/reordered proof, and prevent parent advancement
before proof capture. Reused historical singleton observations cannot become
initial projections; legacy behavior is unchanged. Eight codec vectors have
bounded owner evidence (6 passed first run, 2 passed after a fixture-only chunk
split); full 0085 authority/runtime is still unaccepted. T2/T4 independently
implement encrypted-account Avito Reviews metadata preview, without text/PII,
drafting, sends, source-publication authority or automatic effects.

### Superseding checkpoint (2026-09-10, HEAD 8a623f9)

Avito Reviews metadata backend `2ea2e59` and client `a6fe0e1` are independently
reviewed. ROOT pure67 passed in 0.78s; frontend55 passed in 419ms and TypeScript
passed. Actual limited API-role test `b293c0b` passed 1 in 4.81s with cleanup,
including foreign-account/revoked-permission denial and no service DML.
Negative mathematical zero is normalized without float conversion, retaining
decimal scale; rating/count nulls are not fabricated defaults. Content, buyer
names, images and answer actions are deliberately absent from this preview.

Own default-off config `2b640fc` and bootstrap `8a623f9` preserve empty exact-pair
allowlists and all legacy paths. ROOT entrypoint TDD: 4 failed / 1 passed before
implementation; combined config/source/three preview entrypoints: 100 passed in
2.49s afterward. New bootstrap/tests Ruff and diff checks passed. This is not
full Reviews parity: old UI depends on text, answer text, account-scoped state
and correctly separated canonical action authority. T2 inspects the existing
canonical adapter/publication contract for the next real read-parity slice;
another unrelated metadata preview was not started.

Independent review found two additional unaccepted T1 projection concerns:

- Participant/domain exceptions escaped the closed error boundary. T1 is
  applying and testing closed errors without retained exception contexts.
- Capturing pre-state at membership INSERT cannot detect an earlier parent
  UPDATE before history-run insertion. A staging-run predicate alone is not
  sufficient. The genuine runtime must capture/lock a private baseline before
  invoking its participant and validate it at the final fence. Standalone SQL
  proof additionally requires a protected projection-writer boundary; T1 must
  propose a dedicated role/helper without changing legacy writer authority.
  No GUC assertion or transaction-ID heuristic substitutes for this proof.

The 0085 decision gate remains unaccepted: syntax error was isolated and fixed;
the subsequent genuine-service setup exposed missing fixture history SELECT
rights. Only fixture reads were approved, not live/API privilege expansion.
T1 reruns the same three cases, not an implicitly expanded suite.

The generated Production snapshot is already lazy. T4's separate approved
frontend optimization targets the whole parity-page module at the App route
boundary; it may reduce non-parity route startup only. It must not be described
as a Production-only or universal WB speedup, regenerate data, or change prints.

### Superseding checkpoint (2026-09-10, HEAD 599251b)

App parity lazy boundary `f035544` passed independent review and ROOT browser/
history checks (16 passed in 9.16s), TypeScript and direct Vite build (3.83s).
Manifest verified entry 1,032,432 bytes, lazy parity 1,353,237 bytes, lazy
snapshot 1,561,404 bytes; static entry closure contains only index.html.
This reduces unrelated-route startup, not total parity/WB download. Recovery is
an explicit document reload; no automatic retry and no prototype regeneration.

Pure Avito canonical decoder `599251b` passed independent review and ROOT
81 tests in 0.47s, scoped Ruff. It preserves the existing canonical contract;
answer body/id/status are not versioned by that model. This is not an Avito
publication guard or full Reviews parity. T2 next determines the exact reusable
source-publication authority; no send/preview permission may substitute for it.
CI includes both canonical decoder/contract and App lazy browser regressions.

T1 reported genuine decision 3 passed and a separate 5-case gate passed (empty
upgrade/downgrade, malformed decision evidence, closed callback exceptions).
These do not approve complete 0085. The new genuine unsealed-publication test
failed as intended: 1 failed in 4.44s, commit did not reject the missing seal.
Owned database/three roles were cleaned and the PG slot released. T1 implements
private baseline/sealed final commit plus the dormant protected-writer boundary.
Narrow UPDATE(order_id) is permitted solely for row locks, provided actual
tests prove every direct parent mutation, including no-op, is rejected. No
operational provisioning, provider actions, rollout or legacy removal occurred.

### Superseding checkpoint (2026-09-10, HEAD e9db267)

Received-page Avito canonical publisher `e9db267` (T2 source `5a02706`) passed
independent review and ROOT bounded pure230 (0.63s), actual PostgreSQL17 (4.46s)
and scoped Ruff. Disposable database/role absence was explicitly verified.
The genuine original reviews:write principal, exact credential and incarnation
are revalidated; pending ORM/listener reordering fail closed; receipt follows
physical commit. Existing WB bodies remain unchanged. Fixture-only PURE4 grants
are not operational/final API-role grants. Fetch orchestration, list/answer
parity, final-role wiring and activation remain separate work.

T1 owner evidence: dedicated-role gate1 passed in 5.18s; empty 0085 roundtrip
passed. Actual T3 nonempty publication initially failed at the deferred decision
witness; safe diagnostic confirmed P0001 and the exact witness. Only child
evidence fix `c9d064b` was added, then the same genuine test passed in 5.95s.
Child payload/checksum/time/item binding, receipt and progress were inspected
after commit. This addresses the missing-child defect, not whole history replay.
T1 schema/ops/runtime checkpoint is not yet imported into ROOT: final review
found the new-operation status read also needs pinned entry/final role checks.
No production changes, operational grants, provider actions or rollout occurred.

### Schema-first integration (2026-09-10, HEAD 678a407)

Schema `5a6832a` and dormant role artifact `678a407` passed independent source
review. ROOT reran the exact empty 0085 roundtrip and physical restricted-role
gate together: 2 passed in 9.72s, both owned databases/three roles removed and
absence verified. Scoped Ruff passed. CI now includes these bounded gates.
No operational role was provisioned and no history runtime was activated.

The new-operation status path still awaits T1's runtime fix/integration:
its genuine owner-factory substitution test failed causally (incorrectly allowed
read), not at setup. Used-history downgrade refusal, full replay, late role
drift and final API-reader grants remain separate gates. T3 owns only a new
genuine used-history downgrade regression; it must preserve stored evidence.

### Runtime integration checkpoint (2026-09-10, HEAD d64d442)

Bounded raw Avito fetch/orchestration `118f789` passed independent review and
ROOT pure272 (0.85s), PostgreSQL13 (4.04s), Ruff and owned cleanup. Same original
binding/incarnation is captured before resolution and checked across closed
roots; one GET runs outside transactions. Timeouts are per-operation plus
elapsed checks, not a hard 30-second deadline. No provider call/activation.

History runtime `e02b62e`, T3 participant `f3a5cdf`, child fix `1413419` and
genuine regression files `1b87866`/`d64d442` are integrated after source review.
ROOT pure187 passed in 0.74s. ROOT actual3 passed in 12.08s: genuine child
publication, used-data downgrade refusal with 13 unchanged scoped table hashes,
and required per-run decision columns. Owned databases/roles were removed with
explicit absence checks. An earlier accidental pure invocation included a PG
test and failed at sandbox socket setup; a mistyped sandbox path also prevented
one launch. Neither is a source regression or passing gate; corrected commands
above completed normally.

T1 owner status2 and late-ACL/unsupported-path5 gates passed after causal REDs.
ROOT found an additional contract mismatch during full source review: an already
existing empty identity (version1, no current run/evidence) is eligible for initial
projection under the agreed contract, but the private baseline comparison rejects
it. T1 owns the narrow comparator fix; T3 owns its genuine regression and then
same-fixture replay/EOF checks. No schema or legacy-rule weakening is authorized.
An independent test-only helper prepares the final wb_live_api Avito writer gate;
T1 retains exact grants ownership. These outstanding gates are not completion.

### Saved history read checkpoint (2026-09-10, HEAD 6bf8676)

The existing-empty-identity comparator fix `5aac361` and genuine history replay
tests are integrated. ROOT reran the complete four-case history file on
`a21b5ee`: 4 passed in 9.08s, exit 0. This proves a fresh projection-job replay,
empty EOF, initial projection onto a pre-existing empty identity and changed
cancellation reconciliation without overwriting current state. The changed-run
fixture now uses the genuine manual initializer and exact source cooldown;
production selection and authority rules were not weakened. An older test
session's result was unavailable and is not counted as additional evidence.

Final API Avito source grants `5c2f1d6` closed the diagnosed SQLSTATE 42501:
ROOT's unchanged final API source-sync test passed (1 in 4.01s). No send, auth,
observation-update, delete or additional sequence rights were added.

Genuine history-to-saved-view test `6bf8676` passed independent read-only review
and ROOT PostgreSQL (1 passed in 4.71s, exit 0). It calls the real history
participant and guarded owner-side freeze, then discovers and reads the saved
view through the final API role. Selected-account restriction, session revocation
and denied snapshot writes are asserted. Both fresh fixture runs completed their
owned database/role cleanup assertions. This does NOT prove a final snapshot
publisher runtime identity, HTTP wiring, complete history coverage or UI parity.
CI now includes both history test files and the final API Avito sync regression;
the hosted workflow itself has not been run.

ROOT also reran the existing `test_orders_job_publication.py` against the current
migration head: 11 passed in 6.05s, exit 0 and owned cleanup assertions passed.
This covers the bounded pre-existing publication path, not the entire backend
suite. Scoped Ruff and `git diff --check` passed.

T3 owns the next bounded read-only inventory of the existing snapshot publisher
and explicit history-to-saved-queue wiring. API reads must not acquire publisher
grants. No production, provider requests, real keys, operational provisioning,
rollout, printing, exports or remote pushes occurred.

### Snapshot publisher discovery and design gate (base 923ac15)

T3's read-only inventory and ROOT source inspection found no application callers
of `freeze_orders_view` or composition of `WbHistoryProjectionJobs`. Existing
history POST initializes capture, while saved Orders GET/UI are already wired.
The remaining publisher boundary is not a reader/UI rewrite.

T1's proposal, not implementation approval: a separate explicit WB-only command
with its own restricted identity, original live `sync:run` plus `cabinet:read`
authority, and a durable immutable idempotency receipt. Never expand API or pinned
history-projection privileges. Existing snapshot tables have no command key;
new receipt persistence requires an additive schema contract. No schema or roles
were changed during this discovery.

Independent critic identified requirements that must be resolved before code:

- Current assembly is an accumulated current-account view, NOT membership scoped
  to the selected run. Preserve that distinction and validate every contributing
  run; do not silently describe or change it to a run-specific snapshot.
- Extract a transaction-owned participant before adding command orchestration.
  Current freeze owns and commits its root, so wrapping it with a separately
  committed receipt would leave an atomicity gap. Snapshot and receipt must commit
  together, with exact participant/source/account/query/result binding; a foreign
  key to an arbitrary existing snapshot is insufficient.
- Replay checks fresh authority and exact binding, compares canonical request,
  and returns the original immutable result. Unknown commit uses authorized
  same-key readback, not an automatic new snapshot or provider refetch.
- Bounds must apply before materialization/JSON decoding, including individual
  and aggregate bytes, parents/items/deadlines/Catalog fanout/contributing runs,
  and SQL/lock/overall duration. Exceeding a bound rolls back, never truncates.
  Existing source-page, chunk, HTTP and read-pagination limits do not bound this
  accumulated view. No configured snapshot resource policy exists at this base.
  Required versioned injected policy with fail-closed absence is a proposal;
  operational values must not be invented from synthetic fixtures.
- Dedicated-role tests must prove exact effective permissions and physical final
  authority/role fences. Current lock rights on immutable identifiers differ from
  auth/Catalog timestamp UPDATE capabilities, which are real limited mutations.
  Zero sequence privileges remain a test requirement, not an established fact.

T1 read-only inventory is complete; no publisher implementation or activation is
approved by this checkpoint. No production/provider actions or remote operations.

## Historical deferral (superseded where explicitly resumed above)

Production/printing/KIZ/standalone matcher; new financial formulas/redesign; all 70 historical failures except an actual current-path blocker; broad fault matrix and backup-restore program. Package 2 waits for the real-data first-path acceptance rather than silently proceeding on fixtures.
