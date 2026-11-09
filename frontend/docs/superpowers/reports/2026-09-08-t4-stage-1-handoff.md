# Terminal 4 — Stage 1: canonical ABC/P&L integration handoff

Date: 2026-09-08. Status: bounded local integration complete; **not a production rollout or a green full-suite release gate**.

## Git and ownership

- Repository: /Users/bratishka/Downloads/satornawb-git.
- Worktree: /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t4-experience.
- Branch: `codex/arch-t4-experience`.
- Exact requested base: `c88a474695569af905b294906ba604d61f3de62f`.
- Only candidate `500ca047ea35419c116c0ff45b576119f6e633b2` was cherry-picked, producing `7cd1fd9`.
- Follow-up hardening: `028168b`.
- `1ae1cb1` was read, not cherry-picked: its frontend rollback clarification was applied separately. Its global handoff changes are NOT included.
- All tracked changes from base are under frontend/. Backend runtime, schemas, migrations, JSON data, scheduler, deployment configuration outside frontend, and SATORNA_ARCHITECTURE_HANDOFF.md are unchanged.
- No push, deployment, production reads, real provider/LLM/Telegram calls, printing, export, or live send. No CodeRabbit.

## Source evidence and limits

Current local schemas/router and all three test_abc_pnl_* files were inspected, not inferred solely from the old handoff. Sources:

- backend/app/modules/wb_reports/schemas.py
- backend/app/routers/wb_reports_v2.py
- backend/app/modules/wb_reports/abc_pnl.py
- backend/app/platform/finance/service.py and finance period/snapshot schemas
- backend/tests/test_abc_pnl_v2.py, test_abc_pnl_service.py, test_abc_pnl_costs.py
- Existing canonicalFinance.ts consumer pattern, candidate commit, local architecture handoff/audit, remaining-work report and Reviews/Notifications discovery.

The handoff-referenced original 2026-08-26 frontend architecture design is absent from the available tree/copies inspected. It was not reconstructed or silently treated as read; restoring that source remains an architecture-documentation dependency for Terminal 1. This slice follows the explicit approved Stage 1 task and actual local contract. Backend tests were inspected, not executed in this frontend-only slice. Production assertions in earlier documents were not revalidated.

## Contract fixed by this integration

- One read endpoint: GET /api/v2/wb/reports/abc-pnl. No collector, mutation, legacy cache warm-up, or provider fetch in the canonical branch.
- Required marketplaceAccountId; explicit dateFrom/dateTo from the selected UI period on every page. Backend resolves 1–90 inclusive Moscow business days into UTC boundaries. Missing paired date or invalid period is an error, not a new default.
- Backend remains the authorization owner: finance.read and scoped WB account access. Local backend tests specify 401 unauthenticated, 403 permission/account-scope denial, 404 foreign-tenant account, and 422 invalid period. The browser allowlist grants none of these permissions.
- Pagination: limit 1–500, offset >= 0. Adapter requests up to 500 rows, validates returned offset/limit/account/date range, and loads all pages before publishing.
- Stable identity across pages includes total, complete summary, complete meta: period boundaries, finance snapshot/checksum/version, ABC formula, cost/economics revisions, advertising checksum/source/evidence, state and blockers. Per-response timestamp is intentionally excluded from identity.
- Rows are canonical nmId groups, including at most one null/unattributed bucket; sellerArticle and CatalogSku are not row identities. Duplicate nmIds within/across pages, repeated offsets, oversized pages, stalled pages, drift or incomplete results fail closed.
- Contract version: wb-abc-pnl-fullstats-loyalty-v1. Unknown formula/schema is rejected at the runtime validation boundary.
- Money remains integer kopecks, with signed corrections/returns where allowed. Nullable values stay unknown; they are not coerced to zero.
- netProfitKopecks, profitClass and abcCode remain literal null. salesClass may be A/B/C; UI shows A·—, not an invented second letter. profitAfterLoyaltyKopecks is explicitly preliminary, not final net profit.
- Cost/economics assumed/missing/undated evidence and arbitrary blocker IDs are preserved. No final-profit formula or allocation rules were implemented in the browser.
- Source metadata retains ads_fullstats/finance_promotion and raw/aggregate_only/derived_legacy evidence as supplied; the frontend does not promote weak evidence to raw.

## Rollout and route ownership

VITE_CANONICAL_WB_ABC_PNL_ROLLOUT stays default-off. Complete mapping must be valid decimal positive safe-integer organizationId:marketplaceAccountId entries. Missing, empty, unknown organization, malformed entry, trailing delimiter, duplicate organization, scientific/hex notation or unsafe integer disables the mapping; a valid prefix cannot enable a malformed flag.

Only ABC and financial P&L use the enabled mapping. Operational P&L and flag-off legacy routes remain intact. Tests use fake scoped identities; no real allowlist was configured.

