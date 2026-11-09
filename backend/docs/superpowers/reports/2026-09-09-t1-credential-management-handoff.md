# Credential management commit fence — IMPLEMENTED / UNVERIFIED

Bounded delegated T4 implementation of the full committed plan
`2026-09-09-credential-management-commit-fence.md`, based directly on exact T1
`b21f2166886ade7637b1b9594b5b28c1e38d3e80`. This is existing Stage1 architectural
hardening, not a new permission, provider consumer, migration or release.

No tests/test authoring, imports, compile, lint, review/critic, PostgreSQL, Redis,
provider/network, key loading, real secrets, environment, operational flags,
roles, push or deployment were performed. Pending central gates are NOT waived.
No security/runtime acceptance is implied by the source description below.

## Ancestry and exact ownership

Existing T4 branch a331a1d uses cherry-picked prerequisite history. An attempted
ordinary merge of b21f216 produced conflicts in out-of-scope shared prerequisites;
that clean-start merge was aborted without resolving/overwriting any such paths.
The existing isolated T4 worktree was switched to a new local branch
`codex/t4-credential-management` directly at b21f216. Original
`codex/arch-t4-experience` remains at a331a1d; no domain package was lost or merged.
No new worktree, dependency install or source baseline test was needed/performed.

The one implementation commit changes only:

1. NEW `app/platform/integrations/credential_management.py`.
2. `app/platform/integrations/publication_guard.py`: one exact-class installer.
3. `app/routers/cabinet.py`: canonical credential GET/PUT/DELETE and their imports/helpers.
4. This handoff.

Store/crypto/config/schema/grants/dependencies/tests/legacy routes are unchanged.
T2's subsequent store-internal persisted-readback extraction is separate; this
consumer uses the stable actual private signatures and performs no replacement
crypto or ciphertext access itself. Ownership of these four paths returns to T1
on receipt of the exact commit; T4 will not edit them concurrently thereafter.

## Actual dependencies used

Read actual current publication guard (including ingestion final-root changes),
access.py, ActorContext, IAM and login/account models, exact cabinet route/payload/
status helpers, unique crypto contract, store primitive implementation and complete
transaction-core handoff. Imported source ancestry includes944c6ed put/revoke/
metadata cores and827620a/742c83f ordinary in-session active resolution.

New service constructor, explicit trusted keyword dependencies:

```text
CredentialManagementService(session_factory, keyring_loader, wb_seller_verifier)
```

It does not open a pool, read keys or contact WB at construction/import. Cabinet
supplies existing get_session_factory(), store._load_keyring and
wb_credentials.fetch_wb_seller_id through its new dependency helper.

Public keyword-only methods return safe CredentialMetadata (status may return
None), only after root completion and owned-session cleanup:

```text
status(authenticated_actor, marketplace_account_id, provider, credential_kind)
put(authenticated_actor, marketplace_account_id, provider, credential_kind, plaintext)
revoke(authenticated_actor, marketplace_account_id, provider, credential_kind, reason_code)
```

Only wb/wb_api and avito/avito_oauth_client are admitted. Actor must be an actual
ActorContext with native positive INT4 organization, real user and session IDs;
actor_id/cached permission claims do not authorize the operation. Membership ID
is selected fresh from current DB by exact org/user, then all authority is locked
and validated by the existing shared live-membership/login machinery.

## Narrow management guard, not a relaxed fetch guard

Private `_CredentialManagementGuard` inherits PublicationGuard's context,
user/member permission/scope checks, login validation and revalidation machinery.
Only `_validate_accounts` is specialized for management. Fixed permission is
integrations:write. Lock order remains user → membership → real login → exact
organization/account/provider → credential metadata/actual store write rows.

Actual account external ID, nullable reference, lifecycle status and ingestion
incarnation are captured as data and compared exactly on every revalidation.
No connected/nonblank-account-value policy is invented for credential setup;
an unchanged existing lifecycle state, including disconnected, remains eligible.
Put's second root also compares the exact first principal/session/membership.
An Avito A→B→A change cannot revive its old captured incarnation.

The new `_install_credential_management_guard(session, guard)` accepts only the
exact private class, clean Engine-bound PostgreSQL READ COMMITTED root and exact
logical/physical transaction. It reuses existing listener registration, final
flush/no-requeued-writes checks, last-listener condition, poison and transaction
end semantics. No callback registry, new permission argument, fake principal or
relaxation of public connected-only fetch/Orders/Repricer/Review/ingestion guards.

