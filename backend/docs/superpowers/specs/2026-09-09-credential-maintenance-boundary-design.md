# Credential maintenance boundary — conceptual design

2026-09-09. Local conceptual design approved after independent review of all six corrected finding areas (0 Critical, 0 Important, 0 Minor). Not a migration/provisioning/crypto implementation, actual-role acceptance or operational approval. An exact-head implementation plan and complete privilege/command matrix are still required before dispatch; no revision is reserved. Coordinator-root explicitly authorized local field-sensitive legacy-source invalidation using a separate narrow trigger owner, without caller-GUC omissions or runtime authority expansion. This design resolves that boundary; production verifier, principal/key custody and operational policy remain separate.

## Delivery boundaries

### Actual-schema audit amendment (2026-09-09)

The integration owner approved this source-only correction: `lk_audit_events`
is created by0005 and extended by0006, but has no RLS contract. A restrictive
policy alone would not constrain runner writes. Do not enable/change global audit
RLS or grant the runner direct audit access. Add one fixed maintenance safe-audit
helper, SECURITY INVOKER and ungranted in inert schema, transferred to the dedicated
NOLOGIN/non-superuser/non-BYPASSRLS/non-table-owner helper role before SECURITY
DEFINER activation in the inert provisioning artifact. Runner cannot inherit or
SET ROLE to that owner.

The helper accepts only authorization UUID and closed backfill|verify operation,
authenticates actual session_user OID+name, reuses checked metadata/binding locks
and fresh validity, derives owner/target/object from metadata and the exact
generation1 credential, and writes fixed schema-qualified audit SQL. No caller
JSON, actor, scope, message or arbitrary SQL selects audit authority. Helper owner
receives only required audit INSERT columns and exact existing sequence USAGE,
not audit SELECT. Runner gets only exact helper EXECUTE, never base audit or
sequence privileges. PUBLIC EXECUTE remains denied. Exact inert/active manifest
and deprovision include the helper and its grants.

The trusted store calls it only AFTER actual persisted-readback/decrypt/equality
verification in the same physical root. A directly callable audit helper is not
proof of that verification or malicious-runner algorithm compliance. Document
explicit retry's audit semantics separately from idempotent ciphertext state;
do not promise exactly-once audit without a persisted receipt. Negative ACL,
principal-forgery and rollback/commit cases join the final gate. No role/template
execution, production database or operational key action is authorized.

### Delivery order

1. Inert forward schema, narrow atomic provisioning artifact, actual disposable-role/lock/source-invalidation acceptance. No global operational role creation in Alembic; no key load or consumer.
2. One new store-owned backfill/verify operation using existing crypto and persisted resolver core, same physical root, exact source/history admission, safe audit and rollback; no public consumer or CLI.
3. Inventory/backfill/verify/report CLI and synthetic interruption/restore rehearsal after those two independently accepted contracts. Operational mapping proof, principal custody, validity/timeout/drain and key lifecycle policies are mandatory external inputs, never defaults.

Separate schema and store review gates allow rejecting either without mixing source-trigger security with application encryption claims. Runtime old legacy DML is unchanged; new runner has no direct base-source SELECT/UPDATE or account UPDATE. No claim that runtime already lacks its historical UPDATE rights.

## Persisted authorization metadata

Private schema `credential_maintenance`, owned by schema administration, outside public wildcard grants. Two forced-RLS metadata tables, no sequence or secret material. Immutable `targets` reserves a non-nil target UUID for one exact positive org/account/provider/kind tuple for its lifetime. Target UUID is PK, owner/provider/kind is UNIQUE, and a composite UNIQUE(target UUID plus owner/provider/kind) supports the authorization FK. No target UPDATE/DELETE, runner table grant or cascade. A separate reservation prevents both cross-owner UUID reuse and the renewal conflict identified by the independent critic.

`authorizations` has all following identity/principal/operation/proof/validity fields explicitly NOT NULL unless marked nullable:

