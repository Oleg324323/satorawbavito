# Orders Token Ingestion Sink

**IMPLEMENTED / UNVERIFIED.** Source-only delivery under the instruction to
complete implementation before centralized gates. No tests, imports, compilation,
Ruff, PostgreSQL, Redis, providers or application activation were run here.

## Exact Composition

Imported by ordinary merge: T1 runtime
`ac2c76d293a99df2139aa9d78384d65280b477ca`, including schema
`d1eca023fbb0c921cc9979cf936c42e2536ccd6e` (0076 and its actual ancestry).
No shared schema, guard, router, config or ORM was edited by T3. This also brings
the dedicated credential resolver and executor identity prerequisites together;
this sink does not use credentials or perform acquisition.

`app.orders.ingestion_publication.OrdersIngestionSink(session_factory=...)`
implements T1's actual synchronous callback:

```python
sink(*, raw_bearer, admission, envelope, observed_at)
# -> CommittedIngestionAcknowledgement(run_id, "partial", replayed)
```

The factory is explicitly supplied by trusted bootstrap; there is no global-pool
fallback or connection at construction. Foreign/active/dirty/Connection-bound
Sessions are rejected without taking ownership or closing their transaction.
Accepted Sessions are closed on every outcome. A close failure suppresses the
acknowledgement rather than claiming success.

The sink uses the actual token-only guard in its own physical root. It compares
the previously admitted token ID, owner, scope, expiry, external account, nullable
credential reference, binding schema and incarnation against the freshly verified
guard, plus the decoded manifest owner. Admission metadata and envelope classes
are not authorization. The guard owns final token/account/expiry/physical-root
checks; the sink calls actual require_participation before and after persistence.
No synthetic user principal or extra speculative final-fence registry is used.

Before writes, the closed envelope fields are reconstructed and passed through
the existing strict decoder. Exact envelope equality prevents a caller-created
dataclass from supplying arbitrary checksums, capture provenance, effective time,
complete coverage, receipt time, source versions or identity keys. This does not
prove marketplace authenticity of browser-supplied evidence.

## Persistence And Replay

The existing Orders participant persists partial run, observations, status and
lifecycle evidence, coverage and audit in the guarded root. It never creates a
fulfillment-ready/current item projection from this partial browser page.
`_persist_orders_manifest.actor_user_id` now admits None, matching the existing
nullable audit FK; genuine USER/job audit attribution is unchanged.

The additional `orders.browser_ingestion_published` audit records only verified
token UUID, account binding incarnation and canonical payload checksum, linked to
the scoped run. It records no bearer, verifier, client-supplied actor or raw body.
Both audit records commit atomically with the run and token last-used update.

The decoder's explicit UUID source-run key remains scoped by org/account/source.
Same key with changed semantic manifest or canonical payload returns the existing
ingestion_conflict (409) contract. Replay requires a matching durable token-sink
receipt and incarnation, then passes the original Orders binding/membership/receipt
checks. Legacy runs without this receipt are not silently enrolled. A newly issued
valid token for the same incarnation may replay the same body; its UUID is not
mistaken for the body's idempotency key. A changed incarnation cannot revive old
receipts even if external account/ref later return to their previous values.

Account locking serializes publishers before scoped run lookup; this is source
design, not yet two-session concurrency proof. No retry loop or duplicate-key
exception is interpreted as success. Unknown SQL/commit/cleanup outcomes return
ingestion_unavailable (503); a caller can subsequently submit its same explicit
key/body for guarded replay, not generate a new key automatically. An ACK is
constructed only after physical commit returns successfully.

## Central Gates And Remaining Inputs

NOT RUN: actual 0076 upgrade and runtime-RLS tests; full token/owner/incarnation
substitution matrix; revocation/expiry during waits/final flush; two real sessions
same-key same/changed body; exact replay after token rotation; A-B-A rejection;
malformed envelope and forged checksum/version/time; atomic rollback of both
audits/run/last-used; before/after uncertain commit; foreign roots/close failure;
secret canaries; unchanged USER/job publication and strict decoder regression.

T1 owns dormant router registration and approved TTL/body/abuse/proxy policies.
T4 owns the new producer with explicit observed identities/occurrences; existing
legacy payloads are not backfilled or accepted by inference. This sink does not
activate an endpoint, issue/revoke a real token, contact Redis/provider or enable
complete synchronization. WB fulfillment/filter rules remain separate inputs.
Production assembly/printing is deferred; KIZ/matcher stays excluded and historical
compatibility is not deleted.

Rollback: revert this sink and the nullable helper annotation before activation.
After real ingestion, retain existing persisted history and apply T1's populated
downgrade guards; never delete receipts or restore an obsolete whole-JSON writer.
