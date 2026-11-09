# Credential maintenance implementation package

Spec: `backend/docs/superpowers/specs/2026-09-09-credential-maintenance-boundary-design.md`.
The approved conceptual source is28258e3f39eb388179fe0b27686d0b8840cfa8dc.
This plan completes its three delivery boundaries as one source-first package.
Final security/role/RLS/restore tests follow the implementation, not each edit.

Actual-schema amendment approved by integration owner2026-09-09: legacy
lk_audit_events has no RLS, so runner gets NO direct audit/sequence privileges.
Use the spec's fixed authorizationUUID+operation safe-audit helper, inert invoker
until explicit provisioning, owned by dedicated non-table-owner helper role with
exact audit INSERT columns/sequence USAGE only. Real session_user/locked metadata
derives every audit field; store calls only after persisted decrypt/readback in
same root. No global audit RLS change or exactly-once-audit claim. Include helper
in exact manifests/deprovision and final negative ACL/forgery/rollback gates.

## Global constraints

T1 worktree only. No production, providers, working database/Redis, real source
credentials, .env or keys, operational registration/backfill/rotation/role changes,
flags, deploy/push, historical migrations or unrelated ACL reform. All generated
SQL artifacts are inert and MUST NOT be executed during this implementation.
User changed sequence to complete implementation before final tests/reviews.
No fake crypto or second credential store. Existing AES-GCM/AAD/keyring/secret
wrapper and persisted resolver internals remain authoritative. Master key never
enters SQL, mapping input, logs, report or repository. No production Avito proof
is inferred from IDs, names, counts, timestamps, hashes or an opaque UUID.

## Task 1: Implement inert schema, scoped store, registrar and CLI

Read the full spec, current credential store/crypto and WB resolver, actual source
ORM/schema and applicable security instructions. Implement the exact private
authorization, privilege and source-invalidation contract, not a permissive stub.
Do not rerun earlier conceptual review or operational diagnostics.

### Paths

- New `backend/alembic/versions/YYYYMMDD_NNNN_credential_maintenance.py`, actual
  single head plus next free revision determined on dispatch; stop on conflict.
- New `backend/ops/credential-maintenance-provision.sql`.
- New `backend/ops/credential-maintenance-deprovision.sql`.
- New `backend/app/security/credential_maintenance_contract.py`.
- New `backend/app/security/credential_maintenance_registrar.py`.
- New `backend/app/security/credential_maintenance_cli.py`.
- Modify `backend/app/platform/integrations/credential_store.py`, only the private
  shared persistence/readback primitive and new maintenance entrypoint/safe codes;
  existing public consumer signatures/semantics remain unchanged.
- New `backend/docs/superpowers/reports/2026-09-09-t1-credential-maintenance-handoff.md`.

No other paths, tests, new dependencies, public routers, settings/env changes or
key files. Python registrar accepts an explicitly injected trusted verifier and
session factory; operational verifier defaults unavailable. CLI runner uses an
operator-supplied process DB connection through existing factory configuration,
whose actual direct session identity must pass persisted authorization checks.
No fallback to an API/admin role or auto-creation of authorizations.

### Implementation

1. Add inert private schema `credential_maintenance`, immutable `targets` and
   renewable-history/revoke-only `authorizations` exactly per spec. FORCE RLS,
   schema-owner-only verified unfiltered metadata policy, static invoker helper/
   invalidator/invariant functions with qualified names and fixed search_path.
   Atomically scrub all new schema/table/column/function nonowner default ACLs,
   grant options included. No source trigger/view/role installed by Alembic.
2. Define exact inert/active object manifests in provisioning artifacts. Require
   explicit role identifiers and dedicated database context, no secret parameters.
   Validate attributes/transitive role reachability, source trigger/rewrite state,
   truncate/replication/alter bypass capabilities; refuse rather than repair old
   runtime privileges. Source tables WB→Avito then target→authorization ACCESS
   EXCLUSIVE NOWAIT, unfiltered empty proof for BOTH private tables. No waiting
   loop or broad catalog cleanup/adoption of unrelated objects.
3. Provision narrowly separated helper/view owner and invalidation trigger owner
   as specified. Transfer functions before enabling definer, final views owned
   correctly, strip hostile default rights again, revoke temporary CREATE before
   commit. Exact ordinary AFTER UPDATE OF watched columns / DELETE invalidators
   revoke ALL matching OLD source-ID authorizations under UUID lock order without
   filtering caller/OLD tenant GUC/expiry. Cache-only updates do not revoke. Reject
   unexpected trigger timing/relation/arguments; return NULL, never source record.
