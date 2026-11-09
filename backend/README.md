# ogni-vella-backend

Backend repo for the WB-first Vella / Ogni INDEEPA replacement.

Status: Sprint A/B transition. Source discovery, freshness policies and price apply mapping are implemented; real WB mutations remain feature-flagged.

## Source of truth

Product requirements, handoff docs, source registry and client questions live in the product repo:

```text
/Users/dima/Downloads/Projects/SAAS для Огней
```

Read first in the product repo:

- `docs/handoffs/wb-backend-programmer-brief.md`
- `docs/handoffs/wb-backend-dev-package.md`
- `docs/handoffs/wb-backend-data-handoff-index.md`
- `docs/handoffs/wb-backend-source-registry.md`
- `docs/handoffs/wb-backend-formula-catalog.md`
- `docs/handoffs/wb-backend-reuse-dependency-map.md`
- `docs/open-questions-current.md`
- `docs/api-contracts/`
- `docs/specs/indeepa-wb-replacement/`

## Local checks

One non-destructive local release gate runs the backend/frontend suites, exact
legacy-failure comparison, single-head check, migration roundtrip and existing
contract checks:

```bash
.venv/bin/python ops/release_gate.py
```

The frontend is discovered at the canonical workspace path; set
`SATORNA_FRONTEND_DIR` only for another checkout. Docker must be running with
`postgres:16-alpine` available. The runner creates one uniquely named
`satorna-gate-*` PostgreSQL container/database, prints its exact recovery
command, and removes only that container. It preserves the frontend generated
snapshot and never prints inherited secrets or environment values.

The gate runs the empty database chain directly: `upgrade head → downgrade -1 →
upgrade head`, without stamping or skipping migrations. Revision `0019` accepts
the prompt column owned by `0017`, repairs its absence in historical schemas,
and preserves operator prompts on downgrade. Incompatible existing column
definitions fail explicitly. Real PostgreSQL regression tests are in
`tests/test_empty_database_migrations.py` (local `initdb`, `pg_ctl`, `createdb`
binaries required). The final line is `SATORNA_GATE_SUMMARY=<json>`; any baseline ID
change, new failure, interrupted check or cleanup failure returns non-zero.

