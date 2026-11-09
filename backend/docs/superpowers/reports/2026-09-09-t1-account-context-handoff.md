# T1 transaction-local marketplace account context

2026-09-09. Task 1 only, branch `codex/arch-t1-platform`, baseline
`891974c95142697dc440f96b9f9ae12be0b55f79`. No schema0066, domain module,
consumer, router, task or publication guard changes.

```python
from app.infra.db import (
    MarketplaceAccountContextError,
    set_marketplace_account_context,
)

with Session(runtime_engine) as session, session.begin():
    guard = acquire_publication_guard(session, **trusted_expectations)
    set_marketplace_account_context(
        session, organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
    )
    # Trusted service now acquires its domain locks and performs scoped work.
    # It remains responsible for final authorization and exact context checks.
```

The helper requires an actual Engine-bound Session with a clean active root transaction,
PostgreSQL READ COMMITTED and strictly positive INT4 IDs (`type(value) is int`,
maximum2147483647). Missing/ended/inactive roots, pending new/dirty/deleted ORM
work, Session SAVEPOINTs and actual bound Connection SAVEPOINTs deny admission.
The Connection check runs on initial and repeated calls before context SQL.
This does not intercept arbitrary later Connection/DBAPI commands.

Only an Engine returned by the public `session.get_bind()` is admitted, as in
the normal `get_session_factory`. All Connection-bound Sessions are rejected
before any context SQL, including `control_fully` and `create_savepoint` modes.
Default external-root joins and explicit `rollback_only` can end a Session while
the external physical transaction retains both local settings. The helper does
not inspect private transaction maps or commit/roll back external caller work.
Callers currently composing through a Connection must instead use an Engine-bound
Session that owns its root lifetime; no external-join compatibility is promised.
Actual PostgreSQL negatives verify denial leaves external root, settings and
uncommitted synthetic control data unchanged until the external owner rolls back.

DBAPI autocommit also denies: PostgreSQL's reported isolation can remain READ
COMMITTED when SET LOCAL would have no durable transaction scope. The public
SQLAlchemy pooled connection's `dbapi_connection.autocommit` must be exactly
False. Tested with the existing psycopg driver; a driver that cannot expose
this boolean fails closed, rather than being assumed transactional.

Before either setting changes, one SELECT reads both `app.organization_id` and
`app.marketplace_account_id` under `session.no_autoflush`. Each setting must be
missing/empty or exactly the requested canonical decimal string. Different,
malformed, padded and out-of-range values deny without being overwritten. Both
values are set by parameterized `set_config(..., true)` in one statement.
An existing own marker must identify the same root and exact pair. It never
substitutes for reading the current settings, including on same-pair repeats.

`session.info['satorna_marketplace_account_context']` records the root and IDs.
One constant listener is installed per participating Session; root completion
removes only this helper's marker. No global Session hook or captured root
closure is installed. Commit/rollback, new-root selection, same-connection pool
reborrow and unrelated session.info preservation are tested.

`MarketplaceAccountContextError(RuntimeError).code` exposes only
`account_context_invalid` or `account_context_failed` through str/repr. SQLAlchemy
SQL failures are translated with suppressed exception chaining. Actual PostgreSQL
cast-failure and hostile-setting canaries verify that formatted error tracebacks
do not expose the synthetic value. Caller must roll back on every failure; the
helper neither commits nor promises catch-and-commit poisoning.

`set_tenant_context` is byte-for-byte unchanged: it sets only organization
context, keeps its existing marker behavior and retains that marker after root
completion. The new helper does not rewrite or clear that marker. Real PostgreSQL
composition with publication guard and an inactive-principal rejection control
prove compatibility, not authorization supplied by the context helper.

The existing publication guard authenticates no supplied principal by itself;
trusted caller provenance remains required. It checks live account metadata and
organization context, but does not validate the new account GUC. The consuming
service/schema must enforce the exact account context at its own final boundary.
No claim that this helper guards arbitrary subsequent raw SQL or grants permission,
locks account rows, proves organization/account ownership, or enables a writer.

Verification: initial focused account tests80PASS; the external-lifetime review
fix adds10 cases. Final combined account/publication group and scoped
Ruff/compile/diff results are recorded in the task report.
Tests use only this worktree's backend/.venv and fresh random databases/roles via
the tracked Orders disposable helper. Existing local Unix `/tmp` PostgreSQL is
the authorized maintenance fallback, not a newly initialized server. Runtime
role is NOSUPERUSER/NOBYPASSRLS/NOINHERIT. Task1 tests GUC/root behavior, not the
future0066 RLS policies. Cleanup checks exact resource absence after every run.
The approved separate Ruff environment was used for lint only. No dependencies,
network/provider/Redis/application data/secrets/flags/deploy were touched.

Self-review found and corrected the DBAPI AUTOCOMMIT hole using two real RED
canaries. Independent review found the external Connection lifetime mismatch;
two actual PostgreSQL RED witnesses preceded the Engine-only admission fix.
The requested preflight-critic skill path is absent; manual isolated
Critic Pass followed the global checklist. Independent review belongs to the
controller. Task2's approvals/attempts/audit persistence and production activation
remain separate work.
