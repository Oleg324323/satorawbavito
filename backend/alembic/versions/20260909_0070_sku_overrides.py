"""Account-scoped exact SKU override revisions, CAS heads and audit witnesses.

Trusted consumers own authentication, live offer mapping and publication guards.
All writes participate in one READ COMMITTED root after the exact account lock.
"""

from alembic import op

revision = "20260909_0070"
down_revision = "20260909_0069"
branch_labels = depends_on = None

TABLES = (
    "wb_repricing_sku_override_versions",
    "wb_repricing_sku_override_heads",
    "wb_repricing_sku_override_audit",
)
PURE = (
    "wb_sku_override_decimal(numeric)",
    "wb_sku_override_integral(numeric)",
    "wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)",
)
TRIGGERS = (
    "wb_sku_override_account_lock()",
    "wb_sku_override_row_guard()",
    "wb_sku_override_validate()",
)

HELPERS = r"""
CREATE FUNCTION public.wb_sku_override_decimal(value numeric) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE rendered text;
BEGIN
 IF value IS NULL OR value::text IN ('NaN','Infinity','-Infinity') THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
 END IF;
 IF value=0 THEN RETURN '0'; END IF;
 rendered:=value::text;
 IF strpos(rendered,'.')>0 THEN rendered:=rtrim(rtrim(rendered,'0'),'.'); END IF;
 RETURN rendered;
END $$;
CREATE FUNCTION public.wb_sku_override_integral(value numeric) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL OR value::text IN ('NaN','Infinity','-Infinity') THEN RETURN false; END IF;
 RETURN value=trunc(value);
END $$;
CREATE FUNCTION public.wb_sku_override_bytes(
 organization_id integer,marketplace_account_id integer,catalog_sku_id integer,
 actor_membership_id integer,command_id uuid,expected_version numeric,values_row jsonb
) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE keys text[]; entry record; kind text; rendered text; number numeric;
 nested text:=''; separator text:='';
BEGIN
 IF organization_id IS NULL OR organization_id<1 OR marketplace_account_id IS NULL OR marketplace_account_id<1
  OR catalog_sku_id IS NULL OR catalog_sku_id<1 OR actor_membership_id IS NULL OR actor_membership_id<1
  OR command_id IS NULL OR command_id::text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  OR NOT public.wb_sku_override_integral(expected_version) OR expected_version<0
  OR jsonb_typeof(values_row) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
 END IF;
 SELECT array_agg(key ORDER BY key COLLATE "C") INTO keys FROM jsonb_object_keys(values_row) key;
 IF keys IS DISTINCT FROM ARRAY['allow_negative_margin','automation_enabled','basket_norm_manual','basket_norm_mode',
  'max_margin_kopecks','max_margin_pct','min_margin_kopecks','min_margin_pct','night_median_enabled',
  'p_max_kopecks','p_min_kopecks','price_step_minutes','price_step_pct','rrp_kopecks'] THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
 END IF;
 FOR entry IN SELECT key,value FROM jsonb_each(values_row) ORDER BY key COLLATE "C" LOOP
  kind:=jsonb_typeof(entry.value);
  IF kind='null' THEN rendered:='null';
  ELSIF entry.key IN ('automation_enabled','allow_negative_margin','night_median_enabled') THEN
   IF kind<>'boolean' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
   rendered:=entry.value::text;
  ELSIF entry.key='basket_norm_mode' THEN
   IF kind<>'string' OR (entry.value#>>'{}') NOT IN ('auto','manual','fallback_by_type') THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
   END IF;
   rendered:=entry.value::text;
  ELSE
   IF kind<>'number' THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
   number:=(entry.value#>>'{}')::numeric;
   rendered:=public.wb_sku_override_decimal(number);
   IF entry.key IN ('price_step_minutes','basket_norm_manual') THEN
    IF NOT public.wb_sku_override_integral(number) OR number>2147483647
     OR number<(CASE WHEN entry.key='price_step_minutes' THEN 1 ELSE 0 END) THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
    END IF;
   ELSE
    IF entry.key IN ('p_min_kopecks','p_max_kopecks','rrp_kopecks','min_margin_kopecks','max_margin_kopecks')
     AND (NOT public.wb_sku_override_integral(number) OR number<0) THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
    END IF;
    rendered:=to_json(rendered)::text;
   END IF;
  END IF;
  nested:=nested||separator||to_json(entry.key)::text||':'||rendered;
  separator:=',';
 END LOOP;
 RETURN convert_to('{"actorMembershipId":'||actor_membership_id::text||',"catalogSkuId":'||catalog_sku_id::text
  ||',"commandId":'||to_json(command_id::text)::text||',"commandKind":"replace_overrides","expectedVersion":'
  ||to_json(public.wb_sku_override_decimal(expected_version))::text
  ||',"marketplaceAccountId":'||marketplace_account_id::text||',"organizationId":'||organization_id::text
  ||',"schema":"wb-repricing-sku-overrides/v1","values":{'||nested||'}}','UTF8');
END $$;
"""

