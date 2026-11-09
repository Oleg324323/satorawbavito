"""Dormant Production creation and captured manual-assignment witnesses.

Trusted consumers still own live authentication, source provenance and permission.
Account lock precedes source/work-item locks; obtain one DB clock after all waits.
"""

from alembic import op

revision = "20260909_0069"
down_revision = "20260909_0068"
branch_labels = depends_on = None

TABLES = (
    "production_work_items",
    "production_assignment_receipts",
    "production_assignment_history",
)
PURE = (
    "production_exact_text(text)",
    "production_ascii_json_string(text)",
    "production_assignment_bytes(bigint,bigint,integer,text,text)",
)
TRIGGERS = (
    "production_account_lock()",
    "production_row_guard()",
    "production_validate()",
)

HELPERS = r"""
CREATE FUNCTION public.production_exact_text(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(value<>'' AND value COLLATE "C" = btrim(value,
 U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000') COLLATE "C",false)
$$;
CREATE FUNCTION public.production_ascii_json_string(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE output text := '"'; code integer; ch text; i integer;
BEGIN
 IF value IS NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid'; END IF;
 FOR i IN 1..char_length(value) LOOP
  ch:=substr(value,i,1); code:=ascii(ch);
  IF code=0 OR code BETWEEN 55296 AND 57343 THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  ELSIF code=34 THEN output:=output||E'\\"';
  ELSIF code=92 THEN output:=output||E'\\\\';
  ELSIF code=8 THEN output:=output||E'\\b';
  ELSIF code=9 THEN output:=output||E'\\t';
  ELSIF code=10 THEN output:=output||E'\\n';
  ELSIF code=12 THEN output:=output||E'\\f';
  ELSIF code=13 THEN output:=output||E'\\r';
  ELSIF code<32 OR code BETWEEN 127 AND 65535 THEN output:=output||E'\\u'||lpad(to_hex(code),4,'0');
  ELSIF code>65535 THEN
   code:=code-65536;
   output:=output||E'\\u'||lpad(to_hex(55296+code/1024),4,'0')||E'\\u'||lpad(to_hex(56320+code%1024),4,'0');
  ELSE output:=output||ch;
  END IF;
 END LOOP;
 RETURN output||'"';
END $$;
CREATE FUNCTION public.production_assignment_bytes(work_item_id bigint,expected_version bigint,
 catalog_sku_id integer,idempotency_key text,reason text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF work_item_id IS NULL OR work_item_id<1 OR expected_version IS NULL OR expected_version<1
  OR catalog_sku_id IS NULL OR catalog_sku_id<1 OR NOT public.production_exact_text(idempotency_key)
  OR NOT public.production_exact_text(reason) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
 END IF;
 RETURN convert_to('{"command":{"catalog_sku_id":'||catalog_sku_id::text||',"expected_version":'||expected_version::text
  ||',"idempotency_key":'||public.production_ascii_json_string(idempotency_key)
  ||',"reason":'||public.production_ascii_json_string(reason)||',"work_item_id":'||work_item_id::text||'},"schema_version":'||'1}','UTF8');
END $$;
"""