- `authorization_id UUID PRIMARY KEY` non-nil, `target_credential_id UUID` with exact composite FK to targets; target UUID is NOT individually UNIQUE on authorizations, allowing retained explicit renewal history.
- `organization_id INTEGER`, `marketplace_account_id INTEGER`, both positive; `provider TEXT COLLATE C`, `credential_kind TEXT COLLATE C` restricted to `(wb,wb_api)` or `(avito,avito_oauth_client)`.
- `source_id INTEGER` positive, interpreted only by exact provider; `source_user_id VARCHAR(64)` matches actual LkUserRow.user_id with no normalization; expected external account ID exact nonempty VARCHAR(128) C, `expected_credential_ref VARCHAR(255) COLLATE C NULL` with WB canonical verified-reference requirement. Avito reference is not manufactured.
- `recipient_role_oid OID`, `recipient_role_name NAME` exact authenticated direct session role pair; both must still match current catalog identity, neither `current_user` inside definer nor a caller GUC authenticates a runner.
- `review_id UUID`, `review_authority_id UUID`, `proof_reference_id UUID`, all non-nil; finite `reviewed_at TIMESTAMPTZ`. Opaque registrar-issued nonsecret handles, never URLs/paths/response text/credentials/digests. They reference an independently approved review, not an API body proving itself or a fabricated Avito mapping. No production registrar/proof method is selected by these types.
- `allow_backfill BOOLEAN`, `allow_verify BOOLEAN`, at least one true; `not_before`, `expires_at` required finite TIMESTAMPTZ with strict positive interval and no duration default. DB clock, not transaction-start timestamp, checks expiration after waits.
- `revoked_at TIMESTAMPTZ NULL`, `revocation_reason_code TEXT NULL`: both NULL or finite time plus exact `source_changed|operator_revoked|security_incident`; immutable identity/proof/principal/validity/target fields, revoke-only updates (no un-revoke/renew-in-place). Explicit later reviewed authorization is separate, never automatic renewal.

No legacy-source FK/cascade: source identity may be deleted/reassigned and must instead revoke OLD-locator authority. No implicit replication to multiple accounts, no latest-user/org lookup, no name/cardinality mapping. Supported registration checks exact current user/account/source under the prescribed lock order; source proof is attested independently while source locked, or compared in memory to exactly previously verified input. IDs/updated_at/xmin/digest are not a substitute.

Supported registrar is a fresh-root trusted application wrapper over a narrow metadata SQL helper, not a lone IDs-only SQL call. An independently approved verifier supplies evidence and exact verified source fields; the wrapper retains user→account→source→authorization locks, reads current exact source fields through dedicated scoped access, compares UTF-8 bytes in memory with the verified fields, then inserts registration and commits the SAME physical root. No plaintext SQL registration parameters, memory/log copies or source digests. A verifier attesting the current locked source is an alternative only when its external procedure is approved; this local implementation uses an injectable synthetic verifier for acceptance, and operational registration remains unavailable without an approved implementation. SQL helper proves DB identity/lifecycle/locks and trusts registrar attestation; direct privileged SQL is not proof-procedure enforcement. Registrar and runner are separate capabilities, neither gets unrestricted caller-selected key/owner authority.

Authorization partial UNIQUE(org,account,provider,kind) WHERE revoked_at IS NULL permits one unrevoked authorization, not a time-dependent index predicate. Exact owner/history indexes and (provider,source_id,authorization_id) WHERE unrevoked support revocation. Supported registrar locks user→account→source→existing auth UUID order, inspects the entire owner/target history, and explicitly revokes old authorization with operator_revoked before inserting a new reviewed immutable row for the SAME reserved target. Expired-but-unrevoked rows also need explicit revoke. Same authorization UUID retry requires exact immutable-field equality and never reactivates it. Changed source locator/owner/provider/external/ref mapping refuses; fresh proof may renew the same locator after source_changed, but store still refuses changed payload versus committed ciphertext. No source-global uniqueness or silent remapping. Separate target history is retained even if a first attempt never wrote ciphertext.

## Inert Alembic contract

