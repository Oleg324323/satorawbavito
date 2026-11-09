# Credential maintenance registrar — bounded handoff

Implemented against ROOT `0802ac9`, using the existing 0079 helpers and the frozen T1/ROOT API:

```python
CredentialMaintenanceRegistrar(
    *, session_factory, verifier, registrar_role_oid: int, registrar_role_name: str
).register(
    authorization: MaintenanceAuthorization,
    *, previous_authorization_id: UUID | None
) -> UUID
```

The verifier is REQUIRED, callable, independently trusted, and returns the exact existing `DecryptedCredential`: WB `{token}` or Avito `{clientId, clientSecret}`. No default verifier, operational proof procedure, keyring loader, CLI, configuration, provider, or registration caller is installed. Missing verifier rejects construction without I/O. Opaque metadata UUIDs never manufacture verified mapping proof.

## Transaction and authority boundary

- Validate and reconstruct immutable typed metadata before the verifier or session factory. Explicit previous UUID only; no renewal/default predecessor discovery. Reject already-revoked registration input.
- Invoke verifier before opening locks; retain only exact typed UTF-8 field bytes for comparison. No normalization or digest substitution.
- Require a fresh, clean, Engine-bound PostgreSQL Session and READ COMMITTED/non-autocommit physical root. Reject a borrowed active/dirty root without rollback/close of the caller's root.
- Authenticate actual `session_user` OID/name against independently configured registrar identity, with `current_user=session_user`, LOGIN, restricted role flags, no membership edges, and no relation/schema/function/database ownership. Registrar identity is NOT the metadata recipient runner.
- Set organization/account GUCs from verified metadata; helper ownership does not bypass existing forced tenant RLS. Recheck GUCs as context only, never identity/proof.
- Call `credential_maintenance.lock_registration(ROW(...22 metadata fields...)::credential_maintenance.authorizations)`. Existing SQL owns exact user → account → source → authorization ordering, source-locator checks and lock waits. Require exactly one result row and exact WB/Avito field/null shape; compare UTF-8 bytes in memory.
- Call existing `register_authorization(composite, previousUUID)` in the same root. Validate returned UUID exactly. A final before-commit listener repeats identity/context/root checks, source comparison and exact immutable registration replay, including SQL helper recipient and fresh DB-clock checks. Finish with no-SQL state/listener checks.
- Return authorization UUID only after `Session.commit()` and owned cleanup succeed. Unknown commit/cleanup outcome is `maintenance_commit_unknown`; no retry or blind renewal occurs. SQL helper's exact fixed binding/authorization/conflict codes may be preserved. Other private verifier/source/driver failures become closed unavailable errors, raised outside handlers with no raw exception context.

Plaintext returned by the lock helper is never passed as a SQL argument or put in object representations/logs. Only the fixed 22-field metadata composite, previous UUID, trusted registrar OID/name, and nonsecret scope values enter SQL parameters. Ordinary Python in-memory comparison is not a secure-memory erasure claim.

## Verification

Initial RED: new registrar absent, 15 focused cases failed. Initial implementation GREEN: 15 passed. Self-review added RLS-context and corrupted-metadata cases, RED 2 failed / 15 passed, then corrected both. Final focused command:

`/tmp/satorna-backend311-20260909/bin/python -m pytest tests/test_credential_maintenance_registrar.py -q`

Result: **22 passed in 0.35s**. Tests cover source mismatch and Unicode normalization differences, exact WB/Avito shape, explicit renewal input, no plaintext parameters, invalid verifier/metadata, existing-root refusal, autocommit/isolation refusal, final source/role changes, commit and cleanup uncertainty. The double subclasses SQLAlchemy Session and exercises the actual shared fresh-root checks; its SQL/physical-commit behavior is synthetic and does not prove PostgreSQL ACLs, lock races or commit durability.

Backend-config Ruff passed both Python files. `git diff --check` passed. No PostgreSQL run, provisioning artifact execution, operational role creation, real source/key, provider call, CLI, migration, store, or contract edit occurred.

## Remaining gates and limitations

ROOT/T1 must review the new wrapper and allocate actual disposable-role PostgreSQL acceptance before runtime claims. Required gates include installed narrow helper grants/forced RLS, trusted direct registrar identity and SET ROLE rejection, helper result cardinality, source mutation/renewal contention with observed locks, exact retry/target-history conflicts, and real physical commit/rollback. Existing provisioning remains inert; no operational verifier or approved production proof process exists. Registrar configuration plus synthetic success must never be presented as operational verification.

The preflight-critic skill file was unavailable earlier in this task; a separate manual critic pass checked source/context/physical-root boundaries and found the RLS-context omission corrected above. ROOT owns the shared project-memory update to avoid parallel edits.
