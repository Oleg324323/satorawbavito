"""Add lossless UTF8 alternatives to Review facts without changing identities.

Revision ID: 20260909_0065
Revises: 20260909_0064

Decoder-capable readers must precede byte writers. Once bytes exist, application
rollback must retain that decoder: downgrade refuses any byte-backed row.
CHECK validation scans existing rows and table locks may wait.
Deployment requires exclusive privileged DDL/ACL maintenance: table locks do
not prevent a concurrent owner GRANT from invalidating the ACL preflight.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0065"
down_revision = "20260909_0064"
branch_labels = None
depends_on = None

PAIRS = (
    ("review_facts", "external_review_id", True, True),
    ("review_observations", "external_product_id", False, True),
    ("review_observations", "text", False, False),
    ("review_observations", "source_status", False, True),
    ("review_observations", "source_schema_version", True, True),
    ("review_observations", "normalization_version", True, True),
    ("review_sync_runs_v2", "source_run_id", True, True),
    ("review_sync_runs_v2", "coverage", True, False),
)

PREFLIGHT_SQL = r"""
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_catalog.pg_class c
    CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) acl
    WHERE c.oid IN ('public.review_facts'::regclass,'public.review_observations'::regclass,
                   'public.review_sync_runs_v2'::regclass)
      AND acl.grantee=0 AND acl.privilege_type IN ('INSERT','UPDATE')
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_attribute a
    CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
    WHERE a.attrelid IN ('public.review_facts'::regclass,'public.review_observations'::regclass,
                        'public.review_sync_runs_v2'::regclass)
      AND NOT a.attisdropped AND a.attnum>0
      AND acl.grantee=0 AND acl.privilege_type IN ('INSERT','UPDATE')
  ) THEN
    RAISE EXCEPTION 'review_lossless_acl_unsupported' USING ERRCODE='55000';
  END IF;
END $$;
"""

HELPERS_SQL = r"""
CREATE FUNCTION public.review_strict_utf8(value bytea) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE byte_index integer; chunk_start integer := 0; size integer := pg_catalog.octet_length(value);
BEGIN
  -- Index the original bytes; copy only disjoint chunks, never a remaining suffix.
  FOR byte_index IN 0..size-1 LOOP
    IF pg_catalog.get_byte(value,byte_index)=0 THEN
      PERFORM pg_catalog.convert_from(pg_catalog.substr(value,chunk_start+1,byte_index-chunk_start),'UTF8');
      chunk_start := byte_index+1;
    END IF;
  END LOOP;
  PERFORM pg_catalog.convert_from(pg_catalog.substr(value,chunk_start+1,size-chunk_start),'UTF8');
  RETURN true;
EXCEPTION WHEN character_not_in_repertoire THEN RETURN false;
END $$;

CREATE FUNCTION public.review_coverage_json_object_utf8(value bytea) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
  RETURN pg_catalog.json_typeof(pg_catalog.convert_from(value,'UTF8')::json)='object';
EXCEPTION WHEN character_not_in_repertoire OR invalid_text_representation THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION public.review_strict_utf8(bytea),
  public.review_coverage_json_object_utf8(bytea) FROM PUBLIC;
-- Default function ACLs may also name readers. Narrow only these new helpers;
-- leave the owner's defaults and every preexisting function ACL unchanged.
DO $$
DECLARE entry record;
BEGIN
  FOR entry IN
    SELECT p.proname,acl.grantee FROM pg_catalog.pg_proc p
    CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) acl
    WHERE p.oid IN ('public.review_strict_utf8(bytea)'::regprocedure,
                   'public.review_coverage_json_object_utf8(bytea)'::regprocedure)
      AND acl.grantee NOT IN (0,p.proowner)
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION public.%I(bytea) FROM %I',
      entry.proname,pg_catalog.pg_get_userbyid(entry.grantee));
  END LOOP;
