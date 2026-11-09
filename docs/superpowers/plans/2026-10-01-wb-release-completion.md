# WB Release Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать заказчику корректные и быстрые WB-отчёты: репрайсер, воронку, ABC, рекламу и «Неделя к неделе», включая настоящие XLSX-выгрузки.

**Architecture:** Сохраняем существующие WB cache/builders и исправляем только общие точки, которые создают ошибки: календарную привязку period cards, лишний daily-detail gate ABC и позднюю инициализацию интерактивных таблиц. XLSX строит уже существующий stdlib writer на backend; frontend передаёт нормализованные строки текущего отчёта без новой зависимости.

**Tech Stack:** FastAPI, Pydantic, Python stdlib ZIP/XML, React, TypeScript, Vitest, pytest.

**Spec:** `/Users/ilagulakin/Desktop/Work/OgniWB/ТЗ/TZ-WB.md`

## Global Constraints

- Менять только WB; Avito не затрагивать.
- Метрики строить из текущих WB/финансовых cache и не подменять demo-данными.
- Новые зависимости не добавлять.
- Сохранить Vella production shell и существующие tooltip/help patterns.
- Любой импорт/экспорт и авторизация остаются защищёнными текущими permission checks.

## Review Focus

- Период заканчивается вчера: карточка «Сегодня» должна быть нулевой, а не переименованным последним закрытым днём.
- В ABC нет дневной детализации одного источника: готовый aggregate cache должен отрисоваться без пятиминутного ожидания.
- XLSX получает пустые, слишком большие или управляющие XML-символы: endpoint должен отказать предсказуемо либо сформировать открываемый файл.
- React-таблица появляется после legacy init: сортировка и sticky должны подключаться после ready state.
- Фильтр рекламы/недель применяется при batch-render: скрытые строки и новые порции должны сохранять выбранный фильтр.

---

### Task 1: Backend report correctness and latency

**Files:**
- Modify: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/app/routers/wb_reports_bff.py`
- Modify: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/app/repricer_tasks.py`
- Modify: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/app/routers/wb_repricer_bff.py`
- Test: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/tests/test_wb_reports_bff.py`
- Test: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/tests/test_repricer_tasks.py`

**Interfaces:**
- Consumes: existing weekly balance points and aggregate report caches.
- Produces: period cards with `openCount` and `cartCount`; ABC jobs that build directly from aggregate cache; invalidated repricer SKU snapshot version.

- [x] **Step 1: Write failing backend tests**

Add behavior tests proving actual Moscow today/yesterday selection, funnel counters, direct ABC job enqueue, and no worker daily-detail wait for ABC.

- [x] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_wb_reports_bff.py -k 'period_cards or abc_job_builds_from_aggregate_cache' tests/test_repricer_tasks.py -k 'abc_report_task_builds_from_aggregate_cache'`

Expected: FAIL on wrong date/counters and ABC waiting/refresh path.

- [x] **Step 3: Implement the shared fixes**

Use a patchable Moscow clock in `_build_period_cards`, aggregate `openCount`/`cartCount`, remove only ABC from strict daily-detail maps, and bump `SKU_LIST_SNAPSHOT_VERSION` once.

- [x] **Step 4: Run tests to verify GREEN**

Run the same focused pytest command.

Expected: PASS.

- [x] **Step 5: Commit backend task**

Commit message: `fix: correct and unblock WB report metrics`.

### Task 2: Real report XLSX endpoint

**Files:**
- Modify: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/app/repricer_nomenclature_excel.py`
- Modify: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/app/routers/wb_reports_bff.py`
- Test: `/Users/ilagulakin/Desktop/Work/OgniWB/.worktrees/backend-wb-release-20261001/tests/test_wb_reports_bff.py`

**Interfaces:**
- Consumes: `build_xlsx(rows, sheet_name)` and authenticated `settings:read` actor.
- Produces: `POST /api/wb/reports/table.xlsx` accepting bounded `{reportKind, headers, rows}` and returning an XLSX attachment.

- [x] **Step 1: Write failing endpoint test**

