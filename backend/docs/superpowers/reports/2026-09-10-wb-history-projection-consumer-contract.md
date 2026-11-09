# T7 consumer contract — WB positive partial history projection

2026-09-10. READY FOR CONSUMER IMPLEMENTATION, not implemented or importable yet. No runtime/schema proof is claimed. ROOT approved dormant implementation direction; this documentation-only commit pins the T3 participant boundary before code. No PG/ops/schema/runtime changes. Existing `orders.sync.v1` request bytes and behavior remain unchanged.

## Ownership and exact constants

T1 new modules: `app/platform/integrations/wb_history_projection_contract.py` (pure request/view contracts), `wb_history_projection.py` (service and genuine wrapper capability), `wb_history_projection_store.py` (new-operation selection/progress/receipt SQL). Minimal explicit dispatch extensions in existing UserOrdersJobs contract/store/handle are required; no parallel authority platform. A NEW coordinated migration amends 0072 physical constraints/functions and adds the selection relation. Never edit historical 0072/0082 files. `_register_user_orders_fence` stays unchanged and accepts only the genuine existing UserOrdersPublicationHandle.

T3 consumes the handle from `app.platform.integrations.wb_history_projection`; its participant and read-assembly changes remain T3-owned. Reuse exact pure decoder/types from commit `26097df03c5a0ad4c904600865bd9dad5a3d3b88`, not a second DTO/codec.

- operation_kind: `orders.wb-history.project.v1` (new operation, never an alias of orders.sync.v1).
- provider: `wb`; source_kind: `wb-statistics-supplier-orders`.
- adapter_version: `wb-statistics-orders-stream-v1`; mapping_version: `wb-statistics-status-v1`.
- source_contract_version: **`wb-history-positive-partial-v1`**.
- source_run_key: exact T3 `HistoryChunkPlan.source_run_key`, `wb-history-chunk-v1:{history_job_id}:{history_page_id}:{first}:{stop}`; canonical run uniqueness already includes org/account/source. No projection attempt ID in this key.
- source_snapshot: **`wb-history-run-v1:{history_job_id}:{history_run_id}`**, same for chunks of the same frozen provider run. This denotes captured evidence scope, never full provider coverage.
- canonical run payload_checksum: **exact T3 HistoryChunkPlan.input_checksum** under this new source contract. It is NOT an orders-manifest-v1 checksum and must not be passed through the legacy manifest writer/replay checker.
- CHUNK_SIZE=1000 is the already-approved T3 constant. Selection page bound is a required trusted `max_selection_pages` positive integer constructor input, with no default. Overflow selection fails before job creation; no silent truncation. Its deployment value remains a ROOT composition input, not an HTTP choice.

## Server creation/authority (T1)

Proposed HTTP: POST `/api/v2/wb/accounts/{account_id}/history/projections`, body exactly `{historyJobId, sourceRunId}`, UUIDv4 Idempotency-Key. UUIDs are canonical strings; org/principal come from current authentication. No policy, deadline, permissions, credential, payload, cursor, page/ordinal range or checksum is accepted from HTTP.

The service constructor requires `execution_policy: OrdersExecutionPolicy`, callable trusted `authority_deadline_provider`, and `max_selection_pages`; no policy/TTL defaults or activation path. Provider signature: `authority_deadline_provider(*, principal: UserSessionPrincipal, account: ExpectedAccountBinding, credential: OrdersCredentialDependency, session_expires_at: datetime, database_now: datetime) -> datetime`. Inputs are freshly locked metadata, never request-derived authority; output is an aware finite timestamp. Existing checks enforce deadline > now, first policy lease fits, and deadline does not exceed session or supported credential expiry. Exact idempotency replay uses stored policy/deadline/selection and does not invoke the provider to derive new authorization. No numeric policy is selected here.

Creation, claim and final chunk commit require actual current original user/membership/session with sync:run, exact account external ID/credential_ref, current credential ID+generation, and account incarnation. Every selected page must match that current credential ID/generation/incarnation; ROOT selected fail-closed equality, not rebind. A new login cannot adopt an old projection job. The ordinary durable WB read subscription is not this authority. WB finite-expiry rows unsupported by current Orders dependency are rejected, not coerced to None or inferred from JWT. No secret resolution/provider request occurs in projection.

