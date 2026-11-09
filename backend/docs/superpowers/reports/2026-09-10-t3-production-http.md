# Dormant Production HTTP Factory

T3, 2026-09-10. ROOT-approved bounded adapter over existing Production P1.
No shared bootstrap/config/flags/grants/schema/domain or prototype changes.

New `app/orders/production_http.py` exports `make_production_router` with required
positive `max_request_bytes` and a default-deny runtime dependency. The router is
NOT registered. Explicit `ProductionHttpRuntime(session_factory)` supplies only a
server-owned session factory, not client authority or alternate service methods.

| Method/path | Exact body/result |
| --- | --- |
| POST `/api/v2/production/accounts/{account_id}/work-items` | `{orderItemId:string, expectedSourceItemVersion:string}` -> `{schemaVersion:production-create-v1,workItemId:string,replayed:bool}` |
| GET same prefix `/{work_item_id}` | `production-work-item-v1`, `item` with existing work-item fields in camelCase |
| POST same prefix `/{work_item_id}/assignments` | `{expectedVersion:string,catalogSkuId:int,idempotencyKey:string,reason:string}` -> `production-assignment-v1`, existing seven-field `result` and replayed |

BIGINT IDs, versions and nullable receipt ID use exact positive decimal strings;
account/org/SKU and quantities remain bounded integers. Timestamps serialize UTC
with six fractional digits. No client org/actor/permission field, coercion,
trimming, duplicate JSON key, unknown query/body field or nonfinite number.
Explicit byte budget bounds streamed body; it is not a new domain text cap.
Request and response schemas are exposed in the factory's OpenAPI definitions.

Actual actor comes from `actor_from_request`. Account/principal metadata is
discovered server-side using existing `discover_orders_bindings`; it is not
authority. The actual Production service then acquires fresh fixed
PRODUCTION_READ/CREATE/ASSIGN permissions and current source/account guards.
A factory returning an already-open caller root is rejected BEFORE entering a
context manager, preserving that root without close/rollback. Fresh clean
Engine-bound PostgreSQL Session is required for metadata/service execution.

Known validation/auth/access/not-found/conflict errors map to 400/401/403/404/409;
default-deny runtime is 503 PRODUCTION_DISABLED. Unknown writes, storage errors
with uncertain commit outcome, or post-commit serialization failures return 503
PRODUCTION_READBACK_REQUIRED. No response claims rollback or auto-retries an
operator command. Success and explicit errors carry Cache-Control: no-store.

## Evidence

- Initial offline wire/admission suite: 30 PASS, 2 existing dependency deprecation
  warnings, 0.76s. Domain methods/metadata substituted at the wire-test boundary;
  connection creator explicitly fails if any database connection is attempted.
- Manual critic found caller-root ownership bug in the first context-manager
  placement. New regression RED: 1 FAIL, 30 deselected, 0.64s. Moved validation
  before Session context ownership. Final covering suite: 31 PASS, 2 existing
  warnings, 0.74s, exit 0.
- Scoped Ruff, compileall, git diff --check: PASS.
- Bigint >2^53 round trips, exact request rejection, duplicated JSON, streamed
  budget, body authority injection, safe conflict/error codes, post-commit invalid
  result, default-deny and OpenAPI checked.

This is offline HTTP acceptance, not real bearer/database-role composition. The
earlier separate Production service PG gate (16 cases) is not relabelled HTTP
evidence. Final actual-DB HTTP/current limited API-role/activation gate is NOT_RUN.
No PG slot, provider, production, print/export, KIZ/matcher or user JSON action.
Policy, calendar, readiness, planning and immutable renderers are unchanged.

Focused reproduction from backend under the existing scrubbed safe environment:

```sh
python -m pytest -p no:cacheprovider -q tests/test_production_http.py
python -m ruff check app/orders/production_http.py tests/test_production_http.py
python -m compileall -q app/orders/production_http.py tests/test_production_http.py
git diff --check
```

Rollback is removal of an unregistered factory. ROOT owns any later registration
and explicit byte-budget/runtime composition after remaining gates; no enabled
defaults are supplied here.
