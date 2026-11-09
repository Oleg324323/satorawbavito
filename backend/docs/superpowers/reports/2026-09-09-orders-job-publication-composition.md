# Orders Job Publication Composition

Implementation-first domain package. T1 owns job schema, authority, claim/lease
fences and credential resolution. Its actual public source signatures were supplied
directly before final verification. This composition does not register a handler,
create a user session, fetch a provider or turn a page into complete synchronization.

## Domain Entry Point

`app/orders/job_publication.py`:

```python
publish_claimed_orders_manifest(
    session: Session, *, jobs: UserOrdersJobs,
    claim: ClaimedUserOrdersJob, request: OrdersJobRequest,
    manifest: OrderManifest,
) -> OrdersPublicationResult
```

The source adapter retains the actual immutable request obtained from
`jobs.request_for_fetch(claim=...)`, performs acquisition outside publication locks,
and supplies a validated normalized manifest. This function owns a clean physical
Session root and performs, in order:

1. Validate exact source/header/mapping versions, org/account, nonempty partial
   evidence and one acquisition page. Avito page/limit match the captured selectors;
   WB's inspected single response is page1. The existing INTEGER coverage columns
   impose a 2147483647 page representation bound, not a new provider guarantee.
2. Acquire T1's actual `jobs.acquire_publication_guard(session, claim=claim)` and
   compare its persisted request canonical bytes with the captured request.
3. Persist Orders run/bound observations/status/lifecycle/coverage/audit through the
   extracted existing transaction-local helper. Complete publication is rejected;
   partial evidence never creates a current item projection or fulfillment readiness.
4. Use `handle.initiating_user_id` only for audit attribution. Account/run keys come
   from the live handle, not raw payload. No fabricated `UserSessionPrincipal`.
5. Call `handle.complete(result_sync_run_id=...)` inside the same root transaction,
   then commit. T1 retains final user/account/credential/attempt/lease checks.

The existing USER-session publication entry point retains its original guard and
final revalidation; both use `_persist_orders_manifest` internally to avoid two
different implementations of evidence storage. The internal helper does not grant
authority or own a commit and must not become an HTTP handler.

## Outcomes And Limits

A successful job result can be partial. It does not prove all-account coverage,
provider ordering, missing-order cancellation or printable/sendable units. Empty
or no-evidence acquisition must use the T1 attempt failure/outcome path, not invent
an Orders snapshot. Source date selectors are preserved by the job request; no
local guessed date-field filtering is introduced.

An exception during persistence rolls back both domain and job changes. An SQL
failure at the physical COMMIT boundary raises T1's `READBACK_REQUIRED`, including
uncertain outcomes. The caller must read back the persisted job locator, not fetch
again or replay an old claimant as if it were newly authorized. No retry loop or
delivery worker is implemented here.

Normalized evidence remains a trusted adapter input. These checks bind it to the
job request and source contract; they do not prove the remote body obeyed selectors.
The source contract amendment `2f0422076ac0b365d2e97432b9ce57d6aa6b350f` still lacks
complete-sync semantics, and the shared service's default handler registry stays
empty. Browser token-only ingestion is a different authority and is not routed
through this job/user-session composition.

## Verification Status

**IMPLEMENTED / UNVERIFIED.** T1 schema/runtime dependency
`b274a5097abbe02f078ccabbea0355d68fa3d067` and public handle source
`5e3bcaec350906d597be61cbb15040ab10e7d485` were imported by an ordinary merge.
Existing T3 source and pending changes were preserved. No T1 files were edited.
Implementation and final test cases are prepared; combined consumer verification
is deferred to the coordinated final phase. No tests, compile or runtime import
checks were run for this delivery. No passing tests or shared-head acceptance is
claimed at this checkpoint. The new integration cases
use real persisted create/claim/guard/complete flow, assert audit origin and partial
result, source mismatch/empty/complete rejection, revoked-session denial, atomic
rollback, distinct database backend sessions racing one claim, and readback after
both before-commit and after-commit synthetic transport failures.

## Remaining Inputs

- T1 token-only ingestion guard: await the actual committed handle/root API. The
  browser decoder cannot manufacture a user principal or authorize its own scope.
- Trusted acquisition composition: this publication-only service does not resolve
  credentials. The dedicated resolver handoff `b70f2d2afdd545005b59b65d7a40c66595b4bfcd`
  also requires its executor identity lineage; it is not imported piecemeal into
  this no-fetch package or replaced by a global pool fallback.
- Domain/source owners: complete account traversal, WB fulfillment authority,
  approved execution policies and canonical filter semantics remain unproven.
  No active provider handler, queue, router registration or operational default
  is added to fill those gaps. T1 retains shared registration ownership.
- Production assembly/printing is deferred; KIZ and standalone matcher are
  excluded, while historical compatibility remains intact.
