# All-products stock circulation

The table now displays warehouse + transit to buyer + transit from buyer.
The additive API field `analytics.circulationStockUnits` does not replace
`wbStockUnits`: available warehouse stock remains separate for trading decisions.
Missing/invalid components yield unknown, not zero. New ingestion preserves missing
transit instead of coercing it to zero. Older persisted aggregates may already have
lost missingness; this change cannot reconstruct that evidence.

Local example nmId 1025784485: 121 + 299 + 193 = 613, from the existing saved
stock aggregate. This is not independent reconciliation with a historical WB report.
The current stock endpoint returns current inventory; selected report dates do not
turn it into historical inventory. The cell tooltip states this limitation and the
three components. Historical end-of-period inventory remains a separate unfinished
integration. Ruble value remains a current-price valuation, now of the same total.

Snapshot version 15 invalidates old SKU response snapshots. Sorting and cell status
use circulation; other trading consumers keep warehouse stock.

Verification: 10 backend tests (stock circulation + buyout summary), 33 frontend
adapter tests, TypeScript check passed. Generated HTML snapshot refreshed.
Local API restarted. No production, push, policy changes or price actions.
