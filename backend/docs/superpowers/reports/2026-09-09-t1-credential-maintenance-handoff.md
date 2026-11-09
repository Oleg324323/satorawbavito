# Credential maintenance inert foundation — T1 handoff

2026-09-09. **IMPLEMENTED / UNVERIFIED, inert source foundation only.**

The latest user architecture-foundation priority stops this package at migration
0079 and inert provision/deprovision contracts. The approved larger maintenance
store operation, trusted registrar and CLI are P1 and have not been implemented.
This package provides no operational backfill, verified mapping, key custody,
cutover, restore procedure or executable registration workflow.

Source commit `5ec0746bce06989337803386806721931ed45c6f` adds exactly:

- `backend/alembic/versions/20260909_0079_credential_maintenance.py`
- `backend/ops/credential-maintenance-provision.sql`
- `backend/ops/credential-maintenance-deprovision.sql`

This handoff is the fourth path in the finite package. The scope amendment arrived
immediately after that first source commit, so the handoff is a follow-up commit;
history is preserved without amendment, rebase or restaging disjoint changes.

## Actual dependencies and preserved code

The clean dispatch base was `eb079c6da56df5d90c3c6b7cbde86ab43ee87edd` on
`codex/arch-t1-platform`. All revision/down_revision/branch_labels/depends_on
declarations were read as text, including the historical 0040 merge. There was
one terminal 0078/down0077 and no 0079. The new revision is exactly `20260909_0079`
with `down_revision = "20260909_0078"`. No Alembic/Python execution determined it.

Disjoint controller imports/docs were preserved. T4's actual pure contract arrived
as `c74c4a58ec5fd6ed59d4ffe45945a5d57466ddaf` (origin
`ef027ea87fed51b4bfb80b1e40a95413c0cfbe77`) and was read in full. Its frozen/slotted
`MaintenanceTarget` has five fields and `MaintenanceAuthorization` has all 22
specified fields; `MaintenanceContractError()` takes no argument and exposes one
fixed code, and `validate_operation()` accepts exactly backfill/verify. These are
metadata shape contracts, not proof or current authorization. They remain an
independent imported source package, not newly authored here.

The actual existing `_insert_verified_credential_in_session` in credential_store,
imported as `7f76425`, and its full handoff were read. It remains the single future
maintenance insertion/AEAD/persisted-readback dependency. Existing normal put,
resolve/rekey/revoke/status signatures and guarded management participants were
not changed. No maintenance store/registrar/CLI file was started, so there was no
partial Python implementation to remove or silently claim complete.

## Inert migration object manifest

Private schema `credential_maintenance` contains two tables, no sequence:

| Table | Exact persisted contract |
|---|---|
| targets | Non-nil target UUID primary key; positive INT4 organization/account; C-collated provider/kind limited to wb/wb_api or avito/avito_oauth_client; owner/provider/kind UNIQUE and exact composite target/owner/provider/kind UNIQUE |
| authorizations | Non-nil auth UUID; composite FK to reserved target; positive INT4 org/account/source; exact VARCHAR(64) source user; nonempty VARCHAR(128) C external ID; nullable VARCHAR(255) C reference with canonical `lk_user_wb_tokens:<source_id>` for WB; OID/name recipient pair; non-nil review/authority/proof UUIDs; finite reviewed_at/not_before/expires_at with positive interval; explicit operation booleans; nullable paired finite revoke time and source_changed/operator_revoked/security_incident code |

`source_user_id` has the actual VARCHAR(64) upper bound without invented trim,
normalization or nonempty policy. Source/user existence is checked by helpers.
There is no legacy-source FK or cascade. Partial unique unrevoked owner constraint
plus complete owner/target indexes preserve explicit renewal history. Targets are
immutable for their lifetime; authorization updates are revoke-only, never renewal
or reactivation in place. No UPDATE/DELETE capability is given to a runner.

Both tables have ENABLE/FORCE RLS. `cm_owner_select` on each targets the actual
schema/table owner with verified unfiltered SELECT only. The migration scrubs all
new nonowner schema/table/column/function ACLs, including creator-default grant
options, without changing global defaults or touching old runtime grants.

Two metadata-only invariant triggers are installed: `cm_targets_immutable` and
`cm_authorizations_immutable`, BEFORE ROW UPDATE/DELETE. There are no source
triggers, views, operational roles or SECURITY DEFINER functions in the upgrade.

