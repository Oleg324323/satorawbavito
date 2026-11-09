# WB realization: database to presentation

## Cause

The persisted detailed operations were current. The old derived repricer
projection selected merchandise by `saleDt` and exposed seller price multiplied
by quantity as public revenue. For 2026-10-05 through 2026-10-11 this produced
4,681,032.57 RUB and 1,858 net units. It both used the wrong money basis and
excluded a delayed sale posted inside the requested financial report period.

## Corrected contract

- Public financial realization is signed `retailAmount`, selecting the exact
  inclusive Moscow `rrDate` range. `retailAmount` is an operation total; do not
  multiply it by quantity again. Buyouts are qualifying merchandise sales minus
  merchandise returns, using `nmId`, document and operation classification.
- Compensation and service quantities are not merchandise. Financial credits
  remain separate and must not reduce realization or be added twice.
- Seller-price revenue remains a separate internal input. Price strategy,
  execution, seller discounts, account settings and operational funnel/order
  metrics are not replaced by settlement amounts.
- The settlement commission bridge uses the same realization money basis as
  public revenue, keeping the payout identity intact. ABC and P&L use identical
  input definitions. Existing cost/tax rules retain original business dates;
  a delayed posting does not move a sale into another tax quarter.
- Explicit zero and negative realization never fall back to seller revenue.
  Missing trade money/quantity/date cannot certify a zero total.

Repricer, Statistics, financial report projections, ABC/P&L and Week-over-Week
now use this definition. Existing frontend mappings retain kopeck precision.
Both comparison periods use the same source rules. Independent operational
funnel and advertising performance definitions are unchanged.

Only affected derived cache versions changed: page/report projection, list
snapshots, ABC, P&L/expenses, digest, RNP response and week response. Raw API
coverage and cursors are reused. No source rows, user settings, credentials,
prices or history were deleted or rewritten.

## Evidence

The preceding direct, read-only WB API check paginated the weekly detail to
its terminal response (19,853 rows) and reconciled both weekly report types:

- Sale realization: 3,078,852.48 RUB / 1,968 units.
- Returns: 158,598.93 RUB / 109 units.
- Net realization: **2,920,253.55 RUB / 1,859 units**.

The local current-connection daily raw cache independently contains the same
rows/totals for the requested posting range. After rebuilding the versioned
derived projection, actual DB-backed repricer summary, Statistics, shared
ABC/P&L source and week source all reconciled to 292,025,355 kopecks and 1,859
units. Actual ABC and P&L report row sums also reconciled to that money total.
The raw-cache fingerprint before and after verification was identical. These
DB checks denied external connections except local infrastructure; they did
not download history again.

The official schema was fetched and checked directly:
https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml
(`retailAmount`: realization; `rrDate`: operation date; `saleDt`: sale date;
`retailPriceWithDisc`: retail price including agreed seller discount).

Targeted regressions cover date boundaries, both periods, product matching,
returns/compensation, duplicates, credit/payout reconciliation, zero vs missing,
old snapshots, current raw data taking precedence over old aggregates, DB reuse
and raw revision invalidation, seller price preservation and delayed-sale tax
quarter preservation. Frontend checks cover exact fractional rubles and export
consumers. No production deployment or live price mutation is part of this fix.

## Verification outcome

- 498 targeted finance/attribution/tax/metric tests and 58 frontend tests passed;
  TypeScript and `git diff --check` passed.
- An expanded legacy BFF run initially found one new side effect: rebasing the
  planned commission/acquiring/tax inputs on public realization. It was fixed
  by retaining seller-price revenue for those planning inputs; a dedicated
  regression compares all planned fields/settings before and after projection.
- Seven unrelated legacy BFF failures reproduce with the repository HEAD
  repricer/router modules: four advertising-diagnostic expectations, a missing
  dispatch persistence fixture, a legacy monthly-cache fixture, and a simulator
  fixture requiring unavailable PostgreSQL storage. They were not changed.
- A TestClient HTTP read of `/api/v1/wb-repricer/sku` with a test actor bound to
  the actual local organization returned 150/3556 rows and the correct net money
  and units. The first build took 2.49s; a cached repeat took 0.079s. Authentication
  settings were not changed; this is a data-contract check, not a browser login.
- After the final planning fix, snapshot v21 was rebuilt and checked again:
  3.268s first response, 0.086s repeat, identical correct money/units. The local
  stack was restarted with user approval; frontend/API/Redis/report-worker
  readiness passed. The served frontend module retains fractional rubles.