One transactional new revision creates only private schema/two tables, FORCE RLS, schema-admin-only unfiltered metadata SELECT policies, and fixed static SECURITY INVOKER function definitions. No source/history views or source trigger registrations yet. Functions: lock helper, fixed safe-audit helper, separate provider-specific invalidators and exact metadata invariants. Schema/table/function/column ACLs scrub every non-owner grant/default-created grant option atomically; no global ALTER DEFAULT PRIVILEGES change. All functions `search_path=pg_catalog,pg_temp`; all table references qualified.

The metadata SELECT policy targets the actual schema/table-owner role and grants neither operational registration nor bypass. Empty-proof code validates the owner/policy before treating count zero as true emptiness. Existing forced tenant RLS on accounts/encrypted rows remains unchanged. No `CREATE ROLE`, role membership, SECURITY DEFINER under migrator, auto template invocation or installed runtime flag.

Inert downgrade requires targets then authorizations ACCESS EXCLUSIVE, proved unfiltered emptiness and exact inert catalog/ACL/dependency state; any target/authorization, definer function, trigger, scoped view or operational owner/grant refuses before DDL. Drop only listed empty objects with RESTRICT, never CASCADE/DELETE/TRUNCATE/DROP OWNED. Empty upgrade→downgrade→upgrade and populated old Orders/Review/credential synthetic preservation are separate gates.

## Explicit atomic provisioning

Provisioning is a reviewed parameterized SQL artifact, NOT a call in migrations/startup. Use explicit role identifiers and exact database selection, no secret parameters or defaults. Validate full inert manifest and pristine/dedicated roles; refuse collisions/partial activation rather than adopt or repair. No authorization/key material is created by bootstrap.

Lock exact WB source then Avito source tables ACCESS EXCLUSIVE NOWAIT, then targets and authorizations ACCESS EXCLUSIVE NOWAIT. Contention aborts/rolls back the whole atomic attempt with a fixed code, no in-transaction polling, wait-duration default or process termination. Do not acquire user/account locks after source table locks. Check ALL target and authorization rows empty under lock, including revoked/expired. This prevents blessing any proof recorded before mutation watching was installed. Strong fail-fast locks also avoid helper→account→source→metadata teardown cycles; no generic deadlock-freedom claim.

Two distinct owner roles: NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION NOINHERIT, neither table/schema owner. Reject all effective inheritance/SET ROLE paths from runner/runtime, not just direct membership. Transfer invoker functions to final owners BEFORE changing SECURITY DEFINER. Temporary schema CREATE needed for owner transfer is revoked before commit. Create scoped views under final view-owner identity; strip hostile creator-default ACLs again. Only after exact ownership/policy/function/trigger assertions grant registrar/runner final rights and commit. Failure at each installation checkpoint rolls back all newly created role/object/ACL state.

Helper/view owner gets narrowly listed metadata SELECT and minimum UPDATE(column) for user/account/source/authorization row locks; source payload SELECT only WB token or Avito client ID/secret, never cached access token or password. Complete credential-history SELECT is metadata-only. These are real contained DML/payload capabilities, not intrinsically lock-only privileges.

Separate trigger owner gets only maintenance schema USAGE, authorization identity/revocation SELECT and UPDATE(revoked_at,revocation_reason_code). No base source, account, user, encrypted payload or key access. Owner-only auth-table SELECT USING true and UPDATE USING unrevoked/WITH CHECK finite source_changed revocation provide cross-org metadata access without GUC/BYPASS. Exact OLD restriction lives in fixed trigger body, not RLS (OLD is unavailable in a policy).

Runtime gets no maintenance grants. Trigger invocation does not require direct runtime EXECUTE; prove this with actual role. Functions reject unexpected relation OID/event/row timing/arguments with typed fixed errors; AFTER trigger returns NULL, never payload record. Watch WB token_id/user_id/organization_id/wb_token and Avito credentials_id/user_id/organization_id/client_id/client_secret assignments plus DELETE. Find unrevoked matching provider+OLD source ID authorizations in UUID order, lock then revoke atomically. Do not filter by current/OLD org, expiry or tenant context. Cache-only mutation never revokes; unchanged watched assignment may conservatively revoke. IDs reused, reassigned or deleted/reinserted do not inherit old proof.

