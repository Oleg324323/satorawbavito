# WB Prices and warehouse Stocks current-source storage

Binding request:907ec7b:
backend/docs/superpowers/reports/2026-09-09-t2-price-stock-schema-request.md,
updated full literal appendix at a8581cb5f8cd7c3ee3233ae8812fd70e800481f5.
Actual source contracts: wb_source_requests.py02591f3, price/stock parsers with
chronology correction901c179, all present at a8581cb. Ten frozen raw/page/request/
manifest vectors now exist; their execution and SQL parity remain UNRUN.

This package implements only sections1–6 and their source audit/ACL requirements.
Stock daily revisions plus reviewed correction evidence are a separate bounded
package after these parents, not silently omitted or marked done. No providers,
source fetch/worker/routers, inferred Catalog mapping, price actions or activation.

## Task 1: Implement separate current Prices and Stocks source families

Read full binding request and latest appendix. Read exact existing
app/modules/wb_source_requests.py, wb_price_snapshots.py, wb_stock_snapshots.py
and tests/fixtures/wb_collection_golden_v1.json at a8581cb. They define original
raw hash, exact source fields/grain/presence, request and manifest bytes. Read
actual account/credential/run binding helpers and grants; don't copy T2 parsers
or declare their plain dataclasses to be authenticated provider evidence.

Allowed exactly three paths:

- New backend/alembic/versions/YYYYMMDD_NNNN_wb_current_sources.py with exact
  next revision/down_revision supplied by controller at dispatch after source
  single-head/absence checks. Stop on mismatch, no silent renumbering.
- backend/ops/runtime-db-role.sql, new relations/columns/sequences/helpers only.
- backend/docs/superpowers/reports/2026-09-09-t1-wb-current-source-handoff.md.

No tests or intermediate PG/Redis/import/compile/review gates during source-first
implementation. No old migrations/parent indexes, domain/runtime/frontend/worker,
env/real secrets, network/production/provider/working data, role provisioning or
operational flag/key changes. Final verification is deferred, not waived.

### Physical model

Eleven new tables, not a universal storage relation:
wb_price_runs/pages/product_facts/size_facts/current_heads/audit (six), and
wb_stock_runs/pages/observations/current_heads/audit (five). Use full requested
names wb_price_product_facts and wb_price_size_facts. Every relation has actual
positive INT4 organization/account, WB canonical provider FK and forced BOTH
organization/account RLS. Actual source-native IDs/money/counts use strict BIGINT
where parser caps there; bounded run state/version and exact head counters are
not client timestamps. Never float-coerce provider IDs, size IDs or money.

Run immutable identity: server UUIDv4, request UUIDv4 unique in full owner/source,
source kind/parser, exact collection request BYTEA/SHA, actual DB-issued ingest
sequence, source context and historical account/credential binding below.
State collecting/version0 only becomes complete/partial/failed version1 once;
sealed history never reopens. All terminal null/count/error fields exactly follow
request. Invalid first page gives failed with zero rows/pages; error after valid
prefix gives partial with positive accepted pages. A complete empty collection
still includes its explicit empty terminal page. No partial-with-zero loophole.

ingest_sequence has no runtime INSERT/UPDATE column privilege or callable setval.
A BEFORE INSERT trigger assigns nextval from the exact new family sequence only
when column was omitted/NULL; reject explicit non-NULL. No default that obscures
explicit input before the trigger. Runtime may require USAGE for nextval but not
UPDATE on sequence; burning a value doesn't authorize caller-chosen ordering.
Sequence is request-ingest order, not provider business time. Freeze at creation.

Run retains immutable schema1 connected account external ID/credential_ref bytes
and SHA, plus exact paired wb_api credential ID/generation/payload schema/NULL
expiry. Use actual parent keys plus equality checks; no parent key invention.
Capture metadata under existing live authority and compare same fetch binding at
publication; future T2 source service owns this integration. SQL immutability
prevents later account rebind/generation change from rewriting history. Hash of
page/request alone DOES NOT bind credential generation or real external account.
Creation/current publication checks cannot interpret an actor enum as authority;
source worker/permission bootstrap remains a separate guarded-runtime obligation.

CollectionRequest exact ASCII sorted compact envelope and SHA, max65536 bytes,
closed keys/method/path/body/query from actual encoder. Price only unfiltered
wb_goods_prices_v1/wb-goods-prices/v1; warehouse only
wb_warehouse_v1/wb-warehouse-stocks/v1. Unsupported FBS/filtered products, buyer/SPP/
wallet/finance sources rejected, not coerced to these kinds. Page limit/offset
retain parser BIGINT range, no new business page-size policy; owner remainsINT4.
All source_observed_at fields NULL in these bounded parsers. Do not create a
source timestamp from DB/receipt time or enable a future adapter by arbitrary label.