Safe result metadata is sealed against the actual latest scoped metadata columns
(SELECT + retained row lock, not ciphertext). Final revalidation compares the
same result and captured account, after flush, before physical commit. Status
verification never exposes/reveals its DecryptedCredential wrapper.

## Fixed operation sequences

PUT retains the existing cabinet kind/payload/length/whitespace checks, plus
service-side closed-shape validation. It copies only the admitted plaintext fields.
A short guarded snapshot root commits/closes FIRST. Keyring readiness is checked
before any WB seller I/O. Existing seller-info verifier then runs OUTSIDE all DB
locks, and its exact seller ID must equal the captured external account ID.
Neither provider error nor malformed/mismatched result can admit a write.
The pre-I/O keyring is discarded; a newly loaded trusted keyring precedes the
second fresh guarded root, which must match the same principal/account snapshot.
Actual `_put_marketplace_credential_in_session` receives its existing signature,
live actor user ID, DB clock and exact expected WB external ID. Its existing row,
replacement generation/revocation and audit writes share this root. No second
public store transaction, account reactivation, OAuth exchange or legacy fallback.

DELETE uses the same fixed live root and actual revoke primitive with DB clock;
the existing reason allowlist and missing-credential behavior remain in store.
Disconnected revoke does not need a keyring or current decryptable credential.

GET locks latest safe metadata and calls the actual metadata primitive in one
guarded root. Missing/revoked/expired metadata does not load a keyring. An active
credential is verified with the actual ordinary in-session resolver, not the
connected paired-fetch resolver or ORM decryption. Corrupt/key-unavailable active
credentials fail closed; the result cannot fall back to legacy or guessed active.
The shared final fence and exact metadata seal run before any result is returned.

Rejected dirty/active/nested/Connection-bound factory Sessions remain caller-owned;
the service does not close/rollback them. Every accepted owned root attempts both
rollback and close on all outcomes, even if rollback itself fails. No root is held
across WB seller I/O. Secrets are not put in audit/guard/result/error; copied input
and keyring references are dropped in finally. Python memory zeroization is not
claimed.

## HTTP and safe failure contract

Paths, verbs, MarketplaceCredentialWriteRequest, kind/payload validation,
DataEnvelope[MarketplaceCredentialStatusView] and status/timestamp fields remain.
Only the three canonical exact-account routes replace separate authorization plus
public-store calls with the new service. Existing plaintext/legacy credential
routes, onboarding actions and all unrelated cabinet endpoints are untouched.

WB identity-invalid400 and seller-mismatch409 retain their existing fixed code
and message. Existing store/crypto codes remain bounded: missing/account-missing404,
binding/auth-failed/expired409, configuration/persistence/key-unavailable503,
contract/payload/reason400. Concurrent-update now explicitly maps409.
New management access-denied403, context-invalid503 and readback-required503 have
fixed `{code,message}` values only. Live DB permissions, not stale actor claims,
decide access; granular former separate-access error codes are replaced by this
bounded live denial where applicable.

Unknown write commit/cleanup is readback-required, including a typed exception
from an after-commit callback when no poisoned final fence proves rejection.
No automatic write retry, rollback claim or exactly-once replacement is offered
without a request ID. GET status remains the safe reconciliation read; an operator
must decide subsequent replacement/revocation. Driver/provider/key exceptions are
reduced to fixed codes and raised outside raw handlers without chained diagnostics.

## Pending single centralized gate

All NOT RUN: existing API/disabled-keyring/payload/WB identity compatibility;
same-org wrong account/provider and full live user/member/login/permission/scope
revocation matrix; disconnected setup/revoke; keyring readiness before verifier;
account/principal change across seller I/O; active corrupt/expired status with
legacy/fetch sentinels; row+audit rollback; two-session replacement generations;
latest-result metadata substitution; final flush/requeued-write/poison/listener/
ended/foreign/root sabotage; unknown commit and rollback/close failures; synthetic
secret/error canaries; old public store and other publication-guard regressions.

Integration must include the final unchanged store-core extraction from T2/T1
and verify actual PostgreSQL runtime-role behavior. This source commit is not a
permission to enable providers, deploy or perform credential rotation.