MODEL = """
CREATE TABLE public.wb_repricing_sku_override_versions (
 organization_id integer NOT NULL,
 marketplace_account_id integer NOT NULL,
 catalog_sku_id integer NOT NULL,
 marketplace text NOT NULL,
 revision numeric NOT NULL,
 parent_revision numeric,
 command_id uuid NOT NULL,
 actor_membership_id integer NOT NULL,
 created_at timestamptz NOT NULL,
 canonical_request_bytes bytea NOT NULL,
 request_checksum text COLLATE "C" NOT NULL,
 automation_enabled boolean,
 allow_negative_margin boolean,
 night_median_enabled boolean,
 p_min_kopecks numeric,
 p_max_kopecks numeric,
 rrp_kopecks numeric,
 min_margin_kopecks numeric,
 max_margin_kopecks numeric,
 min_margin_pct numeric,
 max_margin_pct numeric,
 price_step_pct numeric,
 price_step_minutes integer,
 basket_norm_manual integer,
 basket_norm_mode text COLLATE "C",
 CONSTRAINT wb_sku_override_v_pk PRIMARY KEY(organization_id,marketplace_account_id,catalog_sku_id,revision),
 CONSTRAINT wb_sku_override_v_command UNIQUE(organization_id,marketplace_account_id,catalog_sku_id,command_id),
 CONSTRAINT wb_sku_override_v_owner CHECK(organization_id>0 AND marketplace_account_id>0 AND catalog_sku_id>0 AND actor_membership_id>0),
 CONSTRAINT wb_sku_override_v_provider CHECK(marketplace='wb'),
 CONSTRAINT wb_sku_override_v_revision CHECK(public.wb_sku_override_integral(revision) AND revision>0),
 CONSTRAINT wb_sku_override_v_parent CHECK((revision=1 AND parent_revision IS NULL) OR (revision>1 AND parent_revision IS NOT NULL AND parent_revision=revision-1)),
 CONSTRAINT wb_sku_override_v_uuid CHECK(command_id::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
 CONSTRAINT wb_sku_override_v_time CHECK(isfinite(created_at)),
 CONSTRAINT wb_sku_override_v_checksum CHECK(request_checksum ~ '^[0-9a-f]{64}$' AND request_checksum=encode(sha256(canonical_request_bytes),'hex')),
 CONSTRAINT wb_sku_override_v_money CHECK(
  (p_min_kopecks IS NULL OR (public.wb_sku_override_integral(p_min_kopecks) AND p_min_kopecks>=0)) AND
  (p_max_kopecks IS NULL OR (public.wb_sku_override_integral(p_max_kopecks) AND p_max_kopecks>=0)) AND
  (rrp_kopecks IS NULL OR (public.wb_sku_override_integral(rrp_kopecks) AND rrp_kopecks>=0)) AND
  (min_margin_kopecks IS NULL OR (public.wb_sku_override_integral(min_margin_kopecks) AND min_margin_kopecks>=0)) AND
  (max_margin_kopecks IS NULL OR (public.wb_sku_override_integral(max_margin_kopecks) AND max_margin_kopecks>=0))),
 CONSTRAINT wb_sku_override_v_percent CHECK(
  (min_margin_pct IS NULL OR min_margin_pct::text NOT IN ('NaN','Infinity','-Infinity')) AND
  (max_margin_pct IS NULL OR max_margin_pct::text NOT IN ('NaN','Infinity','-Infinity')) AND
  (price_step_pct IS NULL OR price_step_pct::text NOT IN ('NaN','Infinity','-Infinity'))),
 CONSTRAINT wb_sku_override_v_minutes CHECK(price_step_minutes IS NULL OR price_step_minutes>0),
 CONSTRAINT wb_sku_override_v_manual CHECK(basket_norm_manual IS NULL OR basket_norm_manual>=0),
 CONSTRAINT wb_sku_override_v_mode CHECK(basket_norm_mode IS NULL OR basket_norm_mode IN ('auto','manual','fallback_by_type')),
 CONSTRAINT wb_sku_override_v_bytes CHECK(canonical_request_bytes=public.wb_sku_override_bytes(
  organization_id,marketplace_account_id,catalog_sku_id,actor_membership_id,command_id,revision-1,
  jsonb_build_object('automation_enabled',automation_enabled,'allow_negative_margin',allow_negative_margin,
   'night_median_enabled',night_median_enabled,'p_min_kopecks',p_min_kopecks,'p_max_kopecks',p_max_kopecks,
   'rrp_kopecks',rrp_kopecks,'min_margin_kopecks',min_margin_kopecks,'max_margin_kopecks',max_margin_kopecks,
   'min_margin_pct',min_margin_pct,'max_margin_pct',max_margin_pct,'price_step_pct',price_step_pct,
   'price_step_minutes',price_step_minutes,'basket_norm_manual',basket_norm_manual,'basket_norm_mode',basket_norm_mode))),
 CONSTRAINT wb_sku_override_v_account_fk FOREIGN KEY(organization_id,marketplace_account_id,marketplace)
  REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 CONSTRAINT wb_sku_override_v_sku_fk FOREIGN KEY(organization_id,catalog_sku_id)
  REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 CONSTRAINT wb_sku_override_v_actor_fk FOREIGN KEY(organization_id,actor_membership_id)
  REFERENCES public.iam_memberships(organization_id,membership_id),
 CONSTRAINT wb_sku_override_v_parent_fk FOREIGN KEY(organization_id,marketplace_account_id,catalog_sku_id,parent_revision)
  REFERENCES public.wb_repricing_sku_override_versions(organization_id,marketplace_account_id,catalog_sku_id,revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE public.wb_repricing_sku_override_heads (
 organization_id integer NOT NULL,
 marketplace_account_id integer NOT NULL,
 catalog_sku_id integer NOT NULL,
 current_revision numeric NOT NULL,
 version numeric NOT NULL,
 updated_at timestamptz NOT NULL,
 CONSTRAINT wb_sku_override_h_pk PRIMARY KEY(organization_id,marketplace_account_id,catalog_sku_id),
 CONSTRAINT wb_sku_override_h_owner CHECK(organization_id>0 AND marketplace_account_id>0 AND catalog_sku_id>0),
 CONSTRAINT wb_sku_override_h_version CHECK(public.wb_sku_override_integral(version) AND version>0 AND version=current_revision),
 CONSTRAINT wb_sku_override_h_time CHECK(isfinite(updated_at)),
 CONSTRAINT wb_sku_override_h_account_fk FOREIGN KEY(organization_id,marketplace_account_id)
  REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id),
 CONSTRAINT wb_sku_override_h_sku_fk FOREIGN KEY(organization_id,catalog_sku_id)
  REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 CONSTRAINT wb_sku_override_h_current_fk FOREIGN KEY(organization_id,marketplace_account_id,catalog_sku_id,current_revision)
  REFERENCES public.wb_repricing_sku_override_versions(organization_id,marketplace_account_id,catalog_sku_id,revision) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE public.wb_repricing_sku_override_versions ADD CONSTRAINT wb_sku_override_v_head_fk
 FOREIGN KEY(organization_id,marketplace_account_id,catalog_sku_id)
 REFERENCES public.wb_repricing_sku_override_heads(organization_id,marketplace_account_id,catalog_sku_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE public.wb_repricing_sku_override_audit (
 organization_id integer NOT NULL,
 marketplace_account_id integer NOT NULL,
 catalog_sku_id integer NOT NULL,
 before_revision numeric,
 after_revision numeric NOT NULL,
 command_id uuid NOT NULL,
 actor_membership_id integer NOT NULL,
 event_kind text COLLATE "C" NOT NULL,
 occurred_at timestamptz NOT NULL,
 CONSTRAINT wb_sku_override_a_pk PRIMARY KEY(organization_id,marketplace_account_id,catalog_sku_id,after_revision),
 CONSTRAINT wb_sku_override_a_command UNIQUE(organization_id,marketplace_account_id,catalog_sku_id,command_id),
 CONSTRAINT wb_sku_override_a_owner CHECK(organization_id>0 AND marketplace_account_id>0 AND catalog_sku_id>0 AND actor_membership_id>0),
 CONSTRAINT wb_sku_override_a_revision CHECK(public.wb_sku_override_integral(after_revision) AND after_revision>0
  AND after_revision=coalesce(before_revision,0)+1 AND (before_revision IS NULL OR (public.wb_sku_override_integral(before_revision) AND before_revision>0))),
 CONSTRAINT wb_sku_override_a_event CHECK((before_revision IS NULL AND event_kind='created') OR (before_revision IS NOT NULL AND event_kind='replaced')),
 CONSTRAINT wb_sku_override_a_uuid CHECK(command_id::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
 CONSTRAINT wb_sku_override_a_time CHECK(isfinite(occurred_at)),
 CONSTRAINT wb_sku_override_a_account_fk FOREIGN KEY(organization_id,marketplace_account_id)
  REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id),
 CONSTRAINT wb_sku_override_a_sku_fk FOREIGN KEY(organization_id,catalog_sku_id)
  REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 CONSTRAINT wb_sku_override_a_actor_fk FOREIGN KEY(organization_id,actor_membership_id)
  REFERENCES public.iam_memberships(organization_id,membership_id),
 CONSTRAINT wb_sku_override_a_before_fk FOREIGN KEY(organization_id,marketplace_account_id,catalog_sku_id,before_revision)
  REFERENCES public.wb_repricing_sku_override_versions(organization_id,marketplace_account_id,catalog_sku_id,revision) DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT wb_sku_override_a_after_fk FOREIGN KEY(organization_id,marketplace_account_id,catalog_sku_id,after_revision)
  REFERENCES public.wb_repricing_sku_override_versions(organization_id,marketplace_account_id,catalog_sku_id,revision) DEFERRABLE INITIALLY DEFERRED
);
"""

