# T2 → T1: Stage 5 price/stock/daily storage request

Независим от approvals/settings/Orders. Реализованный price parser/transport assembler:
`4e14e21ce79ebda29144d17db0b4a798f4bd2ec4`, `app/modules/wb_price_snapshots.py`.
Read-only discoveries: `wb-prices-stocks-discovery-2026-09-03.md`, source revision
contract `wb_source_revision.py`; runtime source integrity fixes `031c110`, `4c3f6fa`,
`1e2445f`. DDL/repository/real concurrent publication пока NOT RUN.

## 1. Раздельные bounded domains

Предлагаются отдельные relations для Prices и Stocks, не universal state table:

- Prices: runs, pages, product facts, size facts, current heads.
- Stocks: runs, pages, observation facts, current heads, daily revisions, daily heads.

Общие pattern constraints ниже повторяются для каждой семьи; общий framework/service
не требуется. Source v1 scope: WB seller/Club current prices; detailed WB warehouse
current stocks. Seller/FBS — blocked extension, не входит в принимаемый v1 DDL:
local BFF использует body `skus`, а discovery описывает `chrtIds`; exact identity и
omission semantics ещё требуют отдельного доказанного adapter contract. Buyer external
source/41-SPP, async warehouseName fallback, product stockCount metrics и historical
order/finance price не входят в эти source kinds и не могут публиковаться под ними.

Все owner keys включают internal org/account и marketplace WB discriminator. Canonical
CatalogSku mapping не выдумывается по article/nmId; source facts хранят source identity,
catalog read join получает отдельный доказанный account mapping revision.

## 2. Runs: одинаковый точный lifecycle, отдельные таблицы

`wb_price_runs` / `wb_stock_runs`:

| Поле | Тип/nullability/constraint |
| --- | --- |
| organization_id, marketplace_account_id | required canonical integers, composite FK + WB provider discriminator |
| run_id | required server UUID4, UNIQUE org/account/run_id |
| request_key | required canonical UUID4 one collector invocation, UNIQUE org/account/source_kind/request_key |
| source_kind | price: wb_goods_prices_v1; stock: wb_warehouse_v1 |
| parser_version | exact nonblank ASCII text, required; price initial wb-goods-prices/v1 |
| request_bytes | required immutable bytea canonical collector request, maximum 65536 bytes |
| request_checksum | lowercase SHA256 of request_bytes, required |
| ingest_sequence | required positive DB-issued sequence, immutable, client cannot supply/override |
| state | collecting/complete/partial/failed |
| version | integer>=0, initial collecting0, terminal1 |
| started_at | required trusted aware timestamptz |
| finished_at | NULL collecting, required terminal >= started_at |
| source_observed_at | nullable; only direct documented source timestamp, never read-time substitution |
| received_at | nullable collecting; required if any successful page; latest actual accepted page receive time |
| manifest_checksum | NULL collecting/failed; required complete/partial |
| page_count, raw_row_count, fact_count | integer>=0, exact committed counts; collecting provisional, terminal immutable |
| safe_error_code | NULL collecting/complete; required partial/failed |

Safe error enum v1: SOURCE_REQUEST_FAILED, SOURCE_INVALID_PAGE, SOURCE_PAGINATION_STALLED,
SOURCE_PAGINATION_INCOMPLETE, SOURCE_IDENTITY_UNRESOLVED, SOURCE_DUPLICATE_IDENTITY,
SOURCE_SCOPE_MISMATCH, SOURCE_REVISION_UNEXPLAINED. No raw exception/request headers.
Collecting→complete/partial/failed только один раз с expected_version=0. No terminal
reopen, no in-place source revision. Any changed terminal payload requires new run_id
и объяснённую source revision policy; exact terminal replay возвращает existing run.

Terminal null/count matrix: failed имеет ровно 0 accepted pages/facts/raw rows,
received_at и manifest NULL. Partial имеет >=1 accepted page, received_at и manifest
NOT NULL; error после accepted prefix = partial, не failed. Complete имеет >=1 page
(включая explicit empty terminal page), received_at и manifest NOT NULL. Invalid first
page = failed с safe error и без successful page row. Partial-with-zero запрещён.