MODEL = """
CREATE TABLE public.production_work_items (
 work_item_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK(work_item_id>0),
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 order_id bigint NOT NULL CHECK(order_id>0), order_item_id bigint NOT NULL CHECK(order_item_id>0),
 source_item_version bigint NOT NULL CHECK(source_item_version>0),
 required_quantity integer NOT NULL CHECK(required_quantity>0),
 planned_quantity integer NOT NULL DEFAULT 0 CHECK(planned_quantity>=0 AND planned_quantity<=required_quantity),
 remaining_quantity integer GENERATED ALWAYS AS (required_quantity-planned_quantity) STORED,
 catalog_sku_id integer CHECK(catalog_sku_id>0), version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK(isfinite(created_at)),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK(isfinite(updated_at) AND updated_at>=created_at),
 current_assignment_receipt_id bigint CHECK(current_assignment_receipt_id>0),
 UNIQUE(organization_id,marketplace_account_id,order_item_id),
 UNIQUE(organization_id,marketplace_account_id,work_item_id),
 FOREIGN KEY(organization_id,marketplace_account_id,order_id,order_item_id)
  REFERENCES public.marketplace_order_items(organization_id,marketplace_account_id,order_id,order_item_id),
 FOREIGN KEY(organization_id,catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id)
);
CREATE INDEX production_remaining ON public.production_work_items(organization_id,marketplace_account_id) WHERE remaining_quantity>0;
CREATE TABLE public.production_assignment_receipts (
 receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK(receipt_id>0),
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 work_item_id bigint NOT NULL CHECK(work_item_id>0),
 idempotency_key text COLLATE "C" NOT NULL CHECK(public.production_exact_text(idempotency_key)),
 request_schema_version integer NOT NULL CHECK(request_schema_version=1), request_payload jsonb NOT NULL,
 canonical_request_bytes bytea NOT NULL,
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$' AND request_checksum=encode(sha256(canonical_request_bytes),'hex')),
 actor_membership_id integer NOT NULL CHECK(actor_membership_id>0),
 result_version bigint NOT NULL CHECK(result_version>1),
 result_schema_version integer NOT NULL CHECK(result_schema_version=1), result_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL CHECK(isfinite(created_at)),
 UNIQUE(organization_id,marketplace_account_id,work_item_id,receipt_id),
 UNIQUE(organization_id,marketplace_account_id,work_item_id,result_version),
 FOREIGN KEY(organization_id,marketplace_account_id,work_item_id)
  REFERENCES public.production_work_items(organization_id,marketplace_account_id,work_item_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 CHECK(canonical_request_bytes=public.production_assignment_bytes(work_item_id,result_version-1,
  (request_payload->>'catalog_sku_id')::integer,idempotency_key,request_payload->>'reason'))
);
CREATE TABLE public.production_assignment_history (
 assignment_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY CHECK(assignment_event_id>0),
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 work_item_id bigint NOT NULL CHECK(work_item_id>0), receipt_id bigint NOT NULL CHECK(receipt_id>0),
 actor_membership_id integer NOT NULL CHECK(actor_membership_id>0),
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind='manual_assignment'),
 from_version bigint NOT NULL CHECK(from_version>=1),
 to_version bigint NOT NULL CHECK(to_version>1 AND to_version=from_version+1),
 previous_catalog_sku_id integer CHECK(previous_catalog_sku_id>0),
 catalog_sku_id integer NOT NULL CHECK(catalog_sku_id>0),
 reason text COLLATE "C" NOT NULL CHECK(public.production_exact_text(reason)),
 occurred_at timestamptz NOT NULL CHECK(isfinite(occurred_at)),
 UNIQUE(organization_id,marketplace_account_id,receipt_id),
 UNIQUE(organization_id,marketplace_account_id,work_item_id,to_version),
 FOREIGN KEY(organization_id,marketplace_account_id,work_item_id,receipt_id)
  REFERENCES public.production_assignment_receipts(organization_id,marketplace_account_id,work_item_id,receipt_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 FOREIGN KEY(organization_id,previous_catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 FOREIGN KEY(organization_id,catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id)
);
ALTER TABLE public.production_work_items ADD CONSTRAINT production_current_receipt_fk
 FOREIGN KEY(organization_id,marketplace_account_id,work_item_id,current_assignment_receipt_id)
 REFERENCES public.production_assignment_receipts(organization_id,marketplace_account_id,work_item_id,receipt_id) DEFERRABLE INITIALLY DEFERRED;
"""

