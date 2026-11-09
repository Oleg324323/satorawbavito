# Week-over-Week: financial buyouts

Scope: only the `sales` composite displayed as **Выкупили** in Week-over-Week.
Orders, expenses, storage allocation, tax and profit keep their existing rules.

## Source and definitions

- Persisted raw operations from `POST /api/finance/v1/sales-reports/detailed`.
- Product: `nmId`; document: `docTypeName`; payment reason: `sellerOperName`.
- Merchandise requires matching `Продажа/Продажа` or `Возврат/Возврат`.
  Compensation, logistics and other service quantities are not sold goods.
- Inclusive dates: `saleDt`, normalized to Moscow time. This is the actual
  sale/return date, not `orderDt`, `rrDate` or report publication date.
- Units: sale quantities minus return quantities, retaining signed reversals.
- Rubles: `retailPriceWithDisc * quantity`, signed by document, rounded at the
  operation grain to integer kopecks. This is the seller price before commission
  and acquiring, not buyer payment (`retailAmount`) or payout (`forPay`).
- Identical `rrdId` records are counted once; conflicting duplicates within a
  projection fail visibly. Existing raw-reader snapshot precedence handles
  overlapping/corrected intervals. Do not deduplicate all events by `srid`:
  sale and subsequent return can share it.
- A financial correction whose meaning cannot prove merchandise movement makes
  affected values unavailable; its technical quantity/payout is not substituted
  for a buyout. Missing fields and dates do not become zero.
- Operations without `nmId` remain in `buyoutSource.coverage.*.unallocated`.

Both comparison periods use these same definitions. A complete source cursor is
required to confirm a period total or zero; stale complete evidence remains
visible with the existing freshness warning. Only missing/stale sources are
collected by the existing queue. No new WB requests or collection jobs are
started by the projection itself.

## API limitations

The public documentation exposes realization details, not a dedicated endpoint
for the seller portal's Income and Expenses screen. This is a projection of the
authoritative financial operations, not a direct download of that private UI.
Portal refresh lag, late financial postings and ambiguous corrections may still
prevent exact live reconciliation. Do not claim reconciliation without comparing
the same seller, product, period and net-return/price definitions.

## Official references inspected

- [WB API financial reporting documentation (official international portal)](https://dev.wildberries.cn/docs/openapi/documents-and-accounting)
- [WB announcement of v1 realization endpoints](https://dev.wildberries.ru/forum/2123)
- [WB financial detail definitions, actual sale dates, payment reasons and quantities](https://seller.wildberries.ru/instructions/ru/ru/material/how-to-read-fimancial-reports-detalization?recommended=true)
- [Income and Expenses report definitions, including net returns and corrections](https://seller.wildberries.ru/instructions/ru/am/material/income-and-expenses-report-am)

The last link is the official Armenian seller guide; it supports the financial
report definition but is not evidence that an individual Russian seller's live
screen has been reconciled. The Russian API documentation was protected by an
anti-bot challenge during inspection; the official international documentation
and WB's v1 announcement were used instead. The project continues to use its
existing `.ru` API host and existing safety/rate-limit controls.