GUARDS = """
CREATE FUNCTION public.wb_sku_override_account_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='25000',MESSAGE='wb_sku_override_isolation_invalid';
 END IF;
 IF org IS NULL OR account IS NULL OR org !~ '^[1-9][0-9]{0,9}$' OR account !~ '^[1-9][0-9]{0,9}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_context_invalid';
 END IF;
 IF org::bigint>2147483647 OR account::bigint>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_context_invalid';
 END IF;
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer
  AND marketplace_account_id=account::integer AND marketplace='wb' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_account_missing'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') OR (TG_OP='UPDATE' AND TG_TABLE_NAME<>'wb_repricing_sku_override_heads') THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_immutable';
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.wb_sku_override_row_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_context_invalid';
 END IF;
 IF TG_TABLE_NAME='wb_repricing_sku_override_heads' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.version IS DISTINCT FROM 1::numeric OR NEW.current_revision IS DISTINCT FROM 1::numeric THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
   END IF;
  ELSE
   IF ROW(NEW.organization_id,NEW.marketplace_account_id,NEW.catalog_sku_id)
    IS DISTINCT FROM ROW(OLD.organization_id,OLD.marketplace_account_id,OLD.catalog_sku_id)
    OR NEW.version IS DISTINCT FROM OLD.version+1 OR NEW.current_revision IS DISTINCT FROM OLD.current_revision+1
    OR NEW.updated_at<OLD.updated_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
   END IF;
  END IF;
 ELSIF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_immutable';
 END IF;
 RETURN NEW;
END $$;
"""

