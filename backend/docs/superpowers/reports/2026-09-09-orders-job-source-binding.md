# T3 response: user Orders job source binding

Reviewed proposal `949f628fe6d247f443f94719540487a5b6a08b15`, guard design
`1406686de74b52e1d7c92c3d9975d55b778c0cc6`, actual local Avito/WB acquisition code.
Accept one account/session/job, fixed trusted `sync:run`, metadata-only authority,
queue locator only, per-attempt fencing and explicit policy snapshot. No autonomous
scheduler, browser-fetch fiction, provider POST, KIZ or fabricated worker admin.

## Observed source requests, not invented provider semantics

| Source | Executable evidence | Exact local selectors |
|---|---|---|
| avito-order-management | app/avito/orders.py:53-57,188-189,356-363 | GET/order-management/1/orders; dateFrom nullable calendar date converted to UTC midnight Unix seconds; statuses list joined by comma when nonempty; limit1..20 and page>=1 |
| wb-statistics-supplier-orders | app/wb_api/reports_sources_runtime.py:528-533 | GET/api/v1/supplier/orders; dateFrom date formatted `YYYY-MM-DDT00:00:00` without offset; this call supplies no dateTo or flag |

Avito empty statuses omits the parameter. None dateFrom omits the parameter; it
does NOT prove all-history or latest-window semantics. No dateTo exists in the
inspected Avito request. No authoritative half-open remote interval, provider
immutable snapshot token or terminal manifest proof established in these sources.
Legacy synced/page-length fallback total is explicitly insufficient.
WB dateFrom without timezone is not evidence for a UTC bound interpretation.

## Request v1 amendment needed before handler registration

Do not freeze proposed requestedFrom/requestedTo as the full provider request.
Add a typed `sourceRequest` discriminated object, not arbitrary kwargs:

- Avito source request exact fields: `dateFrom` null or ISO calendar date;
  `statuses` array of exact nonempty status strings (no trim/alias/comma embedded);
  `limit` integer1..20; `page` integer>=1. All explicit, no silent model defaults.
  These describe a single page acquisition, NOT complete canonical synchronization.
- WB inspected request exact field: `dateFrom` ISO calendar date rendered exactly
  as above. Flag/dateTo/endpointMode are not added without another reviewed source.

Registry owns sourceKind/adapterVersion/mappingVersion/sourceContractVersion, not
client path/callable. Current status mapping constants remain avito-order-status-v1
and wb-statistics-status-v1. Enqueue cannot choose an arbitrary version and thereby
authorize an adapter. The two named source kinds are recognized domain evidence,
not yet registered complete-sync handlers. Browser upload is excluded as requested.

If generic requestedFrom/requestedTo fields remain, both-null MUST mean "no
normalized coverage interval claimed", NOT unbounded successful coverage. Nonnull
bounds are separately validated normalized evidence and cannot invent an unsupported
provider dateTo or filter. Exact/custom window handler is currently BLOCKED until
source rules state timestamp field, timezone, boundary inclusivity, pagination and
completeness. Do not filter provider rows locally by guessed date field.

## Attempt/run identity

For proposed server UUID job and attempt IDs, accept exact application identity:
`source_run_key = "orders-job-v1:" + canonical_lowercase_uuid(job_id) + ":" + canonical_lowercase_uuid(attempt_id)`.
Scoped additionally by org/account/source in SQL. No provider data, hash or display
title in run identity. New fetch attempt has new run key; retry of same attempt
must match immutable request bytes/bindings, not reopen terminal run.

Create Orders run ONLY after fetch yields validated normalized source snapshot and
manifest, inside publication transaction after user/account/job/attempt locks and
before domain run/projection operations. Job claim cannot fabricate source_snapshot
just to satisfy0062. Failed/no-evidence fetch closes attempt without an Orders run.
Same attempt with changed snapshot/request conflicts and rolls back; not a new run
under the same key. Existing run ownership/terminal state rechecked under FOR UPDATE.
Immutable facts can replay through memberships as already implemented by T3.

No source currently reviewed proves a provider-consistent complete snapshot for this
job. Therefore production `orders.sync.v1` handler registration/activation remains
fail-closed; infrastructure DDL/claim/revocation can use explicitly synthetic source
fixtures, without claiming marketplace synchronization. Proposed job model supports
partial outcome but cannot reinterpret partial as complete publication.

## Dependency acceptance

T1 may implement job authority storage from this amended exact contract while T3
finishes domain acquisition/completeness evidence. Do not discard material selectors
from idempotency bytes. Root still owns finite policy/lifetime/visibility activation.
Existing source-request serializers/transport adapters have not been changed here.
Read-only source review only; no provider, DB, queue, live credential or scheduler
action. No tests claimed for this docs-only request amendment.