GUARDS = """
CREATE FUNCTION public.production_account_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='25000',MESSAGE='production_isolation_invalid';
 END IF;
 IF org IS NULL OR account IS NULL OR org !~ '^[1-9][0-9]{0,9}$' OR account !~ '^[1-9][0-9]{0,9}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_context_invalid';
 END IF;
 IF org::bigint>2147483647 OR account::bigint>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_context_invalid';
 END IF;
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer AND marketplace_account_id=account::integer FOR UPDATE;
 -- Must fail immediately: a following statement could see a newly inserted row
 -- without having retained its lock (0067 account replacement lesson).
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_account_missing'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_immutable';
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION public.production_row_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE source record; key text; value text; maximum text; payload jsonb; keys text[];
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_context_invalid';
 END IF;
 IF TG_TABLE_NAME='production_work_items' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.version<>1 OR NEW.catalog_sku_id IS NOT NULL OR NEW.planned_quantity<>0 OR NEW.current_assignment_receipt_id IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
   END IF;
   SELECT quantity,version INTO source FROM public.marketplace_order_items
    WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
     AND order_id=NEW.order_id AND order_item_id=NEW.order_item_id FOR SHARE;
   IF NOT FOUND OR source.quantity IS DISTINCT FROM NEW.required_quantity OR source.version IS DISTINCT FROM NEW.source_item_version THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_source_mismatch';
   END IF;
  ELSE
   IF ROW(NEW.work_item_id,NEW.organization_id,NEW.marketplace_account_id,NEW.order_id,NEW.order_item_id,
    NEW.source_item_version,NEW.required_quantity,NEW.planned_quantity,NEW.created_at)
    IS DISTINCT FROM ROW(OLD.work_item_id,OLD.organization_id,OLD.marketplace_account_id,OLD.order_id,OLD.order_item_id,
    OLD.source_item_version,OLD.required_quantity,OLD.planned_quantity,OLD.created_at)
    OR NEW.version<>OLD.version+1 OR NEW.catalog_sku_id IS NULL OR NEW.updated_at<OLD.updated_at
    OR NEW.current_assignment_receipt_id IS NULL OR NEW.current_assignment_receipt_id IS NOT DISTINCT FROM OLD.current_assignment_receipt_id THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
   END IF;
  END IF;
 ELSIF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_immutable';
 ELSIF TG_TABLE_NAME='production_assignment_receipts' THEN
  IF NEW.result_version IS NULL OR NEW.result_version<2 OR NEW.request_schema_version IS DISTINCT FROM 1
   OR NEW.result_schema_version IS DISTINCT FROM 1 THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
  -- Validate types and retained decimal representation BEFORE any CHECK cast.
  -- JSONB has already discarded some original exponent lexical information.
  FOREACH key IN ARRAY ARRAY['request_payload','result_payload'] LOOP
   payload:=CASE key WHEN 'request_payload' THEN NEW.request_payload ELSE NEW.result_payload END;
   IF jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
   END IF;
   SELECT array_agg(k ORDER BY k COLLATE "C") INTO keys FROM jsonb_object_keys(payload) k;
   IF keys IS DISTINCT FROM (CASE key WHEN 'request_payload'
    THEN ARRAY['catalog_sku_id','expected_version','idempotency_key','reason','work_item_id']
    ELSE ARRAY['catalog_sku_id','planned_quantity','remaining_quantity','required_quantity','source_item_version','version','work_item_id'] END) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
   END IF;
  END LOOP;
  FOREACH payload IN ARRAY ARRAY[NEW.request_payload,NEW.result_payload] LOOP
   FOR key IN SELECT jsonb_object_keys(payload) LOOP
    IF key IN ('idempotency_key','reason') THEN
     IF jsonb_typeof(payload->key) IS DISTINCT FROM 'string' OR NOT public.production_exact_text(payload->>key) THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
     END IF;
    ELSE
     value:=payload->>key;
     maximum:=CASE WHEN key IN ('catalog_sku_id','planned_quantity','required_quantity','remaining_quantity') THEN '2147483647' ELSE '9223372036854775807' END;
     IF jsonb_typeof(payload->key) IS DISTINCT FROM 'number' OR value IS NULL OR value !~ '^(0|[1-9][0-9]*)$'
      OR length(value)>length(maximum) OR (length(value)=length(maximum) AND value COLLATE "C">maximum COLLATE "C")
      OR (value='0' AND key NOT IN ('planned_quantity','remaining_quantity')) THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
     END IF;
    END IF;
   END LOOP;
  END LOOP;
  IF (NEW.request_payload->>'work_item_id')::bigint IS DISTINCT FROM NEW.work_item_id
   OR (NEW.request_payload->>'expected_version')::bigint IS DISTINCT FROM NEW.result_version-1
   OR NEW.request_payload->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
  IF EXISTS(SELECT 1 FROM public.production_assignment_receipts r WHERE r.organization_id=NEW.organization_id
    AND r.marketplace_account_id=NEW.marketplace_account_id AND r.work_item_id=NEW.work_item_id
    AND r.idempotency_key COLLATE "C"=NEW.idempotency_key COLLATE "C") THEN
   RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='production_idempotency_conflict';
  END IF;
 END IF;
 RETURN NEW;
END $$;
"""

