# Satorna WB

Source code of the Satorna marketplace operations platform.

## VPS release preparation — 2026-10-08

Full current frontend/backend and both Chrome extensions are included. See [VPS setup and integration checks](deploy/README.md) for the production Compose stack, private configuration, fresh database, editable WB server URL and encrypted OpenAI key entry. Local databases, live keys and runtime order artifacts are not distributed.

The historical production snapshot below is not evidence of deployment of this branch. This release must pass its CI gates and then be verified with your DNS/TLS and authorized marketplace browser sessions.

## Structure

- `frontend/` — Vite, React and TypeScript application with Vercel API routes.
- `backend/` — FastAPI modular monolith, Alembic migrations and tests.
- `SATORNA_DELEGATION_PROMPTS/` — standalone task prompts.
- `SATORNA_ARCHITECTURE_HANDOFF.md` — authoritative architecture and rollout state.
- `SATORNA_SPEC_AUDIT.md` — specification audit.

## Production snapshot

Published on 2026-09-08:

- backend application revision: `b96e602`;
- backend documentation revision: `e5c0b5b`;
- production image: `sha256:0aad3379406afd344b1ede23859350752f40efe9a2e8d19a988f8431f4040fa1`;
- Alembic head: `20260905_0060`;
- the frontend tree is unchanged from the preceding `main` snapshot.

Secrets, environment files, runtime state, caches and production backups are not
part of this repository.

## Frontend

Downloading the repository does not include running API services, accounts,
marketplace credentials or a database. For a new full installation, follow
[VPS setup](deploy/README.md). Starting only `npm run dev` serves the interface;
data tabs require a configured backend and report worker.

For the complete existing local development stack (from the repository root):

```bash
.venv/bin/python backend/ops/run_local_project.py
```

This starts Redis on loopback port 59999, a single read-only report worker, the
API on 58017 and frontend on 5177. It verifies readiness and stops its own child
processes on Ctrl+C or failure. Existing `.local-preview/preview.sqlite` data and
keys are preserved. No browser, scheduler or marketplace write worker is opened.
Logs and persistent local queue state stay in `.local-preview/`. Stop any old
local servers before starting; occupied ports fail without killing other apps.
The local launcher refuses to create a replacement when the account database is
missing or unreadable. Restore that file from backup instead. Only for a genuinely
new installation, start `ops/current_cost_local_preview.py --init-local-database`
from `backend/` and register the first account before using the full launcher.
Local preview authentication requires persistent storage: database failures return
503 rather than accepting registrations or sessions in temporary process memory.
The local SQLite file uses WAL with a 30-second busy timeout, allowing report
reads and API writes to coexist. Back up a running database through SQLite's
backup API, not by copying only `preview.sqlite` without its live WAL sidecar.
Running only Vite and the API is insufficient for refreshing WB reports.

For real WB repricing in this existing local account, use the explicit live mode:

```bash
.venv/bin/python backend/ops/run_local_project.py --wb-live
```

This adds a dedicated WB repricer worker and a scheduler that polls explicitly
assigned strategies every five minutes, respecting the account's execution
interval. Assigning a strategy in a product row also requests execution
immediately. Enable the account's automatic price application setting for
scheduled changes (and Night Median automatic application for its price changes).
The read-only report worker and Avito restrictions remain in place. The default
launch command stays read-only. Live mode uses real WB uploads and confirmation,
never local price simulation; minimum price, step, data completeness, schedule,
and all other existing pricing guards still apply. Starting live mode does not
assign products or change these business settings.
Live mode requires exactly one connected WB account in the local database.
Every upload rechecks its bound credential and the current explicit strategy and
automation status of every product; unassigned or paused products are rejected.

The launcher checks that Redis actually runs on the current CPU, rather than
trusting an old Intel Homebrew symlink on Apple Silicon. If system Redis cannot
run, install a compatible Redis or build the official core in
`.local-preview/tools/redis-stable` following the
[Redis source-build instructions](https://redis.io/docs/latest/operate/oss_and_stack/install/install-redis/install-redis-from-source/).
The launcher prefers that project-private binary; it never updates Homebrew.

To run only the frontend against an already running local preview API:

```bash
cd frontend
npm ci
VITE_API_PROXY_TARGET=http://127.0.0.1:58017 npm run dev
```

Quality checks:

```bash
cd frontend
npm run typecheck
npm run test
npm run build
```

## Frontend deployment

The current application runs at https://app.comcookie.store. Nginx on the VPS
serves the built frontend and proxies `/api/*` to FastAPI on the same origin.
The root domain https://comcookie.store redirects application pages to it.

Vercel hosts the compatibility gateway configured in
`deploy/vercel-gateway.json`, not a standalone replacement for the backend.
See [the deployment guide](deploy/README.md) for the complete stack and current
production routing. Private settings, database contents and keyring volumes
must be preserved separately when updating an existing installation.
