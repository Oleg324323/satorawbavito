# T2: WB warehouse stock adapter / manifest

Независимый implementation slice `app/modules/wb_stock_snapshots.py` и
`tests/test_wb_stock_snapshots.py` по bounded source request907ec7b. Только explicit
`{"data":[...]}` для WB warehouse source: local field evidence в
`reports_sources_runtime.py:700–731`. Legacy alternate wrappers, async name fallback,
FBS, product metrics и HTTP204 без explicit array не объявляются этим source v1.
Provider acceptance selected shape и complete pagination всё ещё должны быть
подтверждены collector; текущий loader не подключён и не переписан.

Immutable observations сохраняют `(nmId,chrtId nullable,warehouseId)`; missing/null/zero
различимы. No implicit available=quantity+returns, aggregate-by-nm, max(stockCount),
warehouse-name identity или synthetic size. Unknown native warehouse/product identity
отклоняет страницу целиком; malformed/duplicate/oversized не shortening-to-terminal.
Display warehouseName/regionName не projected в этом minimal adapter, но исходные
bytes связаны raw SHA256. No guessed Catalog mapping.

Page содержит exact CollectionRequest, offset, received_at, raw SHA256 и tuple rows.
Run принимает только contiguous prefix с одного context; terminal только short page,
после terminal ничего нет, receive times monotonic, cross-page identity duplicate
отклоняется даже при equal payload (более строгий вариант permitted schema policy).
Manifest canonical bytes: ASCII compact JSON array
`["wb-warehouse-stocks/v1",collectionRequestChecksum,[[offset,limit,rawChecksum],...]]`.
Данные complete run остаются observations, не автоматически опубликованный current.

`received_business_date_msk` только eligibility для receipt-based daily observation:
complete и все accepted page receipts в один день Europe/Moscow. Иначе NULL, включая
timezone conversion overflow. Source-observed time отсутствует; нет подмены его
receipt. Не создаются historical rows или snapshot за query dateTo. Actual daily
revision/head и approved correction evidence принадлежат будущему repository.

TDD missing-module RED →39PASS. Independent critic нашёл owner bigint range и край
datetime.max при MSK conversion:3RED/39PASS→42PASS. Fresh expanded scoped regression
556PASS, Ruff/compileall/diff0. Self-review fixes не меняют formulas/providers/flags.
No actual PostgreSQL/RLS/CAS/atomic publication proof; no production/data/provider calls.
