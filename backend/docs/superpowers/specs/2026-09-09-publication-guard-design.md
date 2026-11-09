# User-session publication guard design

Status: local implementation decision within the user's all-stage T1 mandate;
no production activation, worker delegation or provider approval. Root coordinator
confirmed permission policy on2026-09-09: Orders service uses fixed `sync:run` for
user-initiated canonical synchronization/publication and fixed `cabinet:read` for
persisted reads. These constants never come from HTTP/client/queue payloads.
Existing membership explicit permissions UNION existing profile permissions remain
authoritative. No permission profile/grants change.

## Chosen approach and alternatives

Use caller-transaction metadata locks and a transaction-bound final validation.
An enqueue-time check cannot fence later revocation; an independent resolver/access
session releases locks before the publication transaction. A new delegation/epoch
schema is unnecessary for this user-session slice and would invent worker policy.
The selected approach costs bounded database lock contention; all network I/O stays
outside the transaction and multiple accounts are locked in ascending ID order.

## Fetch authority capture

Add `credential_store.resolve_marketplace_credential_for_fetch(owner, kind)` returning
`ResolvedCredentialForFetch`, a slots-based redacted/nonserializable envelope with
explicit `.secret` (existing DecryptedCredential) and `.binding` (safe frozen
`CredentialFetchBinding`). The binding contains `owner`, exact `external_account_id`,
exact nullable `credential_ref`, and `credential_identity: CredentialIdentity`.
Identity includes org/account/provider/kind/schema/UUID/generation/expiry, no key
version inventory, nonce or ciphertext. Unknown errors remain typed safe codes.
The new fetch API requires connected canonical account; the old API behavior is
preserved for unchanged consumers. No account-name/latest-organization selection.

One explicit account+active credential joined SELECT supplies both binding and
encrypted row from one PostgreSQL statement snapshot, avoiding mixed account/row
reads. Decrypt that exact row; do not perform a later metadata query to label the
secret. Release the short local session before provider fetch. No provider call,
legacy fallback, raw value serialization, commit of unrelated work or new consumer
switch. Preserve the current resolver's public signature and behavior.

## Publication API

New `app.platform.integrations.publication_guard` exports:

```python
UserSessionPrincipal(organization_id, user_id, membership_id, session_id)
ExpectedAccountBinding(marketplace_account_id, provider, external_account_id, credential_ref)
ExpectedCredential(marketplace_account_id, credential_id, kind, generation,
                   payload_schema_version, expires_at)
ExpectedIngestionToken(marketplace_account_id, token_id, scope, expires_at)
acquire_publication_guard(session, *, principal, required_permissions,
                          accounts, authorities) -> PublicationGuard
PublicationGuard.revalidate_before_write() -> datetime
```

User/session IDs are nonempty strings matching schema limits; org/membership/account
IDs are positive INTEGER-compatible ints, never bool. Provider/kind/schema use the
existing allowlist; UUIDs and aware expiry are exact. `credential_ref=None` requires
NULL, not wildcard. Duplicate/conflicting bindings and authorities are rejected.
Only user-session principal is supported. No worker/admin/session-less/bearer-only
fallback. A verified ingestion token may supplement a user principal, never create
one. Required permissions are a nonempty frozen set supplied by trusted service.
`PublicationGuardError` exposes only one of `publication_context_invalid`,
`publication_access_denied`, `publication_binding_changed`,
`publication_authority_invalid`, `publication_expired`,
`publication_persistence_failed`; unknown codes normalize to persistence failure.
Errors never interpolate SQL, binding metadata, key paths or input fragments.

Principal is captured by trusted service from authenticated ActorContext plus
canonical membership identity before fetch, not accepted from request fields.
The guard freshly matches the expectation against the DB; the dataclass is not
an authentication token. Every credential/token used for fetch must appear in
authorities. Empty authorities are permitted only for a trusted credential-independent
operation such as persisted read; Orders publication service must forbid omission.
Persisted reads use live user/session/membership/account scope, not current credential
availability as a blanket condition. A cursor is not enduring authorization.

## Transaction protocol

Require existing clean, active root SQLAlchemy Session transaction on PostgreSQL
READ COMMITTED, no nested transaction and no pending new/dirty/deleted objects.
Guard never starts another session or commits caller work. Suppress autoflush while
acquiring locks; explicit metadata column projections avoid password/refresh hashes,
verifiers, ciphertext and stale identity-map fields. Reject tenant changes and reuse
on another or ended transaction. Only one guard per root transaction.

Lock order: user SHARE → membership SHARE → login session SHARE → all exact accounts
UPDATE ascending ID → exact credential SHARE sorted(account,kind,UUID) → token SHARE
sorted(account,UUID) → caller's run/projection rows. Re-read after every wait.
User and membership must be active and match org/user/membership IDs. Login session
must match user and be unrevoked/unexpired. Membership permission union and account
scope are fresh. Preserve positive integer/ASCII decimal-string account-list
compatibility; malformed JSON/types deny, never become all-account scope.

Accounts must be connected and match org/provider/external identity/ref. Credential
ID/generation/kind/schema/expiry must match exactly and remain unrevoked. Tokens must
match exact account/provider=avito/UUID/scope `avito.browser_snapshot.write`/expiry
and remain live. New active rows never substitute for revoked old evidence.
Metadata guard performs no decryption, own-session resolver, verifier or network.