All ten static functions are SECURITY INVOKER with fixed
`search_path=pg_catalog, pg_temp`; source/table references are schema-qualified:

| Function signature in credential_maintenance | Inert / proposed active role and scope |
|---|---|
| target_immutable() | Schema owner; metadata trigger invariant, stays invoker |
| authorization_immutable() | Schema owner; metadata trigger invariant, stays invoker |
| assert_inert() | Schema owner; unfiltered empty proof, static body/table-shape seal and exact inert catalog/ACL assertions; stays invoker |
| lock_binding(authorizations) | Helper owner only; source locator precheck, user SHARE → account UPDATE → exact source SHARE, immediate missing-row denial and fresh separate binding checks |
| lock_authorization(uuid,text) | Helper owner; runner EXECUTE only when provisioned; actual session_user role OID/name, exact operation, binding locks → authorization SHARE → fresh identity/clock check |
| lock_registration(authorizations) | Helper owner; registrar EXECUTE only when provisioned; binding locks → complete owner/target/auth UUID-order UPDATE locks; exact source payload result for future in-memory attestation |
| register_authorization(authorizations,uuid) | Helper owner; registrar EXECUTE only when provisioned; metadata-only SQL, immutable exact retry, preserved reservation/history and explicit old-row revocation; this SQL helper is not mapping proof |
| append_safe_audit(uuid,text) | Helper owner; runner EXECUTE only when provisioned; exact direct-session/binding/time/history checks, derive all fixed audit values, no caller JSON/actor/owner arguments |
| invalidate_wb_source() | Separate invalidator owner; ordinary source trigger only, no caller EXECUTE grant |
| invalidate_avito_source() | Separate invalidator owner; ordinary source trigger only, no caller EXECUTE grant |

A schema-owner-controlled comment stores a nonsecret seal of these static function
bodies and new table columns/constraints/index definitions. Operational function
owners cannot change that schema comment. Inert and active assertions compare the
seal; no legacy payload, credential digest or source proof is stored there.

Downgrade locks targets then authorizations ACCESS EXCLUSIVE NOWAIT, verifies
unfiltered emptiness and inert state, and removes only enumerated objects with
RESTRICT. Populated/activated/extra-object state refuses; an unknown dependency
also prevents a restrictive drop and rolls the transactional DDL back. There is
no CASCADE, DROP OWNED, TRUNCATE, row deletion or populated rollback procedure.

## Inert active-state template contract

Both SQL files are inert artifacts, not invoked by migration, startup or CLI.
Deprovision explicitly includes the provisioning file with `cm_action=deprovision`
so lock/object/grant/policy assertions have one source. The whole transaction must
run through psql `-X` with ON_ERROR_STOP only after separate acceptance/approval.
This handoff is not authorization to execute either artifact.

All database and role identities are operator inputs: `cm_database`, and exact
OID/name pairs for schema owner, helper/view owner, invalidator owner, registrar
and runner. `cm_runtime_roles` is an explicit JSON array of runtime OID/name pairs.
No default operational roles, mapping, proof, key, duration or secret parameter
exists. Role existence is required; neither file creates/removes roles. Completeness
of the supplied runtime inventory is a separate owner obligation, not proven by a
JSON list. Direct current/session schema-owner identity and selected database must
match. Dedicated helper/invalidator roles are NOLOGIN, NOINHERIT, non-superuser,
non-BYPASSRLS, non-CREATEROLE/CREATEDB/REPLICATION and not table/schema owners.
Registrar/runner are separate dedicated direct logins with those privilege flags
disabled and no membership paths. Transitive runtime/runner reachability, source
ownership/TRUNCATE/TRIGGER, schema CREATE and session_replication_role SET deny
activation instead of repairing historical capabilities.

Provision/deprovision acquire WB source → Avito source → targets → authorizations
ACCESS EXCLUSIVE NOWAIT, prove BOTH tables empty without tenant filtering, and
refuse unexpected trigger/rewrite/role/ACL/dependency state. There is no polling,
process termination or lock timeout policy. Incompatible unrelated triggers are
preserved by refusal, not adopted/deleted. Invocation must be origin mode.

