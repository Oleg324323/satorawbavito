# T2 — encrypted account Avito order-status preview

2026-09-10. Branch `codex/t2-account-discovery`, after `b4a86e3b7dbacc5398f1c271471f218865e2ead2`.
Five new files only: `app/avito/account_orders.py`, `account_orders_http.py`,
`tests/test_account_avito_order_status.py`, `test_account_avito_order_status_postgres.py`, this report.

## Concrete existing path and transfer boundary

The current `routers/avito_orders.py::_orders_client_for_request` uses user plaintext
credentials, then organization plaintext fallback, then `resolve_user_avito_access_token`.
The v1 Orders GET also reads organization-only source/browser caches, merges browser
and return data, fetches public listing details and writes its cache. None of that
was copied into or changed by this slice. It is **not yet removed or replaced**.

`LiveAvitoOrdersClient.fetch_orders(request, http_client=...)` already supports an
injected HTTP client and one `GET /order-management/1/orders`. An existing paired
encrypted `avito_oauth_access` is sufficient for that boundary; no refresh or
exchange dependency is invented. The old quantity helper defaults missing/zero to
1, money helper converts through float, and total falls back to page length. Those
values are deliberately absent from this preview, not canonical facts.

ROOT approved this narrow read-only status preview. T3 confirmed existing
`map_avito_status`/`avito-order-status-v1` ownership: no trim/lowercase/legacy aliases,
unknown nonblank status remains raw + unmapped + canonical null. Missing/blank/raw
non-string status fails closed, rather than inventing a provider status `unknown`.
`ready_to_ship` mapping does not prove production readiness.

The existing `orders.avito_status_refresh` writes under `sync:run` + `cabinet:read`.
It is **not called**, and its authority is not repurposed as a read subscription.
No Orders manifests, jobs, observations, current heads, audit events, stock/price
facts or production work items are created by this preview.

## Frozen wire contract for ROOT/T4

```text
GET /api/v2/avito/accounts/{internal_account_id}/orders/status-preview
    ?dateFrom=YYYY-MM-DD&page=positive_int64

data:
  marketplaceAccountId: positive int4
  provider: "avito"
  externalAccountId: exact canonical decimal string
  dateFrom: exact ISO date
  page: decimal string
  limit: 20
  coverageState: "partial"
  hasMore: boolean | null
  rows: [
    orderId: exact string
    rawStatus: exact nonblank string
    canonicalStatus: existing mapped canonical status | null
    mappingState: "mapped" | "unmapped"
    mappingVersion: "avito-order-status-v1"
    createdAt: strict actual source ISO timestamp | null
    updatedAt: strict actual source ISO timestamp | null
    accountEvidence: "provider_account_id" | "credential_scope"
  ]
```

Both query fields are required, no duplicate/unknown query keys or override scope;
fixed page size 20, no hidden auto-pagination/status filters. Responses always
`Cache-Control: no-store`; fixed safe codes under `detail.code`:
`AVITO_ORDER_STATUS_ACCESS_DENIED` (401/403), `AVITO_ORDER_STATUS_INVALID_REQUEST`
(422), `AVITO_ORDER_STATUS_DISABLED`/`AVITO_ORDER_STATUS_UNAVAILABLE` (503).

`hasMore` comes only from an actual provider boolean; missing/wrong-type is null.
Even `hasMore=false` on page 1 remains `coverageState=partial`: this is not account
completeness, a publication witness or a full canonical queue.

Provider row-level `accountId`/`userId`/`sellerId`, if present, must match the captured account
exactly (lossless integer or exact string; no bool/float coercion). When absent,
`credential_scope` explicitly means the request used that bound account credential,
not that the provider returned an account-ID field. Duplicate order IDs, non-string
IDs, malformed rows or more than 20 rows reject the page. Present-null aliases
are invalid, not missing evidence. Top-level ownership aliases are not part of
the observed existing adapter contract; any such field rejects the response until
its source semantics are proved. No buyer-field ownership is invented.

Timestamp projection accepts actual calendar-valid ISO date/time with seconds,
optional 1–9 fractional digits and Z or valid hour/minute offset, preserving the
original string. Invalid/naive/non-string timestamps become null, not a guessed
time, freshness promise or delivery deadline. No buyer/contact/address data, prices,
quantities, total count, actions, diagnostics, tokens, raw error bodies or HTML escape.

## Composition and authority

```python
AccountAvitoOrderStatusService(
    engine=explicit_postgresql_engine,
    keyring_loader=explicit_keyring_loader,
    client_factory=BoundedAvitoOrderStatusClient,
    enabled_for=explicit_org_account_allowlist,  # omitted => deny
)
make_account_avito_order_status_router(service_dependency=zero_argument_service_factory)
```

The service's `preview(actor, *, marketplace_account_id, date_from, page)`:

1. Fresh owned physical root validates actual user, membership, login expiry,
   `cabinet:read` and exact account scope. Locks the connected canonical Avito account.
2. Resolves only encrypted access credentials with the explicit key loader; captures
   original principal/account binding, credential ID/generation/schema/expiry and
   `ingestion_binding_version`. Installs one existing public publication guard and
   closing incarnation check. Commit and close before HTTP.
3. Redacted credential reaches `BoundedAvitoOrderStatusClient`; unwrap only at the
   provider edge. No OAuth refresh/client-credential fallback/global cache.
4. New owned root checks **the same captured authority and incarnation**, not freshly
   substituted credential identity. Commit and close before the DTO may escape.

The new HTTP edge is one fixed-origin GET, no retries, no redirects or environment
proxy. Identity encoding/JSON only; reject non-200 before reading error body. Raw
stream max 4 MiB with length verification/duplicate-key/non-finite-number rejection.
Per-operation timeout 20 seconds; elapsed 30-second checks between chunks/at EOF
(not a strict process-wide wall deadline for a blocked operation).

Only supported identity/status fields are fed to the unchanged legacy parser;
raw timestamps and explicit account evidence are retained in the immutable preview
model. Existing parser request parameters and identity/status result are exercised,
but its float money/default quantity/total fallback cannot enter the DTO.

## Tests and verification

Python `/tmp/satorna-backend311-20260909/bin/python`, this worktree's backend cwd,
minimal `env -i` and existing network-deny sandbox
`arch-t1-platform/.superpowers/sdd/2026-09-09-publication-guard/task-1-offline.sb`.
PG adds `ORDERS_TEST_USE_LOCAL_CLUSTER=1`, Unix socket, password/netrc/service files
`/dev/null`; fixture keys and rows are synthetic only.

| Gate | Actual result |
|---|---|
| Initial router/transport RED | missing new module, collection exit 2 |
| Service RED | missing new service class, collection exit 2 |
| Missing-status regression RED | missing raw status was accepted; corrected to existing mapping contract's fail-closed semantics |
| Invalid timezone regression RED | `+00:99` passed Python normalization; added explicit offset hour/minute validation |
| `pytest -q --tb=short tests/test_account_avito_order_status.py tests/test_orders_avito_adapter_characterization.py tests/test_orders_avito_status_source.py` | 63 passed (32 new + 31 existing), 2 existing deprecation warnings, 0.72s, exit 0 |
| `pytest -q --tb=short tests/test_account_avito_order_status_postgres.py` | frozen 15 passed, 5.68s, exit 0 |

The 15 PG cases cover both closed roots/no canonical Orders writes, default deny,
initial session/scope/missing-access/key failures, credential replacement/account
incarnation/permission/login revocation/binding changes during fake HTTP, failure
of either physical commit and foreign org/account. A second actual connection can
lock the account with NOWAIT during the fake provider boundary. Real provider HTTP
is never used; actual transport behavior is covered with MockTransport offline.

ROOT's exclusive `AvitoOrderStatus15` slot was released after natural pytest exit.
Existing synthetic Stats allocator disposes engines and removes only its exact
created DB/role, asserting both are absent in catalog queries. No PG resource retained.

## Remaining integration gates

- Unregistered/default deny, no config/main/frontend/legacy changes here. T4 typed
  client can be written against the frozen wire independently, without UI cutover.
- The actual final API-role grants and assembled bootstrap must be verified by ROOT;
  this fixture uses the existing restricted runtime role, not a claim of final pool readiness.
- Requires real encrypted access-token publication/expiry lifecycle when eventually
  activated; missing/expired credential is unavailable, never automatic refresh.
- Request concurrency/quota admission and full Orders feature parity remain separate.
  This is not a persisted snapshot, a job publisher or a replacement for the old
  complete UI workflow. No broad source proof or provider parity is claimed.
- Final-listener injection, full maintenance reencryption during HTTP and every
  real provider format variant are not separately covered by these 15 PG cases.

Independent local critic pass checked missing-status fabrication, float/default
facts, source timestamp normalization, page-completeness overclaim and sensitive
projection. No real credentials/.env, provider, production, pricing, OAuth, deploy,
push, scheduler or business-rule mutations occurred. Existing flags remain unchanged.

## ROOT review follow-up

Present-null row ownership aliases are no longer treated as absent; unproven
top-level ownership aliases fail closed. HTTP account/page numeric text must be
canonical, without leading zeros. Frozen DTO shape is unchanged.
Regression RED: 12 failed / 3 passed (null alias, three top aliases across three
values, two numeric request cases). Final 75 offline passed (44 new + 31 existing),
2 existing warnings, 1.69s, exit 0. Ruff/compile/diff checks succeeded. No extra
PG run was needed for these pure transport/query guards; original 15 PG evidence
is unchanged. No legacy files, schema, config or activation changes.