frontend/vercel.json has a /api/v2 rewrite before generic /api and SPA fallbacks. There is no frontend/api/v2 handler in this tree. Adapter tests verify default GET semantics, bearer header, no mutation body, exact canonical path. Existing API auth-refresh behavior is retained; successful HTML is rejected as INVALID_API_RESPONSE instead of becoming a successful SPA response.

This is a local configuration/transport check, not proof of deployed Vercel routing. Hosted method/header/cookie preservation must be checked during a separately authorized canary.

Rollback is **build-time**: remove the organization or empty the variable, rebuild and redeploy frontend, or deploy a previously verified flag-off artifact. It is not an immediate runtime kill switch. No such deployment was performed.

## States and regression protections

- Loading clears/hides report values; complete empty, missing-snapshot and future-period responses have distinct copy.
- Ready, partial and blocker-bearing rows preserve source metadata and unknown values. Blockers do not turn partial evidence into final profit.
- 401, 403, 429, 503, successful HTML, malformed JSON, malformed contract and network errors were exercised through fake fetch and the real ABC bridge/API client. Errors remain explicit with no canonical-to-legacy/demo fallback.
- Late account/period responses are ignored even when the fake transport deliberately ignores AbortSignal.
- P&L state is keyed by session, organization/account, period, canonical mode and financial/operational source. The render-time guard hides old ready/error state before new-request effects run.
- ABC stores scope with its React snapshot and checks the current auth/rollout/period at render time, including initial reads from globals. SSR tests use the real KPI island + AuthContext and prove old values disappear on the first changed session/account/period render.
- The same-period ABC cache now respects its existing five-minute TTL; a load after expiry refetches. A fresh cache can still be reused.
- “Stale” here covers stale requests, stale displayed scope and expired frontend cache. Backend meta.state has ready/partial/future/empty/missing, **not a stale value**. No source-freshness age policy or background freshness polling was invented. A dedicated source-stale badge requires an approved backend freshness contract/threshold before a later rollout claims that capability.
- Browser-interactive E2E and production canary were not run. P&L render guard is unit-tested; ABC first-render protection is SSR-tested and its request lifecycle is runtime-tested with fake fetch.

## Changes relative to the candidate

1. Strict fail-closed rollout parser.
2. Duplicate-row/oversized-page checks and explicit cancellation checks around every request.
3. P&L render-time context isolation.
4. ABC render-time context isolation and cache TTL enforcement.
5. Regression coverage for malformed flags, duplicate/drifting/stalled pagination, null semantics, HTTP failures, request races and first-render isolation.
6. Corrected rollback documentation to require frontend rebuild/redeploy.

## Verification

Commands ran locally with the rollout environment variable unset for full tests, typecheck and build. Dependency versions came from the existing sibling node_modules directory through an ignored symlink; package manifests/lockfiles were unchanged.

- Focused: npm run test -- src/features/wb-finance/canonicalAbcPnl.test.ts src/features/wb-finance/vercelCanonicalRoute.test.ts src/features/vella-parity/canonicalAbcPnlCutover.test.ts src/features/vella-parity/abcLiveRow.test.ts src/features/vella-parity/scopedReportState.test.ts src/lib/api.test.ts --maxWorkers=2 --minWorkers=2
  Result: **75 passed, 6 files, exit 0**.
- Typecheck: env -u VITE_CANONICAL_WB_ABC_PNL_ROLLOUT npm run typecheck — **exit 0**.
- Build: env -u VITE_CANONICAL_WB_ABC_PNL_ROLLOUT npm run build — **exit 0**. Existing large-chunk warning remains; no bundle-size cleanup claimed.
- Full baseline: immutable git archive of c88a474 frontend in /tmp/satorna-t4-baseline.PWU1dm/frontend, same dependency symlink and worker settings: **193 passed / 29 failed / 0 skipped, exit 1**.
- Full candidate: **261 passed / 29 failed / 0 skipped, exit 1**.
- Comparison uses relative test-file path + fullName, not only counts: **0 added failure IDs, 0 missing failure IDs, 0 failed suites without failed assertions**. Full suite is still red; existing debt was not skipped, renamed or normalized away.
- Both full runs: env -u VITE_CANONICAL_WB_ABC_PNL_ROLLOUT npm run test -- --maxWorkers=2 --minWorkers=2 --reporter=json --outputFile=<report path>.
- Initial non-immutable baseline attempt is NOT used as evidence. Only the clean archived baseline below is authoritative.
- Red/green evidence: 12 initial flag/pagination/abort failures; then TTL failure; then 3 SSR first-render scope failures. These cases now pass. The new generic scoped-state helper was introduced with tests.
- git diff --check passes. Snapshot generator changed only its timestamp; it was restored, and generated snapshot has zero diff. HTML source unchanged.
- Independent read-only critic found P&L stale rendering, ABC TTL bypass and ABC first-render leakage; all were corrected and re-reviewed. No remaining blocking finding in the inspected Stage 1 delta. This is not approval of production rollout.

Local evidence (temporary files, not committed raw full-suite outputs):