Ownership transfer occurs while functions are invoker, before enabling definer.
Views are created under final helper owner, creator-default ACLs are scrubbed again,
temporary schema CREATE is revoked, and caller grants are applied within the same
atomic transaction before exact active assertions. Every failure rolls back that
activation attempt. Deprovision validates active state, removes caller grants and
exact source triggers/views/policies, switches functions to invoker before returning
ownership, removes exact helper grants and verifies inert state. Retained metadata,
foreign dependencies or unrelated dedicated-role use cause refusal. Dedicated roles
and registrar/runner identities are retained.

Four security-barrier views, owned by the helper, are proposed:

- `authorized_metadata`: active unrevoked direct `session_user` OID/name pair and
  DB-clock window. No caller-selected role string or current_user in definer is proof.
- `authorized_wb_source`: only exact authorized source ID/user/org/current account
  and token, under validated tenant context.
- `authorized_avito_source`: analogous client_id/client_secret only; no cached access.
- `authorized_history`: exact approved connected account under tenant RLS, LEFT JOIN
  every encrypted row for owner/provider/kind without target/gen/revoke/key filtering.
  Exactly one all-NULL credential row is the authorized-empty sentinel; zero denies.

Source trigger pairs are ordinary enabled `O`, AFTER ROW, no arguments:

| Source | UPDATE OF watched fields | DELETE trigger |
|---|---|---|
| public.lk_user_wb_tokens | cm_wb_source_update: token_id,user_id,organization_id,wb_token | cm_wb_source_delete |
| public.lk_user_avito_credentials | cm_avito_source_update: credentials_id,user_id,organization_id,client_id,client_secret | cm_avito_source_delete |

Each function rejects wrong relation/event/timing/level/arguments, locks all matching
OLD provider/source-ID unrevoked authorizations in UUID order and revokes them with
source_changed. There is no caller/OLD tenant GUC/org/expiry omission. Cache-only
updates are not watched; assigning a watched field may conservatively revoke even
if equal. AFTER functions return NULL, never the payload record. Authorization
invalidation does not alter already committed ciphertext or maintain parity.

## Exact active column/privilege matrix

| Role | Base SELECT | Write / execution capability |
|---|---|---|
| helper/view owner | All nonsecret targets/auth metadata; lk_users(user_id,organization_id,is_active); accounts(marketplace_account_id,organization_id,marketplace,external_account_id,credential_ref,status); WB(token_id,user_id,organization_id,wb_token); Avito(credentials_id,user_id,organization_id,client_id,client_secret); encrypted(credential_id,organization_id,marketplace_account_id,provider,credential_kind,generation,payload_schema_version,expires_at,revoked_at,revocation_reason_code,created_at,updated_at) | targets/auth INSERT; UPDATE users(user_id), accounts(marketplace_account_id), WB(token_id), Avito(credentials_id), auth(authorization_id,revoked_at,revocation_reason_code); audit six columns below + exact existing event_id sequence USAGE; owns active views/functions |
| invalidator owner | auth(authorization_id,provider,source_id,revoked_at,revocation_reason_code) only | auth UPDATE(revoked_at,revocation_reason_code); maintenance USAGE; owns only two invalidators |
| registrar | No base source/metadata SELECT; scoped source result only through exact lock_registration EXECUTE | register_authorization EXECUTE; no encryption/key/runner capability |
| runner | Four views; encrypted ORM columns under exact-target RESTRICTIVE SELECT | encrypted generation1 INSERT under RESTRICTIVE policy; lock_authorization and append_safe_audit EXECUTE; no base source/account UPDATE, encrypted UPDATE/DELETE or audit/sequence access |
| runtime | No new rights | No new rights; source-trigger invocation needs no runtime EXECUTE grant |

Row-lock UPDATE(column) grants are genuine contained DML capabilities, not special
lock-only privileges. Helper metadata policies grant explicit SELECT/INSERT and
operator_revoked-only UPDATE; invalidator SELECT is unfiltered across organizations
and UPDATE is unrevoked→finite source_changed only. Exact OLD restriction is fixed
function code because OLD is unavailable to an RLS policy. Existing account/encrypted
tenant policies remain. `cm_runner_select` and `cm_runner_insert` are RESTRICTIVE
and intersect existing permissive tenant policies. Same-org other UUID cannot gain
ciphertext access while complete history remains exposed only as metadata.

## Actual-schema safe-audit amendment

