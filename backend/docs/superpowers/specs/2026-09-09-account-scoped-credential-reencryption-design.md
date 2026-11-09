# Account-scoped credential re-encryption prerequisite

2026-09-09. T1 local design under the user's autonomous Stage3 mandate. No working keys, real credentials, backfill, CLI activation, schema change or operational rotation is authorized here.

## Evidence and decision

At c4e3e373f97be19171f80ebdad2ce919dd7c84dc, `credential_store.reencrypt_credential` opens its own Session and first locks a credential by UUID, without setting tenant context or locking its canonical account. The source has no application callers. Four test calls exist: two in the SQLite store test, one in `test_marketplace_credential_fetch_postgres.py`, one in `test_publication_guard_postgres.py`. Both PostgreSQL cases explicitly switch just this writer to a disposable DB-owner connection. Their GREEN proves their stated snapshot/guard cases, not runtime-role re-encryption.

Three bounded approaches were considered:

1. Explicit account owner in the existing store entry point, account-first locking and fully scoped SELECT/CAS. Chosen: retains the single credential-store boundary and removes the owner-only testing exception.
2. Keep UUID-only lookup under a privileged migration connection. Rejected for this boundary: broad visibility is unnecessary, tenant isolation is not enforced by the call, and credential-first ordering differs from publication/replace/revoke.
3. Add a second privileged rekey repository/service. Deferred: duplicates credential crypto/write handling without resolving any current consumer requirement.

The existing three positional parameters remain in the same order. Add keyword-only `account_identity: MarketplaceAccountCredentialOwner | None = None`; absence is a typed `credential_contract_invalid` before key loading, session creation or SQL. This intentional fail-closed tightening preserves a bounded error for an old three-argument invocation, rather than silently inferring owner from an unrestricted UUID lookup. All four local test calls supply owner. No router/task/provider consumer is changed.

## Contract

```python
reencrypt_credential(
    credential_id: UUID,
    expected_generation: int,
    target_key_version: int,
    *,
    account_identity: MarketplaceAccountCredentialOwner | None = None,
) -> CredentialMetadata
```

Validate the owner instance, canonical positive INTEGER org/account IDs, provider allowlist, UUID and positive non-boolean integer generation/key version before I/O. Target key version must fit the existing INTEGER column (1..2147483647); the next generation must fit the existing BIGINT column. Malformed or overflow input yields the existing safe contract code. These are physical schema bounds, not a key policy. Do not widen other store entry points or globally change their validation in this task.

The store still owns its fresh Session and physical transaction, keyring loading, decrypt/encrypt/decrypt-verify, write and safe audit. Context is not authentication: only a trusted, explicitly authorized administrative caller may invoke it. This task introduces no public rekey route, worker principal or import-role privileges.

Within the fresh transaction: set tenant context, validate and lock the exact canonical account first, then SELECT the exact org/account/provider/credential UUID active row FOR UPDATE. All UPDATE/CAS predicates repeat org/account/provider/UUID/expected generation/non-revoked state. Retain account lock through audit and commit. On PostgreSQL require actual READ COMMITTED and non-autocommit before scoped work; no owner connection substitution. Existing SQLite tests may remain limited contract tests, never RLS/concurrency evidence.

Missing/wrong-owner credential fails without touching another row. Wrong provider fails against canonical account. Revoked or expired rows do not re-encrypt; corruption, missing old/target key, unavailable database or decrypt-verify mismatch fail closed, with no plaintext fallback or partial audit. Error text/repr contains only existing fixed safe codes, never original exception text, key paths, inventory or payload fragments. Recheck expiry using the trusted clock after obtaining locks, not a stale pre-wait timestamp.

Each successful re-encryption retains credential UUID, owner/provider/kind/payload schema and expiry, increments generation once, uses a fresh random nonce even when targeting the current key, preserves exact decoded payload, and writes exactly one safe `reencrypt` audit. Decrypt the new envelope and compare exact payload equality before CAS/commit. A stale expected generation conflicts without writes. Audit/verification/DB failures roll back the envelope and audit together; an injected pre-COMMIT failure does not prove recovery from an unknown network COMMIT.

## Verification and ownership

Modify only T1 credential_store, its three existing test files, one new focused PostgreSQL test file and a handoff. No crypto implementation, model, migration, grants, config, dependency, legacy reader, foreign terminal code or production changes.

TDD must show the existing UUID-only runtime-role call fails to rotate a valid own credential, while ordinary put/resolve succeeds under the same nonowner NOSUPERUSER/NOBYPASSRLS role. Add missing-owner no-I/O RED for the new safe boundary before implementation. Then prove actual RLS and same-org other-account/wrong-org/wrong-provider denial, real owner account lock before credential lock, generation races with observed blocking, both existing publication lock winners and atomic fetch snapshot behavior without their DB-owner substitutions.

Use only the existing authorized Unix-disposable allocator, fresh exact DB/runtime roles, own backend/.venv and scrubbed known-secret/IP-denying sandbox. No native ENOMEM workaround, working service change, skips/xfails or baseline expansion. Synthetic old/new keys stay in test memory; do not mount/load any real keyring. Preserve original tests and their assertion strength; SQLite remains explicitly non-RLS evidence.

The separate future backfill writer must be insert-if-absent across ANY encrypted history and must not call the replacement writer. That design/CLI, exact role privileges, Avito access-token lineage, mixed-version policy and real key lifecycle remain outside this prerequisite.

## Critical pass

Checked: UUID-only compatibility must deny before I/O; owner parameter does not imply user authorization; wrong same-org account cannot exploit org-only RLS; SQL predicates and retained account-first lock both required; expiry evaluated after wait; SQLite success cannot substitute runtime-role proof; existing owner-only test exceptions must be removed; no second writer or operational key action introduced. Shared API errors remain safe codes. This document is not implementation or test evidence.
