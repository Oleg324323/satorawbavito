# Digest empty visibility and Stock boundary parity

Local independent frontend slice; no schema, flags, provider requests, operational
exports, production or deployment actions. Browser traffic is locally intercepted.

## Verified behavior

- Actual React Digest with valid typed cached report and no balance points had the
  correct text hidden by `.balance-empty { display:none }`. Browser RED:1FAIL1PASS;
  the one-point positive control already rendered bars/tooltip. React now applies
  existing `.visible` only when loading has finished and there are no points.
- Actual Stock numeric chip filter used <=7 / >=60, while both backend warehouse
  and SKU builders use <7 / >60 (`wb_reports_bff.py:1673,1798`); OOS KPI agrees.
  Three literal-boundary RED cases with positive available stock,22PASS; source
  HTML now uses strict comparisons. Existing zero-stock risk and tag/decision
  overrides preserved; unknown/blank is not coerced to zero. Pure GREEN25PASS.
- Main admitted sequential combined gate: Digest2 + actual Stock filter browser1
  + Stock VM25 = **28PASS**,5.90s,naturalexit0/browserfinallycleanup.
- TypeScript `tsc -b --noEmit`:exit0. `git diff --check`:0.
- Regenerated snapshot source hash:
  `d4fe862941898495b399154995fe59bdac86c45794a457ea84bb1fd03adbdd0d`.
- Independent read-only critic scopedPASS; did not execute tests independently.

Commands from frontend, scrubbed environment, existing installed runtime:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/node node_modules/vitest/vitest.mjs run src/features/vella-parity/digestEmptyBrowser.test.ts src/features/vella-parity/stockFiltersBrowser.test.ts src/features/vella-parity/stockChipContract.test.ts --maxWorkers=1 --minWorkers=1
env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/node node_modules/typescript/bin/tsc -b --noEmit
```

## Limits and remaining work

No fresh full-suite/build claim. Last actual whole frontend remains405PASS4FAIL;
the later reportLoading4case correction passed separately. These focused tests
do not replace all old broad source tests. Digest multidate aggregation, tooltip
numeric accuracy, responsive layout and session/period races are not proven here.
Stock tests establish numeric thresholds, not separate tag/decision override
parity or missing manager/promotion fields. No financial formula was changed.

All heavy runs were admitted and serialized; explicit release sent to T1/T2/T3.
