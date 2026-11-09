"""Freeze exact account provenance on new Orders runs; preserve unbound history."""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0067"
down_revision = "20260909_0066"
branch_labels = depends_on = None

FIELDS = (
    "account_binding_schema_version", "account_binding_external_account_id",
    "account_binding_credential_ref", "account_binding_payload", "account_binding_checksum",
)
PURE = (
    "orders_binding_text(text,integer)", "orders_binding_ascii_string(text)",
    "orders_run_binding_bytes(integer,integer,text,text,text)",
)
TRIGGERS = ("orders_run_binding_insert_guard()", "orders_run_binding_update_guard()")

HELPERS_SQL = r'''
CREATE FUNCTION public.orders_binding_text(value text, max_chars integer) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
  SELECT COALESCE(char_length(value) BETWEEN 1 AND max_chars
    AND btrim(value,U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000') <> '', false);
$$;

CREATE FUNCTION public.orders_binding_ascii_string(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE result text := '"'; code integer; ch text; index integer;
BEGIN
  IF value IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_invalid';
  END IF;
  FOR index IN 1..char_length(value) LOOP
    ch := substr(value,index,1);
    code := ascii(ch);
    IF code=0 OR code BETWEEN 55296 AND 57343 THEN
      RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_invalid';
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

CREATE FUNCTION public.orders_run_binding_bytes(org integer, account integer,
  provider text, external_id text, credential_ref text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
  IF org IS NULL OR org <= 0 OR account IS NULL OR account <= 0
     OR provider IS NULL OR provider COLLATE "C" NOT IN ('wb','avito')
     OR NOT public.orders_binding_text(external_id,128)
     OR (credential_ref IS NOT NULL AND NOT public.orders_binding_text(credential_ref,255)) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_invalid';
  END IF;
  RETURN convert_to('[' || org::text || ',[[' || account::text || ','
    || public.orders_binding_ascii_string(provider) || ','
    || public.orders_binding_ascii_string(external_id) || ','
    || CASE WHEN credential_ref IS NULL THEN 'null'
            ELSE public.orders_binding_ascii_string(credential_ref) END || ']]]', 'UTF8');
END $$;
'''

GUARDS_SQL = '''
CREATE FUNCTION public.orders_run_binding_insert_guard() RETURNS trigger
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
    RAISE EXCEPTION USING ERRCODE='25000', MESSAGE='orders_isolation_invalid';
  END IF;
  -- Same canonical account as 0064. A separate VOLATILE statement below takes
  -- a fresh RC snapshot after a wait; never trust the outer INSERT snapshot.
  PERFORM 1 FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id
      AND marketplace_account_id=NEW.marketplace_account_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_mismatch';
  END IF;
  SELECT marketplace, external_account_id, credential_ref INTO actual
    FROM public.marketplace_accounts
    WHERE organization_id=NEW.organization_id
      AND marketplace_account_id=NEW.marketplace_account_id;
  IF NOT FOUND OR actual.marketplace COLLATE "C" IS DISTINCT FROM NEW.marketplace COLLATE "C"
     OR actual.external_account_id COLLATE "C" IS DISTINCT FROM NEW.account_binding_external_account_id COLLATE "C"
     OR actual.credential_ref COLLATE "C" IS DISTINCT FROM NEW.account_binding_credential_ref COLLATE "C" THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_mismatch';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION public.orders_run_binding_update_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
  IF ROW(OLD.account_binding_schema_version, OLD.account_binding_external_account_id,
         OLD.account_binding_credential_ref, OLD.account_binding_payload, OLD.account_binding_checksum)
     IS DISTINCT FROM
     ROW(NEW.account_binding_schema_version, NEW.account_binding_external_account_id,
         NEW.account_binding_credential_ref, NEW.account_binding_payload, NEW.account_binding_checksum) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_immutable';
  END IF;
  -- No account lock here: UPDATE already owns a run tuple.
  RETURN NEW;
END $$;
CREATE TRIGGER orders_run_binding_insert BEFORE INSERT ON public.order_sync_runs
  FOR EACH ROW EXECUTE FUNCTION public.orders_run_binding_insert_guard();
CREATE TRIGGER orders_run_binding_update BEFORE UPDATE ON public.order_sync_runs
  FOR EACH ROW EXECUTE FUNCTION public.orders_run_binding_update_guard();
'''


