"""First human Review draft; preserve historical canonical bytes and authority."""
import hashlib
from pathlib import Path
import runpy

from alembic import op

revision = "20260910_0083"
down_revision = "20260910_0082"
branch_labels = depends_on = None

_PRIOR = "20260909_0071_review_local_storage.py"
_PRIOR_SHA256 = "fbfdf583973cb57f32735b63a5d318a371a5b8ceba176de3ac3a6e2cbf5fca95"


def _once(value, old, new):
    if value.count(old) != 1:
        raise RuntimeError("review_manual_frozen_definition_mismatch")
    return value.replace(old, new, 1)


def _definitions():
    # Only immutable historical migration builders, never current runtime code.
    # Pin the complete source so upgrades and exact downgrades stay deterministic.
    path = Path(__file__).with_name(_PRIOR)
    if hashlib.sha256(path.read_bytes()).hexdigest() != _PRIOR_SHA256:
        raise RuntimeError("review_manual_frozen_definition_mismatch")
    prior = runpy.run_path(str(path))
    codecs = tuple(prior["codecs"]())
    definitions = []
    for name in ("review_local_generation_bytes", "review_local_request_bytes"):
        selected = [sql for sql in codecs if sql.startswith(f"CREATE FUNCTION public.{name}(")]
        if len(selected) != 1:
            raise RuntimeError("review_manual_frozen_definition_mismatch")
        definitions.append(selected[0])
    marker = "CREATE FUNCTION public.review_local_row_guard()"
    guards = prior["GUARDS"]
    if guards.count(marker) != 1:
        raise RuntimeError("review_manual_frozen_definition_mismatch")
    definitions.append(marker + guards.split(marker, 1)[1])
    return tuple(_once(sql, "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION") for sql in definitions), prior["TABLES"]


def upgrade():
    (generation, request, guard), _ = _definitions()
    generation = _once(generation, "NOT IN ('fake','manual_edit')", "NOT IN ('fake','manual','manual_edit')")
    generation = _once(generation, "d.generation_completed_at<d.generation_started_at",
        "(d.generation_mode='manual' AND d.revision IS DISTINCT FROM 1::numeric) OR "
        "d.generation_completed_at<d.generation_started_at")
    request = _once(request,
        "OR (r.expected_head_version=0) IS DISTINCT FROM (r.expected_draft_revision=0))",
        "OR (r.expected_head_version=0) IS DISTINCT FROM (r.expected_draft_revision=0)\n"
        "       OR (d.generation_mode='manual' AND (r.expected_head_version IS DISTINCT FROM 0::numeric\n"
        "        OR r.expected_draft_revision IS DISTINCT FROM 0::numeric)))")
    guard = _once(guard,
        "  IF TG_TABLE_NAME='review_draft_revisions' THEN\n   IF NEW.revision",
        "  IF TG_TABLE_NAME='review_draft_revisions' THEN\n"
        "   IF NEW.generation_mode='manual' AND (NEW.revision IS DISTINCT FROM 1::numeric\n"
        "    OR NEW.previous_draft_id IS NOT NULL OR wh.head_id IS NOT NULL) THEN\n"
        "    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';\n"
        "   END IF;\n   IF NEW.revision")
    # CREATE OR REPLACE retains OID/owner/ACL and the unchanged declarations retain
    # invoker security, volatility and search_path. No runtime grants are added.
    for sql in (generation, request, guard):
        op.execute(sql)


def downgrade():
    definitions, tables = _definitions()
    op.execute("LOCK TABLE " + ",".join("public." + table for table in tables) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security=off")
    # Every manual receipt/audit references its immutable draft through existing
    # deferred FKs; the authoritative generation column covers all manual history.
    op.execute("""DO $$ BEGIN
 IF EXISTS(SELECT FROM public.review_draft_revisions WHERE generation_mode='manual') THEN
  RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='review_manual_downgrade_nonempty';
 END IF;
END $$""")
    for sql in definitions:
        op.execute(sql)
