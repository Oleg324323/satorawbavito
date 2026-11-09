# Avito Known-order Status Refresh

Root approved the bounded application freshness policy on 2026-09-09 after
official documentation inspection. Production remains deferred. This plan is
Orders-only and does not authorize real provider execution or route activation.

1. RED pure official single-order normalization: exact string IDs/status,
   required hasMore=false/exactly one requested order, unique listing identity,
   strict positive quantities, aware source timestamps. Preserve raw updatedAt
   and a SHA256 of all non-status/non-updatedAt fields as a namespaced application
   evidence descriptor in the existing opaque source_revision field. It is NOT
   a provider revision. Do not persist buyer/contact/raw payload fields.
2. RED actual service: initial normalized fake source + existing publication,
   then capture committed current versions and immutable owner proof under fixed
   live guard BEFORE a single documented ids/page1/limit20 fetch outside the
   transaction. After fetch repeat authority and source/CAS checks. Unbound or
   old observations lacking the descriptor cannot be relabelled with invented
   baseline fingerprints; they remain reconciliation.
3. GREEN minimal service using existing evidence/projection repositories.
   Advance only unchanged non-status fingerprint/items, strictly later nonfuture
   updatedAt and unchanged captured versions. Equal identical replay returns
   current receipt; invalid/equal changed/older, concurrent change, item mismatch
   and existing Production work reconcile. Preserve mappings and immutable
   snapshots. Read coverage remains partial with application-freshness provenance,
   never account complete or print readiness. No new tables or background workers.
4. Runtime-role integration: fake HTTP source -> persisted revision -> frozen read;
   one fetch, scope drift/revoke, replay, partial/malformed, older/equal/future,
   concurrent change and existing-work exclusion. Existing official-normalized
   initial publication remains a trusted caller boundary, not active account sync.
5. Scoped regression/Ruff/compile/diff and read-only critic. Current consumer
   fixture uses head to match runtime grants; historical migration fixtures stay
   pinned. Heavy gates serialized with explicit peer ACK/release and cleanup.
6. One consolidated Orders handoff after active-scope work. Document missing
   strong provider chronology/full-account coverage and deferred Production;
   do not claim full source sync, completed printing, final integration or deploy.

Clock policy: reject source times later than the DB receipt clock without an
invented tolerance window. This is fail-closed, not a claim of synchronized
provider clocks; acceptable provider clock skew remains an external input.

## Verification Checkpoint

Pure normalization started with a missing-module import failure, then passed
18 cases; combined identity/ingestion/binding/source checks passed 147 cases.
The runtime service started with a missing-function import failure. Independent
review then identified two regressions: rejected valid observations were not
durable, and the status policy could hide unrelated conflicting evidence.
Both were reproduced against the implementation: 17 passed / 2 failed, 8.76s,
exit 1. The fixes preserve rejected observations without promoting them and
exclude only explicitly accepted predecessor lineage from divergence detection.

After the fixes and an additional overlapping-fetch test with two independent
database sessions, the bounded runtime-role file passed **20 tests in 8.76s**,
exit 0. Both fetches occur outside transactions and only one CAS may advance.
The fixture verified absence of its exact temporary database and runtime role
after cleanup. Inherited `/dev/null` password-file warnings remain disclosed.
Ruff, compileall and diff checks passed. Adjacent five-file Orders publication,
read assembly/service, HTTP and browser-publication regression passed 48 tests
with two existing deprecation warnings in 20.22s, exit 0. All five exact owned
databases and runtime roles were verified absent after cleanup. Final scoped
review found no further significant defects. Fresh pure source/identity checks
passed 64 tests in 0.42s. Consolidated root acceptance remains separate; this
checkpoint does not activate a route, worker or provider.

Rollback: retain additive shared schema and all evidence/history; roll back to
the previous dormant consumer, which reconciles changed facts. Do not rewrite
historical snapshots, enable legacy JSON writers or downgrade populated schema.