def upgrade():
    op.execute("LOCK TABLE public.order_sync_runs IN ACCESS EXCLUSIVE MODE")
    op.execute(sa.text(HELPERS_SQL))
    op.execute('''ALTER TABLE public.order_sync_runs
      ADD COLUMN account_binding_schema_version SMALLINT,
      ADD COLUMN account_binding_external_account_id TEXT COLLATE "C",
      ADD COLUMN account_binding_credential_ref TEXT COLLATE "C",
      ADD COLUMN account_binding_payload BYTEA,
      ADD COLUMN account_binding_checksum TEXT,
      ADD CONSTRAINT ck_orders_run_binding CHECK (
        (account_binding_schema_version IS NULL AND account_binding_external_account_id IS NULL
         AND account_binding_credential_ref IS NULL AND account_binding_payload IS NULL
         AND account_binding_checksum IS NULL)
        OR
        (account_binding_schema_version IS NOT NULL AND account_binding_schema_version=1
         AND account_binding_external_account_id IS NOT NULL
         AND account_binding_payload IS NOT NULL AND account_binding_checksum IS NOT NULL
         AND public.orders_binding_text(account_binding_external_account_id,128)
         AND (account_binding_credential_ref IS NULL OR public.orders_binding_text(account_binding_credential_ref,255))
         AND account_binding_payload=public.orders_run_binding_bytes(organization_id,
           marketplace_account_id,marketplace,account_binding_external_account_id,account_binding_credential_ref)
         AND account_binding_checksum COLLATE "C"=encode(sha256(account_binding_payload),'hex') COLLATE "C")
      )''')
    op.execute(sa.text(GUARDS_SQL))
    # Clear inherited default function grants including grant options. Preserve
    # all old table/column/default ACLs, and only admit existing INSERT/UPDATE
    # grantees whose CHECK evaluations need the pure helpers. Readers need none.
    for signature in (*PURE, *TRIGGERS):
        op.execute(sa.text("""DO $$ DECLARE target record; BEGIN
          FOR target IN SELECT acl.grantee FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(p.proacl) acl
            WHERE p.oid='public.""" + signature + """'::regprocedure
              AND acl.grantee NOT IN (0,p.proowner) LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION public.""" + signature + """ FROM %I',
              pg_get_userbyid(target.grantee));
          END LOOP;
        END $$;"""))
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    signatures = ",".join("public." + sig for sig in PURE)
    op.execute(sa.text("""DO $$ DECLARE target record; BEGIN
      FOR target IN SELECT DISTINCT acl.grantee FROM pg_class c
        CROSS JOIN LATERAL (
          SELECT grantee,privilege_type FROM aclexplode(c.relacl)
          UNION SELECT x.grantee,x.privilege_type FROM pg_attribute a
            CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid=c.oid
        ) acl WHERE c.oid='public.order_sync_runs'::regclass
          AND acl.grantee NOT IN (0,c.relowner) AND acl.privilege_type IN ('INSERT','UPDATE') LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION """ + signatures + """ TO %I',
          pg_get_userbyid(target.grantee));
      END LOOP;
    END $$;"""))


def downgrade():
    op.execute("LOCK TABLE public.order_sync_runs IN ACCESS EXCLUSIVE MODE")
    # Does not bypass FORCE RLS: refusal is mandatory if all-row visibility is
    # unavailable, even when the actor sees an empty organization.
    op.execute("SET LOCAL row_security=off")
    op.execute(sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.order_sync_runs WHERE """
      + " OR ".join(f"{field} IS NOT NULL" for field in FIELDS) + """
      ) THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='orders_run_binding_downgrade_bound';
      END IF;
    END $$;"""))
    for name in ("orders_run_binding_insert", "orders_run_binding_update"):
        op.execute(f"DROP TRIGGER {name} ON public.order_sync_runs")
    op.execute("ALTER TABLE public.order_sync_runs DROP CONSTRAINT ck_orders_run_binding")
    for field in FIELDS:
        op.execute(f"ALTER TABLE public.order_sync_runs DROP COLUMN {field}")
    for signature in (*TRIGGERS, *reversed(PURE)):
        op.execute(f"DROP FUNCTION public.{signature}")