END $$;
"""

# Frozen actual 0063 definitions, unchanged except CREATE OR REPLACE. Downgrade
# restores these exact bodies rather than approximating old lifecycle semantics.
ORIGINAL_IDENTITY_SQL = r"""
CREATE OR REPLACE FUNCTION public.review_exact_identity_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE duplicate boolean; logical_constraint text;
BEGIN
  IF current_setting('transaction_isolation')<>'read committed' THEN
    RAISE EXCEPTION 'Review identity insertion requires READ COMMITTED' USING ERRCODE='25000';
  END IF;
  PERFORM 1 FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
      AND marketplace=NEW.marketplace FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Review account binding unavailable' USING ERRCODE='23503';
  END IF;
  IF TG_TABLE_NAME='review_sync_runs_v2' THEN
    SELECT EXISTS (SELECT 1 FROM public.review_sync_runs_v2
      WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
        AND marketplace=NEW.marketplace AND source_run_id COLLATE "C"=NEW.source_run_id COLLATE "C") INTO duplicate;
    logical_constraint := 'uq_review_run_source';
  ELSE
    SELECT EXISTS (SELECT 1 FROM public.review_facts
      WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
        AND marketplace=NEW.marketplace AND external_review_id COLLATE "C"=NEW.external_review_id COLLATE "C") INTO duplicate;
    logical_constraint := 'uq_review_fact_external';
  END IF;
  IF duplicate THEN
    RAISE EXCEPTION 'Review identity already exists'
      USING ERRCODE='23505',CONSTRAINT=logical_constraint;
  END IF;
  RETURN NEW;
END $$;
"""

ORIGINAL_FACT_SQL = r"""
CREATE OR REPLACE FUNCTION public.review_fact_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
  IF (NEW.review_id,NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.external_review_id,NEW.first_observed_at)
    IS DISTINCT FROM (OLD.review_id,OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.external_review_id,OLD.first_observed_at)
    OR NEW.version IS DISTINCT FROM OLD.version+1 THEN
    RAISE EXCEPTION 'Review fact identity or version conflict' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
"""

ORIGINAL_RUN_SQL = r"""
CREATE OR REPLACE FUNCTION public.review_run_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
  IF OLD.status<>'running' OR
    (NEW.sync_run_id,NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.source_run_id,NEW.run_sequence,NEW.request_checksum,NEW.started_at)
    IS DISTINCT FROM
    (OLD.sync_run_id,OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.source_run_id,OLD.run_sequence,OLD.request_checksum,OLD.started_at) THEN
    RAISE EXCEPTION 'Review run identity or terminal conflict' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
"""

EXTEND_ACL_SQL = r"""
DO $$
DECLARE entry record; target text;
BEGIN
  -- Grant on the existing direct grantee, so inherited roles retain their shape.
  -- Table rights already include added columns; extend only paired column ACLs.
  FOR entry IN
    SELECT a.attname,acl.grantee,acl.privilege_type,acl.is_grantable
    FROM pg_catalog.pg_attribute a
    CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
    WHERE a.attrelid='public.review_sync_runs_v2'::regclass
      AND a.attname IN ('source_run_id','coverage')
      AND acl.grantee<>0 AND acl.privilege_type IN ('INSERT','UPDATE')
  LOOP
    target := pg_catalog.pg_get_userbyid(entry.grantee);
    EXECUTE format('GRANT %s (%I) ON public.review_sync_runs_v2 TO %I%s',
      entry.privilege_type,entry.attname||'_utf8',target,
      CASE WHEN entry.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
  END LOOP;
  -- Any writer can cause CHECK evaluation on retained bytes, including an UPDATE
  -- of another column. Give only the validation helpers needed by that relation.
  FOR entry IN
    SELECT DISTINCT c.relname,acl.grantee FROM pg_catalog.pg_class c
    CROSS JOIN LATERAL (
      SELECT grantee,privilege_type FROM pg_catalog.aclexplode(
        COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner)))
      UNION ALL
      SELECT x.grantee,x.privilege_type FROM pg_catalog.pg_attribute a
      CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) x
      WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
    ) acl
    WHERE c.oid IN ('public.review_facts'::regclass,'public.review_observations'::regclass,
                   'public.review_sync_runs_v2'::regclass)
      AND acl.grantee<>0 AND acl.privilege_type IN ('INSERT','UPDATE')
  LOOP
    target := pg_catalog.pg_get_userbyid(entry.grantee);
    EXECUTE format('GRANT EXECUTE ON FUNCTION public.review_strict_utf8(bytea) TO %I',target);
    IF entry.relname='review_sync_runs_v2' THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION public.review_coverage_json_object_utf8(bytea) TO %I',target);
    END IF;
  END LOOP;