Install ordinary enabled source triggers (tgenabled='O'), exact AFTER ROW UPDATE OF watched columns and DELETE; not ENABLE ALWAYS. Preflight refuses runtime/runner effective legacy TRUNCATE, source ownership, ALTER/DISABLE capability, privileged SET ROLE or applicable session_replication_role SET privilege; it never repairs old runtime ACLs. Refuse incompatible pre-existing rewrite/trigger behavior rather than claim cache-only safety blindly. Exact origin-mode, unrelated trigger preservation, bypass denial and absent/wrong GUC tests required. Privileged replica/restore/truncate/disable stays a separate suspension/re-review operation.

Runner encrypted-table SELECT and INSERT policies are explicitly role-targeted RESTRICTIVE, intersecting the existing permissive tenant policy with active persisted direct-session principal and exact approved target/owner/kind; INSERT also requires allow_backfill and generation1. Audit uses only the fixed helper in the actual-schema amendment, not a runner base-table grant or a non-enforcing policy on the existing non-RLS table. No runner UPDATE/DELETE/TRUNCATE. Helper/view owner has metadata-only history rights and is distinct from runner, so ALL historical UUIDs remain visible as metadata through the scoped view. Caller GUC cannot bypass the restrictive authorization condition; adding merely permissive policies is insufficient.

Separate explicit empty-only deprovision artifact uses the same fixed-order ACCESS EXCLUSIVE NOWAIT locks, proves BOTH private tables unfiltered empty and exact active manifest, then removes caller rights/exact views/triggers and operational policies, converts functions to invoker BEFORE ownership return, and proves exact inert state before commit. Any failure rolls back; no waits/retries/cleanup of populated history. Dedicated owner roles may be removed only if exact dependency checks prove no unrelated use; never delete registrar/runner identities or DROP OWNED/CASCADE. Any retained target/authorization (even expired/revoked) refuses. Populated suspension/retirement remains a later owner-approved procedure, not a destructive rollback.

## Store interfaces and execution

New boundary (name selected, implementation not present):

```python
backfill_mapped_credential(authorization_id: UUID, operation: str) -> CredentialMetadata
# operation is exactly "backfill" or "verify". No Session/owner/plaintext input.
```

Validate types before I/O. Open exactly one own Engine-bound Session/READ COMMITTED/non-autocommit physical root. Scoped authorization metadata view first resolves only this active authenticated session principal/operation and exact ID; zero/multiple/malformed rows deny. Set tenant from that validated metadata inside the root; caller IDs/GUC never substitute for persisted authority.

Lock helper authenticates authorization nonlocking and validates source locator BEFORE foreign locks; expected user SHARE→exact account UPDATE→exact source SHARE→exact authorization SHARE. Immediate missing-lock checks, then fresh separate statements after waits. Verify active same-org user, connected canonical provider/external/ref, unchanged exact source owner/ID/org, valid current direct recipient and operation/DB clock. Never locking-join user after account. Revoke-before-final-lock denies; helper-first revoke drains admitted root before denying later roots. No retroactive cancellation or expiry-at-network-COMMIT guarantee.

Scoped readonly security-barrier source views expose only authorized source payload. Complete-history view has owner rights/metadata-only SELECT, joins exact authorized canonical parent under tenant RLS then LEFT JOIN ALL exact owner/provider/kind encrypted rows. No target/gen/revoked/key filter. One NULL-credential sentinel alone proves authorized empty history; zero rows is authorization/context denial. Target-only ciphertext policy does not establish historical absence.

