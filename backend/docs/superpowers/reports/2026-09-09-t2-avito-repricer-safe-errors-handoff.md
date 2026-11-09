# Avito repricer diagnostic/error boundary — source handoff

**IMPLEMENTED / UNVERIFIED**. Exact bounded paths: repricer router diagnostic/
failure spans and this report. No tests/test authoring, review, import, compile/
lint, PG/Redis, provider/network, price action, flag, push or deployment executed.
Existing local untracked `.superpowers/` is preserved.

## Changed output contracts

- Price-apply failure no longer logs approval/item IDs or repr/str of exception.
  It logs fixed `AVITO_PRICE_APPLY_FAILED`. The same HTTP409/code is raised with
  fixed `Avito price apply failed`, OUTSIDE the exception handler, no raw chain.
  Approval removal/history success updates remain after this failure exit only.
- OAuth failure still returns HTTP409/AVITO_OAUTH_FAILED, raised outside except
  without the raw request-bearing cause. No credentials/resolver change.
- Rate-limit classification still examines the existing internal error before
  sanitization; cooldown duration remains **70 seconds** with the same account
  selection, cache key, retry time and save/read behavior. Cache message is fixed
  `Avito HTTP429` text (rendered `Avito HTTP 429`); historical cached message is
  never echoed. Outward cooldown uses fixed rate_limited/retryable true/
  AVITO_RATE_LIMIT, retaining canonical retryAfterUntil/seconds.
- `_safe_source_error` projects only locally known codes with fixed messages,
  exact bool retryable, known fixed blockers, parsed aware retry datetime and
  builtin nonnegative seconds. Unknown code becomes avito_request_failed; raw
  message/extra fields are dropped. No generic recursive secret redaction claim.
- `_safe_source_diagnostics` permits only builtin nonnegative groupings,
  dataTotalCount, itemsCount, pages and valid builtin HTTP status; bool, arbitrary
  keys, previews, request URL/body, ID inventories and provider text are omitted.
- New response construction and historical cache-hit/stale paths apply these
  projections. Cached business rows, sorting/strategy decoration, summary, account
  and period metadata are not sanitized as if they were debug data. Cache hit
  continues clearing error; stale response keeps its existing partial/stale state
  and receives only sanitized error/diagnostics. No stored history/cache migration.

Known error vocabulary retained here: auth_required, forbidden_scope, rate_limited,
avito_server_error, avito_request_failed, transport_error. Known blocker vocabulary:
AVITO_AUTH, AVITO_SCOPE, AVITO_RATE_LIMIT, AVITO_STATS, AVITO_LISTINGS, AVITO_CHATS.
Their messages are fixed local strings; unrecognized provider codes/blockers are
not returned merely because they have plausible names. Diagnostic helper signatures
retain existing message arguments for compatibility but do not use their raw values.

## Preserved behavior / explicit nonclaims

Provider price request, OAuth/request endpoints, credentials/account resolver,
price permission checks, disabled-by-flag409, pending approval mutation/history,
successful result payload, strategy formulas and operational flags are unchanged.
Failures are not converted to success; no retry/send/fallback was added. This
hardening does not solve org-only resolution, concurrent Avito approvals or
external price idempotency. Existing upstream client logs and price-history event
error storage outside these selected spans remain separate surfaces; no claim
that every Avito error path is safe. Existing source.api endpoint metadata remains
business/debug contract as before, not a rewritten configuration/URL policy.

## Pending central acceptance

Canaries in exceptions, IDs, URLs, provider messages/body/key inventories and old
cached diagnostics/errors must not reach these outputs/logs/chained HTTP errors.
Assert failure409 leaves pending/history unchanged and disabled mode never calls
provider; OAuth same status; rate detection and exact70-second cooldown; typed
bool/count/status rejection; cache-hit/stale rows/summary/account/date equivalence;
same outbound request and success behavior using fakes only. All gates pending.
Rollback reverts source/report only; no runtime state or credentials were changed.