VALIDATION = """
CREATE FUNCTION public.production_validate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE w public.production_work_items%ROWTYPE; r public.production_assignment_receipts%ROWTYPE;
 h public.production_assignment_history%ROWTYPE; s record; expected jsonb;
 previous_sku integer:=NULL; previous_time timestamptz; next_version bigint:=2;
BEGIN
 -- INSERT event captures its original source, even if this transaction assigns
 -- the new work item afterward. Later source changes do not refresh old items.
 IF TG_TABLE_NAME='production_work_items' AND TG_OP='INSERT' THEN
  SELECT quantity,version INTO s FROM public.marketplace_order_items WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND order_id=NEW.order_id AND order_item_id=NEW.order_item_id FOR SHARE;
  IF NOT FOUND OR s.quantity IS DISTINCT FROM NEW.required_quantity OR s.version IS DISTINCT FROM NEW.source_item_version THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_source_mismatch';
  END IF;
 END IF;
 IF TG_TABLE_NAME='production_work_items' AND TG_OP='UPDATE' THEN
  SELECT * INTO r FROM public.production_assignment_receipts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND work_item_id=NEW.work_item_id AND receipt_id=NEW.current_assignment_receipt_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid'; END IF;
  SELECT * INTO h FROM public.production_assignment_history WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND work_item_id=NEW.work_item_id AND receipt_id=r.receipt_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid'; END IF;
  expected:=jsonb_build_object('work_item_id',NEW.work_item_id,'version',NEW.version,'catalog_sku_id',NEW.catalog_sku_id,
   'required_quantity',NEW.required_quantity,'planned_quantity',NEW.planned_quantity,'remaining_quantity',NEW.remaining_quantity,'source_item_version',NEW.source_item_version);
  IF r.result_payload IS DISTINCT FROM expected OR r.result_version<>NEW.version OR (r.request_payload->>'expected_version')::bigint<>OLD.version
   OR (r.request_payload->>'catalog_sku_id')::integer<>NEW.catalog_sku_id OR r.created_at<>NEW.updated_at
   OR h.previous_catalog_sku_id IS DISTINCT FROM OLD.catalog_sku_id OR h.catalog_sku_id<>NEW.catalog_sku_id
   OR h.from_version<>OLD.version OR h.to_version<>NEW.version OR h.occurred_at<>NEW.updated_at
   OR h.actor_membership_id<>r.actor_membership_id OR h.reason COLLATE "C" IS DISTINCT FROM (r.request_payload->>'reason') COLLATE "C" THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
 END IF;
 SELECT * INTO w FROM public.production_work_items WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND work_item_id=NEW.work_item_id;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid'; END IF;
 IF (SELECT count(*) FROM public.production_assignment_receipts WHERE organization_id=w.organization_id AND marketplace_account_id=w.marketplace_account_id AND work_item_id=w.work_item_id)<>w.version-1
  OR (SELECT count(*) FROM public.production_assignment_history WHERE organization_id=w.organization_id AND marketplace_account_id=w.marketplace_account_id AND work_item_id=w.work_item_id)<>w.version-1 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
 END IF;
 IF w.version=1 THEN
  IF w.current_assignment_receipt_id IS NOT NULL OR w.catalog_sku_id IS NOT NULL THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
  RETURN NULL;
 END IF;
 previous_time:=w.created_at;
 FOR r IN SELECT * FROM public.production_assignment_receipts WHERE organization_id=w.organization_id
   AND marketplace_account_id=w.marketplace_account_id AND work_item_id=w.work_item_id ORDER BY result_version LOOP
  SELECT * INTO h FROM public.production_assignment_history WHERE organization_id=w.organization_id
   AND marketplace_account_id=w.marketplace_account_id AND work_item_id=w.work_item_id AND receipt_id=r.receipt_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid'; END IF;
  expected:=jsonb_build_object('work_item_id',w.work_item_id,'version',r.result_version,'catalog_sku_id',(r.request_payload->>'catalog_sku_id')::integer,
   'required_quantity',w.required_quantity,'planned_quantity',w.planned_quantity,'remaining_quantity',w.remaining_quantity,'source_item_version',w.source_item_version);
  IF r.result_version<>next_version OR r.result_version>w.version OR r.result_payload IS DISTINCT FROM expected
   OR h.from_version<>r.result_version-1 OR h.to_version<>r.result_version OR h.actor_membership_id<>r.actor_membership_id
   OR h.previous_catalog_sku_id IS DISTINCT FROM previous_sku OR h.catalog_sku_id<>(r.request_payload->>'catalog_sku_id')::integer
   OR h.reason COLLATE "C" IS DISTINCT FROM (r.request_payload->>'reason') COLLATE "C"
   OR h.occurred_at<>r.created_at OR r.created_at<previous_time
   OR (SELECT count(*) FROM public.production_assignment_receipts same WHERE same.organization_id=w.organization_id
    AND same.marketplace_account_id=w.marketplace_account_id AND same.work_item_id=w.work_item_id
    AND same.idempotency_key COLLATE "C"=r.idempotency_key COLLATE "C")<>1 THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
  IF r.result_version=w.version AND (w.current_assignment_receipt_id IS DISTINCT FROM r.receipt_id
    OR w.catalog_sku_id IS DISTINCT FROM h.catalog_sku_id OR w.updated_at<>r.created_at) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='production_assignment_invalid';
  END IF;
  previous_sku:=h.catalog_sku_id; previous_time:=r.created_at;
  -- No overflow at BIGINT_MAX: only increment when another version is possible.
  IF next_version<w.version THEN next_version:=next_version+1; END IF;
 END LOOP;
 RETURN NULL;
END $$;
"""