From this repo root (Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .[test]
.\.venv\Scripts\python -m pytest -q tests backend_contracts\tests
```

Optional syntax check:

```powershell
$files = Get-ChildItem app,backend_contracts\vella_wb_19_05 -Recurse -Filter *.py
foreach ($f in $files) { .\.venv\Scripts\python -m py_compile $f.FullName }
```

Generate contract models from OpenAPI (no handwritten drift for generated layer):

```powershell
.\.venv\Scripts\python -m datamodel_code_generator `
  --input contracts/openapi/v1.yaml `
  --input-file-type openapi `
  --output app/contracts/vella_wb_19_05_generated.py `
  --output-model-type pydantic_v2.BaseModel `
  --target-python-version 3.11
```

## Local run

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open:

- `GET /health`
- `GET /docs`

## Auth (email + password)

Auth endpoints:

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`

Example body:

```json
{
  "email": "admin@vella.local",
  "password": "AdminPass123!"
}
```

Response returns Bearer token (`accessToken`) and TTL (`expiresIn`).
Protected endpoints require `Authorization: Bearer <token>`.
Legacy `x-actor-*` auth headers are removed.
Session scheme:

- access token: short-lived (default 15 minutes);
- refresh token: `HttpOnly + Secure` cookie;
- refresh rotation on each `/api/v1/auth/refresh`;
- active sessions list and device logout in cabinet endpoints.

Default local permission profiles matrix v1:

- `viewer`
- `settings_editor`
- `price_sender`
- `finance_viewer`
- `admin`

Default local demo users (dev bootstrap):

- `viewer@vella.local / ViewerPass123!`
- `editor@vella.local / EditorPass123!`
- `sender@vella.local / SenderPass123!`
- `finance@vella.local / FinancePass123!`
- `admin@vella.local / AdminPass123!`

Legacy business aliases are mapped to permission profiles during migrations/imports: `Owner -> admin`, `Sales -> price_sender`, `Production -> settings_editor`.

Personal cabinet (LK) endpoints:

- `GET /api/v1/cabinet/me`
- `GET /api/v1/cabinet/team/users`
- `POST /api/v1/cabinet/team/users`
- `GET /api/v1/cabinet/sessions`
- `GET /api/v1/cabinet/sessions/current`
- `POST /api/v1/cabinet/sessions/revoke-others`
- `POST /api/v1/cabinet/sessions/{sessionId}/revoke`
- `GET /api/v1/cabinet/integrations`
- `PUT /api/v1/cabinet/integrations/{provider}`
- `GET /api/v1/cabinet/audit/events`
- `GET /api/v1/cabinet/preferences`
- `PUT /api/v1/cabinet/preferences`

## Sprint A demo quick run

```powershell
.\.venv\Scripts\python -m pytest -q tests backend_contracts\tests
```

Key demo endpoints:

- `GET /health`
- `GET /api/v1/wb/account/health`
- `GET /api/v1/wb/account/health?scenario=missing_token`
- `GET /api/v1/source-registry?module=wb`
- `GET /api/v1/wb-reports/pnl`
- `GET /api/v1/wb-reports/ads/performance?groupBy=campaign`
- `GET /api/v1/wb-reports/ads/performance?groupBy=sku`
- `GET /api/v1/wb-reports/rnp`
- `GET /api/v1/wb-reports/abc?filters=status=locomotive`
- `GET /api/v1/wb-repricer/sku/FBBT_42/price-guard`
- `GET /api/v1/wb-reviews/wb-review-006/approval?rating=2`

## Phase A1 infra skeleton

One-command local stack:

```powershell
docker compose up --build
```

This starts:

- `postgres`
- `redis`
- `api` on `http://localhost:8000`
- `worker`
- `beat`

Stop everything:

```powershell
docker compose down
```

If you want to reset the local PostgreSQL volume too:

```powershell
docker compose down -v
```

Manual split run is still available.

Start only local dependencies:

```powershell
docker compose up -d postgres redis
```

Run baseline migration against local PostgreSQL:

```powershell
 $env:VELLA_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5433/vella_backend"
.\.venv\Scripts\python -m alembic upgrade head
```

Start worker and beat (separate terminals):

```powershell
.\.venv\Scripts\python -m celery -A app.infra.celery_app:celery_app worker --loglevel=info
.\.venv\Scripts\python -m celery -A app.infra.celery_app:celery_app beat --loglevel=info
```

## Current endpoints

- `GET /health`
- `GET /api/v1/wb/account/health`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `GET /api/v1/cabinet/me`
- `GET /api/v1/cabinet/team/users`
- `POST /api/v1/cabinet/team/users`
- `PATCH /api/v1/cabinet/team/users/{userId}/permission-profile`
- `GET /api/v1/cabinet/sessions`
- `GET /api/v1/cabinet/sessions/current`
- `POST /api/v1/cabinet/sessions/revoke-others`
- `POST /api/v1/cabinet/sessions/{sessionId}/revoke`
- `GET /api/v1/cabinet/integrations`
- `PUT /api/v1/cabinet/integrations/{provider}`
- `GET /api/v1/cabinet/audit/events`
- `GET /api/v1/cabinet/preferences`
- `PUT /api/v1/cabinet/preferences`
- `GET /api/v1/source-registry`
- `GET /api/v1/source-registry/blockers`
- `GET /api/v1/source-registry/blockers/paginated`
- `GET /api/v1/source-registry/blockers/catalog`
- `GET /api/v1/source-registry/blockers/catalog/paginated`
- `GET /api/v1/source-registry/entries`
- `GET /api/v1/source-registry/entries/paginated`
- `GET /api/v1/source-registry/formulas`
- `GET /api/v1/source-registry/formulas/paginated`
- `GET /api/v1/source-registry/entries/unresolved-blockers`
- `POST /api/v1/settings/versions`
- `GET /api/v1/settings/versions`
- `GET /api/v1/settings/versions/{version}`
- `GET /api/v1/settings/versions/{toVersion}/diff?fromVersion={version}`
- `POST /api/v1/settings/versions/{version}/activate`
- `GET /api/v1/audit/events`
- `POST /api/v1/sync-jobs`
- `GET /api/v1/sync-jobs`
- `POST /api/v1/sync-jobs/{jobId}/run-noop`
- `POST /api/v1/sync-jobs/noop-tick`
- `POST /api/v1/wb-repricer/actions/price-apply`
- `POST /api/v1/wb-repricer/actions/price-apply-placeholder`
- `GET /api/v1/wb-repricer/wb23/freshness-policies`
- `GET /api/v1/wb-repricer/sku/{articleId}/price-metrics`
- `POST /api/v1/wb-repricer/sku/{articleId}/margin-preview`
- `POST /api/v1/wb-repricer/sku/{articleId}/pmin-pmax/validate`
- `GET /api/v1/wb-repricer/uploads/{uploadId}/apply-status`
- `GET /api/v1/wb-reports/pnl`
- `GET /api/v1/wb-reports/ads/performance`
- `GET /api/v1/wb-reports/rnp`
- `GET /api/v1/wb-reports/abc`
- `GET /api/v1/wb-repricer/sku/{articleId}/price-guard`
- `GET /api/v1/wb-reviews/{reviewId}/approval?rating=2`
- `GET /api/v1/wb-discovery/repricer/source-map`
- `GET /api/v1/wb-discovery/repricer/readiness`
- `GET /api/v1/wb-discovery/repricer/readiness/adapter-result`
- `POST /api/v1/wb-discovery/repricer/probes/read-prices`
- `POST /api/v1/wb-discovery/repricer/probes/task-history`
- `POST /api/v1/wb-discovery/repricer/probes/upload-lifecycle`

## Rules

- WB API calls go only through typed adapters.
- Unknown sources return typed `blocked`, `unknown`, `stale`, `partial` or `manual_fallback` states.
- Risky actions use `draft -> preview/diff -> approval -> commit -> audit`.
- Common API contracts are centralized in `app/contracts/`:
  - error envelope;
  - pagination envelope;
  - money in integer kopecks;
  - UTC-aware datetime fields;
  - shared adapter result statuses.
- OpenAPI-generated contract models live in `app/contracts/vella_wb_19_05_generated.py`.
- Source registry is seeded from handoff markdown files and promoted to database tables:
  - `source_registry_entries`;
  - `source_registry_blockers`;
  - `source_registry_formulas`.
- Control-plane A4 tables:
  - `cp_settings_versions`;
  - `cp_audit_events`;
  - `cp_sync_jobs`.
- Real price apply route exists, but live WB mutation is enabled only when `VELLA_REAL_PRICE_APPLY_ENABLED=true`.
- Adapter mode is controlled by `VELLA_WB_API_MODE` (`fake` by default in code, `real` when passed through `.env` / docker compose for live WB calls).
- `docker compose` now forwards all WB adapter env vars into `api`, `worker`, and `beat`. To run live WB data, create `.env` from `.env.example`, fill the WB tokens, and keep `VELLA_WB_API_MODE=real`.
- Money is integer kopecks.
- Dates are ISO 8601 UTC.
- Raw tokens and raw external payloads must not leak into logs or API responses.

## Publish to GitHub

`gh` CLI is not installed in the current local environment. Create an empty GitHub repository named `ogni-vella-backend`, then run:

```bash
git remote add origin https://github.com/<owner>/ogni-vella-backend.git
git push -u origin main
```