Source inspection showed `lk_audit_events` in 0005/0006 has no RLS. The explicit root
ruling recorded in the approved spec/plan commit `619c0c5` therefore uses the fixed
helper described above. No ineffective restrictive audit policy, global audit RLS
change or runner base audit privilege is included.

Only the helper receives INSERT on
`organization_id,actor_user_id,action,object_type,object_id,details` and USAGE on the
actual catalog-verified serial/identity dependency of event_id. No audit SELECT or
RETURNING occurs. It derives actor NULL, object type marketplace_account_credential,
target UUID and fixed action integration.marketplace_credential.backfill|verify.
The closed details keys are credentialKind, generation=1, marketplaceAccountId,
operation, provider and resultCode=ready. The runner supplies only authorization
UUID plus closed operation; no arbitrary owner/actor/details are accepted.

Future trusted store code must verify persisted AEAD and exact typed source equality
BEFORE calling the helper in the same physical root. Directly calling the helper is
not proof of that algorithm. Every successful invocation writes one event; an explicit
committed retry may write another, including after uncertain commit. Cipher state
may be idempotent without exactly-once audit. No receipt state or fake exactly-once
guarantee was added.

## Required P0 verification and deferred P1 completion

No tests or test files, RED, reviews/critic, import, compile, lint, PostgreSQL, Redis,
Alembic, provider/network calls, key/config loads or operational SQL were run for
this package. Source actions were bounded reads/searches, apply_patch, exact staging
and local commits with per-command hooks disabled. Source existence is not gate
evidence. Final centralized verification must include actual bootstrap/migration,
inert role isolation and protected existing credential integration before foundation
acceptance. Inert templates cannot be declared operationally safe from source alone.

The full approved maintenance spec retains these unrun obligations for later
operational readiness:

- Empty upgrade/downgrade/upgrade, hostile default ACL/grant options, exact owner
  policy/shape seals and populated Orders/Review/credential synthetic preservation.
- Atomic provisioning failure checkpoints, dedicated/transitive SET ROLE denial,
  wrong OID/name/login/database, ordinary trigger state, runtime bypass refusal,
  incompatible unrelated trigger preservation and NOWAIT empty deprovision rollback.
- Actual-role restrictive encrypted RLS against permissive-OR/context spoofing,
  unauthorized target/provider/source/expired/revoked access and history sentinel.
- Runtime watched source UPDATE/DELETE with missing/malformed/wrong GUC, multiple
  OLD mappings, source ID reuse and cache-only updates, source+revoke rollback.
- User/account/source/auth lock races, source changes between independent verification
  and registrar lock, registration/mutation/helper/WB binding/cabinet/store/rekey
  deadlock regressions with actual blocking observations and reliable worker release.
- Immutable exact authorization retry, explicit renewal retaining the same target,
  cross-owner target collisions, expired-but-unrevoked replacement and changed locator
  refusal; no UUID proof handle accepted as independently verified provider authority.
- Complete historical UUID/gen2/revoked/newer/target-plus-other metadata, scoped
  ciphertext denial and no-history resurrection refusal with unchanged counts.
- Future store single root/connection/non-autocommit READ COMMITTED, actual persisted
  corruption/decrypt/typed-equality canaries before audit, rollback and uncertain commit.
- Safe-audit principal/argument/ACL forgery, direct runner audit/sequence denial,
  fixed detail shape, one-event-per-successful-invocation semantics and rollback.
- Future IDs-only CLI strict schema/duplicate conflicts/safe errors/help without I/O,
  per-authorization interruption/committed resume/rekey conflict and synthetic restore.

P1 implementation still requires the fresh-root trusted verifier/registrar wrapper,
store-owned backfill_mapped_credential(UUID,operation) consuming the existing single
readback core, and IDs-only inventory/backfill/verify/report CLI. The SQL registration
helper trusts an attestation that no implemented wrapper currently supplies; granting
it alone would not create that trust procedure. No synthetic or production verifier
was invented. All proof, reviewer authority, runtime inventory, principal/key custody,
validity/duration, timeout/drain, writer fence, parity, observation, retention and
restore policies remain explicit owner inputs. Legacy/encrypted divergence after
a source write is unresolved by this inert foundation. There is no activation,
fallback, provider consumer, new writer, flags, production action, push or deployment.