`ingest_sequence` устанавливается при creation из sequence, недоступной runtime на
manual set/override; это ordering request start, а не доказательство source business
time. Late earlier run не перезаписывает newer published head. Real provider revision
закрытого дня не признаётся idempotency failure: отдельный run/evidence, old facts immutable.

Request canonical envelope (exact keys): `schema`, `organizationId`, `accountId`,
`sourceKind`, `parserVersion`, `method`, `path`, `query`, `body`, `pageLimit`.
schema=`wb-source-collection/v1`; ASCII JSON sort_keys, compact separators, ensure_ascii,
allow_nan=False; numeric JSON fields int only, bool distinct, null explicit. Никаких
headers/credentials/URL host. `offset` исключён из collection query/body, записывается
в pages. В price v1 разрешён только unfiltered collection: path
`/api/v2/list/goods/filter`, method POST, query={}, body={}; pageLimit>0.
Adapter добавляет limit/offset в actual request query из page context, не body.
Existing filtered `nmList` mode (`wb23_runtime.py:477`, `repricer_bff.py:2508`)
требует отдельного bounded request kind/coverage; guessed filterNmID не поддержан.
Exact unsupported request
fields/source modes блокируются, не silently discarded. Для WB warehouse stock path
`/api/analytics/v1/stocks-report/wb-warehouses`, POST, query={}, body={"stockType":"wb"};
adapter добавляет limit/offset в actual JSON body (не query) из page context,
как `reports_sources_runtime.py:354`.
FBS transport не фиксируется этим request: `wb_reports_bff.py:1485` использует skus,
не guessed chrtIds. Не смешивать barcode/native-size identities до separate contract.
Current parser price request_checksum supplied trusted collector;
его byte builder и stock collector — следующий code slice перед repository wiring.

## 3. Pages и terminal publication proof

`wb_price_pages` / `wb_stock_pages`: scoped run FK, page_no integer>=0, offset integer>=0,
requested_limit positive integer, received_at required timestamptz, http_status int,
raw_checksum required char64, raw_row_count integer0..limit, terminal boolean,
request_id nullable safe text (no headers), UNIQUE scoped run/page_no и run/offset.
Append while collecting only; after terminal immutable. Retries unsuccessful network
attempts не вставляются как successful pages; same page/offset/hash replay no-op,
changed hash under same page key → conflict/partial, не last-write-wins.

Prices: page_no0/offset0 first; next offset=previous offset+limit; все preceding page
rows==limit; final raw_count<limit, explicit valid listGoods array. Duplicate product
across pages blocks complete. Canonical parser уже проверяет это и raw error markers.
WB warehouse stock: тот же offset pattern с explicit empty/short page, malformed/repeated
full pages block. FBS chunking/omission/publication остаются вне v1; нельзя missing
item превратить в zero или принять его за WB warehouse observation.

Для всех: org/account/request_checksum/parser/source согласованы; receive times
монотонны; counts/manifest сверяются по final transaction rows. Provider atomic
snapshot isolation между страницами не обещается. В complete не допускаются unresolved
source identities; source-native identified, но unmapped CatalogSku не создаёт guessed
mapping и блокирует соответствующий repricer read, сохраняя source facts.

## 4. Immutable price facts: product и size раздельно

`wb_price_product_facts`: PK `(org,account,run_id,nm_id)`; nm_id positive bigint,
FK price run/page; product-source discount и club_discount (каждый presence enum
missing/null/value + nullable NUMERIC amount, 0..100). Current parser требует integer
percentage; fractional contract требует version bump, не молчаливого округления.

`wb_price_size_facts`: PK `(org,account,run_id,nm_id,size_id)`; size_id positive native
source ID, scoped FK product fact; list_price, discounted_price, club_price каждый
имеет presence и nullable integer kopecks>=0 до bigint max. currency='RUB' NOT NULL.
Для каждой пары CHECK: presence=value ⇔ amount NOT NULL; иначе amount NULL.
missing отличается от null, zero не равно missing. Никаких buyer/SPP/wallet columns
под seller aliases. Product percent не переносится во все sizes как direct size fact.