VALIDATION = """
CREATE FUNCTION public.wb_sku_override_validate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE h public.wb_repricing_sku_override_heads%ROWTYPE;
 v public.wb_repricing_sku_override_versions%ROWTYPE;
 a public.wb_repricing_sku_override_audit%ROWTYPE;
 previous_revision numeric:=NULL; previous_time timestamptz:=NULL;
BEGIN
 -- Captured owner must remain in the trusted transaction scope. In particular,
 -- changing a GUC may not hide and silently skip an earlier owner's witnesses.
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_context_invalid';
 END IF;
 IF TG_TABLE_NAME='wb_repricing_sku_override_heads' THEN
  SELECT * INTO v FROM public.wb_repricing_sku_override_versions
   WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND catalog_sku_id=NEW.catalog_sku_id AND revision=NEW.current_revision;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
  SELECT * INTO a FROM public.wb_repricing_sku_override_audit
   WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND catalog_sku_id=NEW.catalog_sku_id AND after_revision=NEW.current_revision;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
  IF TG_OP='UPDATE' THEN
   previous_revision:=OLD.version; previous_time:=OLD.updated_at;
  END IF;
  IF v.revision IS DISTINCT FROM NEW.version OR v.parent_revision IS DISTINCT FROM previous_revision
   OR v.created_at IS DISTINCT FROM NEW.updated_at OR a.occurred_at IS DISTINCT FROM NEW.updated_at
   OR a.before_revision IS DISTINCT FROM previous_revision OR a.actor_membership_id IS DISTINCT FROM v.actor_membership_id
   OR a.command_id IS DISTINCT FROM v.command_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
  END IF;
  IF TG_OP='UPDATE' AND NOT EXISTS(SELECT 1 FROM public.wb_repricing_sku_override_versions
   WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND catalog_sku_id=NEW.catalog_sku_id AND revision=previous_revision AND created_at=previous_time) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
  END IF;
 END IF;
 SELECT * INTO h FROM public.wb_repricing_sku_override_heads
  WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND catalog_sku_id=NEW.catalog_sku_id;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
 IF (SELECT count(*) FROM public.wb_repricing_sku_override_versions WHERE organization_id=h.organization_id
    AND marketplace_account_id=h.marketplace_account_id AND catalog_sku_id=h.catalog_sku_id)<>h.version
  OR (SELECT count(*) FROM public.wb_repricing_sku_override_audit WHERE organization_id=h.organization_id
    AND marketplace_account_id=h.marketplace_account_id AND catalog_sku_id=h.catalog_sku_id)<>h.version
  OR (SELECT max(revision) FROM public.wb_repricing_sku_override_versions WHERE organization_id=h.organization_id
    AND marketplace_account_id=h.marketplace_account_id AND catalog_sku_id=h.catalog_sku_id) IS DISTINCT FROM h.version THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
 END IF;
 previous_revision:=NULL; previous_time:=NULL;
 FOR v IN SELECT * FROM public.wb_repricing_sku_override_versions WHERE organization_id=h.organization_id
  AND marketplace_account_id=h.marketplace_account_id AND catalog_sku_id=h.catalog_sku_id ORDER BY revision LOOP
  SELECT * INTO a FROM public.wb_repricing_sku_override_audit WHERE organization_id=v.organization_id
   AND marketplace_account_id=v.marketplace_account_id AND catalog_sku_id=v.catalog_sku_id AND after_revision=v.revision;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid'; END IF;
  IF v.revision<>coalesce(previous_revision,0)+1 OR v.parent_revision IS DISTINCT FROM previous_revision
   OR a.before_revision IS DISTINCT FROM v.parent_revision OR a.command_id IS DISTINCT FROM v.command_id
   OR a.actor_membership_id IS DISTINCT FROM v.actor_membership_id OR a.occurred_at IS DISTINCT FROM v.created_at
   OR a.event_kind IS DISTINCT FROM (CASE WHEN previous_revision IS NULL THEN 'created' ELSE 'replaced' END)
   OR (previous_time IS NOT NULL AND v.created_at<previous_time) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
  END IF;
  previous_revision:=v.revision; previous_time:=v.created_at;
 END LOOP;
 IF previous_revision IS DISTINCT FROM h.current_revision OR previous_time IS DISTINCT FROM h.updated_at THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
 END IF;
 RETURN NULL;
END $$;
"""