# Only the new objects are narrowed; old/default ACLs remain byte-for-byte intact.
ACL = """
DO $$ DECLARE t record; a record; col record; f record; grantee text; allowed text[]; rights text;
BEGIN
 FOR t IN SELECT oid,relname,relowner,relkind,relacl FROM pg_class WHERE relnamespace='public'::regnamespace
  AND (relname IN ('production_work_items','production_assignment_receipts','production_assignment_history')
   OR oid IN (SELECT d.objid FROM pg_depend d JOIN pg_class p ON p.oid=d.refobjid
    WHERE d.classid='pg_class'::regclass AND d.deptype='i' AND p.relnamespace='public'::regnamespace
     AND p.relname IN ('production_work_items','production_assignment_receipts','production_assignment_history'))) LOOP
  allowed:=CASE WHEN t.relkind='S' THEN ARRAY['USAGE'] WHEN t.relname='production_work_items' THEN ARRAY['SELECT','INSERT','UPDATE'] ELSE ARRAY['SELECT','INSERT'] END;
  FOR a IN SELECT x.grantee,array_agg(x.privilege_type) AS privileges FROM aclexplode(t.relacl) x
   WHERE x.grantee<>t.relowner GROUP BY x.grantee LOOP
   grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON %s public.%I FROM %s',CASE WHEN t.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,t.relname,grantee);
   SELECT string_agg(p,',') INTO rights FROM unnest(a.privileges) p WHERE p=ANY(allowed);
   IF a.grantee<>0 AND rights IS NOT NULL THEN
    EXECUTE format('GRANT %s ON %s public.%I TO %s',rights,CASE WHEN t.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,t.relname,grantee);
   END IF;
  END LOOP;
  IF t.relkind<>'S' THEN
   FOR col IN SELECT attrib.attname,x.grantee,array_agg(x.privilege_type) privileges FROM pg_attribute attrib
    CROSS JOIN LATERAL aclexplode(attrib.attacl) x WHERE attrib.attrelid=t.oid AND x.grantee<>t.relowner GROUP BY attrib.attname,x.grantee LOOP
    grantee:=CASE WHEN col.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(col.grantee)) END;
    EXECUTE format('REVOKE ALL (%I) ON TABLE public.%I FROM %s',col.attname,t.relname,grantee);
    IF col.grantee<>0 THEN
     FOREACH rights IN ARRAY col.privileges LOOP
      IF rights=ANY(allowed) THEN EXECUTE format('GRANT %s (%I) ON TABLE public.%I TO %s',rights,col.attname,t.relname,grantee); END IF;
     END LOOP;
    END IF;
   END LOOP;
  END IF;
  EXECUTE format('REVOKE ALL ON %s public.%I FROM PUBLIC',CASE WHEN t.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,t.relname);
 END LOOP;
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc
  WHERE pronamespace='public'::regnamespace AND proname IN
   ('production_exact_text','production_ascii_json_string','production_assignment_bytes','production_account_lock','production_row_guard','production_validate') LOOP
  FOR a IN SELECT DISTINCT x.grantee FROM aclexplode(f.proacl) x WHERE x.grantee<>f.proowner LOOP
   grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,grantee);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
 -- Restore pure helper evaluation for surviving table/column grantees only.
 -- Include relation owners: objects may have distinct explicitly assigned owners.
 FOR a IN SELECT DISTINCT entitled.grantee FROM (
  SELECT c.relowner grantee FROM pg_class c WHERE c.relnamespace='public'::regnamespace
   AND c.relname IN ('production_work_items','production_assignment_receipts','production_assignment_history')
  UNION
  SELECT x.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
   WHERE c.relnamespace='public'::regnamespace AND c.relname IN ('production_work_items','production_assignment_receipts','production_assignment_history')
  UNION
  SELECT x.grantee FROM pg_class c JOIN pg_attribute at ON at.attrelid=c.oid CROSS JOIN LATERAL aclexplode(at.attacl) x
   WHERE c.relnamespace='public'::regnamespace AND c.relname IN ('production_work_items','production_assignment_receipts','production_assignment_history')
 ) entitled WHERE entitled.grantee<>0 LOOP
  FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
   AND proname IN ('production_exact_text','production_ascii_json_string','production_assignment_bytes') LOOP
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,pg_get_userbyid(a.grantee));
  END LOOP;
 END LOOP;
END $$;
"""


