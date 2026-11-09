# T2 — canonical account-scoped Avito statistics

Date: 2026-09-10. Branch: `codex/t2-account-discovery`, following metadata discovery `90c9fa70eaecec046861b61122f94e6029e19174` on ROOT base `a4b5fe5e478c8a0887f2f6c3f1b653c3d99579f0`.

## Delivered boundary, not activation

Six new files only: `app/avito/account_stats.py`, `app/avito/account_stats_http.py`, three `tests/test_account_avito_stats*.py` files and this report. Existing stats/parser/router, auth, config, schema, jobs, frontend and provider behavior are unchanged. ROOT approved this independent slice while T1 shared work continued.

Composition (ROOT owns registration/config/pool wiring):

```python
AccountAvitoStatsService(
    engine=dedicated_postgresql_engine,
    keyring_loader=explicit_keyring_loader,
    client_factory=BoundedAvitoTotalsClient,
    enabled_for=explicit_org_account_allowlist,  # default is deny
)
make_account_avito_stats_router(service_dependency=zero_argument_service_factory)
```

`GET /api/v2/avito/accounts/{internal_account_id}/statistics?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD`. Both dates are mandatory, exact ISO dates, inclusive window at most 270 days (existing application contract, not a newly claimed provider limit). Duplicate/unknown query keys, external/account/org overrides and invalid internal IDs are rejected. Every response is `Cache-Control: no-store`; auth 401 is retained; safe fixed codes only for 403/422/503.

Wire follows ROOT's approved shape:

```text
data:
  marketplaceAccountId: positive internal integer
  provider: avito
  externalAccountId: exact canonical positive decimal string
  dateFrom, dateTo: ISO dates
  status: synced | partial
  rows: [{itemId, sourceStatus, metrics}]
  daily: [{date, metrics}]
metrics: impressions, views, contactsMessenger, contacts, contactsShowPhone,
         contactsShowPhoneAndMessenger, favorites, spendKopecks, orders, buyouts
```

Every non-null metric is a decimal string, including counts; explicit zero is `"0"`. No names, credential fields, URLs, raw provider errors or diagnostics escape. At most one totals row; daily rows are bounded by the requested inclusive interval and have unique in-range dates. Every account/items/daily owner must equal the canonical external ID. This is not item-grain statistics or a newly persisted source snapshot.

## Authorization and lifecycle

```text
Root A: fresh Session/physical transaction
  actual user + membership + login + cabinet:read + exact internal account scope
  lock canonical Avito account; capture ingestion_binding_version
  resolve ONLY encrypted avito_oauth_access using explicit keyring
  capture original UserSessionPrincipal / ExpectedAccountBinding / ExpectedCredential
  ONE existing public publication guard + closing incarnation check
  physical COMMIT; Session CLOSE
                    |
                    v
  redacted ResolvedCredentialForFetch -> actual bounded provider edge
  reveal accessToken/expiresAt only here; exactly one totals POST
                    |
                    v
Root B: new Session/physical transaction
  SAME original principal/account/credential ID/generation/schema/expiry
  SAME captured ingestion_binding_version
  ONE public guard, closing live checks, physical COMMIT; Session CLOSE
                    |
                    v
  safe DTO may return
```

No DB transaction spans HTTP. A real second connection obtains the account lock with `NOWAIT` inside the fake provider boundary in tests. Root B never replaces authority with a newly resolved credential or decrypts again. Scope/permission/credential/account changes during HTTP discard fetched data. Database and key-loader failures never use plaintext, user/org cache, files, environment credentials, OAuth client credentials, refresh, or token-exchange fallback.

The incarnation check runs before the shared final guard callback and under the locked account. The shared guard must remain the last effective commit callback. This slice does not modify shared guard internals. Hostile additional listener-order injection and actual expiry crossing specifically inside the final callback were not added to this slice's PG suite; existing shared-guard gates remain prerequisites.

## Transport and parser reuse

`BoundedAvitoTotalsClient.fetch_stats(AvitoStatsFetchRequest)` consumes the existing singleton `accountIds=[canonical_external_id]`, `grouping="totals"` contract. It composes `LiveAvitoStatsClient._accounts` / `_stats_totals` and existing metric/date parsers with a post-only bounded HTTP edge. No legacy `fetch_stats()` invocation that would create an unbounded client; no self-discovery, items, public images or pagination loops.

Exactly one POST to `https://api.avito.ru/stats/v2/accounts/{id}/items`, original dates/metrics, `limit=1`, `offset=0`; never an automatic retry. Redirects/environment proxies are disabled. Only HTTP 200, JSON and identity content encoding are accepted. Error bodies are not consumed. The stream is capped at 4 MiB, including undeclared length; duplicate JSON keys, non-finite values and mismatched Content-Length fail closed. HTTPX per-operation timeout is 20 seconds; an elapsed 30-second budget is checked between chunks and after EOF (not a strict process-wide 30-second deadline for a blocked operation).

