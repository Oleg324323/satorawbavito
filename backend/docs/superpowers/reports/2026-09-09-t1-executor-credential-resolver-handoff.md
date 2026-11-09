# Dedicated executor credential resolver — source handoff

Status: **IMPLEMENTED / UNVERIFIED**. Bounded delegation from T1 plan
`afa505a7f703c215c19ccd34252cc624d20ff4ae`, on existing executor/physical verifier
`9b30eb2cb59b38d18e859773b9b9607406832bb5`. No tests, review, import, compile,
PostgreSQL or Redis gates run for this package, per source-first direction.

## Exact entrypoint

```python
from app.platform.integrations.credential_store import make_executor_credential_resolver

# All names below are pre-existing trusted bootstrap dependencies, not defaults.
resolver = make_executor_credential_resolver(
    session_factory=executor_session_factory,
    keyring_loader=explicit_keyring_loader,
    identity=executor_role_identity,
)
# Inject resolver as credential_resolver into the existing RepricerJobExecutor.
# Invocation: resolver(account_identity: MarketplaceAccountCredentialOwner,
#                      kind: str) -> ResolvedCredentialForFetch
```

All three constructor arguments are required. Identity is the exact existing
`ExecutorRoleIdentity`; loaders/factories must be callable. Construction performs
no connection/key loading. The private slotted callable has redacted repr/str and
refuses copy/deepcopy/pickle. It has no configured pool/key fallback, role creation,
default credential, provider client or queue integration.

## Same physical read root

The callable validates the existing owner/kind contract. A malformed factory's
non-Session, inactive/dirty/pending/nested or Connection-bound result is rejected
without rollback/close of that foreign root. An accepted clean Engine-bound PG
Session becomes owned by this call. It opens a physical root, requires actual
non-autocommit READ COMMITTED via the concrete verifier and checks actual
`session_user/current_user` before key loading or encrypted-row selection.

An explicit loader must return the exact existing `CredentialKeyring`. After key
loading the same Session root/Connection/physical transaction is checked again,
then exact org/account transaction-local context is installed. One existing paired
account/active-credential outer join reads external identity, credential reference
and encrypted ORM row together. Existing expiry, identity, decrypt and redacted
wrapper assembly are reused in `_resolve_fetch_in_session`, not reimplemented.
Final concrete login and same-root checks precede read rollback and Session close.
Both cleanup operations are attempted; either failing prevents returning the
wrapper. No ORM object escapes. No domain/provider callback runs in the root.

This requires ordinary encrypted-row SELECT and the existing worker-identity
catalog queries on the dedicated physical login, with current FORCE RLS/context
contracts. No grants or role/config changes were made. Runtime custody/key-loader
isolation and the existing job/user/generation checks remain required; a credential
read is not authorization to send. Closing does not fetch credentials.

## Compatibility and safe failures

The existing public `resolve_marketplace_credential_for_fetch(owner, kind)` keeps
its signature, configured loader/pool, tenant-context setup, SQLite compatibility,
missing-account versus missing-credential distinction and typed error behavior.
It delegates to the same private query/crypto core. No other store reader/writer,
rekey or maintenance behavior was edited.

New adapter uses existing error types/codes only:

- invalid callable inputs, role verification or key loader: configuration invalid;
- malformed owner/kind or foreign/root substitution: contract invalid;
- existing missing account/credential and expiry codes are preserved;
- existing safe `CredentialCryptoError` is preserved;
- unexpected SQL/session/cleanup failures: persistence failed, without raw text.

No URLs, role/key inventories or raw errors are added to metadata. Python object
memory is not zeroized; no such guarantee is claimed. Operational grant/key custody
and external-effect guarantees are outside this source package.

## Pending centralized acceptance

Observe the actual resolver query connection's session/current user with synthetic
executor/API roles. Deny API/global/wrong-role/SET ROLE before key/ciphertext access;
exercise malformed/dirty/nested/joined/autocommit roots, loader/SQL/cleanup failures,
same-root sabotage, no fallback, wrong scope/provider, expired/revoked/corrupt rows,
changed generation at the existing job fence and repr/copy/pickle canary checks.
Run original paired resolver compatibility and T2/T4 composition gates. Existing
T2 worker tests still use their earlier injected resolver fixture; they must consume
this concrete adapter in a later separately scoped composition change. No claim
that those pending tests already verify this factory.

Rollback reverts only this factoring/adapter and this handoff; no persisted data,
schema, operational grants, scheduler, provider or production state changed.