def upgrade():
    op.execute(HELPERS)
    op.execute(MODEL)
    op.execute(GUARDS)
    op.execute(VALIDATION)
    for table in TABLES:
        op.execute(
            f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY"
        )
        # Comparing canonical stored IDs as text avoids unsafe GUC casts entirely.
        predicate = 'organization_id::text COLLATE "C"=current_setting(\'app.organization_id\',true) COLLATE "C" AND marketplace_account_id::text COLLATE "C"=current_setting(\'app.marketplace_account_id\',true) COLLATE "C"'
        op.execute(
            f"CREATE POLICY production_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(
            f"CREATE TRIGGER production_account_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.production_account_lock()"
        )
        op.execute(
            f"CREATE TRIGGER production_guard BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.production_row_guard()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER production_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.production_validate()"
        )
    op.execute(ACL)


def downgrade():
    op.execute(
        "LOCK TABLE "
        + ",".join("public." + table for table in TABLES)
        + " IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security=off")
    for table in TABLES:
        op.execute(
            f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='production_downgrade_nonempty'; END IF; END $$"
        )
    for table in TABLES:
        op.execute(
            f"DROP TRIGGER production_witness ON public.{table}; DROP TRIGGER production_guard ON public.{table}; DROP TRIGGER production_account_first ON public.{table}"
        )
    for function in TRIGGERS:
        op.execute("DROP FUNCTION public." + function)
    op.execute(
        "ALTER TABLE public.production_work_items DROP CONSTRAINT production_current_receipt_fk"
    )
    for table in reversed(TABLES):
        op.execute("DROP TABLE public." + table)
    for function in reversed(PURE):
        op.execute("DROP FUNCTION public." + function)
