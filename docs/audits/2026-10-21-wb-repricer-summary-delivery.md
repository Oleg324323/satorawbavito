# WB repricer summary delivery and extension connection — 21 October 2026

## Scope

WB repricer read paths and the WB extension's connection/download verification.
Existing unrelated changes were preserved. No Avito code, pricing strategies,
marketplace prices, discounts, tax settings, costs, mappings, migrations or
scheduled collection were changed. No production deployment or service restart
was performed. Source-data investigation used query-only local SQLite with
outbound sockets denied and derived cache writes intercepted in memory.

## Confirmed defects and repairs

1. **Missing buyer price still displayed a downward price hint.** Both the
   production HTML renderer and its runtime override compared seller/base prices
   while rendering `нет данных` for the displayed buyer price. The hint is now
   suppressed when the displayed price is missing, or the compared seller/base
   values are unavailable. Existing valid-price hints, pricing bases, SPP
   percentages and manual price controls are preserved. No quote is invented.

2. **The previous buyout comparison could reconstruct the entire expense ledger.**
   Profiling identified full financial projections on the summary's comparison
   path, although that comparison consumes only net trade units and retailAmount.
   It now first probes an existing source-revision-valid full projection without
   rebuilding it. A cold comparison uses a separate trade-only projection through
   the existing account/connection-scoped raw-source loader. Posting dates,
   signed sales/returns, duplicate/conflict validation, product filtering,
   unallocated trades, coverage, freshness and equal-length periods are retained.
   The small projection is separately versioned and cannot certify expense,
   storage-allocation, payout or final-profit data. Request memoization includes
   organization, connection and dates. No additional WB request is introduced.

3. **Financial advertising was captioned with unrelated campaign counts.** The
   amount already prioritizes financial-report `WB Продвижение` deductions,
   including cabinet-level charges without nmId. The repricer card now identifies
   that financial source instead of presenting campaign/product counts as its
   provenance. Campaign statistics and their other consumers are unchanged.

4. **The profile download link targeted an absent generic ZIP.** The existing
   deletion was not reverted. The link now targets the already available,
   verified `satorna-wb-prices-extension-0.1.12.zip`. The local frontend serves it
   with HTTP 200 and `Content-Type: application/zip`. No collector, permission,
   token issuance or authentication implementation was modified.

## Source and rendered-value reconciliation

All dates are inclusive. Values below come from saved local financial sources,
not a new live WB reconciliation. Whole summary objects before and after the
comparison optimization matched on all four ranges.

| Period | Coverage | Revenue, RUB | Net units | Returns | Financial advertising, RUB | Expenses, RUB | Dated COGS, RUB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 05–11.10 | complete | 2,920,253.55 | 1,859 | 109 | 134,923.00 | 1,034,806.19 | 1,487,200.00 |
| 12–16.10 | complete | 2,136,609.29 | 1,329 | 64 | 0.00 | 788,323.55 | unavailable |
| 11.10 | complete | 439,779.55 | 267 | 15 | 134,923.00 | 292,182.83 | 213,600.00 |
| 14–18.10 | partial | 1,354,850.05 | 835 | 36 | 0.00 observed | 516,757.81 | unavailable |

The partial range's values describe only its stored coverage, not a confirmed
full-period total. Advertising was not moved between weeks and was not replaced
by campaign spend. Available COGS includes existing assumed cost records; only
1025784485's 800 RUB/unit was previously user-confirmed. This task did not confirm
any other cost.

An isolated Chromium/Chrome component replay used these source-backed summary
values with a synthetic product row. It checked the rendered advertising,
revenue, buyout quantity/amount, net sales, returns and COGS states, retained
unavailable margin/in-sale values, and absence of the misleading price hint.
The campaign endpoints were not needed for the monetary advertising card.
Separate browser regressions exercised date switching, late responses, expiry,
editing guards and absence of price submissions. This is not verification of
every real product row or authenticated production HTTP behavior.

## Local timing

