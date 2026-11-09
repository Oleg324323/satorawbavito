# WB repricer reporting

Scope: WB reporting only. No marketplace price writes, Avito changes, or financial-policy changes.

## Source flow and definitions

- Calendar: last completed day in `Europe/Moscow`, independent of cache coverage. Manual input and calendar create a draft; Apply commits the inclusive range. Invalid/future ranges are rejected.
- Missing/stale operational observations: `productsSourceSync` → existing source-selective `/api/v1/wb-repricer/sync/run` (`period-stats`, `baskets`, manual, force=false) → persisted exact-period caches → SKU list. Finance keeps its separate `repricer-finance` report-worker job.
- Orders: `/api/analytics/v3/sales-funnel/products`, keyed by `product.nmId`; `statistic.selected.orderCount` and `orderSum`. The `past`/comparison period never supplies the selected-period columns.
- Buyout: `statistic.selected.conversions.buyoutPercent`, unchanged from WB (no sales/orders formula or averaging daily percentages). Broader daily caches supply additive counts but explicitly lack the exact-period conversion; that condition triggers an exact-period collection. An exact funnel aggregate is not replaced by a newer broader daily rollup.
- Sales: the existing financial-operation projection's gross `salesUnits` and `grossSalesKopecks`, before returns. Its seller-price basis uses historical operation prices, not today's catalog price. Returns and net `sellerRevenueKopecks` remain separate. Existing financial calculations are unchanged. These operations are not WB funnel order-cohort buyouts, so equal totals are not assumed.
- Missing observations stay null; explicit zero stays zero. Partial coverage, stale source timestamps, and loading failures are visible. Stitched sources retain their oldest observation for freshness checks.

WB references: [API analytics](https://dev.wildberries.ru/en/openapi/analytics), [seller funnel documentation](https://seller.wildberries.ru/instructions/ru/by/material/sales-funnel-report). API documentation was available via indexed excerpts; direct retrieval returned 498. No live WB account values were compared to the reference screenshots.

## XLSX

Repricer Export → XLSX uses a dedicated `/api/v1/wb-repricer/table.xlsx` endpoint, not the nomenclature/settings import template or ABC/P&L dataset. The client reads every catalog page (500 per request), applies the same UI predicates and sorting to the complete collection, and sends ordered nmIds and filter metadata. The server reconstructs all data columns from the authenticated organization's caches. Hidden columns are included, selection does not limit rows, source changes/incomplete pagination abort rather than silently truncate. Reporting dates, freshness/coverage, and definitions are recorded in the workbook.

Quantities, RUB amounts, and fractional percentages are numeric Excel cells with corresponding formats; identifiers and full text are strings. Formula-like product text remains literal inline text. Missing data is blank. Photo data is exported as its source URL, not rendered images.

## Checks and limitations

Targeted node tests cover Moscow dates, source loading/cancellation, mapping, buyout null/zero, export pagination/filter/sort/revision rejection. Backend tests cover exact WB request dates/comparison, nmId mapping, gross/net separation, cache conversion provenance, the authenticated export route, and XLSX XML/types/formatting/completeness beyond 150 rows. No browser or rendering job is used.

The pre-existing `test_repricer_stats_baskets.py` suite has 13 failures concerning finance cache materialization/tax/source revisions. The same 13 failures were reproduced with the current reporting changes reversed in memory only. They are not evidence of a successful live WB comparison and were not hidden by changing safety checks.

The broader sync test `test_wb_sync_runs_four_independent_sources_in_parallel_and_waits_for_goods` also fails independently: its `save_source_cache` mock rejects the existing `strict` keyword. The sync implementation was not changed by this reporting task. XLSX was additionally opened with an independent reader (openpyxl): 621 product rows, 34 columns, literal text, numeric RUB and percentages, and complete filter range passed without rendering.