Creation validates and captures an immutable complete source run: scoped published pages only, exact native cursor chain, ending in the immutable empty terminal page, bounded to max_selection_pages. Selection order follows that chain, not timestamp or UUID sort. Staging/gap/branch/mismatch/overflow fails closed. The current mutable Source.run_id/state is not subsequently treated as the selection identity.

## T3 writer capability — exact public surface

`WbHistoryProjectionHandle` is an exact class, nonserializable/redacted, constructor not a consumer API. It wraps a genuinely acquired and extended UserOrdersPublicationHandle registered through the unchanged exact-class registry. No duck typing, subclass casting, user-supplied callback guard, private read guard or fabricated committed claim. The registered underlying handle's new-operation-specific final fence validates selection/progress as well as the existing authority. Its old-operation branch remains unchanged.

Public interface:

```python
class WbHistoryProjectionHandle:
    def require_root(self, session: Session) -> None: ...
    def revalidate_before_write(self) -> None: ...
    @property
    def page(self) -> HistoryPageEvidence: ...
    @property
    def first_ordinal(self) -> int: ...
    def captured_rows(self) -> tuple[dict, ...]: ...
    @property
    def input_checksum(self) -> str: ...
    @property
    def source_run_key(self) -> str: ...
    @property
    def source_snapshot(self) -> str: ...
    @property
    def source_contract_version(self) -> str: ...
    @property
    def account_binding(self) -> ExpectedAccountBinding: ...
    @property
    def initiating_user_id(self) -> str: ...
```

`require_root(session)` returns None only for the identical active physical PG root and Session that minted the handle, unchanged account/organization GUCs, genuine unconsumed publication capability and current claim/progress. Other session/root, nested transaction, ended/replaced/poisoned guard, foreign job/account or stale claim fails safely. It does not install another guard. Acquisition requires the usual clean root; later validation permits the participant's own pending writes in that same root, never silently starts/commits/rolls back one. `revalidate_before_write()` runs genuine current auth/claim/selection checks after waits; failures poison the root through the existing guard so catching an exception cannot permit a later commit. Properties and captured_rows validate before exposing data. Exact safe error surface remains OrdersJobError / PublicationGuardError with existing closed codes (JOB_FENCE_INVALID, JOB_CONFLICT, JOB_CONTRACT_INVALID or existing publication codes); no raw SQL/request/credential details.

`page` is the exact frozen T3 HistoryPageEvidence (org/account/job/page/run/credential UUID strings, credential_generation, account_incarnation, request/raw checksums, exact input_date_from/next_date_from text, row_count, terminal, aware published_at, state='published'). `captured_rows()` returns at most 1000 exact dicts with the nine decoder fields: ordinal, srid, nm_id, barcode, semantic_checksum, source_row_checksum, observation, source_revision, effective_at. Every call returns fresh defensive copies including nested observation JSON. Mutable returned dicts are data, not authority; mutation cannot change the handle's private captured input/checksum. Full owner predicates and frozen selection index are used for capture, never bare page UUID.

T1 computes baseline `decode_history_chunk(page=page, first_ordinal=first, rows=private_rows)` before calling T3 and retains its exact input checksum/key. T3 may query at most the incoming 1000 scoped identities for previous canonical observations, then call the same decoder with `previous=...`. Those comparisons do not enter the hash. T3 must compare the resulting key/checksum to the handle before writes; no recomputed alternate checksum format. Source snapshot/version and account_binding come from the handle, never from a caller-authored descriptor. initiating_user_id is immutable audit origin, not a replacement principal.

## Participant signature and root ownership

T3-owned participant contract (suggested implementation module `app.orders.history_publication`):

```python
def persist_wb_history_chunk(
    session: Session, *, handle: WbHistoryProjectionHandle
) -> OrdersPublicationResult: ...
```

OrdersPublicationResult is the EXISTING exact type from `app.orders.publication_service`, fields `(run_id:int, state:str, replayed:bool, reconciliation_count:int)`. No new participant result DTO and no participant authority factory. The participant calls require_root/revalidate_before_write; it may flush but never commit, rollback, open another root/session or acquire a second publication guard. It returns a provisional real canonical run result. Only the T1 orchestrator seals the receipt/checkpoint and owns commit.

T1 orchestrator acquires a real committed claim through UserOrdersJobs, starts a fresh owned root, acquires the genuine underlying publication handle, captures its exact selected next chunk, calls T3, flushes and validates the REAL scoped canonical run plus checksum/provenance, records receipt/advances checkpoint in the SAME root, and commits with the existing final guard listener last. It refreshes any usable continuation claim only after physical commit. A stale pre-advance claim cannot be reused, and normal successful progress does not consume a retry attempt. No fake job success between chunks. Uncertain commit returns READBACK_REQUIRED and must use durable receipt/cursor readback; no blind advance.