Неизвестный sizeID не создаёт synthetic size row: run partial/identity_unresolved,
raw page checksum/count сохраняются, diagnostic source rows остаются вне current
fact set. Парсер сохраняет unresolved entries локально, но complete storage gate
их не публикует. Source row position может быть diagnostic pointer, не offer identity.
Source totals count `product_fact_count + size_fact_count`; raw_row_count считает goods,
не размеры, чтобы sizes не ломали terminal pagination.

## 5. Immutable stock observation facts

`wb_stock_observations`: scoped run/page FK; stock_scope='wb_warehouse' в v1,
warehouse_id positive native bigint, chrt_id nullable positive bigint,
nm_id required positive bigint; chrt may be NULL (product grain remains explicit).
warehouse namespace = stock_scope, никакого unscoped warehouse ID.
UNIQUE `(org,account,run_id,stock_scope,warehouse_id,nm_id,chrt_id)` NULLS NOT DISTINCT.
Grain сохраняется; nmId не считается размером, не суммируется при вставке.

quantity, in_way_to_client, in_way_from_client: для каждого presence missing/null/value
и amount bigint>=0 NULL iff not value. FBS amount не нормализуется этим adapter.
Source не даёт stock reserve — колонки
reserve нет. Negative counts block источник v1, не abs/zero. warehouseName может быть
nullable display text, не identity; missing warehouseId в этом source kind partial.

Дубли same source identity + same typed payload могут быть collapse только с явным
raw-count/fact-count учётом; conflicting duplicate → partial/duplicate_identity.
Publication проверяет факты/manifest, а не считает сгруппированный nmId total evidence.
Product stockCount и source quantity не смешиваются max/sum, seller/WB не смешиваются.

## 6. Current heads: CAS publication

`wb_price_current_heads` / `wb_stock_current_heads`: PK `(org,account,source_kind,
parser_version,request_checksum)`, run_id FK того же полного context, published_sequence
NOT NULL, version positive int, published_at timestamptz. First head создаётся только
в transaction с первым complete run, version1; empty heads отсутствуют.

`finalize_and_publish(scope, run_id, expected_run_version=0, expected_head_version:
int|None, manifest_checksum)` проверяет pages/facts/counts, меняет run collecting→complete,
и head CAS одной transaction. expected None означает insert if absent. Stale head CAS
полностью rollback finalization/publication; retry перечитывает head и может seal run
без publication через отдельный `finalize_without_publish` с теми же checks. Older
ingest_sequence никогда не заменяет newer head. Partial/failed никогда не меняет head.
No-op replay на already-terminal run требует exact final manifest, не ещё один CAS.

Read current возвращает exact run facts + source/received time и last-attempt diagnostics.
Если latest attempt partial/failed или TTL истёк, last complete может отображаться stale,
но не смешивается с partial. TTL передаёт source policy; отсутствующая approved TTL
блокирует fresh/apply eligibility, не хранение immutable observations. Cache key содержит
полный owner/context + published run ID/manifest + mapping/settings versions при calculation.

## 7. Actual stock daily revisions

`wb_stock_daily_revisions`: owner `(org,account,stock_scope,request_checksum,
business_date_msk,revision)`; positive revision, source_run_id FK complete stock run,
basis enum source_observed/received, effective_observation_at timestamptz, created_at,
actor_membership_id NULL только initial automated observation, required correction;
correction_reason NULL initial, required correction,
supersedes_revision nullable. `wb_stock_daily_heads`: тот же owner без revision,
current_revision required FK, version=current_revision; first revision/head=1 atomic.

Daily хранит immutable membership на весь complete compatible source run; stock facts
не дублируются и не переписываются. business_date_msk равна дате effective_observation_at
в Europe/Moscow. Basis source_observed только при реально подтверждённом timestamp;
иначе received — фактическое время accepted collection, явно labelled. Run, чьи page
receive dates пересекли midnight без общего source observed time, не eligible для
одного daily observation: history remains missing/partial, не backfill по last page.

First daily observation выбирается explicit automated service call (worker, membership
NULL), не query dateTo; manual first creation вне v1. Exact replay
same run no-op. Later replacement в закрытом дне требует новой revision с nonblank safe
correction_reason matching `[A-Za-z][A-Za-z0-9_]{0,127}`, actor membership и supersedes FK,
а также accepted source revision
evidence; никогда overwrite. Пропущенный день не создаётся из current quantity. Future
historical data с доказанным source timestamp — отдельный разрешённый source import,
не exception для today's stock. Stock availability неизвестна без compatible coverage.

