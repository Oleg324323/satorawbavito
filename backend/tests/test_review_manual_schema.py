"""Additive manual mode: exact SQL bytes and existing physical guards only."""
import hashlib
import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_local_storage_codec as codec
from tests import test_review_local_storage_lifecycle as lifecycle
from tests import test_review_local_storage_schema as storage
from tests.test_review_lossless_migration import scripts
from tests.test_review_manual_draft import manual_command

cluster = candidate.cluster
db = storage.db
TARGET = "20260910_0083"
PREVIOUS = "20260910_0082"
NAMES = ("review_local_generation_bytes", "review_local_request_bytes", "review_local_row_guard")
GOLDEN_HASH = "8719f302e913cebc2ac3d135ce04d31f0408611588b17fa49c9457f9bc152443"


def generation_sql():
    return text("SELECT review_local_generation_bytes(" + codec.composite("review_draft_revisions", "d") + ")")


def test_manual_generation_exact_golden_sql_bytes(db):
    generation = manual_command()["input"]["generation"]
    expected = json.dumps(generation, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(expected).hexdigest() == GOLDEN_HASH
    row = {**codec.literal_generation(generation), "revision": 1}
    with db[0].connect() as c:
        actual = bytes(c.execute(generation_sql(), {"d": codec.json_row(row)}).scalar_one())
    assert actual == expected and hashlib.sha256(actual).hexdigest() == GOLDEN_HASH
    assert json.loads(actual)["modelVersion"] == "fake-v1"


def test_manual_first_request_exact_bytes(db):
    request = manual_command()
    expected = lifecycle.canonical(request)
    with db[0].connect() as c:
        actual = bytes(c.execute(text(codec.REQUEST_SQL), manual_request_projections(request)).scalar_one())
    assert actual == expected


def test_manual_sql_rejects_predecessor_and_nonzero_expectations(db):
    for changed in ({"previous_draft_id": str(uuid4())}, {"revision": 2}, {"revision": None}):
        generation = {**codec.literal_generation(manual_command()["input"]["generation"]), "revision": 1, **changed}
        with pytest.raises(DBAPIError, match="review_local_invalid"), db[0].begin() as c:
            c.execute(generation_sql(), {"d": codec.json_row(generation)})
    for head, revision in ((1, 1), (4, 2), (0, 1), (1, 0)):
        request = manual_command()
        request["input"].update(expectedHeadVersion=head, expectedDraftRevision=revision)
        with pytest.raises(DBAPIError, match="review_local_invalid"), db[0].begin() as c:
            c.execute(text(codec.REQUEST_SQL), manual_request_projections(request))


def manual_request_projections(request):
    params = codec.request_projections(request)
    params["d"] = codec.json_row({**json.loads(params["d"]), "revision": 1})
    return params


def test_old_fake_edit_and_request_golden_bytes_are_unchanged(db):
    with db[0].connect() as c:
        for name in ("fakeGeneration", "manualGeneration"):
            vector = codec.STORAGE[name]
            row = codec.literal_generation(json.loads(vector["canonicalUtf8Json"]))
            codec.assert_literal(c.execute(generation_sql(), {"d": codec.json_row(row)}).scalar_one(), vector)
        for vector in codec.REQUEST_VECTORS:
            actual = c.execute(text(codec.REQUEST_SQL), codec.request_projections(json.loads(vector["canonicalUtf8Json"]))).scalar_one()
            codec.assert_literal(actual, vector)


@pytest.mark.parametrize("mutation", ["revision", "predecessor"])
def test_manual_persisted_first_revision_and_null_predecessor(db, mutation):
    with db[1].begin() as c:
        lifecycle.setup_policy(c)
        request, _ = lifecycle.preparation(c, mode="manual")
    def change(table, row):
        if table == "review_draft_revisions":
            row["revision" if mutation == "revision" else "previous_draft_id"] = 2 if mutation == "revision" else str(uuid4())
        return row
    with pytest.raises(DBAPIError), db[1].begin() as c:
        lifecycle.execute(c, request, transform=change)
    with db[1].begin() as c:
        storage.scope(c)
        assert lifecycle.lookup(c, "review_draft_revisions", draft_id=request["input"]["draftId"]) is None


def test_manual_row_guard_rejects_any_existing_workflow_head(db):
    with db[1].begin() as c:
        lifecycle.setup_policy(c)
        fake, source = lifecycle.preparation(c)
        lifecycle.execute(c, fake)
    with db[1].begin() as c:
        storage.scope(c)
        manual, _ = lifecycle.preparation(c, source=source, mode="manual")
        before = lifecycle.snapshot(c)
    # SQL participant intentionally does not use the runtime request validator.
    # The draft row itself must fail before a receipt can be inserted.
    reached = []
    def observe(table, row):
        reached.append(table)
        return row
    with pytest.raises(DBAPIError, match="review_local_invalid"), db[1].begin() as c:
        lifecycle.execute(c, manual, transform=observe)
    assert reached == ["review_draft_revisions"]
    with db[1].begin() as c:
        assert lifecycle.snapshot(c) == before


def catalog(c):
    functions = c.execute(text("SELECT oid,proowner,proacl::text,prosecdef,proconfig,prosrc,pg_get_functiondef(oid) "
        "FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=ANY(:names) ORDER BY proname"), {"names": list(NAMES)}).all()
    relations = c.execute(text("SELECT oid,relowner,relacl::text,relrowsecurity,relforcerowsecurity "
        "FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(:names) ORDER BY relname"), {"names": list(storage.TABLES)}).all()
    policies = c.execute(text("SELECT polrelid,polname,polcmd,polroles,pg_get_expr(polqual,polrelid),pg_get_expr(polwithcheck,polrelid) "
        "FROM pg_policy WHERE polrelid=ANY(:ids) ORDER BY polrelid,polname"), {"ids": [r[0] for r in relations]}).all()
    return functions, relations, policies


def test_upgrade_downgrade_restores_exact_definitions_oids_acl_and_rls(cluster):
    role = "review_manual_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION public.review_local_generation_bytes(public.review_draft_revisions) TO {role}")
                c.exec_driver_sql(f"GRANT SELECT ON public.review_draft_revisions TO {role}")
                original = catalog(c)
            for action, target in (("upgrade", TARGET), ("downgrade", PREVIOUS), ("upgrade", TARGET)):
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    current = catalog(c)
                    assert current[1:] == original[1:]
                    assert [tuple(row[:5]) for row in current[0]] == [tuple(row[:5]) for row in original[0]]
                    if action == "downgrade":
                        assert current == original
                    else:
                        assert all(row[3] is False for row in current[0])
                        assert all(row[4] == ["search_path=pg_catalog, public"] for row in current[0])
        finally:
            owner.dispose()


