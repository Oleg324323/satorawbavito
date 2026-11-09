# T1 atomic fetch authority handoff

Date: 2026-09-09. Task 1 only, against base
`d44c971148ab1e5d185adcc079c8d2ab9aced813`, branch `codex/arch-t1-platform`.
No consumer cutover, publication guard implementation, schema change or provider
activity is included.

## Exact interface

Import from `app.platform.integrations.credential_store`:

```python
resolve_marketplace_credential_for_fetch(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> ResolvedCredentialForFetch

CredentialFetchBinding(
    owner: MarketplaceAccountCredentialOwner,
    external_account_id: str,
    credential_ref: str | None,
    credential_identity: CredentialIdentity,
)
```

`CredentialFetchBinding` is frozen. `credential_identity` is the existing frozen
type with exact organization/account/provider/kind/schema/UUID/generation/expiry.
It carries no ciphertext, nonce or key-version inventory. A null reference is
captured as `None`, not a wildcard. No account-name/latest-organization lookup.

`ResolvedCredentialForFetch` has slots and read-only `.secret` and `.binding`
properties. `.secret` is the existing `DecryptedCredential`; only explicit
`.secret.reveal()` exposes its payload. Repr/str are
`<ResolvedCredentialForFetch redacted>`. Iteration, pickle and shallow/deep copy
raise `TypeError("credential_contract_invalid")`; generic JSON/dataclass/FastAPI
encoding and default Pydantic schema generation refuse the envelope.

## Snapshot and lifecycle semantics

After tenant context setup, one SQL statement selects two safe account columns
plus the encrypted credential row:

```sql
SELECT account.external_account_id, account.credential_ref, credential.*
FROM marketplace_accounts AS account
LEFT OUTER JOIN marketplace_account_credentials AS credential
  ON credential.organization_id = account.organization_id
 AND credential.marketplace_account_id = account.marketplace_account_id
 AND credential.provider = account.marketplace
 AND credential.credential_kind = :kind
 AND credential.revoked_at IS NULL
WHERE account.organization_id = :organization_id
  AND account.marketplace_account_id = :marketplace_account_id
  AND account.marketplace = :provider
  AND account.status = 'connected'
```

This is a schematic rendering of the tested SQLAlchemy statement, not an extra
query. PostgreSQL tenant setup is a separate `set_config` statement and supplies
no binding evidence. The outer join preserves the existing safe distinction
between account-not-found and credential-missing without another snapshot.
The selected credential's identity supplies both AEAD authentication and binding;
no subsequent metadata query relabels the secret. The short session closes before
return, uses no publication locks and commits no caller work.

Missing/wrong-tenant/wrong-account/disconnected accounts use
`credential_account_not_found`; missing/revoked rows use `credential_missing`;
expired authority uses `credential_expired`. Contract and persistence errors
remain safe `CredentialStoreError` codes; crypto failures retain existing safe
`CredentialCryptoError` codes. The old
`resolve_marketplace_credential(owner, kind) -> DecryptedCredential` behavior is
unchanged, including its existing disconnected-account behavior.

## WB writer compatibility

Only `_binding_candidate(lock=True)` changes. It locates token ownership with a
metadata-only projection, locks that exact user's ID/org/active projection
`FOR SHARE`, validates the refreshed user, locks the exact connected account
`FOR UPDATE` with ORM refresh, then locks the token using
`FOR UPDATE OF lk_user_wb_tokens`. The token query requires the located owner
again, so reassignment after the locator fails closed. No user password or session
refresh hash columns are selected by this path.

User organization/activity and account status are refreshed after waits. Existing
seller probe placement outside transactions, verified seller UUID handling,
exact-token comparison, current external identity/reference checks, audit and
safe error behavior remain unchanged. Other WB writers/resolvers are not refactored.

## Verification evidence

- RED: missing paired API, `1 failed in 0.43s`, exit 1, before implementation.
- RED: actual WB binder/publisher lock race, PostgreSQL `DeadlockDetected`,
  `1 failed in 3.97s`, exit 1, before lock repair. Events pause the actual binder
  after account acquisition; an observer verifies the exact publisher backend
  waits for that binder using `pg_blocking_pids`, then releases the token query.
- GREEN: final focused plus crypto/store/WB binding run: `94 passed in 3.78s`,
  exit 0. The exact command, commit and all disposable resource names are in the local task report
  `.superpowers/sdd/2026-09-09-publication-guard/task-1-report.md`.
- Real PostgreSQL tests run the actual migration through `20260908_0061` in a
  fresh randomly named database and fresh restricted role. Three statement-result
  races commit credential replacement/revocation/re-encryption plus account
  identity/reference mutation before result consumption; captured evidence stays
  old, and subsequent reads see the change. Actual WB races cover both the repaired
  cycle and changes to active user, user org, token owner/org and account status
  while binding waits.
- New test files pass Ruff. Four-file Ruff retains five baseline findings:
  four `UP017` uses in old UTC helpers and one `BLE001` in the existing seller
  adapter. Exact-base lint comparison confirmed them; unrelated behavior is
  preserved. Compileall and diff checks pass.

All test/app execution uses this worktree's `backend/.venv`. The only borrowed
runtime is the controller-approved Ruff-only executable; no installs. Execution
uses scrubbed environment, null pass/service/netrc files and an OS sandbox denying
IP networking and secret-file reads, allowing only the authorized local PostgreSQL
Unix socket. Synthetic `.env` read denial and TCP/UDP denial were verified.

## Limits

This is fetch-time evidence, not authorization to publish. The future caller-owned
publication guard must freshly compare it and hold its own metadata locks through
publication. This slice does not authenticate a principal, enforce permissions,
implement Task 2 or alter consumers. Binding equality is current-state evidence;
there is no historical A→B→A epoch. Fetch expiry uses the existing UTC helper;
the future publication guard must use its specified database clock checks.

The existing UUID-only re-encryption maintenance function requires a privileged
runner; its synthetic race writer used only this test's fresh database owner.
Fetches and the actual binder ran under the fresh non-superuser runtime role.
Full application suite, production activation, real provider behavior and other
writers' lock-order compliance are outside this bounded verification.

An isolated critic pass checked the spec, source diff, failure evidence, actual
SQL/races, serialization and cleanup. The requested local `preflight-critic` skill
file was unavailable; the controller confirmed self-review fallback. Independent
controller review is the next acceptance step.