## 8. Audit и DB gates

Run/page/fact immutable history + head publication audit append-only per family:
event enum run.created/run.completed/run.partial/run.failed/current.published/
daily.created/daily.corrected (daily kinds только stock). Required org/account/run_id;
nullable daily owner revision только daily events; occurred_at; actor_kind worker/membership,
membership required только daily.corrected, остальные worker/NULL. Before/after run
state: created NULL→collecting V NULL→0; finalized collecting→terminal 0→1; publication
complete→complete V1→1, after_head_version=coalesce(before_head_version,0)+1;
daily complete→complete,
revision null→1 либо previous→previous+1. No request payload/token/raw error metadata.
Unique event kind/scoped run/head-or-daily revision; no audit on exact replay/CAS loss.

FORCE org/account RLS + WB account composite FK на всех relations. Direct runtime
UPDATE/DELETE sealed history запрещён; shared grants/sequence protection принадлежат T1.
Indexes: runs `(org,account,source_kind,request_checksum,ingest_sequence DESC)`, partial
collecting scan; facts own grain PK; heads own context PK; daily date/revision PK.
Downgrade empty-only across family incl audit; history не удаляется ради rollback.

Real PostgreSQL acceptance (NOT RUN): two publishers same head→one winner; old run
cannot replace newer; partial keeps previous head stale; zero/missing distinct after
restart; rollback fact/manifest/audit/head all-or-nothing; foreign account same nm/chrt/
warehouse invisible; exact replay no new rows; changed source data new revision;
unknown identity blocks publication; page gap/overlap/duplicate blocks complete;
midnight and missing-day daily guards; correction immutable prior revision; runtime
role has no sequence override or sealed-history update path. Provider no-call tests
используют synthetic transport. T1 can serialize these migrations independently of approvals.

## Full synthetic collection/page/run literals for SQL parity

`tests/fixtures/wb_collection_golden_v1.json` contains ten full synthetic vectors,
not provider evidence: CollectionRequest input/canonical_ascii/SHA256, original
raw_utf8_text for each page (UTF8 encode once, never reserialize JSON), raw byte count
and SHA256, offset/received_at/terminal/unknown source time, exact typed observation
projection, complete flag and manifest_canonical_ascii/byte count/SHA256.

Existing source implementation pins: `02591f3c249fbb32082e802b27ca7a058fff83e6`
`app/modules/wb_source_requests.py`; last shared price/stock chronology change
`901c1796e9a0ab04c0a4c7256e19bcce64dbaef1` in `wb_price_snapshots.py` and
`wb_stock_snapshots.py`. No runtime semantics or encoder changed for these literals.
Manifest formats remain:

- Prices: compact ASCII JSON `["wb-goods-prices/v1",[org,account,limit,request_hash],
  [[offset,limit,raw_hash],...]]`.
- Stocks: `["wb-warehouse-stocks/v1",request_hash,[[offset,limit,raw_hash],...]]`.

Page checksum is raw-byte SHA256, not a second canonical observation encoder.
Terminal is the actual short-page predicate, including an explicit empty terminal
after a full page. Prefix with no terminal stays incomplete. Missing/null/zero,
multiple sizes including unresolved size identity, exact integers above2**53,
Unicode raw evidence and separate account identity are pinned. Parse outer JSON
with arbitrary-precision integers, not JS Number.

Important existing identity limitation: same page bytes/request on different receipt
instants have the SAME manifest hash. `stock-empty-terminal` and
`stock-cross-business-day` deliberately pin this: only the former has one receipt
business date; the latter is complete but not eligible for receipt-based daily row.
Manifest hash is not a run ID, clock, evidence of provider observation time, daily
eligibility, credential generation or authenticated account binding. Keep distinct
run identity/time and the shared expected external/ref+paired generation binding;
do not infer that parent identity from Catalog mapping or a manifest checksum.

The literals were generated once from existing Python parsing/encoding and pinned;
they are not independently validated SQL results. `test_wb_collection_golden.py`
reads them without regenerating, for the final source acceptance batch. No new
intermediate test gate was started for this source-first handoff. New test execution
and SQL parity remain pending; no publication/current-head/CAS acceptance implied.
