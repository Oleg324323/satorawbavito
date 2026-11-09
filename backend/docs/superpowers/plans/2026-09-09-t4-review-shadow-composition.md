# Guarded received-DTO Review shadow composition

> **For agentic workers:** Use executing-plans and TDD inline under the original eight-stage authorization. No activation or production calls.

**Goal:** Compose the accepted user guard and lossless repository around already fetched Review DTOs without another provider request.

**Architecture:** A trusted authenticated caller obtains paired fetch binding,
registers a source run before provider I/O, then publishes normalized received DTOs
in a separate guarded root. Reservation order is preserved for late-response fencing.
Fixed permission is existing manual-sync reviews:write, not a client-supplied grant.

**Spec:** Original T4 Stage2; publication handoff4860c53; lossless writer0ce0517.

## Boundaries

- New app/reviews/shadow_service.py and tests/test_review_shadow_service.py only,
  plus this plan and handoff. Existing routes/tasks remain unwired in this slice.
- Fresh Engine-bound Sessions only, no external Connection/joined transaction or
  Connection SAVEPOINT. T1 physical-session guard follow-up must be consumed before
  eventual runtime wiring. No service-owned provider client, fallback store or logs.
- Review-specific default-off exact org/account selector is queued with T1; never
  reuse collector flags. This domain composition is not an existing/activated HTTP API.
- ActorContext comes from trusted authentication; session_id mandatory. Capture exact
  canonical membership from live org/user, then let real guard enforce current scope,
  permissions/session/account/credential. No scheduler/synthetic principal in runtime.
- Bind every authority actually used for fetch. Initial implementation accepts one
  paired CredentialFetchBinding for one WB account/wb_api credential. This is the
  existing WB manual-fetch path; Avito shadow requires a separately verified source
  adapter/all-authority contract and is not silently enabled by a provider switch.
- Finish consumes tuple of already received WbFeedbackRow DTOs. It normalizes them;
  no provider/LLM call, raw generic audit or CatalogSku inference. Caller supplies
  typed coverage; this bounded page-shadow always publishes partial, never tombstones.
- Errors escape root and rollback. Running reservation remains after provider failure,
  revoked authority or failed publication; automated run recovery is not claimed.

## Task: reserve-before-fetch and guarded publication

Interfaces in shadow_service.py:

```python
begin_review_shadow(engine, *, actor, binding, source_run_id, request_checksum) -> ReviewShadowTicket
publish_received_review_rows(engine, *, ticket, rows, coverage) -> ReviewShadowReceipt
```

Ticket is frozen/redacted internal state, not a bearer token or user-deserializable
request. It binds principal/account/credential, source key/checksum and durable run
reference. Receipt exposes only run UUID/manifest/count, never customer content.

- [ ] RED actual disposable test: real synthetic encrypted credential is resolved
  with T1 paired API. Begin returns a committed running run before a fake provider
  executes once. Publish those exact DTOs and read persisted content/checksum.
- [ ] Implement explicit safe error allowlist; bind principal organization and all
  credential owner/identity metadata consistently. Do not trust ActorContext.permissions.
- [ ] Begin creates a clean Engine-bound Session root, captures membership with
  scoped metadata SELECT, acquires guard with reviews:write, reserves run using DB
  validation time and commits. No provider I/O while locks are held.
- [ ] Finish normalizes before writes, acquires real guard for exact ticket, obtains
  current expected versions under account lock, revalidates and ingests that run in
  command_savepoints=False mode. Return receipt only after root commit succeeds.
- [ ] Exact replay creates no duplicate fact/observation; changed same-run response
  conflicts. Earlier reserved/late-completed unknown-time response cannot replace a
  later run's current evidence. Partial responses preserve absent facts.
- [ ] Test revoked session/membership/scope/account/credential after reservation:
  no published fact, reservation stays running. Test actual lock-wait race against
  current session revocation and rollback, not just mocked predicates.
- [ ] SQLAlchemy acquisition/physicalCOMMIT errors map to safe constants; no memory
  fallback. Engine and ticket/DTO shape errors fail closed. Tests have no real network.
- [ ] Run new+existing Review/guard suites, scoped Ruff/compile/diff, independent
  critic; commit and hand off exact limitations. Then wire approved rollout policy
  to the owned manual route using explicit account selection, never scheduler actor.

Conceptual caller (not an endpoint):

```python
fetched = resolve_marketplace_credential_for_fetch(owner, kind)
ticket = begin_review_shadow(engine, actor=actor, binding=fetched.binding,
                             source_run_id=source_key, request_checksum=request_hash)
rows = provider_fetch(fetched.secret.reveal())  # Outside all publication transactions.
receipt = publish_received_review_rows(engine, ticket=ticket, rows=tuple(rows), coverage=coverage)
```

The existing fetched DTO is also the legacy consumer input at eventual wiring;
do not call provider_fetch again for shadow. No code in this slice enables that route.

## Execution checkpoint

Dormant composition and all listed scoped acceptance checks completed:19newPGPASS,
combined641PASS48.76s, Ruff/compile/diff0 and independent scoped criticPASS.
The checklist above records the original plan; HTTP wiring is still a separate
dependency-gated next step. Commit-error injection occurs before physical COMMIT,
not an unknown-result network recovery test. See the same-date shadow report.
