# Avito Failure Surface Handoff

**IMPLEMENTED / UNVERIFIED.** Four-path temporary delegation from T1. No tests,
test authoring, import, compile, lint, review, DB, Redis, provider, credential,
network, production or activation actions were performed. This is not a claim
that all Avito diagnostic surfaces are sanitized.

## Changes

- `app/avito/returns_tasks.py`: existing decorated task name, signature, retry
  count and dispatch remain. Its body delegates to a private implementation under
  one outer failure boundary, covering settings/credential lookup, client calls,
  upsert and status persistence. Failure attempts to save the fixed string
  avito_returns_sync_failed with the existing failed state and retryable=True.
  Failure of that status save is contained. A fixed RuntimeError is raised outside
  both exception handlers, so Celery still receives a failed task, not a synthetic
  successful/skipped result, without original chained exception content.
- Blocked task errors retain object/null shape, closed known codes, boolean retry
  flag and known blockers, but replace arbitrary source messages. Completed and
  blocked diagnostics retain only fixed endpoint, bounded integral HTTP status and
  counts, plus fixed error/null reason. URL, request parameters, provider key names
  and unexpected diagnostic members are not returned or cached by this boundary.
  Successful business upsert/count/date/status payloads are otherwise unchanged.
- `app/routers/avito_orders.py`: return inventory enrichment failure retains
  unavailable status and zero counts with fixed avito_return_inventory_unavailable.
  Browser filter fallback retains rows/source/status but emits fixed
  avito_orders_filter_unavailable instead of exception text. Existing HTTPException
  handling and scope/auth/route/cache behavior are unchanged.
- `app/routers/avito_overview.py`: caught exception conversion returns closed
  code/message rather than dynamic class/text. The existing rate-limit classifier
  is retained internally to preserve cooldown/stale selection; only its boolean
  result escapes. Retryable remains True. A broken exception formatter falls back
  to generic unavailable, rather than throwing another diagnostic error.

## Explicit Residual Risks

The actual local Orders client still logs diagnostic request URL/parameters before
these return-task boundaries and can build transport errors from raw exception
text. It was outside the delegated files and was not changed. Other overview
branches directly forwarding typed client errors and historical cached errors
were not part of the delegated `_section_error` span. Thus this patch cannot claim
global secret-safe logging or sanitization of all successful/cached HTTP responses.
T1 retains source-client/shared diagnostic and credential cutover ownership.

Legacy returns tasks still enumerate organizations (including the existing empty
list fallback), use organization-level credentials/cache/storage and scheduler
identity, and do not implement canonical permission-filtered account selection.
Those business and security migrations are not silently performed here. Normal
Avito return matching is preserved; it is not the removed standalone KIZ matcher.

No historical JSON/cache records were rewritten or erased. Existing operational
defaults, schedules, retry declarations, task arguments and browser stale labels
are preserved. Changing exception class/message is intentional failure-surface
compatibility, not evidence of successful task execution.

## Final Canary Cases Pending

Central verification must inject synthetic secret canaries at initial settings,
credential access, provider read, return upsert, success/blocked status write and
failed-status write. Assert failure remains failure, persisted error and serialized
Celery exception contain only fixed content, and no cause/context retains the raw
exception. Exercise unknown diagnostic keys/URLs/body keys, oversized or boolean
counts/status, known blocked codes/retry flags, and unchanged successful business
DTOs. Check return inventory and browser fallback rows/types, HTTPException path,
overview rate-limit cooldown/stale branch and broken exception formatting.
Run existing return matching/task/router regression at the final coordinated gate.

Rollback is a bounded revert of this commit's four paths; it would restore the
unsafe diagnostic behavior and should not be used to recover deleted data because
no persisted user data was changed by this source package.

## Follow-up: Orders Client Diagnostic Output

Separate two-path source-only delegation closes the specific upstream leak noted
above in `app/avito/orders.py`. The previous residual paragraph describes the
state before this follow-up, not the current Orders client diagnostic output.

The real GET, URL construction, headers, parameters, paging, row parser, total,
status handling and timeout/retry behavior remain unchanged. Diagnostic output
no longer contains requestUrl/requestParams, payloadKeys/resultKeys/firstOrderKeys
or any source row identifier. Only fixed endpoint, bounded integer HTTP status,
bounded count/rawCount and fixed/null reason remain. Out-of-range diagnostic
integers become null, never truncated business counts. The logging function
independently projects only these fixed fields; direct callers cannot log arbitrary
dictionary values or provider identifiers through its `%s` arguments.

Transport failures now return the same blocked/transport_error/retryable DTO with
a fixed message, constructed outside the exception handler. HTTP failures retain
existing known codes/blockers/retry behavior for valid statuses; invalid status
types/ranges produce a fixed request-failed error without formatting raw input.
No original exception is returned, raised, chained or logged by this boundary.

No test authoring, tests, imports, compile, lint, review, network, providers or
services were run. Final canaries must cover URL credentials/query canaries,
provider top-level/row key canaries, transport exception strings, forged diagnostic
values with sensitive repr, HTTP 401/403/429/5xx and exact unchanged request/row
semantics. Diagnostic-only consumers expecting removed keys need their assertions
updated; removed fields are intentionally not retained as compatibility leaks.
Other overview typed/cached errors and external HTTP-library/application logging
remain outside this source-only change. No whole-Avito sanitization claim is made.

## Follow-up: Overview Typed Errors And Historical Read Output

Separate two-path delegation covers the remaining identified Overview diagnostic
spans. A single closed projection is used for stats/listings/chats/reviews typed
errors and caught exceptions. Known codes and blockers are allowlisted, messages
are fixed, retryable is accepted only as a boolean, and unknown section keys are
omitted. Existing rate-limit classification may inspect raw text internally but
emits only a safe rate-limit enum; formatter/projection exceptions return a fixed
unavailable object without dynamic repr or provider error text.

All four typed result branches now project before response/cache writes. Diagnostic
events use only the four known section names and fixed text. Historical cache reads
copy source.errors through this projection and rebuild only exact event kinds
stats_blocked/listings_blocked/chats_blocked/reviews_blocked. The actual event
discriminator is `kind`, not an invented ID. Other business events retain their
original objects, ordering and payload; summaries/accounts/topItems/source.api and
source.sections remain outside the diagnostic edit. No stored cache is rewritten.

Cooldown messages are fixed on write and read. Existing 70-second policy, key,
timestamp comparison, naive-time UTC treatment and branch decisions are preserved.
retryAfterUntil is emitted only after parsing a bounded string and canonical
datetime re-encoding; invalid diagnostic timestamps become null or are omitted,
never reflected as raw strings. Safe positive integer retryAfterSeconds may be
retained. Cache hit/stale status and stale partial semantics remain explicit.

Pending final canaries: four typed errors and exceptions containing synthetic
secrets in code/message/blockers/extra keys; historical source.errors and all four
blocked-event text fields; unknown section keys; malformed timestamp and formatter;
same business rows/events before and after projection; current/historical 429
cooldown and stale/hit behavior; no source-cache write during cached read. No tests
or other gates were authored/run for this source-only delivery.

The previously noted typed/cached Overview diagnostic residual is addressed in
these exact spans, not globally. Arbitrary business fields, external library logs,
other routers and the unchanged legacy organization-level credential selection
are not certified by this change. No schema/auth/provider request/TTL/registration
or KIZ behavior was modified.