# Intersect only these new relation/column/function ACLs. Default and old ACLs
# remain intact, including readers' narrower inherited entitlements.
ACL = """
DO $$ DECLARE t record; a record; col record; f record; grantee text; allowed text[]; rights text;
BEGIN
 FOR t IN SELECT oid,relname,relowner,relacl FROM pg_class WHERE relnamespace='public'::regnamespace
  AND relname IN ('wb_repricing_sku_override_versions','wb_repricing_sku_override_heads','wb_repricing_sku_override_audit') LOOP
  allowed:=CASE WHEN t.relname='wb_repricing_sku_override_heads' THEN ARRAY['SELECT','INSERT','UPDATE'] ELSE ARRAY['SELECT','INSERT'] END;
  FOR a IN SELECT x.grantee,array_agg(x.privilege_type) privileges FROM aclexplode(t.relacl) x
   WHERE x.grantee<>t.relowner GROUP BY x.grantee LOOP
   grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s',t.relname,grantee);
   SELECT string_agg(p,',') INTO rights FROM unnest(a.privileges) p WHERE p=ANY(allowed);
   IF a.grantee<>0 AND rights IS NOT NULL THEN
    EXECUTE format('GRANT %s ON TABLE public.%I TO %s',rights,t.relname,grantee);
   END IF;
  END LOOP;
  FOR col IN SELECT at.attname,x.grantee,array_agg(x.privilege_type) privileges FROM pg_attribute at
   CROSS JOIN LATERAL aclexplode(at.attacl) x WHERE at.attrelid=t.oid AND x.grantee<>t.relowner GROUP BY at.attname,x.grantee LOOP
   grantee:=CASE WHEN col.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(col.grantee)) END;
   EXECUTE format('REVOKE ALL (%I) ON TABLE public.%I FROM %s',col.attname,t.relname,grantee);
   IF col.grantee<>0 THEN
    FOREACH rights IN ARRAY col.privileges LOOP
     IF rights=ANY(allowed) THEN EXECUTE format('GRANT %s (%I) ON TABLE public.%I TO %s',rights,col.attname,t.relname,grantee); END IF;
    END LOOP;
   END IF;
  END LOOP;
  EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',t.relname);
 END LOOP;
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('wb_sku_override_decimal','wb_sku_override_integral','wb_sku_override_bytes',
   'wb_sku_override_account_lock','wb_sku_override_row_guard','wb_sku_override_validate') LOOP
  FOR a IN SELECT DISTINCT x.grantee FROM aclexplode(f.proacl) x WHERE x.grantee<>f.proowner LOOP
   grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,grantee);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
 FOR a IN SELECT DISTINCT entitled.grantee FROM (
  SELECT c.relowner grantee FROM pg_class c WHERE c.relnamespace='public'::regnamespace
   AND c.relname IN ('wb_repricing_sku_override_versions','wb_repricing_sku_override_heads','wb_repricing_sku_override_audit')
  UNION
  SELECT x.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x WHERE c.relnamespace='public'::regnamespace
   AND c.relname IN ('wb_repricing_sku_override_versions','wb_repricing_sku_override_heads','wb_repricing_sku_override_audit')
  UNION
  SELECT x.grantee FROM pg_class c JOIN pg_attribute at ON at.attrelid=c.oid CROSS JOIN LATERAL aclexplode(at.attacl) x
   WHERE c.relnamespace='public'::regnamespace
   AND c.relname IN ('wb_repricing_sku_override_versions','wb_repricing_sku_override_heads','wb_repricing_sku_override_audit')
 ) entitled WHERE entitled.grantee<>0 LOOP
  FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
   AND proname IN ('wb_sku_override_decimal','wb_sku_override_integral','wb_sku_override_bytes') LOOP
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
        predicate = 'organization_id::text COLLATE "C"=current_setting(\'app.organization_id\',true) COLLATE "C" AND marketplace_account_id::text COLLATE "C"=current_setting(\'app.marketplace_account_id\',true) COLLATE "C"'
        op.execute(
            f"CREATE POLICY wb_sku_override_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(
            f"CREATE TRIGGER wb_sku_override_account_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.wb_sku_override_account_lock()"
        )
        op.execute(
            f"CREATE TRIGGER wb_sku_override_guard BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_sku_override_row_guard()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER wb_sku_override_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wb_sku_override_validate()"
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
            f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='wb_sku_override_downgrade_nonempty'; END IF; END $$"
        )
    for table in TABLES:
        op.execute(
            f"DROP TRIGGER wb_sku_override_witness ON public.{table}; DROP TRIGGER wb_sku_override_guard ON public.{table}; DROP TRIGGER wb_sku_override_account_first ON public.{table}"
        )
    for function in TRIGGERS:
        op.execute("DROP FUNCTION public." + function)
    op.execute(
        "ALTER TABLE public.wb_repricing_sku_override_versions DROP CONSTRAINT wb_sku_override_v_head_fk"
    )
    for table in reversed(TABLES):
        op.execute("DROP TABLE public." + table)
    for function in reversed(PURE):
        op.execute("DROP FUNCTION public." + function)
