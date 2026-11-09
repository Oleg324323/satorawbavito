# Avito chats/stats safe diagnostics — source handoff

Status: **IMPLEMENTED / UNVERIFIED**. Exact T1 delegated paths: `app/avito/chats.py`,
`app/avito/stats.py`, this report. No provider/network, imports, compile, tests,
reviews, PostgreSQL/Redis gates or production operations performed. Final canary
and behavior acceptance belong to the centralized queue.

## Error boundary

Generic fetch exceptions retain `blocked`, `transport_error`, retryable=true and
existing AVITO_CHATS/AVITO_STATS blocker, but message is the fixed
`Avito transport error`, never `str(exc)`. Genuine HTTP401/403/429/5xx/other mapping
and status are unchanged. Only exact builtin integer HTTP100..599 can be formatted
into an error/diagnostic message; malformed status becomes fixed request-failed
text, without coercion or arbitrary object repr. Chats typed HTTP upstream errors
are raised `from None`, not chained to the request-bearing httpx error. Normal
HTTP status is retained; malformed non-HTTP status uses502 only for this already
typed upstream-error boundary. No retry or provider-call behavior was introduced.

No new generic send/read exception-to-status policy was invented: non-HTTP failures
of those action methods retain their pre-existing propagation path. This slice
sanitizes generic **fetch** results and the existing typed HTTP action wrappers;
broader action exception classification requires its own explicit contract.

## Intentional diagnostic contract changes

Chats raw preview keeps only fixed type `dict|list|other` and a builtin list count;
no payload/source keys, first-row inventories, custom type names or previews.
Debug print has a fixed label, emits only that projection, and has no raw fallback.
Result diagnostics retain chats/messages counts and safe rawChats projection.
`rawMessages` is null; `messagesFailed` is an unkeyed list of locally derived safe
HTTP errors/statuses rather than a dictionary indexed by provider chat IDs.
Actual chats/messages rows, their IDs/text/images and fallback-message business
behavior are unchanged. `_messages` retains its optional diagnostics argument for
call compatibility but no longer populates it with source-derived keys.

Stats requestUrl/requestBody/bodyPreview/resultKeys/itemIdsSample are null.
Status is a strict builtin integer HTTP100..599 or null; grouping/count fields are
builtin nonnegative integers or null (bool is not accepted). Reason is only the
existing locally derived `avito_returned_timestamp_without_groupings` or null.
Logging independently allowlists those four fields; URLs/bodies/previews are not
logged even if an arbitrary diagnostic dictionary is passed. No string conversion
of an untrusted diagnostic value is used. Chunk dates/offset are null; totals
grouping is the fixed local `totals` enum. Page/chunk/count shape remains diagnostic.

### Actual internal dependency preserved

`_v2_item_analytics_for_account` previously consumed diagnostics.groupings and
diagnostics.dataTotalCount for pagination. Those control values now read directly
from the original result payload with the **same existing** list/int interpretation,
before existing offset/break logic. Diagnostic sanitization must not change the
business pagination path (including its existing malformed-value behavior). No
request endpoint/body/auth, metric parser, aggregation, sourceStatus, date chunks,
items/accounts/daily DTO, cache, permission or task semantics were changed.

No frontend files/callers were inspected or altered under this bounded delegation.
Consumers of diagnostic-only fields must accept the intentional null/unkeyed-safe
projection. If a downstream consumer uses raw diagnostics as business data, that
must be reported and separately resolved, not supplied fabricated replacements.

## Pending final verification / rollback

Canaries in exception messages, URLs/querystrings, request/response bodies, headers,
chat identifiers, arbitrary keys and malformed status/count objects must not appear
in fetch errors/diagnostics/stdout/logs or chained typed HTTP action traceback.
Verify bool/str/custom-object values are not coerced into diagnostic counts/status;
normal HTTP mapping, message fallback, metrics, offsets/full pagination, totals and
daily outputs remain equivalent. Exercise repeated fetches and empty payloads.
No PASS claim is made. Rollback reverts these two source files and this report;
no stored data, grants, configuration or production rollback is needed.