## Canonical run and replay proof

T3 writes new run as staging, with real account schema-1 binding bytes/checksum, exact constants/key/snapshot above, then seals state=partial, manifest_state=partial, payload_checksum=handle.input_checksum, requested_from=requested_to=NULL, page_count=1, expected_order_count=NULL, completed_at from DB clock. order_count/item_count describe this chunk's real canonical evidence/membership, not the whole provider run. T1 verifies exact scope, source/adapter/mapping/contract, run key, snapshot, payload_checksum, account binding bytes/checksum, partial state, NULL requested interval, finite completed_at >= started_at and <= freshly guarded DB now, and exact chunk identity before recording progress. Replay may legitimately reference completion before this transaction; it must not be assigned a new completion time.

On existing run-key replay T3 compares these original input/run properties and returns the same real run; changed checksum or binding conflicts, regardless of current previous-comparison results. T1 stores the original reconciliation_count in its immutable receipt and uses it for later receipt readback (replayed=True is the delivery marker, not a change to original semantic result). Never regenerate a changed result from mutable current Orders state and advance the same chunk.

Domain observations/memberships, permitted positive initial current projection and reconciliation evidence, canonical partial run, T1 receipt and cursor all commit or roll back together. A standalone partial run without T1 receipt cannot be accepted by the special read branch. Later job expiry/failure does not erase an already committed chunk receipt; fresh readers still require their own current read/publish authority.

## Exact empty EOF contract

For terminal page row_count=0, first=stop=0, captured_rows=(), T3 decoder returns its normal deterministic empty plan/key/hash. T3 creates or replays a partial run with page_count=1, order_count=item_count=0, expected_order_count=NULL and the same digest/account/source rules; no observation or membership is fabricated. T1 records the `(0,0)` receipt and only then may mark the projection job succeeded/partial after all frozen pages were contiguously consumed. This never creates a complete source snapshot or changes absent orders.

Verified source evidence: 0062 order_sync_runs permits zero counts for state/manifest_state partial and a hex payload_checksum; its nonempty/expected-count checks apply to complete. 0067 supplies the real binding columns. The new digest→history receipt link is NOT present today and must be supplied by the new migration/final witness. Old job_publication rejects empty input and is not called.

## Exact reader receipt storage contract (new migration)

Use existing canonical order_sync_runs as results and existing append-only `public.user_orders_job_audit` for chunk receipts, not a second results table. New relation `public.user_orders_history_selection_pages` has columns:

`organization_id integer, marketplace_account_id integer, job_id uuid` (projection job), `page_index integer` (zero-based), `history_job_id uuid`, `history_source text` (fixed wb-statistics-supplier-orders), `history_page_id uuid`, `history_run_id uuid`, `header_checksum text` (64 lowercase hex).

PK `(organization_id,marketplace_account_id,job_id,page_index)`; scoped job FK; full stable 0082 page FK `(organization_id,marketplace_account_id,history_job_id,history_source,history_page_id)`; unique selected history page within projection; immutable sealed selection and contiguous bounded index witness. No FK to mutable generation/current Source.run_id.

New projection-only columns on `public.user_orders_jobs`: `history_job_id uuid`, `history_run_id uuid`, `history_selection_digest text`, `history_page_count integer`, `history_terminal_page_id uuid`, `history_cursor_page integer`, `history_cursor_ordinal integer`, `history_progress_version bigint`. Immutable first five selection fields; only final three progress fields may advance under the new operation witness. Legacy operation has all eight NULL. New request bytes canonically bind immutable selection fields. Operation-scoped idempotency lookup must not continue hardcoding orders.sync.v1.

New nullable columns on `public.user_orders_job_audit`: `history_page_index integer`, `history_page_id uuid`, `history_first_ordinal integer`, `history_next_ordinal integer`, `history_input_checksum text`, `history_result_sync_run_id bigint`, `history_reconciliation_count integer`. Present as one closed set only on `event_kind='job.chunk_committed'`; legacy audit events keep them NULL. FK to exact projection selection/page; scoped real canonical result FK; unique receipt `(organization_id,marketplace_account_id,job_id,history_page_index,history_first_ordinal)`. The result run/checksum can be shared by a later separately authorized projection job only after exact equality validation; its new job still needs its own receipt/progress proof. No new input hash or invented copied canonical run.