def test_manual_history_blocks_downgrade_and_is_invisible_without_scope(db):
    with db[1].begin() as c:
        lifecycle.setup_policy(c)
        request, _ = lifecycle.preparation(c, mode="manual")
        lifecycle.execute(c, request)
    with db[0].connect() as c:
        before = catalog(c)
    module = scripts().get_revision(TARGET).module
    with pytest.raises(DBAPIError, match="review_manual_downgrade_nonempty"), db[0].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            module.downgrade()
    with db[0].connect() as c:
        assert catalog(c) == before
        assert c.scalar(text("SELECT count(*) FROM review_draft_revisions WHERE draft_id=:d"), {"d": request["input"]["draftId"]}) == 1
    for org, account in ((None, None), (91002, 91101), (91001, 91102)):
        with db[1].begin() as c:
            if org is not None:
                storage.scope(c, org, account)
            assert c.scalar(text("SELECT count(*) FROM review_draft_revisions WHERE draft_id=:d"), {"d": request["input"]["draftId"]}) == 0


def test_actual_single_head_revision():
    directory = scripts()
    heads = directory.get_heads()
    assert len(heads) == 1
    # Later additive slices may follow this migration, but cannot bypass it or
    # introduce a competing branch. Keep the exact historical parent witness.
    assert TARGET in {revision.revision for revision in directory.walk_revisions(base="base", head=heads[0])}
    assert directory.get_revision(TARGET).down_revision == PREVIOUS
