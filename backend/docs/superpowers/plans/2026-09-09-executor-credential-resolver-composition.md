# Actual executor-pool credential resolver composition

Source predecessors: repricer executor7d50ee3a96cc647c6baf9945d0723602fd3581f1,
physical-login verifier9b30eb2cb59b38d18e859773b9b9607406832bb5 and existing paired
credential resolver4860c53. Repricer/Review constructor injection is explicit, but
resolve_marketplace_credential_for_fetch currently opens global get_session_factory
internally. An executor wrapper's outer SQL login does not attest that inner read.
This bounded source package closes that composition gap inside the ONE store.

## Task 1: Bind existing paired resolver to actual dedicated read root

Read whole existing paired resolver, secret/binding wrapper, worker_identity,
physical-root guard, repricer constructor/resolve_fetch and their actual handoffs.
Actual code, not a same-named substitute or memory note, defines compatibility.

Allowed exactly two source/handoff paths:

- backend/app/platform/integrations/credential_store.py.
- New backend/docs/superpowers/reports/2026-09-09-t1-executor-credential-resolver-handoff.md.

No tests/DDL/grants/config/env/roles/bootstrap deployment, domain tasks/providers/
routers, secrets, existing legacy readers/writers or operational flags. No gates/
review/import/compile/PG/Redis until final verification after remaining source.

Provide a concrete redacted callable adapter/factory in credential_store itself:
make_executor_credential_resolver(*, session_factory, keyring_loader, identity).
All three inputs required; identity exact existing ExecutorRoleIdentity. Construction
must not open DB/load key/run role provisioning. Returned callable accepts exact
existing (MarketplaceAccountCredentialOwner, kind), returns only existing
ResolvedCredentialForFetch, compatible with already required RepricerJobExecutor
credential_resolver injection and future Review equivalent. No secrets or role/
connection/key inventory in repr/serialization/errors. No default pool/key loader,
fallback global resolver, secret copied into a broker-compatible dataclass, or new
custom decrypt/query/store implementation.

Factor the current account/active-credential OUTER JOIN, identity/expiry check and
decrypt/wrapper assembly into ONE private in-session core. Existing public paired
resolver signature and default behavior stay intact by delegating to this core;
do not globally change old SQLite-compatible tests or public API callers here.
Public wrapper owns its same short read session, preserving current safe error
and missing-account versus missing-row distinction. New concrete executor adapter
owns a freshly returned clean Engine PostgreSQL READ COMMITTED physical root.
Reject pre-existing/nested/joined Connection/autocommit/dirty/inactive sessions;
never rollback/close somebody else's root returned by a malformed factory.

On this actual same resolver connection, begin physical root, call concrete
verify_executor_login(identity=identity) BEFORE loading keys or selecting encrypted
rows, set exact tenant/account scope as current RLS contract requires, then execute
the existing ONE paired join/crypto code. No trusted boolean/caller callback login
attestation, SET ROLE, GUC identity, API login, arbitrary role inventory error or
second session hidden inside this path. Verify exact connection again before
leaving the read root; never run domain/provider callbacks inside it. Distinguish
read closure from write commit uncertainty: adapter performs no mutation and may
rollback its owned read root on completion before returning wrapper. Session must
actually close before provider I/O; cleanup failures cannot expose raw errors or
return an unclosed supposed successful authority. Ordinary Python cannot guarantee
zeroization; do not claim it. No ORM result escapes the root.

Use existing credential configuration/contract/persistence typed errors, safe-map
ExecutorIdentityDenied without role names. Supplied keyring_loader is trusted
bootstrap dependency, not an HTTP parameter. Require actual CredentialKeyring;
loader exceptions fail closed without str(exc), key path or fallback. No connection
URL or raw key source may be retained in newly serializable metadata. Repricer
still verifies exact captured job account/credential generation AFTER fetch and
before dispatch; this adapter is pool/crypto authority, NOT permission to bypass
job/user/domain guard or substitute a refreshed credential. Closing never uses it.

Handoff gives exact import/constructor/callable signature, injection example using
already-provisioned executor factory plus explicit keyring loader/role identity,
required ordinary encrypted-row SELECT and actual login check, final login SQL
evidence still pending. No executable default engine creation or operational role
grant. Existing root/Docker key isolation policy remains separate activation input.
Mark source IMPLEMENTED / UNVERIFIED, not full worker bootstrap/deployment ready.

One bounded source commit; no amend/rebase of communicated SHAs. Final test gate:
actual synthetic executor login observed ON resolver query connection, API/global
factory and wrong role/SET ROLE denied before key/ciphertext fetch, wrong org/account/
provider and changed generation, corrupt/expired/revoked fail closed, no fallback,
missing key/SQL/cleanup failures safe, wrapper no repr/pickle/copy/canary leakage,
all physical-root sabotage cases plus original paired resolver compatibility and
actual T2/T4 participant composition. Production/provider operations forbidden.