4. Complete the exact column/policy matrix: helper/view owner only needed source
   payload and metadata plus narrow lock columns; invalidator owner only auth
   identity/revocation fields; registrar metadata/source attestation capabilities;
   runner scoped metadata/source/history views and restrictive encrypted SELECT/
   generation1 INSERT + exact safe-audit helper EXECUTE. No runner base-audit or
   audit-sequence rights, base-source SELECT/UPDATE,
   account UPDATE or encrypted UPDATE/DELETE, no runtime maintenance grants.
   Do not add merely permissive policies to existing permissive tenant RLS.
5. Scoped security-barrier views expose exact active direct-role authorization,
   exact approved source only, and COMPLETE owner/kind encrypted metadata history
   independent of target/gen/revoked/key. Exactly one NULL-credential sentinel is
   authorized empty; zero rows denies. Recipient role OID+name authenticates actual
   session_user, not current_user in definer or a caller-controlled role string.
6. Deprovision uses same fixed NOWAIT locks, exact active manifest, both tables
   unfiltered empty, explicit dependency removal, invoker before owner restoration,
   exact inert verification. Refuse any target/history/foreign dependencies; no
   DROP OWNED/CASCADE/deletion/cleanup of runner/registrar identities.
7. Registrar owns a fresh physical root: verified source result from independent
   trusted verifier, prescribed user→account→source→authorization locks, exact
   in-memory typed UTF-8 field comparison with locked source, then metadata insert
   and commit. Opaque proof handles are NOT proof themselves. Same authorization
   retry exact; renewal explicitly revokes previous row and preserves target and
   full history. Changed locator/binding/target conflicts. No public fake verifier
   flag, source digest or plaintext SQL registration argument. Operational verifier
   absent means unavailable; synthetic injectable verifier is a final-test fixture.
8. Implement store `backfill_mapped_credential(authorization_id, operation)` where
   operation exactly backfill|verify, no Session/owner/plaintext input. One owned
   Engine-bound READ COMMITTED non-autocommit root, scoped metadata lookup then
   tenant context; exact helper lock and fresh clock/binding/operation checks after
   waits. No history→approved target UUID/generation1 only; existing history succeeds
   solely exact same target/gen1 active equal payload and no other history. Verify
   never inserts, rekey/newer/revoked/alternate history refuses, no ON CONFLICT UPDATE.
9. Extract/reuse minimal private store persistence/resolver readback core rather
   than public put/resolve/rekey nested roots. Existing AEAD/keyring/current key/
   fresh nonce, insert/flush/expire/requery persisted row, decrypt/validate and exact
   typed equality BEFORE fixed safe-audit helper and physical commit. Failure rolls everything
   back, including corrupted readback/expired auth; return redacted metadata only
   after commit. Uncertain commit safe unknown, not automatic replacement/retry.
10. IDs-only CLI inventory/backfill/verify/report with strict mapping schema,
    duplicate/conflicting IDs rejected. Per-authorization root permits interruption
    and exact resume, no batch overwrite/new generation. Inventory/report outputs
    IDs/counts/allowlisted result codes only, never source fields, payload/ciphertext,
    verifiers, SQL/exception strings, paths or key inventory in errors. Raw key material
    is loaded only by the existing store boundary in its explicitly configured runner.
    No default role/mapping/policy or automatic operational registrar. CLI help/import
    must not connect, resolve settings, load keyring or read source data.

### Final evidence and delivery

Document actual function/view/role/column/trigger/policy matrix, source-watch
fields, lock ordering, fresh-root registrar trust, CLI signatures, strict input
and safe output shape. Preserve every required evidence item from conceptual spec
as a final test obligation, including real-role spoofing/hostile ACL/source-mutation
races/history sentinel, exact persisted decrypt equality, interruption/rekey
conflict and synthetic restore. No tests/processes in this implementation task.

Commit schema + inert SQL templates as `feat: add constrained credential maintenance storage`;
store/contracts as `feat: add scoped credential maintenance operations`;
registrar/CLI/handoff as `feat: add credential maintenance command boundary`.
Status IMPLEMENTED / UNVERIFIED until final gates. No provider verifier, operational
proof/key custody/duration/drain/observation/retention/restore policy is invented.
Legacy mutation invalidates maintenance authorization, NOT existing ciphertext;
writer fence/parity/observation remain mandatory before encrypted-only production.