- Baseline: /tmp/satorna-t4-baseline-clean.json; SHA-256 24fff74b856cb94a8e641970e217636e1a4af1e7b97f008ae5c0d6047e98cc13.
- Candidate: /tmp/satorna-t4-candidate-final.json; SHA-256 049f0eb023b1f3998d754d3a617deb679303745e64a77b7a0bf85c1c53e2d36a.
- Focused log: /tmp/satorna-t4-focused.txt.
- Typecheck log: /tmp/satorna-t4-typecheck-final.txt.
- Build log: /tmp/satorna-t4-build.txt.

## Exact unchanged failure IDs

The following 29 file :: fullName IDs occur in BOTH full runs:

- src/features/notifications/repository.test.ts :: notifications repository filters by period, category, severity, manager, read state and search
- src/features/vella-parity/abcAuthHelpSource.test.ts :: ABC auth and help source shows a reauthorization panel and hides report blocks when ABC auth token is expired
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring closes Avito listings and stats detail panels through React state
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring keeps Avito overview date inputs, applied period, and backend request in sync
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring lets Avito stats users choose a period and apply backend loading explicitly
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring renders Avito repricer as an actionable strategy table with settings in a modal
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring uses backend Avito listings data without static listing mock arrays
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring uses backend Avito notifications data and actions on the Avito notifications route
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring uses backend Avito orders data through a dedicated route island
- src/features/vella-parity/avitoLiveIntegration.test.ts :: Avito live integration wiring uses backend Avito overview data without static overview mocks
- src/features/vella-parity/parityPerformanceSource.test.ts :: Vella parity performance guards has a single route-driven product load and does not refetch strategies on every path
- src/features/vella-parity/reportInfoButtonsSource.test.ts :: report info buttons source coverage adds human help to report table headers with formulas
- src/features/vella-parity/repricerStatsLiveSource.test.ts :: Repricer stats live source loads the repricer stats tab from the backend stats endpoint
- src/features/vella-parity/rnpLoadingSource.test.ts :: RNP period loading state loads latest cache only for the currently selected range
- src/features/vella-parity/weekLiveSource.test.ts :: stock and week report user-facing states shows clean stock loading and empty states instead of technical source panels
- src/features/vella-parity/weekLiveSource.test.ts :: stock and week report user-facing states shows clean week loading and empty states instead of technical source panels
- src/features/vella-static/vella-source.test.ts :: vella source of truth binds the production RNP report to backend data instead of the secondary mock renderer
- src/features/vella-static/vella-source.test.ts :: vella source of truth does not bring back deprecated reports UI components in frontend source
- src/features/vella-static/vella-source.test.ts :: vella source of truth does not let the legacy secondary renderer retain fallback rows for live report roots
- src/features/vella-static/vella-source.test.ts :: vella source of truth filters report tables by chip, search, and manager in the static Vella shell
- src/features/vella-static/vella-source.test.ts :: vella source of truth keeps report copy threshold-based instead of interpretive
- src/features/vella-static/vella-source.test.ts :: vella source of truth reloads Ads and Stock from backend for the shared selected period without legacy mock rows
- src/features/vella-static/vella-source.test.ts :: vella source of truth renders the WB reviews UX prototype in the production Vella shell
- src/features/vella-static/vella-source.test.ts :: vella source of truth renders the stabilized 8 May report decisions after Vella JS initialization
- src/features/vella-static/vella-source.test.ts :: vella source of truth requires live backend rows for the ABC report table without static row fallback
- src/features/vella-static/vella-source.test.ts :: vella source of truth runs period-scoped network effects only for the active WB surface
- src/features/vella-static/vella-source.test.ts :: vella source of truth scales digest balance chart across empty, daily, and aggregated periods
- src/features/vella-static/vella-source.test.ts :: vella source of truth shows live week request diagnostics instead of hiding the failing stage
- src/features/vella-static/vella-source.test.ts :: vella source of truth supports create and edit flows for strategy cards in the HTML shell

## Terminal 1 / next execution handoff

- Integrate only the commits on this T4 branch after the common integration gate; do not import sibling history or reapply 500ca04 twice.
- Update the global handoff yourself: frontend candidate is now integrated locally on T4, not deployed. Carry forward the 1ae1cb1 build-time rollback correction, not its whole commit.
- Keep final profit/classification null and source-evidence blockers visible. No frontend feature flag can close missing business evidence.
- Resolve the missing original architecture design and define source freshness semantics before claiming a complete rollout gate.
- Existing 29 frontend failures remain release debt. Production canary, observation window, verified flag-off artifact and hosted rewrite/auth checks are pending.
- **Stop after Stage 1 as requested.** Reviews/Notifications persistence, policies, approvals, send commands and delivery recovery (Stages 2–8) have NOT been implemented here.
- Next authorized execution is Stage 2 only after a new instruction: reuse the existing Reviews canonical_contract.py and coordinate sync-run/observation schema, RLS and ownership constraints with Terminal 1. Do not independently edit migrations/config/app registration.