No history + backfill: exact preassigned UUID/generation1, existing schema1 payload/AEAD/current key/fresh nonce; no revoke, replace, ON CONFLICT UPDATE or cached Avito access. Flush, expire/requery persisted row via shared resolver core in SAME root, validate AAD/schema/expiry and exact typed field equality in memory, then safe audit. Existing history succeeds only one exact target UUID/generation1 active equal payload with no other history; revoked/other UUID/newer generation/legitimate rekey all conflict without writes. Verify never creates. Existing public put/resolve/rekey not called inside this root; no second crypto/persistence repository.

All failures rollback ciphertext and audit. Return redacted metadata only after physical commit; uncertain commit reports failure/unknown safe code, not success or automatic rerun overwrite. Existing resolver/store outputs/consumers remain unchanged. Key/config/driver errors must not reveal exception text/SQL parameters/key inventory/source fragments. Canaries cover failures/readback/audit/CLI later.

Runner restrictions prove approved identities and columns, not arbitrary SQL algorithm compliance. Runner is trusted to execute this reviewed store operation and temporarily holds keyring; direct permitted INSERT does not prove helper locks, all-history admission or AEAD verification. Characterize that surface explicitly; do not market role grants as malicious-runner tamper proof. Actual trusted-store tests must establish no-history resurrection and single-root atomicity.

Source invalidation revokes maintenance authorization only; it neither replaces nor revokes already committed encrypted credentials. A legacy write after a successful backfill can therefore invalidate parity. Report/verify must show unresolved authority/source mismatch, and encrypted-only production switching still requires approved writer fence/drain, parity, restore and observation gates. This slice makes no continuous legacy/encrypted synchronization claim, never adds trigger privileges over ciphertext, and never solves divergence with plaintext fallback or automatic backfill overwrite.

## Required evidence before READY

This remains a conceptual design, not dispatchable Task text: exact free revision/path, fresh-root registrar surface/trust boundary and full provisioning policy/column matrix must be specified before dispatch. Independent critic's target-renewal, restrictive RLS, opaque UUID proof, ordinary trigger-state and NOWAIT empty-deprovision findings are incorporated above; actual tests remain future obligations. No proof handle is verified provider authority by itself. Current store safe-code allowlist must explicitly admit any new fixed maintenance codes; do not rely on unrecognized codes silently collapsing to generic persistence failure for authorization semantics.

- Empty inert migration no new roles/trigger/view/definer and hostile-default ACL tests; populated synthetic preservation and guarded downgrade.
- Atomic template rollback at every privilege transition, role collision/transitive SET ROLE denial, no callable unsafe-owner window.
- Immutable target reservation, renewed authorization exact committed retry, cross-owner target collision, expired-but-unrevoked explicit replacement, changed-source/gen2 refusal; no original-proof-from-UUID claim. Registration source mutation between verification and lock must fail exact in-memory equality before metadata insert; synthetic proof acceptance is not operational Avito verification.
- Existing permissive tenant-policy OR cannot bypass restrictive runner policies; same-org alternate target ciphertext denied while complete metadata history remains visible. Ordinary trigger state/bypass-capability refusal preserves existing ACLs; contended empty deprovision NOWAIT rolls back all own changes.
- Runtime watched UPDATE/DELETE absent/malformed/wrong tenant GUC revokes every OLD mapping, preserves unrelated/cached source, rollback restores source+authorization, ID reuse/multiple mappings stable order.
- Actual registration/mutation/helper, WB bind/locked resolver/cabinet delete and store put/rekey/revoke races with observed pg_blocking_pids and finally release before thread join; no original40P01 regression.
- Exact source views, forced RLS, spoofed role/context/owner/provider/expiry/revoke denials with actual eventual roles and positive controls.
- History alternate UUID revoked/newer/target-plus-other visible only as metadata; wrong context gives zero not empty sentinel; store refuses before insertion and exact counts unchanged.
- One physical root/connection, persisted encrypted readback canary corruption, audit/commit failure rollback; interrupted-before-commit vs committed retry/rekey conflict; no separate public put/resolve call.
- No new consumer/queue/flags/key deployment/live backfill and no production proof invented. Production rollout still needs owner inputs and later parity/restore/observation.