Before the existing parser, reject floats/bools/negative/string metric values, duplicate slugs, mixed top-level/grouped metrics, mixed totals/day groupings, duplicate/out-of-period days and declared row count disagreement. Null metric entries are removed so the legacy `int(value or 0)` helper cannot invent observed zeros; missing metrics remain null. No new aliases or aggregation formulas are introduced. A provider payload indicating an incomplete page is unavailable, not silently current. No extra page is fetched speculatively.

## Verification evidence

All execution used synthetic data, clean environment and an existing network-deny sandbox. No real keys/.env/marketplace credentials were read; no provider request, production connection, OAuth refresh, price action, push or deploy occurred.

Python: `/tmp/satorna-backend311-20260909/bin/python`.
Sandbox: `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/.superpowers/sdd/2026-09-09-publication-guard/task-1-offline.sb`.
Commands ran from this worktree's `backend` directory with `env -i`, minimal PATH/TMPDIR/USER, `PYTHONDONTWRITEBYTECODE=1`; PG run additionally set `PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1`.

| Gate | Actual result |
|---|---|
| Initial DTO/router RED | collection exit 2: missing new module |
| Transport RED | collection exit 2: missing `BoundedAvitoTotalsClient` |
| Service RED | collection exit 2: missing `AccountAvitoStatsService` |
| Ambiguous/incomplete payload regression RED | 2 failed / 21 passed; then fixed new boundary validation |
| `pytest -q --tb=short tests/test_account_avito_stats.py tests/test_account_avito_stats_transport.py` | Final 43 passed, 2 existing dependency deprecation warnings, 0.62s, exit 0 |
| Frozen PostgreSQL first run | 18 passed / 1 failed, 17.58s, exit 1; corruption fixture's 16-byte ciphertext was correctly rejected by existing SQL length CHECK before service execution |
| Same PostgreSQL19 after fixture correction | 19 passed, 4.53s, exit 0; corruption uses bit flip preserving ciphertext length, no schema/guard weakening |

The initial transport positive fixture used an already-consumed HTTPX response; changed only that synthetic fixture to a real byte stream to exercise `iter_raw`. No production accommodation for a consumed/unbounded response was added.

Final five Python files: Ruff check exit 0; `compileall -q` exit 0 with cache redirected to `/tmp/satorna-t2-account-discovery-pycache`. Independent local critic pass rechecked original scope, actual date-window helper, parser coercions, closing transaction boundaries and unverified gates. The configured local `preflight-critic/SKILL.md` is absent, so no claim of executing that missing skill or an external review. No unrelated tracked file changed; the preexisting untracked `.superpowers/t2-repository-offline.sb` remains untouched/uncommitted.

PostgreSQL used ROOT's exclusive `AvitoStats19` slot after T1 quota12. Exactly the frozen 19 cases ran: happy path, default deny, six initial authority failures, six during-fetch authority changes, both physical commit failures, cross-org/account and key-loader failure. Runtime role is the existing allocator's `NOSUPERUSER NOBYPASSRLS` role, not the final dedicated API pool. The allocator disposes engines and verifies exact created DB/role are absent in `pg_database`/`pg_roles` after cleanup. Pytest exited naturally; slot explicitly released to ROOT/T1. No PG resources retained.

## Integration gates and limitations

1. ROOT/T1 must wire the dormant route/service and confirm the actual least-privilege API role has only required metadata/credential read-and-lock authority. Generic restricted-role test evidence is not that final role's acceptance gate.
2. A real encrypted `avito_oauth_access` with its actual provider expiry is required. No invented five-minute TTL, no refresh/exchange/publication wiring was added here. Missing access token remains unavailable.
3. ROOT's org/account activation and request concurrency/admission policy is required before enabling synchronous calls. This slice bounds each request; it does not claim a durable global Avito quota owner.
4. Existing parser semantics are reused; strict boundary may reject provider variants not supported by synthetic fixtures. Actual Avito/provider parity is unverified, not a reason to enable live credentials for tests.
5. Credential replacement/generation change is covered. A separate full maintenance reencryption workflow during HTTP, final-listener injection and final dedicated-role bootstrap tests were not run here.
6. This slice does not complete durable repricer, sources, finance decisions, final profit or live integration rollout. Profit/business-rule blockers remain unchanged.

Rollback: leave route unregistered/allowlist false, or remove only this new factory's registration. No DDL, data migration, provider mutation or old-route rollback is needed. No existing flags changed.

## Review follow-up — daily completeness

ROOT review found that removing null metrics before legacy daily aggregation
could publish `day1=5 + day2=null` as a complete total of 5, dropping the null
day. The new boundary now fails closed for daily groups unless all requested
dates exist and every day has the same nonempty set of non-null, valid integer
metrics. Missing dates, missing metrics and explicit null values are not zero.
Explicit totals nulls still remain null; complete daily values 5 and 0 still
produce total `"5"` and daily `"5", "0"`. No old parser or formula change.

Regression RED: 3 failed / 1 passed (null day, missing metric, missing date).
Final offline DTO/transport: 47 passed, 2 existing warnings, 0.60s, exit 0.
Pure edge change only; no further PostgreSQL run or slot needed. This supersedes
the unconditional null-entry filtering description above for **daily** groups.
