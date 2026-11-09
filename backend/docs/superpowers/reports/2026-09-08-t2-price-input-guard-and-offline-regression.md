# T2: current buyer-price input boundary and offline regression

## Scope and delivery

Branch: `codex/arch-t2-economics`; preceding commit: `5803e932a50f8eb21e376367add311d5ee9ea071`.
No migration, ORM, shared wiring, config, formula, flag, provider or frontend changes.

`app/modules/wb_repricing_price_inputs.py` is an unwired pure boundary, not a price source or send authorization.
It preserves an immutable observation, exact account/product/optional source-offer grain,
request checksum and original observed time. Internal org/account IDs reject bool/string/zero.
Missing money differs from explicit zero. Both prevent repricing. Historical buyer price,
seller price, Club and wallet values cannot qualify as current buyer price.
Incomplete observations, unknown/future timestamps and expired observations cannot qualify.
TTL and `now` are explicit inputs; this module neither establishes provider provenance nor
chooses a business freshness policy. Complete-run proof remains an adapter responsibility.

## Remaining legacy risk

`app/wb23_runtime.py:405` stamps read time; line 410 falls back from
`buyerPriceNoWalletKopecks` to `avgPriceWithSppKopecks`. This report does NOT claim that
legacy behavior is fixed. Wiring requires original source observation time, source kind,
complete run and account/offer ownership to survive the current DTO/cache path. Unknown
time must stay unknown, not be replaced by now or epoch. Existing approval/economics/SPP
guards must remain in addition to this input check. No unverified buyer price may unblock send.

## Test debt and isolation

- Legacy calculation characterization grows from 53 to 80 cases: cross-midnight windows,
  night median/direction/caps, skip guards, liquidation floors/due time, strategy resolution.
  Synthetic mutable containers are restored through monkeypatch. Formula code is unchanged.
- Full RNP test module: three stale provider mocks now target current cache boundaries;
  cache fixture uses existing v5 key. Consumer assertions retained. This follows an isolated
  red run (4 failed, 5 passed); it is not a repository-wide baseline claim.
- Cache-store tests isolate DB/Redis boundaries. The insert-race test still supplies its
  FakeSession and checks rollback/update behavior; it is not a two-session PostgreSQL proof.
- Opt-in `tests.repricer_offline_plugin.py` blocks Python DNS/TCP/UDP, persistent SQLite,
  `.env` reads and legacy runtime-state file access before collection. Caught forbidden I/O
  still makes session exit nonzero. The subprocess self-test verifies all six denials,
  permitted in-memory SQLite, and session failure after caught denials.
  This is NOT an OS sandbox: native libraries and child processes need separate isolation.

## Verification

Run from `backend` with
`/Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python`.
New guard and plugin were tested red with missing modules, then green. Price guard has 32 cases.

```sh
python -m pytest -q -p tests.repricer_offline_plugin \
  tests/test_economics_internal_identity.py tests/test_economics_policies.py \
  tests/test_abc_pnl_costs.py tests/test_finance_normalization.py tests/test_abc_pnl_service.py \
  tests/test_wb_stock_page_validation.py tests/test_wb_price_units.py \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_paginates_until_short_page \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_uses_official_max_page_limit_by_default \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_keeps_rows_when_next_page_is_rate_limited \
  tests/test_repricer_cache_store.py tests/test_repricer_calculation_characterization.py \
  tests/test_wb_source_revision.py tests/test_wb_repricing_approval_domain.py \
  tests/test_wb_repricing_repository_contract.py tests/test_rnp_source_period_isolation.py \
  tests/test_rnp_runtime.py tests/test_wb_repricing_price_inputs.py tests/test_repricer_offline_guard.py
```

Result: **396 passed in 2.15s, exit 0**. The narrower changed-area group: **142 passed, exit 0**.
No full historical suite or real persistence test claim. Python compilation and diff checks
are performed before commit. Manual critic pass: legacy risk and provenance limitations
remain explicit; no durability, live-data completeness or send-safety completion claimed.

## Dependency and next permitted work

T1 observed at `edd9d4b927252148b7e25d6933b9834a307d9493`; accepted approval/attempt/audit
schema still absent. Atomic SQL claim, real two-session races, rollback/restart/backfill,
RLS and durable dispatch therefore remain pending. Independent synthetic source-grain,
revision and crash-policy tests can continue. Financial decision approval is separate;
final net profit/class/ABC remain null until approved rules and complete evidence exist.
