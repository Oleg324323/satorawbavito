# Runtime report route contract implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four runtime WB report GET operations have the same real handler in dispatch and OpenAPI, without changing domain handlers or standalone reference contracts.

**Architecture:** `app.main` already mounts real `wb_reports_sprint_d` before the reference adapter. `wb_19_05` then mounts four duplicate stubs: runtime dispatch selects the first real route, but OpenAPI describes the last stub and loses PNL's `source` parameter. Build a private APIRouter from a filtered new list of the reference router's existing direct routes, then include that private router. Do not mutate the reference router or rely on include_router flattening. Do not suppress warnings or invent operation IDs to conceal duplicate operations.

**Tech Stack:** Existing FastAPI/APIRouter, pytest/httpx; no dependencies, DB or service needed.

**Spec:** Binding local decision in this document, derived from actual `app/main.py`, `app/routers/wb_19_05.py`, real `wb_reports_sprint_d.py` and `backend_contracts/vella_wb_19_05/router.py`. Those sources must be read before edits. No separate spec is necessary for this bounded diagnosed registration fix.

## Global constraints

- T1 worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform` only; no external network/providers/Redis/production/.env/real credentials, push/deploy or host changes.
- No domain report math/data/provider adapter edits, no frontend/serverless/static generated contract changes. The standalone `vella_wb_19_05.router` and its source registry/repricer/reviews routes remain intact.
- Tests use own backend/.venv and scrubbed environment under network/secret-file-denying sandbox. Import/generate OpenAPI without production startup/lifespan. HTTP checks use synthetic in-process stubs and no background services.
- Keep the real runtime handler/permissions/query parameters unchanged. No broad stub retirement or activation. Duplicate findings outside these exact four paths return to controller, not automatic route removal.

## Task 1: Exclude four superseded GET stubs from runtime adapter

**Files:**
- Modify `backend/app/routers/wb_19_05.py` only reference inclusion boundary.
- Create `backend/tests/test_runtime_report_route_contract.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-runtime-report-route-handoff.md`.

**Consumes:** real first-match FastAPI registrations and standalone reference router; no business service contract changes.

**Produces:** exactly one GET registration in runtime app for each of:

```python
SUPERSEDED_REPORT_PATHS = frozenset({
    "/api/v1/wb-reports/pnl",
    "/api/v1/wb-reports/ads/performance",
    "/api/v1/wb-reports/rnp",
    "/api/v1/wb-reports/abc",
})
```

- [ ] Step1 write RED: full runtime app routes have one operation for each exact path/GET, endpoint is real module's corresponding function, OpenAPI PNL contains `source` with preliminary default and actual accepted enum, all actual date/group/filter parameters unchanged. Compare expected parameter schema to standalone app containing only real report router, not handwritten duplicate schemas. Capture original four duplicate warning IDs as defect evidence, not accepted warnings.

```python
from fastapi.routing import iter_route_contexts
for path in SUPERSEDED_REPORT_PATHS:
    matches = [route for route in iter_route_contexts(app.routes)
               if getattr(route, "path", None) == path
               and "GET" in (getattr(route, "methods", None) or ())]
    assert len(matches) == 1
    assert matches[0].endpoint.__module__ == "app.routers.wb_reports_sprint_d"
assert app.openapi()["paths"]["/api/v1/wb-reports/pnl"]["get"]["parameters"] == real_only_schema["paths"]["/api/v1/wb-reports/pnl"]["get"]["parameters"]
```

- [ ] Step2 run expected assertion RED before implementation. Current installed FastAPI include_router stores _IncludedRouter instead of flattening; iter_route_contexts is its actual OpenAPI traversal. Use that traversal for test-only effective route counts. In application code create a private APIRouter with a newly filtered direct route list BEFORE inclusion; reference_router is currently plain APIRouter() with seven direct routes and no custom router options/lifespan. Preserve source router object/list/route objects and all other local/reference operations. Do not use private FastAPI internals in runtime code. Use exact path+method matching; never delete mixed GET/POST by path-only filtering, change main ordering or clear caches as a fix. Tests compare retained standalone/runtime operation metadata as well as source inventory.

```python
_runtime_reference_router = APIRouter(routes=[
    route for route in reference_router.routes
    if not (getattr(route, "path", None) in SUPERSEDED_REPORT_PATHS
            and getattr(route, "methods", None) == {"GET"})
])
router.include_router(_runtime_reference_router)
```

Constants may use private naming to avoid public API expansion; no route helper abstraction needed. The test must fail if future mixed-method registration restores a duplicate GET; such a change requires explicit review rather than silently deleting the other method.

- [ ] Step3 GREEN plus synthetic in-process HTTP checks. Directly stub the real report module's actor/permission/data boundaries: unauthorized request remains denied before report data access; authorized request reaches the same real handler with provided source/date values. Never invoke seller APIs, file/default business stores or startup jobs. Instantiate TestClient without application lifespan if needed and close naturally. Independently mount original reference router into a fresh app and verify all seven original paths/handlers remain, including the four reference report stubs. Verify adapter's source-registry endpoints and non-report reference operations unchanged.

```sh
.venv/bin/python -m pytest -q tests/test_runtime_report_route_contract.py
.venv/bin/python -m compileall -q app/routers/wb_19_05.py tests/test_runtime_report_route_contract.py
git diff --check
```

Run relevant existing report API/contract tests selected by file discovery once; preserve any preexisting failures as exact baseline evidence, no skips or fixture weakening. Scoped Ruff may use approved lint-only `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check`; never use it as application runtime.

- [ ] Step4 self-review, bounded commit `fix: align runtime report routes with OpenAPI`, exact commands/exit codes/RED→GREEN handoff, independent task review. This closes only duplicate runtime registration, not complete generated frontend contract parity or full suite baseline.

## Preflight

Single task touches only runtime composition and its tests/report. Real handlers and standalone contracts are read-only dependencies. Unique runtime operation and untouched standalone reference router assertions jointly prevent overbroad deletion. Local source count currently seven reference routes; test source-preservation should compare the exact pre-import route inventory rather than freeze seven forever. No secret/network/production action is required.
