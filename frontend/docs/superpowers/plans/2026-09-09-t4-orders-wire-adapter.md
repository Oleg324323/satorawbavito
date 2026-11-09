# Dormant Orders wire adapter implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. User authorizes autonomous continuation; no per-slice approval pause.

**Goal:** Prepare strict typed decoding and request-path construction for the accepted Orders read wire, without runtime cutover.

**Architecture:** Pure Zod decoder and URL builder under features/orders. No fetch, hooks, storage, readiness inference or automatic pagination. A future authorized consumer owns session lifecycle, rollout and error handling through shared apiRequest.

**Tech Stack:** Existing TypeScript, Zod, Vitest; no new dependencies.

**Spec:** T3 da7e017 backend/app/orders/{http_contracts,contracts,ingestion}.py and backend/app/modules/orders.py; handoff2026-09-09-orders-read-http-handoff.md. T3 confirmed ab272ee leaves wire unchanged; actual shared errors use error.code.

## Global constraints

- Frontend-only; no backend/schema/shared registration or production/provider operations.
- Preserve WB/Avito raw and canonical statuses separately. Never infer printable from status, mapping or coverage.
- snapshot_id and row_version are exact positive INT64 decimal strings; SQL INT4 owner/catalog IDs remain numbers.
- query_checksum selects a prepublished view, not an implemented UI filter.
- No route activation or legacy removal. Historical unbound source gate remains open.

### Task 1: Pure request and response boundary

Files: create src/features/orders/canonicalOrders.ts and canonicalOrders.test.ts.
Interfaces: buildCanonicalOrdersReadPath(request): string;
parseCanonicalOrdersPage(payload: unknown, requestScope): CanonicalOrdersPage.
Request scope is explicit organizationId + unique accountIds, optional exact snapshotId.

- [ ] Write literal synthetic Avito/WB response tests. Catch precision loss with row_version `9007199254740993`, snapshot `9223372036854775807`, external ID `00042`; preserve null statuses/blockers. Malformed, extra-field, unsafe number, scope mismatch, duplicate rows/coverage and inconsistent item identity must reject the whole page with fixed safe error.
- [ ] Write URL tests: repeated account_id, checksum, limit1..200, exclusive snapshot/cursor. Missing/duplicate/unsafe owner IDs, invalid checksum/version and both selectors reject locally. Example expected suffix: `account_id=11&account_id=12&query_checksum=` plus literal64hex and `&limit=100&snapshot_id=9007199254740993`.
- [ ] Run focused Vitest; verify missing implementation RED, then behavior RED if necessary.
- [ ] Implement strict schemas with no coercion, then relational scope/item/coverage checks; return decoded data unchanged. Fixed `CANONICAL_ORDERS_RESPONSE_INVALID` error without raw payload/details. URL builder emits only accepted query keys.
- [ ] Run focused tests, TypeScript, adjacent/full frontend baseline comparison; do not waive existing21 failures. Compare backend-model serialized fixture with decoder using isolated pure tests, no DB/providers.
- [ ] Independent critique, record SHA/tests/remaining in local coordinator matrix, commit exact slice. Do not message root per slice.
