# Orders Saved-View Discovery And Filters

Date: 2026-09-10. Owner: T3. Branch: `codex/wb-live-t3-reads`.
Parent: `910614ad44728db44d5daa8f7892427875a6021b`.

## Scope

This package adds a saved-snapshot selector and SQL filters to the existing
persisted Orders read API. It does not claim that the common WB/Avito queue or
Production workflow is complete. No schema, provider, writer, bootstrap, config,
frontend, KIZ, matcher, or user-data changes.

`app/modules/orders.py` remains unchanged and pure. Unknown marketplace status
remains raw evidence with unmapped/null canonical status. WB Statistics is not
fulfillment readiness, deadlines, or sticker authority.

## Frozen Wire For T4

`GET /api/v2/orders/snapshots/latest?account_id=91101` accepts repeated account IDs
for an exact account scope. Duplicate/nonpositive IDs fail 400. It selects the
greatest nonexpired snapshot ID in that exact scope, regardless of saved query
checksum, then validates its stored coverage and live account binding.

The typed response is `app/orders/http_contracts.py:OrdersSavedSnapshotResponse`:

- `selection_kind`: always `saved_snapshot`.
- `organization_id`: integer; `marketplace_account_ids`: integer array.
- `snapshot_id`: decimal string; `query_checksum`: exact lowercase 64-hex string.
- `high_water_mark`, `published_at`: existing Orders string/timestamp semantics.
- `coverage_state`: complete, partial, or missing; `account_coverage`: existing
  typed source/version/coverage metadata.
- `row_count`: decimal string, total frozen item rows BEFORE optional filters.

This is a limited saved view. Storage retains a query hash, not the original query
definition. Neither `coverage_state=complete` nor this selector proves that the
snapshot is the entire unfiltered operational queue. The UI must not label it so.
No existing snapshot returns 409; discovery never creates one. The selector reads
at most two row payloads and decodes one to reuse stored snapshot validation.

Use returned snapshot ID and query checksum in `GET /api/v2/orders`, with the same
account scope. The existing snake_case `OrdersReadPageResponse` is unchanged.
Optional AND filters:

| Parameter | Meaning |
| --- | --- |
| marketplace | exact wb or avito |
| canonical_status | existing CanonicalOrderStatus enum |
| mapping_state | mapped, unmapped, ambiguous |
| resolution_state | resolved, unmapped, ambiguous, stale, manual_override |
| external_order_id | exact text, 1..4096 characters, preserves leading zeroes |
| raw_status | exact text, 1..2048 characters, no trim/lowercase/aliases |

Filters only narrow the saved item rows, in SQL before LIMIT. Ordering remains
immutable snapshot position ascending; limit defaults to 100 and is at most 200.
Unsupported sort/period/query parameters and duplicate scalar parameters fail
400 rather than being silently ignored. Repeated account_id remains supported.
Enum/length query parsing retains FastAPI 422; exact-text/model failures are safe
400. Existing auth 401, scope 403, snapshot 409, storage 503 remain.

Cursor MAC scope includes the canonical filter checksum. A filter or saved query
change requires restarting pagination; replaying that cursor fails 400. Empty
filters preserve the previous cursor checksum exactly. Coverage describes the
saved source view, not the filtered result, and is never recomputed as complete.

## Verification

Source-first bounded implementation followed the coordinator's explicit process.
No import-failure RED run is claimed.

- New pure/query/HTTP/OpenAPI tests: 22 passed, 2 existing deprecation warnings,
  1.00s, exit 0 (`tests/test_orders_filters.py`).
- New actual PostgreSQL tests: 2 passed, 2 existing deprecation warnings, 15.33s,
  exit 0 (`tests/test_orders_filters_postgres.py`). Selected order follows four
  nonmatching frozen rows, proving filtering before LIMIT; two distinct repeated
  listing lines paginate without duplication. Changed filter/cursor fails 400;
  metadata returns actual six-row snapshot/checksum; rebound account fails 409;
  revoked permission fails 403; observed service SQL has no business DML.
- Existing pure identity/status/cursor/serialization regression: 82 passed,
  0.33s, exit 0.
- Ruff, compileall, git diff --check: exit 0. Initial Ruff fixture import F811
  was corrected with existing aliased-fixture convention before final gate.
- Manual isolated critic pass checked source JSON paths, bound SQL parameters,
  cursor compatibility, scope guard/revalidation, and unchanged pure contract.
  The configured preflight-critic skill file was absent locally.

PG ran only in the approved local Unix-socket sandbox using the existing owned
disposable database fixture and current branch Alembic head/runtime-role script.
Fixture cleanup asserted the exact owned database and roles absent. Natural
process exit confirmed and exclusive PG slot released to ROOT. No live DB,
production, external marketplace, physical printing, push, or deploy actions.

## Reproduction And Limits

From `backend`, under `ops.release_gate.safe_environment()` with plugin autoload
disabled and no pytest cache:

```sh
python -m pytest -p no:cacheprovider -q tests/test_orders_filters.py
python -m pytest -p no:cacheprovider -q tests/test_orders_contract.py tests/test_orders_cursor.py tests/test_orders_serialization.py
python -m pytest -p no:cacheprovider -q tests/test_orders_filters_postgres.py
python -m compileall -q app/orders/query.py app/orders/read_service.py app/orders/router.py app/orders/http_contracts.py app/orders/snapshot_repository.py tests/test_orders_filters.py tests/test_orders_filters_postgres.py
```

The PG command additionally requires the coordinator's exclusive slot,
`ORDERS_TEST_USE_LOCAL_CLUSTER=1`, and the existing `orders-tests.sb` network/file
sandbox. Do not substitute application DATABASE_URL or a live database.

JSON predicates have no new expression index. Large-snapshot selectivity/latency,
every possible filter combination, and final assembled local API-role grants
are NOT measured by this small acceptance gate. Existing full backend suite was
not rerun. Root/T4 owns integration and UI consumer. No publisher query-definition
contract was invented. Production persistence, frozen artifact parity and
operator command acceptance remain separate work.

Rollback is code-only: remove this reader package and its new UI selector/filter
consumer together; saved snapshots and current Orders data are unchanged.
