"""Review Facts SQL contract, exercised only in exact disposable PostgreSQL DBs."""
# Separate contexts expose commit-time constraints.
# ruff: noqa: SIM117

import ast
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.reviews.canonical_contract import normalize_avito_review
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import (
    assert_narrow_privileges,
    runtime_script,
)

cluster = candidate.cluster
TABLES = ('review_sync_runs_v2', 'review_facts', 'review_observations', 'review_sync_run_items')
RUN_COLUMNS = ('sync_run_id', 'organization_id', 'marketplace_account_id', 'marketplace',
               'source_run_id', 'request_checksum', 'status', 'completeness', 'started_at',
               'completed_at', 'observed_count', 'manifest_checksum', 'coverage', 'error_code')
OWNER = {
    "organization_id": 91001,
    "marketplace_account_id": 91101,
    "marketplace": "avito",
}


def test_historical_roundtrip_does_not_invoke_latest_runtime_script():
    tree = ast.parse((candidate.ROOT / "tests/test_review_facts_schema.py").read_text())
    historical = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_expand_acl_intersection_and_empty_roundtrip_preserve_old_rows"
    )
    calls = {
        node.func.id
        for node in ast.walk(historical)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "runtime_script" not in calls

    latest_fixture = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "db"
    )
    latest_calls = {
        node.func.id
        for node in ast.walk(latest_fixture)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    migration_targets = [
        node.args[2].value
        for node in ast.walk(latest_fixture)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "migrate"
    ]
    assert migration_targets == ["head"]
    assert "runtime_script" in latest_calls


def insert(c, table, values, returning=None, override=False):
    columns = ','.join(values)
    binds = ','.join(':' + key for key in values)
    sql = f'INSERT INTO {table} ({columns}) '
    if override:
        sql += 'OVERRIDING SYSTEM VALUE '
    sql += f'VALUES ({binds})'
    if returning:
        sql += f' RETURNING {returning}'
    return c.execute(text(sql), values)


def run(c, **changes):
    values = dict(OWNER, sync_run_id=uuid4(), source_run_id=uuid4().hex,
                  request_checksum='a' * 64, status='running', completeness='partial',
                  started_at='2026-09-09T00:00:00Z', coverage='{}')
    values.update(changes)
    return insert(c, TABLES[0], values, 'sync_run_id,run_sequence').one()


def identity(c, **changes):
    values = dict(OWNER, review_id=uuid4(), external_review_id=uuid4().hex,
                  first_observed_at='2026-09-09T00:00:00Z', last_observed_at='2026-09-09T00:00:00Z')
    values.update(changes)
    return insert(c, TABLES[1], values, 'review_id').scalar_one()


def observation(c, rid, run_id, **changes):
    values = dict(OWNER, observation_id=uuid4(), review_id=rid, source_run_id=run_id,
                  revision=1, source_created_at='2026-09-09T00:00:00Z',
                  observed_at='2026-09-09T00:00:00Z', answered=False, can_answer=None,
                  text=None, source_schema_version='synthetic-v1', normalization_version='v1',
                  content_checksum='a' * 64)
    values.update(changes)
    return insert(c, TABLES[2], values, 'observation_id').scalar_one()


def item(c, rid, oid, run_id, **changes):
    values = dict(OWNER, review_id=rid, observation_id=oid, sync_run_id=run_id,
                  content_checksum='a' * 64, ordinal=0, observed_at='2026-09-09T00:00:00Z')
    values.update(changes)
    insert(c, TABLES[3], values)


def advance(c, rid, oid, expected_version=0, **changes):
    values = {"current_observation_id": oid, "version": expected_version + 1}
    values.update(changes)
    return c.execute(text('UPDATE review_facts SET ' + ','.join(f'{key}=:{key}' for key in values)
                          + ' WHERE review_id=:rid AND version=:expected'),
                     dict(values, rid=rid, expected=expected_version)).rowcount


def complete_fact(c, **changes):
    run_id, sequence = run(c)
    rid = identity(c, **changes)
    oid = observation(c, rid, run_id)
    assert advance(c, rid, oid, last_source_run_id=run_id, last_source_run_sequence=sequence) == 1
    item(c, rid, oid, run_id)
    return rid, oid, run_id


def seed(c):
    c.exec_driver_sql("INSERT INTO lk_organizations(organization_id,slug,name) VALUES (91001,'review-one','Synthetic'),(91002,'review-two','Synthetic')")
    c.exec_driver_sql("""INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status)
        VALUES (91101,91001,'avito','synthetic-a','connected'),(91102,91001,'avito','synthetic-b','connected'),
               (91103,91001,'wb','synthetic-c','connected'),(91201,91002,'wb','synthetic-d','connected')""")


@pytest.fixture(scope='module')
def db(cluster):
    role = 'review_runtime_' + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, 'upgrade', 'head')
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
            result = runtime_script(owner, role)
            assert result.returncode == 0, result.stderr
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def test_registered_revision_and_forced_rls(db):
    config = Config(str(candidate.ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(candidate.ROOT / 'alembic'))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert '20260909_0063' in {r.revision for r in scripts.iterate_revisions(heads[0], 'base')}
    assert scripts.get_revision('20260909_0063').down_revision == '20260909_0062'
    with db[0].connect() as c:
        assert c.exec_driver_sql('SELECT inet_server_addr() IS NULL').scalar_one()
        print('Review PostgreSQL version:', c.exec_driver_sql('SHOW server_version').scalar_one(), '; transport=local Unix socket')
        assert c.execute(text('SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY(:tables) ORDER BY relname'),
                         {'tables': list(TABLES)}).all() == [(table, True, True) for table in sorted(TABLES)]


def test_final_row_commit_and_exact_nullable_evidence(db):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, _ = complete_fact(c, external_review_id='000-' + uuid4().hex)
    with db[1].begin() as c:
        candidate.scope(c)
        assert c.execute(text('SELECT version,current_observation_id,external_review_id FROM review_facts WHERE review_id=:rid'), {'rid': rid}).one()[:2] == (1, oid)
        assert c.execute(text('SELECT text,can_answer FROM review_observations WHERE observation_id=:oid'), {'oid': oid}).one() == (None, None)
        assert c.execute(text('SELECT external_review_id FROM review_facts WHERE review_id=:rid'), {'rid': rid}).scalar_one().startswith('000-')
    with pytest.raises(DBAPIError), db[1].begin() as c:
        candidate.scope(c)
        identity(c)


@pytest.mark.parametrize('org', [None, 91002])
def test_no_or_wrong_org_crud(db, org):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, run_id = complete_fact(c)
    for table in TABLES:
        with db[1].begin() as c:
            if org:
                candidate.scope(c, org)
            assert c.exec_driver_sql(f'SELECT count(*) FROM {table}').scalar_one() == 0
            if table in TABLES[:2]:
                assert c.exec_driver_sql(f'UPDATE {table} SET organization_id=organization_id').rowcount == 0
        with pytest.raises(DBAPIError), db[1].begin() as c:
            if org:
                candidate.scope(c, org)
            c.exec_driver_sql(f'DELETE FROM {table}')
    for create in (lambda c: run(c), lambda c: identity(c),
                   lambda c: observation(c, rid, run_id, revision=9),
                   lambda c: item(c, rid, oid, run_id)):
        with pytest.raises(DBAPIError), db[1].begin() as c:
            if org:
                candidate.scope(c, org)
            create(c)


def assert_acl(c, role):
    for table in TABLES:
        allowed = {'SELECT', 'UPDATE'} if table == TABLES[0] else ({'SELECT', 'INSERT', 'UPDATE'} if table == TABLES[1] else {'SELECT', 'INSERT'})
        for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'):
            assert c.execute(text('SELECT has_table_privilege(:role,:table,:p)'), {'role': role, 'table': table, 'p': privilege}).scalar_one() == (privilege in allowed)
            assert not c.execute(text('SELECT has_table_privilege(:role,:table,:p)'), {'role': role, 'table': table, 'p': privilege + ' WITH GRANT OPTION'}).scalar_one()
    for column in (*RUN_COLUMNS, 'run_sequence'):
        assert c.execute(text("SELECT has_column_privilege(:role,'review_sync_runs_v2',:column,'INSERT')"), {'role': role, 'column': column}).scalar_one() == (column != 'run_sequence')
    seq = c.exec_driver_sql("SELECT pg_get_serial_sequence('review_sync_runs_v2','run_sequence')").scalar_one()
    for privilege in ('USAGE','SELECT','UPDATE'):
        assert c.execute(text('SELECT has_sequence_privilege(:role,:seq,:p)'), {'role': role, 'seq': seq, 'p': privilege}).scalar_one() == (privilege == 'USAGE')


def ordering_denials(runtime):
    for override in (False, True):
        with pytest.raises(DBAPIError), runtime.begin() as c:
            candidate.scope(c)
            values = dict(OWNER, sync_run_id=uuid4(), source_run_id=uuid4().hex, run_sequence=999999,
                          request_checksum='a'*64, status='running', completeness='partial',
                          started_at='2026-09-09T00:00:00Z', coverage='{}')
            insert(c, TABLES[0], values, override=override)
    with runtime.begin() as c:
        candidate.scope(c)
        run_id, sequence = run(c)
        assert sequence > 0
    for sql in ("UPDATE review_sync_runs_v2 SET run_sequence=run_sequence+1 WHERE sync_run_id=:id",
                "SELECT setval(pg_get_serial_sequence('review_sync_runs_v2','run_sequence'),1)"):
        with pytest.raises(DBAPIError), runtime.begin() as c:
            candidate.scope(c)
            c.execute(text(sql), {'id': run_id})


def test_actual_runtime_acl_and_server_order(db):
    with db[0].connect() as c:
        assert_acl(c, db[1].url.username)
        assert_narrow_privileges(c, db[1].url.username)
        assert c.execute(text('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=:role'), {'role': db[1].url.username}).one() == (False,False,False,False)
        assert c.execute(text('SELECT count(*) FROM pg_class WHERE relname=ANY(:tables) AND relowner=(SELECT oid FROM pg_roles WHERE rolname=:role)'), {'tables': list(TABLES), 'role': db[1].url.username}).scalar_one() == 0
    ordering_denials(db[1])
    for table in TABLES:
        for statement in (f'DELETE FROM {table}', f'TRUNCATE {table}', f'ALTER TABLE {table} ADD COLUMN forbidden int'):
            with pytest.raises(DBAPIError), db[1].begin() as c:
                candidate.scope(c)
                c.exec_driver_sql(statement)
    with pytest.raises(DBAPIError), db[1].begin() as c:
        c.exec_driver_sql('CREATE TABLE public.review_forbidden(id int)')


@pytest.mark.parametrize('table', TABLES)
def test_actual_script_requires_every_force_rls_flag(db, table):
    owner, runtime = db
    try:
        with owner.begin() as c:
            c.exec_driver_sql(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY')
        result = runtime_script(owner, runtime.url.username)
        assert result.returncode != 0 and 'canonical tables without forced RLS' in result.stderr
        with owner.connect() as c:
            assert_acl(c, runtime.url.username)
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')


@pytest.mark.parametrize(
    "change",
    [
        {"marketplace_account_id": 91102},
        {"marketplace": "wb"},
        {"organization_id": 91002},
    ],
)
def test_scoped_internal_references_reject_substitution(db, change):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, run_id = complete_fact(c)
    for create in (lambda c: observation(c, rid, run_id, revision=2, **change),
                   lambda c: item(c, rid, oid, run_id, ordinal=1, **change)):
        with pytest.raises(DBAPIError), db[1].begin() as c:
            candidate.scope(c)
            create(c)
    with pytest.raises(DBAPIError), db[1].begin() as c:
        candidate.scope(c)
        run(c, marketplace_account_id=91101, marketplace='wb')


@pytest.mark.parametrize('kind,change', [
    ('run', {'request_checksum':'A'*64}), ('run', {'status':'unknown'}),
    ('run', {'completeness':'complete'}), ('run', {'observed_count':-1}),
    ('run', {'coverage':'[]'}), ('run', {'source_run_id':''}),
    ('run', {'status':'complete','completed_at':'2026-09-09T00:00:01Z'}),
    ('run', {'completed_at':'2026-09-09T00:00:01Z'}),
    ('observation', {'rating':0}), ('observation', {'rating':6}),
    ('observation', {'revision':0}), ('observation', {'content_checksum':'z'*64}),
    ('observation', {'source_schema_version':''}), ('observation', {'normalization_version':''}),
    ('fact', {'version':-1}), ('fact', {'last_source_run_id':uuid4()}),
    ('fact', {'source_order_state':'ambiguous'}), ('fact', {'external_review_id':''}),
    ('item', {'ordinal':-1}), ('item', {'content_checksum':'b'*64}),
])
def test_scalar_and_paired_constraints(db, kind, change):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, run_id = complete_fact(c)
    with pytest.raises(DBAPIError), db[1].begin() as c:
        candidate.scope(c)
        if kind == 'run':
            run(c, **change)
        elif kind == 'observation':
            observation(c, rid, run_id, **dict({'revision': 2}, **change))
        elif kind == 'fact':
            identity(c, **change)
        else:
            new_run, _ = run(c)
            item(c, rid, oid, new_run, **change)


def test_duplicate_keys_and_three_checksum_revisions(db):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, run_id = complete_fact(c)
        for revision, checksum in ((2,'b'*64),(3,'a'*64)):
            observation(c, rid, run_id, revision=revision, content_checksum=checksum)
        assert c.execute(text('SELECT revision,content_checksum FROM review_observations WHERE review_id=:id ORDER BY revision'), {'id': rid}).all() == [(1,'a'*64),(2,'b'*64),(3,'a'*64)]
        source_key = c.execute(text('SELECT source_run_id FROM review_sync_runs_v2 WHERE sync_run_id=:id'), {'id': run_id}).scalar_one()
        external = c.execute(text('SELECT external_review_id FROM review_facts WHERE review_id=:id'), {'id': rid}).scalar_one()
    for create in (lambda c: run(c, source_run_id=source_key), lambda c: identity(c, external_review_id=external),
                   lambda c: observation(c, rid, run_id), lambda c: item(c, rid, oid, run_id)):
        with pytest.raises(DBAPIError), db[1].begin() as c:
            candidate.scope(c)
            create(c)
    with pytest.raises(DBAPIError), db[1].begin() as c:
        candidate.scope(c)
        other = identity(c)
        other_obs = observation(c, other, run_id)
        advance(c, other, other_obs)
        item(c, other, other_obs, run_id, ordinal=0)


def test_fact_pointer_and_watermark_are_scoped_and_cas_guarded(db):
    with db[1].begin() as c:
        candidate.scope(c)
        rid, oid, _ = complete_fact(c)
        _, foreign_oid, _ = complete_fact(c)
        wrong_run, wrong_seq = run(c, marketplace_account_id=91102)
    for change in ({'current_observation_id':foreign_oid}, {'ambiguous_observation_id':foreign_oid,'source_order_state':'ambiguous'},
                   {'last_source_run_id':wrong_run,'last_source_run_sequence':wrong_seq},
                   {'last_source_run_sequence':999999}, {'external_review_id':'changed'},
                   {'marketplace_account_id':91102}, {'first_observed_at':'2026-09-08T00:00:00Z'},
                   {'version':3}, {'version':1}):
        with pytest.raises(DBAPIError), db[1].begin() as c:
            candidate.scope(c)
            advance(c, rid, oid, 1, **change)
    barrier = Barrier(2)
    def update():
        with db[1].begin() as c:
            candidate.scope(c)
            barrier.wait(timeout=10)
            return advance(c, rid, oid, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [0,1]


def test_run_and_evidence_guards_even_with_broad_privileges(db):
    owner, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        _, _, run_id = complete_fact(c)
    for column, value in [('sync_run_id', str(uuid4())), ('source_run_id','changed'), ('request_checksum','b'*64),
                           ('started_at','2026-09-08T00:00:00Z'), ('marketplace_account_id',91102)]:
        with pytest.raises(DBAPIError), runtime.begin() as c:
            candidate.scope(c)
            c.execute(text(f'UPDATE review_sync_runs_v2 SET {column}=:value WHERE sync_run_id=:id'), {'value': value,'id':run_id})
    with runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE review_sync_runs_v2 SET status='complete',completeness='complete',completed_at=now(),manifest_checksum=:sum,observed_count=1 WHERE sync_run_id=:id"), {'sum':'a'*64,'id':run_id})
    with pytest.raises(DBAPIError), runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE review_sync_runs_v2 SET observed_count=2 WHERE sync_run_id=:id"), {'id':run_id})
    # Savepoint-scoped broader grants prove triggers independently; grants roll back.
    acl_sql = text("""SELECT c.oid,c.relacl::text,
        ARRAY(SELECT has_table_privilege(:role,c.oid,p.privilege)
              FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS p(privilege))
        FROM pg_class c WHERE c.relnamespace='public'::regnamespace
          AND c.relkind IN ('r','p') ORDER BY c.oid""")
    with owner.connect() as c:
        tx = c.begin()
        try:
            before_acl = c.execute(acl_sql, {'role': runtime.url.username}).all()
            c.exec_driver_sql(f'GRANT UPDATE,DELETE,TRUNCATE ON review_sync_runs_v2,review_facts,review_observations,review_sync_run_items TO {runtime.url.username}')
            # Only this owned disposable schema/root: include future FK CASCADE children.
            c.exec_driver_sql(f'GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO {runtime.url.username}')
            c.exec_driver_sql(f'SET LOCAL ROLE {runtime.url.username}')
            candidate.scope(c)
            for table in TABLES[2:]:
                for sql in (f'UPDATE {table} SET content_checksum=content_checksum', f'DELETE FROM {table}', f'TRUNCATE {table} CASCADE'):
                    with pytest.raises(DBAPIError) as error, c.begin_nested():
                        c.exec_driver_sql(sql)
                    assert error.value.orig.diag.message_primary == 'Review evidence is immutable'
        finally:
            tx.rollback()
        assert c.execute(acl_sql, {'role': runtime.url.username}).all() == before_acl


def test_expand_acl_intersection_and_empty_roundtrip_preserve_old_rows(cluster):
    broad, reader = 'review_broad_' + uuid4().hex, 'review_reader_' + uuid4().hex
    with candidate.disposable_database(cluster, (broad,reader)) as database:
        assert candidate.migrate(database.url,'upgrade','20260909_0062').returncode == 0
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=broad), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
                candidate.scope(c)
                order_id = candidate.order(c)
                c.exec_driver_sql("INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) VALUES(91301,91001,'synthetic')")
                c.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO {broad},{reader}')
                c.exec_driver_sql(f'GRANT SELECT,UPDATE ON marketplace_accounts TO {broad}')
                c.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {broad} WITH GRANT OPTION')
                c.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO {broad} WITH GRANT OPTION')
                c.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {reader} WITH GRANT OPTION')
                c.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES GRANT SELECT ON SEQUENCES TO {reader}')
                before = c.exec_driver_sql('SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid').all()
                old_acl = c.exec_driver_sql("SELECT relname,relacl::text FROM pg_class WHERE relname IN ('marketplace_orders','catalog_skus') ORDER BY relname").all()
                rows = c.exec_driver_sql('SELECT * FROM marketplace_orders').all()
            for action, target in [('upgrade','20260909_0063'), ('downgrade','20260909_0062'), ('upgrade','20260909_0063')]:
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                assert_acl(c,broad)
                assert c.exec_driver_sql('SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid').all() == before
                assert c.exec_driver_sql("SELECT relname,relacl::text FROM pg_class WHERE relname IN ('marketplace_orders','catalog_skus') ORDER BY relname").all() == old_acl
                assert c.exec_driver_sql('SELECT * FROM marketplace_orders').all() == rows
                assert c.exec_driver_sql('SELECT catalog_sku_id FROM catalog_skus').scalar_one() == 91301
                for table in TABLES:
                    assert c.execute(text("SELECT has_table_privilege(:r,:t,'SELECT')"), {'r':reader,'t':table}).scalar_one()
                    for privilege in ('INSERT','UPDATE','DELETE','TRUNCATE','SELECT WITH GRANT OPTION'):
                        assert not c.execute(text('SELECT has_table_privilege(:r,:t,:p)'), {'r':reader,'t':table,'p':privilege}).scalar_one()
                assert not c.execute(text("SELECT has_any_column_privilege(:r,'review_sync_runs_v2','INSERT')"), {'r':reader}).scalar_one()
            ordering_denials(runtime)
            with runtime.begin() as c:
                candidate.scope(c)
                complete_fact(c)
            # A non-bypass role with no tenant context sees zero rows, but must
            # never be able to mistake that for an empty destructive downgrade.
            with owner.connect() as c:
                tx = c.begin()
                try:
                    c.exec_driver_sql(f'GRANT ALL ON review_sync_runs_v2,review_facts,review_observations,review_sync_run_items TO {reader}')
                    c.exec_driver_sql(f'SET LOCAL ROLE {reader}')
                    assert c.exec_driver_sql('SELECT count(*) FROM review_facts').scalar_one() == 0
                    config = Config(str(candidate.ROOT / 'alembic.ini'))
                    config.set_main_option('script_location', str(candidate.ROOT / 'alembic'))
                    module = ScriptDirectory.from_config(config).get_revision('20260909_0063').module
                    with pytest.raises(DBAPIError, match='row-level security'), c.begin_nested():
                        with Operations.context(MigrationContext.configure(c)):
                            module.downgrade()
                finally:
                    tx.rollback()
            result = candidate.migrate(database.url,'downgrade','20260909_0062')
            assert result.returncode != 0 and 'Review Facts downgrade blocked' in result.stderr
            with owner.connect() as c:
                assert c.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one() == '20260909_0063'
                assert c.exec_driver_sql('SELECT count(*) FROM review_facts').scalar_one() == 1
                assert c.exec_driver_sql('SELECT order_id FROM marketplace_orders').scalar_one() == order_id
        finally:
            runtime.dispose()
            owner.dispose()


def test_exact_external_identity_is_independent_per_account_provider_and_org(db):
    external = '000-' + uuid4().hex
    ids = []
    for org, account, provider in [(91001,91101,'avito'),(91001,91102,'avito'),
                                   (91001,91103,'wb'),(91002,91201,'wb')]:
        scope = {
            "organization_id": org,
            "marketplace_account_id": account,
            "marketplace": provider,
        }
        with db[1].begin() as c:
            candidate.scope(c,org)
            run_id, _ = run(c, **scope)
            rid = identity(c,external_review_id=external,**scope)
            oid = observation(c,rid,run_id,external_product_id='000001',**scope)
            assert advance(c,rid,oid) == 1
            item(c,rid,oid,run_id,**scope)
            ids.append(rid)
    assert len(set(ids)) == 4


def test_sensitive_domain_text_is_preserved_and_guard_error_does_not_echo_it(db):
    sentinel = 'synthetic-domain-' + uuid4().hex
    with db[1].begin() as c:
        candidate.scope(c)
        run_id, _ = run(c)
        rid = identity(c)
        oid = observation(c,rid,run_id,text=sentinel,rating=5)
        advance(c,rid,oid)
        assert c.execute(text('SELECT text FROM review_observations WHERE observation_id=:id'), {'id':oid}).scalar_one() == sentinel
    with db[0].connect() as c:
        tx = c.begin()
        try:
            c.exec_driver_sql(f'GRANT UPDATE ON review_observations TO {db[1].url.username}')
            c.exec_driver_sql(f'SET LOCAL ROLE {db[1].url.username}')
            candidate.scope(c)
            with pytest.raises(DBAPIError) as error, c.begin_nested():
                c.execute(text('UPDATE review_observations SET text=:payload WHERE observation_id=:id'), {'payload':sentinel,'id':oid})
            assert error.value.orig.diag.message_primary == 'Review evidence is immutable'
            assert sentinel not in (error.value.orig.diag.message_detail or '')
        finally:
            tx.rollback()


def test_actual_script_removes_existing_column_privileges(db):
    owner, runtime = db
    try:
        with owner.begin() as c:
            c.exec_driver_sql(f'GRANT INSERT(run_sequence) ON review_sync_runs_v2 TO {runtime.url.username}')
            c.exec_driver_sql(f'GRANT UPDATE(text) ON review_observations TO {runtime.url.username}')
        assert runtime_script(owner,runtime.url.username).returncode == 0
        with owner.connect() as c:
            assert not c.execute(text("SELECT has_any_column_privilege(:role,'review_observations','UPDATE')"), {'role':runtime.url.username}).scalar_one()
            assert_acl(c,runtime.url.username)
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(f'REVOKE INSERT(run_sequence) ON review_sync_runs_v2 FROM {runtime.url.username}')
            c.exec_driver_sql(f'REVOKE UPDATE(text) ON review_observations FROM {runtime.url.username}')


@pytest.mark.parametrize('field', ['external_review_id', 'source_run_id'])
def test_domain_valid_long_opaque_identifiers_roundtrip(db, field):
    value = 'opaque-' + ''.join(uuid4().hex for _ in range(256))
    fact = normalize_avito_review(
        {'id':value if field=='external_review_id' else uuid4().hex,
         'created_at':'2026-09-09T00:00:00Z','answered':False},
        organization_id=91001, marketplace_account_id=91101,
        source_run_id=value if field=='source_run_id' else uuid4().hex,
        observed_at='2026-09-09T00:00:00Z')
    try:
        with db[1].begin() as c:
            candidate.scope(c)
            run_id, _ = run(c,source_run_id=fact.source_run_id)
            rid = identity(c,external_review_id=fact.identity.external_review_id)
            oid = observation(c,rid,run_id)
            advance(c,rid,oid)
            # Distinct long keys sharing their entire prefix remain distinct.
            other_run, _ = run(c,source_run_id=fact.source_run_id+'x')
            other_rid = identity(c,external_review_id=fact.identity.external_review_id+'x')
            other_oid = observation(c,other_rid,other_run)
            advance(c,other_rid,other_oid)
            assert other_run != run_id and other_rid != rid
    except DBAPIError as error:
        pytest.fail(f'Domain-valid opaque {field} rejected: SQLSTATE {error.orig.sqlstate}', pytrace=False)


@pytest.mark.parametrize('field', ['external_review_id', 'source_run_id'])
def test_concurrent_exact_identifier_duplicate_has_one_winner(db,field):
    value = 'opaque-' + ''.join(uuid4().hex for _ in range(256))
    barrier = Barrier(2)
    pids = Queue()
    def create():
        try:
            with db[1].begin() as c:
                candidate.scope(c)
                assert c.exec_driver_sql('SHOW transaction_isolation').scalar_one() == 'read committed'
                # Establish a read before INSERT starts waiting for the account.
                c.exec_driver_sql('SELECT count(*) FROM review_facts').scalar_one()
                pids.put(c.exec_driver_sql('SELECT pg_backend_pid()').scalar_one())
                barrier.wait(timeout=10)
                if field == 'source_run_id':
                    run(c,source_run_id=value)
                else:
                    complete_fact(c,external_review_id=value)
            return 'inserted'
        except DBAPIError as error:
            assert error.orig.diag.constraint_name == ('uq_review_run_source' if field=='source_run_id' else 'uq_review_fact_external')
            return error.orig.sqlstate
    with ThreadPoolExecutor(max_workers=2) as pool:
        with db[0].begin() as blocker:
            blocker.exec_driver_sql('SELECT marketplace_account_id FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE')
            futures = [pool.submit(create) for _ in range(2)]
            own_pids = [pids.get(timeout=10) for _ in range(2)]
            deadline = monotonic()+10
            while True:
                blocker.exec_driver_sql('SELECT pg_stat_clear_snapshot()')
                waits = blocker.execute(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=ANY(:pids)"), {'pids':own_pids}).scalars().all()
                if waits == ['Lock','Lock']:
                    break
                assert monotonic()<deadline, 'Own INSERT sessions did not wait on the canonical account'
                sleep(0.01)
        assert sorted(f.result(timeout=20) for f in futures) == ['23505','inserted']


@pytest.mark.parametrize('table', TABLES[:2])
@pytest.mark.parametrize('isolation', ['REPEATABLE READ','SERIALIZABLE'])
def test_exact_key_insert_refuses_snapshot_isolation(db,table,isolation):
    with db[1].connect().execution_options(isolation_level=isolation) as c:
        with pytest.raises(DBAPIError) as error, c.begin():
            candidate.scope(c)
            if table==TABLES[0]:
                run(c)
            else:
                identity(c)
        assert error.value.orig.sqlstate == '25000'