A new deferred reciprocal witness validates receipt↔job/progress/attempt version change, real canonical partial run properties and exact next cursor. The final EOF event also transitions job/attempt succeeded with last real run as terminal result; old-operation success witness remains exact original behavior. Audit’s history_result_sync_run_id is separate from terminal-only existing result_sync_run_id fields, avoiding ambiguous intermediate job results.

T3 read_assembly admission is an exact branch, not allow_partial: a current source run must match the pinned source/adapter/mapping/contract and partial state, valid schema-1 account binding, then have an immutable `job.chunk_committed` audit receipt whose history_result_sync_run_id matches the run and history_input_checksum matches run.payload_checksum. Join audit `a` to selection `p` on `(a.organization_id,a.marketplace_account_id,a.job_id,a.history_page_index)=(p.organization_id,p.marketplace_account_id,p.job_id,p.page_index)` AND `a.history_page_id=p.history_page_id`; join `p` to published 0082 page `h` on `(p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id)=(h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)` AND `p.history_run_id=h.run_id`; join audit to canonical `r` on `(a.organization_id,a.marketplace_account_id,a.history_result_sync_run_id)=(r.organization_id,r.marketplace_account_id,r.sync_run_id)`. Verify captured header digest, ordinal bounds/count and source_snapshot/key. A committed chunk from a still-running projection job is valid partial evidence: do not require parent terminal success. The proposed T1 module exposes `load_history_projection_receipt(session, *, organization_id, marketplace_account_id, sync_run_id) -> HistoryProjectionReceipt` for this bounded provenance lookup; this returns data, NOT authority, and must be called only inside T3’s already-guarded same-account read-assembly root. It performs no write, commit, snapshot publication or “latest” selection. Multiple matching job receipts must agree on identical canonical input/page/bounds/result; inconsistent evidence fails closed. Reader SQL must not load all historical rows to prove a chunk receipt: the write-time reciprocal witness is the durable proof.

`HistoryProjectionReceipt` exact frozen data fields: organization_id, marketplace_account_id, sync_run_id, history_job_id, history_run_id, history_page_id (UUID strings), first_ordinal, next_ordinal, input_checksum, source_run_key, source_snapshot, source_contract_version, credential_id (UUID string), credential_generation, account_incarnation, reconciliation_count, coverage_state='partial'. Receipt validity is not conditional on the projection job still being running or its initiation login still being active; it proves a past guarded commit. Caller independently validates current authority/account binding as usual. No generic receipt-less partial acceptance.

Receipt lookup stays bounded: one SQL statement selects one deterministic matching receipt with LIMIT 1 and uses SQL EXISTS to detect any differing canonical input/page/bounds/result receipt in the same MVCC snapshot. Never materialize all matching receipts with .all(); absence or disagreement fails closed. The implementation needs a scoped result-run lookup index for this exact query.

Read output retains partial coverage, source_readiness_unproven and wb_fulfillment_source_missing. Explicit cancellation evidence only; no absence cancel/delete, inferred fulfillment readiness or automatic changed/newer current overwrite. Production keeps its partial-source rejection. Snapshot creation remains the explicit existing guarded action, never cache GET or worker side effect.

## Required implementation prerequisites / freeze limits

0072 currently physically forbids this new operation/request/progress/event and pins one attempt-specific result key. T1 must add a NEW migration with exact new-operation branch and old codec/constraint behavior regression tests before any consumer can run. No claim is made that the proposed class or receipt columns already exist. SQL helpers need proper fixed search_path/invoker/ACL and forced-RLS treatment; ops changes are separately owned. Implementations remain dormant until explicit policy/deadline provider/bound are supplied.

No business/time default needs to be chosen to implement the participant. Only outstanding deployment input is the explicit bounded selection page limit and approved policy/deadline composition; numeric chunk limit is already fixed by T3. ROOT requires historical=current credential ID/generation/incarnation, so no unresolved rebind policy remains in this draft.

Self critic/source check: exact current handle registration, current result type/account binding type, actual old empty-partial SQL constraints, T3 decoder bytes/key/bounds, and old final witness incompatibility inspected. No tests, PG, fake claims or importable stub authority produced. Main reviewed and approved this consumer design; runtime/schema implementation and its focused verification require the next explicit brief.