Pages immutable while collecting and sealed with run: exact scoped run, contiguous
page number/offset, common requested limit, nondecreasing receive instants, raw
SHA and raw row count<=limit, terminal iff short. First offset0; each preceding
page full; no page after terminal. Same page exact intent replay only, changed
bytes/hash under that slot conflict. Successful HTTP2xx status is trusted transport
metadata, never guessed from request; optional request_id cannot contain headers/
raw errors/secrets and is not an authority. No raw page JSON storage is added.

Prices retain separate product facts (nm_id, discount/club discount presence and
integral percent0..100) and size facts (native positive size_id, three distinct
seller price presences/kopecks, RUB only). Product has explicit source_size_count
and unresolved_size_count metadata from parsed source, never synthetic size IDs.
Known stored size count must equal source_size_count minus unresolved_size_count.
Complete publication requires every nonempty product to have source_size_count>0
and unresolved_size_count=0; unresolved source identity stays partial with the
proper safe error, not silently dropped to produce complete. Empty goods run can
complete. Product raw counts differ from product+size fact counts. The current
GoodsPriceRun.complete is TRANSPORT completion only; physical eligible complete
adds the requested identity check, does not rewrite T2 parser semantics.

Stock observation grain exactly wb_warehouse/warehouse_id/nm_id/chrt_id(nullable),
NULLS NOT DISTINCT unique, nullable key not illegal nullable primary-key component.
Three presence/amount pairs, nonnegative BIGINT. No reserve/FBS/count-total aliases,
negative-to-zero conversion, warehouse-name identity or Catalog guess. Current
strict parser rejects duplicate identities even with equal payload; do not add
collapse behavior from older permissive request wording. Optional display name
not produced by current parser is nullable metadata, never a source identity.

Canonical manifests are EXACT existing formats, not hashes recomputed from SQL
facts or JSONB formatting:
price `["wb-goods-prices/v1",[org,account,limit,request_hash],[[offset,limit,raw_hash],...]]`;
stock `["wb-warehouse-stocks/v1",request_hash,[[offset,limit,raw_hash],...]]`.
All compact ASCII. SQL verifies page/run count/manifest and exact source fact
ownership/grain. Raw hash does not by itself prove that typed rows came from that
raw response; T2 trusted decoder/transport and final integration test are necessary.
Same raw pages/request at different dates may have identical manifest hashes;
never make manifest UNIQUE as run identity or infer receipt time from it.

Current heads use exact owner/source/parser/request checksum PK and scoped run FK;
first complete run+head1 in one transaction, no empty heads. version increments
once on newer publication, published_sequence equals referenced run. No older/
equal sequence replaces a newer head; partial/failed never publish. Losing head
CAS rolls back finalization/facts/audit together; trusted service can separately
seal a valid complete run without publishing. Exact sealed replay emits no audit
or second head update. No freshness TTL default or last-complete→fresh fallback.
Represent latest failed/partial attempt separately from last complete; stale
read/apply policy supplied by owner, not a table default.

Each family audit only run.created/completed/partial/failed/current.published.
Exact typed run before/after and optional head versions, worker actor/memberNULL,
actual DB timestamps; no fake UserSession. Full reciprocal deferred witnesses,
contiguous audit/state/counts, no ghost audit/orphan head/unwitnessed terminal or
head change. Daily audit kinds are not accepted here; later separate daily audit
can reference these immutable complete run parents. RLS context switching cannot
hide earlier pending writes from final checks. Source/credential live guard
precedes account→run→head/domain locks; SQL role alone isn't origin authorization.

New-only exact ACL scrub (including column/default/sequence/helper leaks), runtime
SELECT and column-restricted INSERT/update of collecting/terminal/head metadata,
no identity/order/history update/delete/truncate. Immutable facts/pages never
silently rewritten. Pure functions exact regprocedure grants; trigger functions
not directly runtime/PUBLIC executable. Avoid redundant child indexes; protect
actual sequence override paths. Empty-only downgrade across all eleven tables,
unfiltered hidden history refusal, stable exact locks, own dependencies only,
no CASCADE, parent account/credential/history preserved.

### Delivery and remaining work

One source commit: `feat: add account-scoped WB price and stock source storage`.
Publish exact columns/types/relations/grants/functions and terminal/head/graph
write requirements, parsed metadata versus raw-source proof, safe error codes,
frozen binding, old/new sequence race and replay behavior. IMPLEMENTED / UNVERIFIED.
No source fetching/handler/worker, automatic scheduler, Catalog mapping/context,
stock daily/history correction or provider completeness claim.

Final gates after all source implementation: ten pinned request/raw/page/run
vectors (SQL output independently compared, not regenerated expected), source
identity/unresolved sizes/count/presence/NULL/zero/>2**53/Unicode failures, exact
manifest same hash/different time, page gap/overlap/repetition/short-final matrix,
root rollback/CAS/newer sequence, actual runtime sequence override denial, dual
forced RLS/account/credential binding, ACL/immutable/hidden empty-downgrade and
synthetic empty/production-shaped upgrade→downgrade→upgrade; actual T2 trusted
source service before eligibility. No intermediate test execution in this task.
