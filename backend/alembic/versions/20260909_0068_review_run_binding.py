"""Immutable external-account provenance for new Review runs; no history backfill.

Exclusive privileged DDL/ACL maintenance is required: the relation lock is not
a fence against concurrent GRANT. Rollback binaries retain this expanded schema
once any run is bound. Consumers and per-account authorization belong to T4.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0068"
down_revision = "20260909_0067"
branch_labels = depends_on = None

FIELDS = (
    "account_binding_schema_version", "account_binding_external_account_id",
    "account_binding_credential_ref", "account_binding_payload", "account_binding_checksum",
)
PURE = ("review_binding_ascii_string(text)",
        "review_run_binding_bytes(integer,integer,text,text,text)")
TRIGGERS = ("review_run_binding_insert_guard()", "review_run_binding_update_guard()")
# Full existing runtime INSERT allowlist, excluding generated run_sequence.
OLD_INSERT = (
    "sync_run_id", "organization_id", "marketplace_account_id", "marketplace",
    "source_run_id", "request_checksum", "status", "completeness", "started_at",
    "completed_at", "observed_count", "manifest_checksum", "coverage", "error_code",
    "source_run_id_utf8", "coverage_utf8",
)

HELPERS_SQL = r'''
CREATE FUNCTION public.review_binding_ascii_string(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE result text := '"'; code integer; ch text; index integer;
BEGIN
  IF value IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_invalid';
  END IF;
  FOR index IN 1..char_length(value) LOOP
    ch := substr(value,index,1);
    code := ascii(ch);
    IF code=0 OR code BETWEEN 55296 AND 57343 THEN
      RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_invalid';
    ELSIF code=34 THEN result := result || E'\\"';
    ELSIF code=92 THEN result := result || E'\\\\';
    ELSIF code=8 THEN result := result || E'\\b';
    ELSIF code=9 THEN result := result || E'\\t';
    ELSIF code=10 THEN result := result || E'\\n';
    ELSIF code=12 THEN result := result || E'\\f';
    ELSIF code=13 THEN result := result || E'\\r';
    ELSIF code < 32 OR code BETWEEN 127 AND 65535 THEN
      result := result || E'\\u' || lpad(to_hex(code),4,'0');
    ELSIF code > 65535 THEN
      code := code - 65536;
      result := result || E'\\u' || lpad(to_hex(55296 + code / 1024),4,'0')
                       || E'\\u' || lpad(to_hex(56320 + code % 1024),4,'0');
    ELSE result := result || ch;
    END IF;
  END LOOP;
  RETURN result || '"';
END $$;

CREATE FUNCTION public.review_run_binding_bytes(org integer, account integer,
  provider text, external_id text, credential_ref text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
  IF org IS NULL OR org <= 0 OR account IS NULL OR account <= 0
     OR provider IS NULL OR provider COLLATE "C" NOT IN ('wb','avito')
     OR external_id IS NULL OR octet_length(external_id)=0 THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_invalid';
  END IF;
  RETURN convert_to('{"credentialRef":'
    || CASE WHEN credential_ref IS NULL THEN 'null'
            ELSE public.review_binding_ascii_string(credential_ref) END
    || ',"externalAccountId":' || public.review_binding_ascii_string(external_id)
    || ',"marketplace":' || public.review_binding_ascii_string(provider)
    || ',"marketplaceAccountId":' || account::text
    || ',"organizationId":' || org::text || ',"schemaVersion":' || '1}', 'UTF8');
END $$;
'''

GUARDS_SQL = '''
CREATE FUNCTION public.review_run_binding_insert_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE actual record;
BEGIN
  IF NEW.account_binding_schema_version IS NULL
     AND NEW.account_binding_external_account_id IS NULL
     AND NEW.account_binding_credential_ref IS NULL
     AND NEW.account_binding_payload IS NULL AND NEW.account_binding_checksum IS NULL THEN
    RETURN NEW;
  END IF;
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION USING ERRCODE='25000', MESSAGE='review_run_binding_isolation_invalid';
  END IF;
  PERFORM 1 FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id
      AND marketplace_account_id=NEW.marketplace_account_id
      AND marketplace COLLATE "C"=NEW.marketplace COLLATE "C" FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_mismatch';
  END IF;
  -- Separate VOLATILE query takes a fresh RC snapshot after any lock wait.
  SELECT marketplace, external_account_id, credential_ref INTO actual
    FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id
      AND marketplace_account_id=NEW.marketplace_account_id;
  IF NOT FOUND OR actual.marketplace COLLATE "C" IS DISTINCT FROM NEW.marketplace COLLATE "C"
     OR actual.external_account_id COLLATE "C" IS DISTINCT FROM NEW.account_binding_external_account_id COLLATE "C"
     OR actual.credential_ref COLLATE "C" IS DISTINCT FROM NEW.account_binding_credential_ref COLLATE "C" THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_mismatch';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION public.review_run_binding_update_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
  IF ROW(OLD.account_binding_schema_version, OLD.account_binding_external_account_id,
         OLD.account_binding_credential_ref, OLD.account_binding_payload, OLD.account_binding_checksum)
     IS DISTINCT FROM
     ROW(NEW.account_binding_schema_version, NEW.account_binding_external_account_id,
         NEW.account_binding_credential_ref, NEW.account_binding_payload, NEW.account_binding_checksum) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_immutable';
  END IF;
  -- UPDATE already owns a run tuple: never acquire an account lock here.
  RETURN NEW;
END $$;
CREATE TRIGGER review_run_binding_insert BEFORE INSERT ON public.review_sync_runs_v2
  FOR EACH ROW EXECUTE FUNCTION public.review_run_binding_insert_guard();
CREATE TRIGGER review_run_binding_update BEFORE UPDATE ON public.review_sync_runs_v2
  FOR EACH ROW EXECUTE FUNCTION public.review_run_binding_update_guard();
'''


def upgrade():
    op.execute("LOCK TABLE public.review_sync_runs_v2 IN ACCESS EXCLUSIVE MODE")
    op.execute(sa.text(HELPERS_SQL))
    op.execute('''ALTER TABLE public.review_sync_runs_v2
      ADD COLUMN account_binding_schema_version SMALLINT,
      ADD COLUMN account_binding_external_account_id TEXT COLLATE "C",
      ADD COLUMN account_binding_credential_ref TEXT COLLATE "C",
      ADD COLUMN account_binding_payload BYTEA,
      ADD COLUMN account_binding_checksum TEXT,
      ADD CONSTRAINT ck_review_run_binding CHECK (
        (account_binding_schema_version IS NULL AND account_binding_external_account_id IS NULL
         AND account_binding_credential_ref IS NULL AND account_binding_payload IS NULL
         AND account_binding_checksum IS NULL)
        OR
        (account_binding_schema_version IS NOT NULL AND account_binding_schema_version=1
         AND account_binding_external_account_id IS NOT NULL
         AND octet_length(account_binding_external_account_id)>0
         AND account_binding_payload IS NOT NULL AND account_binding_checksum IS NOT NULL
         AND account_binding_payload=public.review_run_binding_bytes(organization_id,
           marketplace_account_id,marketplace,account_binding_external_account_id,account_binding_credential_ref)
         AND account_binding_checksum COLLATE "C"=encode(sha256(account_binding_payload),'hex') COLLATE "C")
      )''')
    op.execute(sa.text(GUARDS_SQL))
    # Remove only newly created function grants (including hostile defaults and
    # grant options), atomically. Old function/default ACLs remain untouched.
    for signature in (*PURE, *TRIGGERS):
        op.execute(sa.text("""DO $$ DECLARE target record; BEGIN
          FOR target IN SELECT DISTINCT acl.grantee FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(p.proacl) acl
            WHERE p.oid='public.""" + signature + """'::regprocedure
              AND acl.grantee NOT IN (0,p.proowner) LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION public.""" + signature + """ FROM %I',
              pg_get_userbyid(target.grantee));
          END LOOP;
        END $$;"""))
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    signatures = ",".join("public." + sig for sig in PURE)
    required = ",".join("'" + col + "'" for col in OLD_INSERT)
    columns = ",".join(FIELDS)
    op.execute(sa.text("""DO $$ DECLARE target record; BEGIN
      FOR target IN SELECT DISTINCT acl.grantee,c.relowner FROM pg_class c
        CROSS JOIN LATERAL (
          SELECT grantee,privilege_type FROM aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner)))
          UNION SELECT x.grantee,x.privilege_type FROM pg_attribute a
            CROSS JOIN LATERAL aclexplode(a.attacl) x
            WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        ) acl WHERE c.oid='public.review_sync_runs_v2'::regclass
          AND acl.grantee<>0 AND acl.privilege_type IN ('INSERT','UPDATE') LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION """ + signatures + """ TO %I',
          pg_get_userbyid(target.grantee));
        IF target.grantee<>target.relowner AND NOT EXISTS (SELECT 1 FROM unnest(ARRAY[""" + required + """]) col
          WHERE NOT has_column_privilege(target.grantee,'public.review_sync_runs_v2',col,'INSERT')) THEN
          EXECUTE format('GRANT INSERT (""" + columns + """) ON public.review_sync_runs_v2 TO %I',
            pg_get_userbyid(target.grantee));
        END IF;
      END LOOP;
    END $$;"""))


def downgrade():
    op.execute("LOCK TABLE public.review_sync_runs_v2 IN ACCESS EXCLUSIVE MODE")
    # This refuses filtered reads under FORCE RLS; it does not grant bypass.
    op.execute("SET LOCAL row_security=off")
    op.execute(sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.review_sync_runs_v2 WHERE """
      + " OR ".join(f"{field} IS NOT NULL" for field in FIELDS) + """
      ) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='review_run_binding_downgrade_bound';
      END IF;
    END $$;"""))
    for name in ("review_run_binding_insert", "review_run_binding_update"):
        op.execute(f"DROP TRIGGER {name} ON public.review_sync_runs_v2")
    op.execute("ALTER TABLE public.review_sync_runs_v2 DROP CONSTRAINT ck_review_run_binding")
    for field in FIELDS:
        op.execute(f"ALTER TABLE public.review_sync_runs_v2 DROP COLUMN {field}")
    for signature in (*TRIGGERS, *reversed(PURE)):
        op.execute(f"DROP FUNCTION public.{signature}")
