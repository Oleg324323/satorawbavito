# T4 — RNP period/session isolation

## Defect and bounded fix

The mounted RNP consumer retained its previous ready rows while a different period
was loading. Initializing state to loading did not reset it on subsequent requests;
the former source-snippet test passed without detecting the actual defect.

Use the existing scoped-report selection pattern with an in-memory scope containing
session, organization and requested date range. A render for another scope selects
loading immediately; the stable scoped setter checks the active scope both before
enqueueing and when applying a state updater. Existing request cancellation remains.
No endpoint, formula, scheduler, flag or backend change. RNP is still a legacy consumer.
Session material is not logged, serialized to storage or sent to a new destination.

## Executable evidence

The new Playwright/Vitest case mounts the actual RnpReportIsland with synthetic auth.
It observes the first period, holds the second response, confirms old rows are hidden
and loading is shown, then releases the second period. It checks both exact ranges,
groupBy=sku/source=operational/preset=custom, and clears the session: rows disappear,
session-expired state appears and no third request occurs. Every non-fixture or
non-GET request is aborted; no refresh, provider, print/export or other write action.

RED: the actual old implementation retained FIRST-PERIOD-SKU while the second
response was held. GREEN: the new implementation passes. Initial test setup needed
the exact rendered article prefix, an inline image, and past dates to avoid the
existing current-date clamp; these fixture corrections are not product fixes.
Two old source cases are replaced by one substantive browser case; other RNP/Ads
guards remain. No skips or blanket expectation updates.

Full frontend: **282 passed / 21 failed / 0 pending**, exit1, compared with
282/22 before this slice. No new failure IDs; obsolete RNP call-string failure
resolved. Evidence: /tmp/satorna-t4-rnp-period-full.json. Independent scoped review
found no important defect. Fresh TypeScript no-emit and Vite production build exit0
(existing large-chunk/plugin-time warnings); source HTML/generated snapshot unchanged.
Out-of-order responses, organization switching and
polling races were reviewed statically, not exercised by this browser test.

This is not a full green release gate, canonical RNP cutover, backend parity or
production verification. Remaining frontend failures are not waived.