END $$;
"""


def execute(sql):
    op.execute(sa.text(sql))


def upgrade():
    # Stabilize data/DDL in the same fixed order as downgrade, before any DDL.
    # These relation locks do NOT fence privileged GRANT; coordinated exclusive
    # DDL/ACL maintenance is a migration operator precondition.
    execute("LOCK TABLE public.review_sync_runs_v2,public.review_facts,public.review_observations "
            "IN ACCESS EXCLUSIVE MODE")
    execute(PREFLIGHT_SQL)
    execute(HELPERS_SQL)
    for table, column, required, nonempty in PAIRS:
        new = column + "_utf8"
        execute(f"ALTER TABLE public.{table} ADD COLUMN {new} BYTEA")
        if required:
            execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} DROP NOT NULL")
        pair = (f"({column} IS NOT NULL)::integer+({new} IS NOT NULL)::integer=1"
                if required else f"{column} IS NULL OR {new} IS NULL")
        predicate = "review_coverage_json_object_utf8" if column == "coverage" else "review_strict_utf8"
        valid = f"public.{predicate}({new})"
        if nonempty:
            valid += f" AND pg_catalog.octet_length({new})>0"
        execute(f"ALTER TABLE public.{table} ADD CONSTRAINT ck_review_{column}_pair CHECK ({pair}), "
                f"ADD CONSTRAINT ck_review_{column}_utf8 CHECK ({new} IS NULL OR ({valid}))")
    identity = ORIGINAL_IDENTITY_SQL
    for column in ("source_run_id", "external_review_id"):
        identity = identity.replace(
            f'{column} COLLATE "C"=NEW.{column} COLLATE "C"',
            f"COALESCE({column}_utf8,pg_catalog.convert_to({column},'UTF8'))="
            f"COALESCE(NEW.{column}_utf8,pg_catalog.convert_to(NEW.{column},'UTF8'))",
        )
    execute(identity)
    execute(ORIGINAL_FACT_SQL.replace("NEW.external_review_id,", "NEW.external_review_id,NEW.external_review_id_utf8,")
            .replace("OLD.external_review_id,", "OLD.external_review_id,OLD.external_review_id_utf8,"))
    execute(ORIGINAL_RUN_SQL.replace("NEW.source_run_id,", "NEW.source_run_id,NEW.source_run_id_utf8,")
            .replace("OLD.source_run_id,", "OLD.source_run_id,OLD.source_run_id_utf8,"))
    execute(EXTEND_ACL_SQL)


def downgrade():
    execute("LOCK TABLE public.review_sync_runs_v2,public.review_facts,public.review_observations "
            "IN ACCESS EXCLUSIVE MODE; SET LOCAL row_security=off")
    # row_security=off refuses filtered access; it does not bypass FORCE RLS.
    # Finish ALL visibility/data checks before changing any schema definition.
    for table in ("review_sync_runs_v2", "review_facts", "review_observations"):
        present = " OR ".join(f"{column}_utf8 IS NOT NULL" for relation, column, _, _ in PAIRS if relation == table)
        execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{table} WHERE {present}) THEN
          RAISE EXCEPTION 'Review lossless downgrade blocked: byte-backed data' USING ERRCODE='55000';
          END IF; END $$""")
    execute(ORIGINAL_IDENTITY_SQL)
    execute(ORIGINAL_FACT_SQL)
    execute(ORIGINAL_RUN_SQL)
    for table, column, required, _ in PAIRS:
        if required:
            execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} SET NOT NULL")
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT ck_review_{column}_pair, "
                f"DROP CONSTRAINT ck_review_{column}_utf8, DROP COLUMN {column}_utf8")
    execute("DROP FUNCTION public.review_strict_utf8(bytea); "
            "DROP FUNCTION public.review_coverage_json_object_utf8(bytea)")