Assert permission enforcement, XLSX content type/ZIP signature, sanitized XML text and row/column limits.

- [x] **Step 2: Run test to verify RED**

Run: `pytest -q tests/test_wb_reports_bff.py -k table_xlsx`

Expected: FAIL with missing route/signature.

- [x] **Step 3: Implement minimal bounded export**

Extend the existing stdlib writer with an optional safe sheet name and add one strict Pydantic request model plus route.

- [x] **Step 4: Run test to verify GREEN**

Run: `pytest -q tests/test_wb_reports_bff.py -k table_xlsx`

Expected: PASS.

- [x] **Step 5: Commit backend task**

Commit message: `feat: export WB report tables to XLSX`.

### Task 3: Customer-facing WB report controls and metrics

**Files:**
- Create: `frontend/src/features/wb-reports/tableXlsx.ts`
- Create: `frontend/src/features/wb-reports/tableXlsx.test.ts`
- Modify: `frontend/src/features/wb-reports/types.ts`
- Modify: `frontend/src/features/vella-parity/VellaHtmlParityPage.tsx`
- Modify: `frontend/public/vella-production.html`
- Regenerate: `frontend/src/features/vella-parity/vellaProductionSnapshot.generated.ts`
- Test: `frontend/src/features/vella-parity/wbReleaseContracts.test.ts`

**Interfaces:**
- Consumes: Task 1 period-card fields and Task 2 XLSX endpoint.
- Produces: `downloadReportTableXlsx(...)`; funnel stages; ABC/ads export; ads dimension filters/sorting/sticky columns; week paired units/rubles and working chips; explicit cost upload label and KPI trend arrows.

- [x] **Step 1: Write failing frontend behavior tests**

Test the XLSX request/download boundary and render real components/DOM helpers to prove funnel counters, filter attributes, paired week metrics, post-mount table enhancement, and explicit cost-upload copy.

- [x] **Step 2: Run tests to verify RED**

Run: `npm test -- --run frontend/src/features/wb-reports/tableXlsx.test.ts frontend/src/features/vella-parity/wbReleaseContracts.test.ts`

Expected: FAIL on missing helper/UI behavior.

- [x] **Step 3: Implement the minimum React/legacy bridge changes**

Reuse `applyGenericReportFilter`, `enhanceReportTableSorting`, `installAdsStickyIdentityColumns`, existing report rows, and existing nomenclature import; add no new table/filter framework.

- [x] **Step 4: Regenerate the Vella snapshot**

Run the repository's existing Vella snapshot generator.

Expected: generated snapshot matches `frontend/public/vella-production.html`.

- [x] **Step 5: Run focused tests and typecheck**

Run: `npm test -- --run frontend/src/features/wb-reports/tableXlsx.test.ts frontend/src/features/vella-parity/wbReleaseContracts.test.ts && npm run typecheck`

Expected: PASS.

- [x] **Step 6: Commit frontend task**

Commit message: `fix: finish WB report customer workflows`.

### Task 4: Browser and metric verification

**Files:**
- Create: `docs/WB_RELEASE_VERIFICATION_2026-10-01.md`

**Interfaces:**
- Consumes: completed backend/frontend worktrees and authenticated browser sessions.
- Produces: reproducible evidence for repricer, funnel, ABC, ads, week-over-week, download files, timings, and CodeMP comparisons.

- [x] **Step 1: Run focused backend/frontend verification suites**

Expected: all newly added and directly affected tests pass.

- [ ] **Step 2: Start local backend/frontend and inspect every requested WB route with browser automation**

Expected: no blank route; tables populate; filters/sorts/sticky/export work; ABC initial report no longer waits for daily detail.

- [ ] **Step 3: Compare the same account/date range with CodeMP**

Record exact matching values or the source/basis difference for orders, sales, revenue, buyout, COGS and profit.

- [x] **Step 4: Record timings and infrastructure hot points**

Separate measured backend/cache/UI bottlenecks from unproven hosting hypotheses.

- [x] **Step 5: Commit verification evidence**

Commit message: `docs: verify WB release workflows`.