Two sequential paired runs per range; identical persisted data, no provider
requests, request/derived in-memory caches reset between runs. Medians include
the current finance read and summary construction. These are small local
samples, not production latency measurements or an SLA.

| Period | Prior path, seconds | Updated path, seconds |
| --- | ---: | ---: |
| 05–11.10 | 4.53 | 3.98 |
| 12–16.10 | 0.60 | 0.54 |
| 11.10 | 1.28 | 1.20 |
| 14–18.10 | 2.63 | 2.16 |

An earlier run overlapped browser/type-check work and had highly variable
timings, including a slower optimized sample; it is not used as speedup proof.
Cold current-period projection remains necessary when no reusable projection
exists. No broader optimization or reporting architecture rewrite was attempted.

## Genuine gaps deliberately left unchanged

- **12–16 October: 17 financial product contexts still have
  `tax_mapping_missing`.** No catalog links were guessed or created; incomplete
  cabinet COGS stays unavailable.
- **Final margin and profit ABC:** existing payout, dated-economics and shared
  expense attribution requirements remain. Unallocated amounts include
  11,134.80 RUB storage and 42,990 RUB other deductions for 12–16 October.
- **Promotion participation:** the separate promotion membership cache is empty.
  The existing 0% display is not evidence of verified nonparticipation. No new
  collector, import or membership inference was introduced.
- **Products in sale:** the existing price/variant/stock verification limitation
  remains; no guessed availability is shown.
- **Current buyer prices/SPP:** absent or expired observations remain unknown.
  Freshness limits, size/account/seller-price matching and financial history were
  not relaxed or replaced with current quotes.

## Extension verification

- Source popup and the downloadable 0.1.12 ZIP contain exactly **one credential
  input**, without a server-address input.
- Actual platform encoder output is accepted by the actual extension parser for
  production and loopback origins, preserving exact local ports and ignoring an
  unrelated saved origin.
- The entire copied profile value includes its issuing origin. Users need not
  enter an address. A bare legacy raw token cannot independently identify an
  arbitrary local server; its existing default/verified-origin rules remain.
- Malformed/wrong-marketplace keys, unsafe destinations, permission denial and
  failed replacement preserve the existing safety behavior. No automatic origin
  probing or credential fallback was added.
- ZIP integrity and the packaged parser were checked separately from source.
- The actual extension-worker → HTTP → disposable PostgreSQL roundtrip was
  exercised with synthetic credentials, along with token/account/observation
  authority tests. No real user key was issued, rotated, printed or revoked.
- The user's installed Chrome copy and live provider collection were not
  controlled, reinstalled or claimed verified.

## Checks and limitations

- **117 targeted backend tests passed** for repricer finance/presentation/cache
  and local/browser-price projection paths.
- **78 extension/backend PostgreSQL tests passed**, including worker roundtrip,
  token authority, durable snapshots and account isolation. The first fixture
  start failed because macOS's temporary Unix socket path exceeded 103 bytes;
  rerunning in a dedicated short `/tmp` test directory passed. Working data was
  not used by those disposable databases.
- **39 focused frontend tests passed**, including real browser rendering,
  period races, extension settings, encoder/parser interoperability and price
  hints. Updated summary assertions and the download-link check were rerun.
- **90 extension Node tests passed**, plus manifest/syntax checks.
- TypeScript and `git diff --check` passed.
- An expanded legacy BFF run was stopped after **162 passed / 5 failed** to
  investigate its failures. All five failures reproduced with the prior
  comparison implementation restored in process memory: one dispatch-storage
  fixture failure and four pre-existing statistics/CTR/DRR expectations.
  No price-dispatch or Statistics behavior was changed to silence them. The
  remainder of that broad run is not claimed passed.

After the user's authorization, the local API and report worker were restarted
gracefully. API health and frontend HTTP checks returned 200, the report worker
answered its readiness ping, and neither startup log contained new error lines.
Redis and the frontend were left running; no database reset, settings change,
marketplace price write or production deployment was performed.
