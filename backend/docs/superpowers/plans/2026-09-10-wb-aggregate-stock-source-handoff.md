# T2 actual stock source — additive aggregate-size adapter

Implemented in `wb_aggregate_stock_source.py`, no shared wiring or DDL.
Legacy `wb_stock_snapshots.py` and its hashes remain unchanged.

## Evidence and reason for a separate source format

Current [official analytics documentation](https://dev.wildberries.ru/en/openapi/analytics)
was read including expanded `postV1StocksReportWbWarehouses` response schema on
2026-09-10. The request contains optional nmIds/chrtIds, limit<=250000 and offset;
there is no stockType field. Response200 is `data.items`, response204 is no data.
chrtId is mandatory, not nmId. Critically, current warehouseId is documented as
`-999999` only, with warehouseName and regionName both `Склад WB`. Quantities are
uint64. The source updates every30minutes and admits one request per20seconds.
Documented eligible tokens: personal, service, base with secret.

This cannot truthfully enter the existing positive-warehouse-ID v1 parser/storage
or reuse its `stockType=wb` request hash. The new collection schema is
`wb-aggregate-size-stock-collection/v1`, scope `wb_aggregate_size`, exact owned
org/account, method/path and page limit. No filter means all eligible products;
filtered variants are deliberately not implemented. Page offset is separate
manifest evidence. Preserve original wire-body SHA256 and response SHA256.

## Implemented behavior

- Immutable source rows preserve nmId/chrtId and external sentinel -999999.
  Different IDs/names/regions fail closed as unresolved grain, never guessed.
- Count presence distinguishes missing, explicit null and explicit zero.
  Exact Python integers retain the uint64 range; no clamp to signed bigint.
- Duplicate keys/identities, malformed wrappers, float/bool counts and oversized
  pages fail closed. A short nonempty page is not EOF; only empty/204 ends this
  conservative pager. No filtered-out bad rows or swallowed partial failures.
- One actual read-only POST per call, bounded64MiB/45second stream, no retry,
  sleep, global limiter, fallback source, env proxy or redirect.
- Required trusted admission `(org,account,20)->True` reserves durable quota and
  checks entitlement/authority; credential binding is exact org/account/WB.
- Required `record_cooldown(org,account,deadline)->True` commits monotonic deadline
  extension immediately on headers, BEFORE body. Error/parse/stream failures do
  not erase successfully stored Retry-After. Admission is not a substitute for
  publication authorization. No permissive production callbacks are supplied.

## T1/ROOT publication contract prerequisites

1. Admit this distinct request/parser identity and aggregate-size grain; don't
   cast sentinel to a warehouse FK or invent a real location. Existing stock
   daily positive-warehouse rows are incompatible. No historical reconstruction
   from today's observations; source time is unknown, only received_at is known.
2. Storage counts must represent uint64 exactly (or explicitly block unsupported
   values), with count-presence preserved. Do not silently overflow bigint.
3. Store page status, offset/limit, request+rawchecksums, owned collection hash
   and observations under a run manifest; unchanged replay is no-op, changed
   source data is a revision. Enforce cross-page identity dedup and contiguous
   offsets, cap run pages/time and only promote a fully validated EOF run.
4. Missing/null required counts are retained as incomplete evidence, NOT eligible
   complete current stock. Never merge partial run with previous current. Keep
   latest complete as explicitly stale if refresh fails. An EOF page alone is
   not a complete run or a source timestamp.
5. Source has no snapshot token: even complete offset traversal isn't proof of
   a point-in-time warehouse snapshot while provider refreshes. Record that
   limitation; do not claim stock history from a sequence of today's polls.
6. Root owns durable request admission + lease/job/publication integration and
   policy; T1 owns schema/RLS. This package calls no publication participant.

## Independent runtime assessment

CurrentSourceTransaction has no application caller outside StockDailyTransaction;
the existing stock modules are persistence primitives, not a connected pipeline.
Separately, live prices use actual GET request evidence but old CollectionRequest
describes an unfiltered POST. Never relabel live GET pages with that old hash.
These are explicit integration/source-format gaps, not reasons to mutate existing
checksums or install a permissive transport fallback.

Offline fixtures cover real MockTransport path/body, scope/admission denial,
uint64 and count presence, sentinel/size identity, 204/empty, wrapper/key/row
duplicates, invalid numbers, HTTP failures/no retry and durable throttle feedback
before stream failure. No production, provider execution or full persistence
acceptance is claimed. Rollback leaves adapter unregistered and old paths intact.
