# T4 exact0068 run binding storage codec

Consumes T1 READY4d95b24 physical contract without schema/domain activation.
New pure `app/reviews/run_binding_storage.py` composes the unchanged descriptor
codec from45e2ccb. Encoder emits only the five accepted binding columns; decoder
requires every column, distinguishes all-NULL legacy from missing/partial data,
compares normalized owner/provider/descriptor fields against strictly checked
canonical bytes and SHA256, and returns only a frozen descriptor or legacyNone.
Errors are fixed and never echo row values. Extra normal run columns are allowed.

Actual RED: new module absent, collection exits2. GREEN:45 new cases plus61 prior
codec cases=106PASS. Final adjacent command with lossless codec/storage payloads/
literal storage goldens:208PASS0.22s/exit0, no skips. Scrubbed environment, pytest
plugin autoload/cache disabled, all-network-denied sandbox; no PostgreSQL/browser
or other heavy resource slot used. Scoped Ruff exits0, diff-check exits0.
Independent read-only critic found no important issues and compared exact0068
columns/legacy/missing-vs-NULL/equality/error boundaries; critic did not run tests.

This is not repository/SQL/ACL/RLS/auth acceptance. Native SQL NUL prohibition is
separate from pure Unicode; no source-body values are altered. No run is backfilled,
reserved, published or read from a database, and no current public API uses this
adapter. Same-descriptor ABA detection, mixed historical admission, safe409,
completed rebind, actual guard lifetime and integration require the next consumer
slice. Existing schema-expanded binary rollback rules remain T1-owned.

Readiness references: plan `2026-09-09-t4-review-run-storage-codec.md` under plans;
T1 handoff at exact4d95b24 read via Git, migration211lines read completely. Before
integrating, read remaining exact T1-owned test/grant changes and dependency
chain0066/0067 mandatory fix, then use the separately admitted PG slot. No schema,
shared registration, flags, provider/production actions, credentials or push.