Use DB `clock_timestamp()`, not transaction-fixed now(), after lock waits and before
write. A per-session before_commit hook flushes pending caller writes and validates
again after flush waits; validation failure prevents commit. Handle cleanup is tied
to root transaction completion/rollback; caller must roll back failures. Guard checks
must also detect same-transaction metadata/context tampering. No global auth hook
affects sessions that did not acquire a guard. Direct Session.commit cannot silently
omit the final check. No nested/SAVEPOINT use while guarded.

Supported Session callback lifecycle is fail-closed: at commit callback entry,
verify that the guard is the last effective before_commit callback. Trusted
callbacks registered before it may run; any later callback, even intended as a
read-only observer, is unsupported and denies commit rather than guessing its
effects. Include effective class/instance listener order in this check. Perform
one explicit flush, then reject any remaining new/dirty/deleted ORM work before
validation; reject pending ORM work created during validation too. Do not leave
work for SQLAlchemy's later _prepare_impl flush loop, silently drain an unbounded
hook loop or mutate the listener collection while dispatching. A guard denial
poisons the handle until caller rollback. This is a supported caller-Session
protocol, not a sandbox for arbitrary private-state mutation, raw DBAPI/connection
COMMIT or cursor/connection callbacks executing SQL after the last check; those
bypasses are prohibited caller behavior and not covered authority guarantees.

Locks establish which committed operation wins: publisher first may commit before
revoke; revoke first makes publisher abort. Final validation proves expiry at that
validation instant, not an impossible guarantee that time cannot advance between
the last SQL statement and COMMIT. Account binding equality is current-state proof,
not historical A→B→A detection: no approved binding epoch exists. Access-token exp
is not in ActorContext; this slice validates live DB login-session expiry and must
not call it access-token/job-delegation expiry.

## Existing WB writer compatibility

`wb_credentials._binding_candidate(lock=True)` currently locks account then a token
JOIN user with unqualified FOR UPDATE, creating user→account vs account→user cycle.
Repair using metadata-only token owner locator, lock that exact user SHARE first,
then account UPDATE, then token `FOR UPDATE OF lk_user_wb_tokens`; refresh owner,
organization, active status, token/account relationship after waits. If locator
changes, fail closed. Do not merely drop owner lock. Preserve seller verification,
current-token exact comparison and all existing safe behavior, mock provider only.

## Verification and boundaries

RED then GREEN: exact metadata types/permissions/scope/provider/generation/expiry,
redaction, envelope serialization refusal, no secret-column reads, session lifecycle,
same-row capture, final commit enforcement, transaction reuse/nesting/context drift.
Real disposable PostgreSQL: both winner orders for credential/token/user/membership/
session/account revoke; replacement/reencrypt, stale ORM identity map, expiry during
account/metadata/domain-lock waits, final flush expiry rollback, actual WB binder
lock-order race, pool context reuse, rollback on audit/flush/commit failure. Use
barriers/events and actual owned writers where relevant, synthetic rows only.

No schema or migrations, permission expansion, HTTP/router/task/domain edits,
production key access, real provider activity or KIZ/matcher work. Worker delegation,
bearer-only ingestion authority and future historical binding epoch are separate
contracts; none block this user-session implementation. Coordinator receives exact
imports/arguments/tests and remaining limits before T3 wires its own repository.

## Physical transaction admission follow-up

Account-context testing demonstrated that a SQLAlchemy Session logical root and
get_isolation_level()==READ COMMITTED can coexist with DBAPI AUTOCOMMIT. Such a
connection does not satisfy this guard's existing transaction-local lock contract.
Connection.begin_nested() is likewise not represented by Session nested hooks.
These are admission gaps to test directly against the guard, not evidence that a
configured ordinary READ COMMITTED consumer already bypasses authorization.

Also require an Engine-bound Session via its public get_bind type. A Session
joined to an externally started Connection root can finish its before_commit hook
and logical transaction without physical COMMIT. Reject all Connection-bound
Sessions, rather than guess join ownership or inspect private transaction maps.
The standard get_session_factory Engine binding remains supported. Real default
conditional_savepoint and explicit rollback_only external-root tests must deny
before application context/authority SQL and leave external owner work untouched.

At acquisition before context SQL and at every guard context revalidation, inspect
the actual bound Connection: active root transaction, no Connection nested
transaction, and publicly exposed DBAPI autocommit exactly False. Missing or
unverifiable driver state denies with publication_context_invalid; SQLAlchemy
inspection failures retain publication_persistence_failed. Keep Session checks,
READ COMMITTED checks and finalizer unchanged. Do not use private transaction maps,
global hooks or issue BEGIN/COMMIT/ROLLBACK to repair an unsupported caller mode.

An unsupported mode observed after acquisition poisons the guard through its
existing safe failure path and requires caller rollback. An arbitrary raw
SAVEPOINT opened and closed entirely between checks is not claimed to be observed;
direct SQL/DBAPI protocol bypasses remain unsupported. No new driver support,
schema/permissions, consumer writes or provider actions.

Actual PostgreSQL tests must show engine AUTOCOMMIT and connection
execution_options AUTOCOMMIT rejection; initial Connection SAVEPOINT rejection;
post-acquisition active Connection SAVEPOINT rejection during explicit validation
and guarded commit; positive ordinary Session root and existing finalizer/lock
tests unchanged. Observe no application context/authority SQL on initial invalid admission
(ordinary driver connection initialization is not a guard authority query),
and no durable synthetic publication on denied commit.
