"""Separate immutable WB current seller-price and warehouse-stock source families.

Source storage only. A trusted, credential-bound collector and its live origin
authorization must precede these transactions. SQL actor labels are not authority.
"""

from alembic import op

revision = "20260909_0077"
down_revision = "20260909_0076"
branch_labels = depends_on = None

FAMILIES = ("price", "stock")
TABLES = (
    "wb_price_runs", "wb_price_pages", "wb_price_product_facts",
    "wb_price_size_facts", "wb_price_current_heads", "wb_price_audit",
    "wb_stock_runs", "wb_stock_pages", "wb_stock_observations",
    "wb_stock_current_heads", "wb_stock_audit",
)
PURE = (
    "wb_current_uuid(uuid)", "wb_current_time(timestamptz)",
    "wb_current_presence(text,bigint)",
    "wb_current_request_bytes(integer,integer,text,text,bigint)",
    "wb_price_manifest_bytes(public.wb_price_runs,public.wb_price_pages[])",
    "wb_stock_manifest_bytes(public.wb_stock_runs,public.wb_stock_pages[])",
)
TRIGGERS = ("wb_current_account_lock()", "wb_current_immutable()") + tuple(
    f"wb_{family}_{name}()" for family in FAMILIES
    for name in ("run_guard", "child_guard", "head_guard", "audit_guard", "emit_audit", "graph")
)

HELPERS = r"""
CREATE FUNCTION public.wb_current_uuid(v uuid) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND v::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' $$;
CREATE FUNCTION public.wb_current_time(v timestamptz) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND isfinite(v) AND v>='0001-01-01 00:00:00+00'::timestamptz
 AND v<'10000-01-01 00:00:00+00'::timestamptz $$;
CREATE FUNCTION public.wb_current_presence(p text, v bigint) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT coalesce((p COLLATE "C"='value' AND v IS NOT NULL AND v>=0)
 OR (p COLLATE "C" IN ('missing','null') AND v IS NULL),false) $$;
CREATE FUNCTION public.wb_current_request_bytes(org integer, account integer,
 kind text, parser text, page_limit bigint) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE body text; path text;
BEGIN
 IF org IS NULL OR org<=0 OR account IS NULL OR account<=0
 OR page_limit IS NULL OR page_limit<=0 OR kind IS NULL OR parser IS NULL THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_request_invalid'; END IF;
 IF kind COLLATE "C"='wb_goods_prices_v1' AND parser COLLATE "C"='wb-goods-prices/v1' THEN
  body:='{}'; path:='/api/v2/list/goods/filter';
 ELSIF kind COLLATE "C"='wb_warehouse_v1' AND parser COLLATE "C"='wb-warehouse-stocks/v1' THEN
  body:='{"stockType":"wb"}'; path:='/api/analytics/v1/stocks-report/wb-warehouses';
 ELSE RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_request_invalid'; END IF;
 -- Closed ASCII envelope: no JSONB serialization, filters, offsets or credentials.
 RETURN convert_to('{"accountId":'||account::text||',"body":'||body
 ||',"method":"POST","organizationId":'||org::text||',"pageLimit":'||page_limit::text
 ||',"parserVersion":"'||parser||'","path":"'||path
 ||'","query":{},"schema":"wb-source-collection/v1","sourceKind":"'||kind||'"}','UTF8');
END $$;
CREATE FUNCTION public.wb_current_account_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed'
 OR org IS NULL OR account IS NULL OR org !~ '^[1-9][0-9]{0,9}$'
 OR account !~ '^[1-9][0-9]{0,9}$' OR org::numeric>2147483647
 OR account::numeric>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_context_invalid'; END IF;
 -- Statement trigger precedes run/head tuple locks. Live origin checks belong
 -- before this account lock in the caller's guarded root transaction.
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer
 AND marketplace_account_id=account::integer AND marketplace COLLATE "C"='wb' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_binding_changed'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.wb_current_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_history_immutable';
END $$;
"""


def owner_columns():
    return """
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 marketplace text COLLATE "C" NOT NULL CHECK(marketplace='wb'),
 FOREIGN KEY(organization_id,marketplace_account_id,marketplace)
 REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
"""


def run_reference(family):
    return f"""
 run_id uuid NOT NULL,
 FOREIGN KEY(organization_id,marketplace_account_id,run_id)
 REFERENCES public.wb_{family}_runs(organization_id,marketplace_account_id,run_id),
"""


def presence(name, upper=None):
    return f"""
 {name}_presence text COLLATE "C" NOT NULL,
 {name} bigint,
 CHECK(public.wb_current_presence({name}_presence,{name})),
 {f'CHECK({name} IS NULL OR {name}<={upper}),' if upper is not None else ''}
"""


def models():
    for family in FAMILIES:
        kind = "wb_goods_prices_v1" if family == "price" else "wb_warehouse_v1"
        parser = "wb-goods-prices/v1" if family == "price" else "wb-warehouse-stocks/v1"
        op.execute(f"""
 CREATE SEQUENCE public.wb_{family}_ingest_sequence AS bigint NO CYCLE;
 CREATE TABLE public.wb_{family}_runs (
 {owner_columns()}
 run_id uuid NOT NULL CHECK(public.wb_current_uuid(run_id)),
 request_key uuid NOT NULL CHECK(public.wb_current_uuid(request_key)),
 source_kind text COLLATE "C" NOT NULL CHECK(source_kind='{kind}'),
 parser_version text COLLATE "C" NOT NULL CHECK(parser_version='{parser}'),
 page_limit bigint NOT NULL CHECK(page_limit>0),
 request_bytes bytea NOT NULL CHECK(octet_length(request_bytes) BETWEEN 1 AND 65536),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{{64}}$'
 AND request_checksum=encode(sha256(request_bytes),'hex')),
 ingest_sequence bigint NOT NULL CHECK(ingest_sequence>0),
 state text COLLATE "C" NOT NULL DEFAULT 'collecting' CHECK(state IN ('collecting','complete','partial','failed')),
 version integer NOT NULL DEFAULT 0 CHECK(version IN (0,1)),
 started_at timestamptz NOT NULL CHECK(public.wb_current_time(started_at)),
 finished_at timestamptz CHECK(finished_at IS NULL OR (public.wb_current_time(finished_at) AND finished_at>=started_at)),
 source_observed_at timestamptz CHECK(source_observed_at IS NULL),
 received_at timestamptz CHECK(received_at IS NULL OR (public.wb_current_time(received_at)
  AND received_at>=started_at AND (finished_at IS NULL OR received_at<=finished_at))),
 manifest_checksum text COLLATE "C" CHECK(manifest_checksum IS NULL OR manifest_checksum ~ '^[0-9a-f]{{64}}$'),
 page_count bigint NOT NULL DEFAULT 0 CHECK(page_count>=0),
 raw_row_count bigint NOT NULL DEFAULT 0 CHECK(raw_row_count>=0),
 fact_count bigint NOT NULL DEFAULT 0 CHECK(fact_count>=0),
 safe_error_code text COLLATE "C" CHECK(safe_error_code IS NULL OR safe_error_code IN (
  'SOURCE_REQUEST_FAILED','SOURCE_INVALID_PAGE','SOURCE_PAGINATION_STALLED',
  'SOURCE_PAGINATION_INCOMPLETE','SOURCE_IDENTITY_UNRESOLVED','SOURCE_DUPLICATE_IDENTITY',
  'SOURCE_SCOPE_MISMATCH','SOURCE_REVISION_UNEXPLAINED')),
 account_binding_schema_version smallint NOT NULL CHECK(account_binding_schema_version=1),
 account_binding_external_account_id text COLLATE "C" NOT NULL CHECK(public.orders_binding_text(account_binding_external_account_id,128)),
 account_binding_credential_ref text COLLATE "C" CHECK(account_binding_credential_ref IS NULL OR public.orders_binding_text(account_binding_credential_ref,255)),
 account_binding_payload bytea NOT NULL,
 account_binding_checksum text COLLATE "C" NOT NULL CHECK(account_binding_checksum ~ '^[0-9a-f]{{64}}$'
  AND account_binding_checksum=encode(sha256(account_binding_payload),'hex')),
 credential_id uuid NOT NULL REFERENCES public.marketplace_account_credentials(credential_id),
 credential_kind text COLLATE "C" NOT NULL CHECK(credential_kind='wb_api'),
 credential_generation bigint NOT NULL CHECK(credential_generation>0),
 credential_payload_schema_version smallint NOT NULL CHECK(credential_payload_schema_version=1),
 credential_expires_at timestamptz CHECK(credential_expires_at IS NULL),
 PRIMARY KEY(organization_id,marketplace_account_id,run_id),
 UNIQUE(organization_id,marketplace_account_id,source_kind,request_key),
 UNIQUE(organization_id,marketplace_account_id,source_kind,parser_version,request_checksum,run_id),
 CHECK(request_bytes=public.wb_current_request_bytes(organization_id,marketplace_account_id,source_kind,parser_version,page_limit)),
 CHECK(account_binding_payload=public.review_run_binding_bytes(organization_id,marketplace_account_id,
  marketplace,account_binding_external_account_id,account_binding_credential_ref)),
 CHECK((state='collecting' AND version=0 AND finished_at IS NULL AND received_at IS NULL
  AND manifest_checksum IS NULL AND safe_error_code IS NULL AND page_count=0 AND raw_row_count=0 AND fact_count=0)
 OR (state='failed' AND version=1 AND finished_at IS NOT NULL AND received_at IS NULL
  AND manifest_checksum IS NULL AND safe_error_code IS NOT NULL AND page_count=0 AND raw_row_count=0 AND fact_count=0)
 OR (state IN ('complete','partial') AND version=1 AND finished_at IS NOT NULL AND received_at IS NOT NULL
  AND manifest_checksum IS NOT NULL AND page_count>0
  AND ((state='complete' AND safe_error_code IS NULL) OR (state='partial' AND safe_error_code IS NOT NULL))))
 );
 ALTER SEQUENCE public.wb_{family}_ingest_sequence OWNED BY public.wb_{family}_runs.ingest_sequence;
 CREATE INDEX ix_wb_{family}_latest_attempt ON public.wb_{family}_runs
 (organization_id,marketplace_account_id,source_kind,parser_version,request_checksum,ingest_sequence DESC);
 CREATE INDEX ix_wb_{family}_collecting ON public.wb_{family}_runs
 (organization_id,marketplace_account_id,ingest_sequence) WHERE state='collecting';
 CREATE TABLE public.wb_{family}_pages (
 {owner_columns()}{run_reference(family)}
 page_no bigint NOT NULL CHECK(page_no>=0),
 page_offset bigint NOT NULL CHECK(page_offset>=0),
 requested_limit bigint NOT NULL CHECK(requested_limit>0),
 received_at timestamptz NOT NULL CHECK(public.wb_current_time(received_at)),
 source_observed_at timestamptz CHECK(source_observed_at IS NULL),
 http_status integer NOT NULL CHECK(http_status BETWEEN 200 AND 299),
 raw_checksum text COLLATE "C" NOT NULL CHECK(raw_checksum ~ '^[0-9a-f]{{64}}$'),
 raw_row_count bigint NOT NULL CHECK(raw_row_count>=0 AND raw_row_count<=requested_limit),
 terminal boolean NOT NULL CHECK(terminal=(raw_row_count<requested_limit)),
 request_id text COLLATE "C" CHECK(request_id IS NULL OR request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'),
 PRIMARY KEY(organization_id,marketplace_account_id,run_id,page_no),
 UNIQUE(organization_id,marketplace_account_id,run_id,page_offset)
 );
 CREATE TABLE public.wb_{family}_current_heads (
 {owner_columns()}
 source_kind text COLLATE "C" NOT NULL CHECK(source_kind='{kind}'),
 parser_version text COLLATE "C" NOT NULL CHECK(parser_version='{parser}'),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{{64}}$'),
 run_id uuid NOT NULL,
 published_sequence bigint NOT NULL CHECK(published_sequence>0),
 version bigint NOT NULL CHECK(version>0),
 published_at timestamptz NOT NULL CHECK(public.wb_current_time(published_at)),
 PRIMARY KEY(organization_id,marketplace_account_id,source_kind,parser_version,request_checksum),
 FOREIGN KEY(organization_id,marketplace_account_id,source_kind,parser_version,request_checksum,run_id)
 REFERENCES public.wb_{family}_runs(organization_id,marketplace_account_id,source_kind,parser_version,request_checksum,run_id)
 );
 CREATE TABLE public.wb_{family}_audit (
 {owner_columns()}{run_reference(family)}
 audit_id uuid NOT NULL CHECK(public.wb_current_uuid(audit_id)),
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind IN
 ('run.created','run.completed','run.partial','run.failed','current.published')),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind='worker'),
 actor_membership_id integer CHECK(actor_membership_id IS NULL),
 occurred_at timestamptz NOT NULL CHECK(public.wb_current_time(occurred_at)),
 before_run public.wb_{family}_runs,
 after_run public.wb_{family}_runs NOT NULL,
 before_head public.wb_{family}_current_heads,
 after_head public.wb_{family}_current_heads,
 PRIMARY KEY(organization_id,marketplace_account_id,audit_id),
 UNIQUE(organization_id,marketplace_account_id,run_id,event_kind)
 );
 CREATE UNIQUE INDEX uq_wb_{family}_audit_head_version ON public.wb_{family}_audit
 (organization_id,marketplace_account_id,((after_head).source_kind),((after_head).parser_version),
 ((after_head).request_checksum),((after_head).version)) WHERE event_kind='current.published';
 """)
    op.execute(f"""
 CREATE TABLE public.wb_price_product_facts (
 {owner_columns()}{run_reference('price')}
 page_no bigint NOT NULL CHECK(page_no>=0),
 nm_id bigint NOT NULL CHECK(nm_id>0),
 source_size_count bigint NOT NULL CHECK(source_size_count>=0),
 unresolved_size_count bigint NOT NULL CHECK(unresolved_size_count>=0 AND unresolved_size_count<=source_size_count),
 {presence('discount',100)}{presence('club_discount',100)}
 PRIMARY KEY(organization_id,marketplace_account_id,run_id,nm_id),
 FOREIGN KEY(organization_id,marketplace_account_id,run_id,page_no)
 REFERENCES public.wb_price_pages(organization_id,marketplace_account_id,run_id,page_no)
 );
 CREATE TABLE public.wb_price_size_facts (
 {owner_columns()}{run_reference('price')}
 nm_id bigint NOT NULL CHECK(nm_id>0),
 size_id bigint NOT NULL CHECK(size_id>0),
 currency text COLLATE "C" NOT NULL CHECK(currency='RUB'),
 {presence('list_price_kopecks')}{presence('discounted_price_kopecks')}{presence('club_price_kopecks')}
 PRIMARY KEY(organization_id,marketplace_account_id,run_id,nm_id,size_id),
 FOREIGN KEY(organization_id,marketplace_account_id,run_id,nm_id)
 REFERENCES public.wb_price_product_facts(organization_id,marketplace_account_id,run_id,nm_id)
 );
 CREATE TABLE public.wb_stock_observations (
 {owner_columns()}{run_reference('stock')}
 page_no bigint NOT NULL CHECK(page_no>=0),
 stock_scope text COLLATE "C" NOT NULL CHECK(stock_scope='wb_warehouse'),
 warehouse_id bigint NOT NULL CHECK(warehouse_id>0),
 nm_id bigint NOT NULL CHECK(nm_id>0),
 chrt_id bigint CHECK(chrt_id IS NULL OR chrt_id>0),
 warehouse_display_name text CHECK(warehouse_display_name IS NULL OR char_length(warehouse_display_name) BETWEEN 1 AND 255),
 {presence('quantity')}{presence('in_way_to_client')}{presence('in_way_from_client')}
 UNIQUE NULLS NOT DISTINCT(organization_id,marketplace_account_id,run_id,stock_scope,warehouse_id,nm_id,chrt_id),
 FOREIGN KEY(organization_id,marketplace_account_id,run_id,page_no)
 REFERENCES public.wb_stock_pages(organization_id,marketplace_account_id,run_id,page_no)
 );
 """)


def live_binding(alias):
    # Actual credential PK is credential_id alone. Historical generation is a
    # frozen observation, not an invented FK to a mutable parent generation.
    return f"""
 SELECT external_account_id,credential_ref,status INTO a FROM public.marketplace_accounts
 WHERE organization_id={alias}.organization_id AND marketplace_account_id={alias}.marketplace_account_id
 AND marketplace COLLATE "C"='wb';
 IF NOT FOUND OR a.status COLLATE "C" IS DISTINCT FROM 'connected'
 OR ROW(a.external_account_id COLLATE "C",a.credential_ref COLLATE "C") IS DISTINCT FROM
 ROW({alias}.account_binding_external_account_id COLLATE "C",{alias}.account_binding_credential_ref COLLATE "C") THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_binding_changed'; END IF;
 PERFORM 1 FROM public.marketplace_account_credentials WHERE credential_id={alias}.credential_id FOR SHARE;
 SELECT organization_id,marketplace_account_id,provider,credential_kind,generation,
 payload_schema_version,expires_at,revoked_at INTO c FROM public.marketplace_account_credentials
 WHERE credential_id={alias}.credential_id;
 IF NOT FOUND OR ROW(c.organization_id,c.marketplace_account_id,c.provider COLLATE "C",c.credential_kind COLLATE "C",
 c.generation,c.payload_schema_version,c.expires_at,c.revoked_at) IS DISTINCT FROM
 ROW({alias}.organization_id::bigint,{alias}.marketplace_account_id::bigint,'wb'::text,'wb_api'::text,
 {alias}.credential_generation,{alias}.credential_payload_schema_version,{alias}.credential_expires_at,NULL::timestamptz) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_binding_changed'; END IF;
 """


def codecs_and_guards():
    for family in FAMILIES:
        prefix = f"wb_{family}"
        manifest_context = (
            "'[\"wb-goods-prices/v1\",['||r.organization_id::text||','||r.marketplace_account_id::text||','||r.page_limit::text||',\"'||r.request_checksum||'\"],['"
            if family == "price" else
            "'[\"wb-warehouse-stocks/v1\",\"'||r.request_checksum||'\",['"
        )
        op.execute(f"""
 CREATE FUNCTION public.{prefix}_manifest_bytes(r public.{prefix}_runs, pages public.{prefix}_pages[]) RETURNS bytea
 LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT convert_to({manifest_context}||coalesce(string_agg(
 '['||p.page_offset::text||','||p.requested_limit::text||',"'||p.raw_checksum||'"]',',' ORDER BY p.page_no),'')||']]','UTF8')
 FROM unnest(pages) p $$;
 CREATE FUNCTION public.{prefix}_run_guard() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE a record; c record;
 BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_context_invalid'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.run_id IS NOT NULL OR NEW.ingest_sequence IS NOT NULL
  OR NEW.started_at IS NULL OR NEW.started_at>clock_timestamp()
  OR NEW.state<>'collecting' OR NEW.version<>0 OR NEW.finished_at IS NOT NULL THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_creation_invalid'; END IF;
  {live_binding('NEW')}
  NEW.run_id:=gen_random_uuid();
  NEW.ingest_sequence:=nextval('public.{prefix}_ingest_sequence'::regclass);
 ELSE
  IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
  IF OLD.state<>'collecting' OR OLD.version<>0 OR NEW.state NOT IN ('complete','partial','failed') OR NEW.version<>1
  OR (to_jsonb(NEW)-ARRAY['state','version','finished_at','received_at','manifest_checksum','page_count','raw_row_count','fact_count','safe_error_code'])
  IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','version','finished_at','received_at','manifest_checksum','page_count','raw_row_count','fact_count','safe_error_code'])
  OR NEW.finished_at IS DISTINCT FROM OLD.finished_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_history_immutable'; END IF;
  NEW.finished_at:=clock_timestamp();
 END IF;
 RETURN NEW;
 END $$;
 CREATE FUNCTION public.{prefix}_child_guard() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE r public.{prefix}_runs%ROWTYPE; existing_page jsonb;
 BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_context_invalid'; END IF;
 SELECT * INTO r FROM public.{prefix}_runs WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.run_id FOR UPDATE;
 IF NOT FOUND OR r.state<>'collecting' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_history_immutable'; END IF;
 IF TG_TABLE_NAME='{prefix}_pages' THEN
  SELECT to_jsonb(p) INTO existing_page FROM public.{prefix}_pages p
  WHERE p.organization_id=NEW.organization_id AND p.marketplace_account_id=NEW.marketplace_account_id
  AND p.run_id=NEW.run_id AND (p.page_no=(to_jsonb(NEW)->>'page_no')::bigint
  OR p.page_offset=(to_jsonb(NEW)->>'page_offset')::bigint);
  IF FOUND THEN
   IF existing_page IS NOT DISTINCT FROM to_jsonb(NEW) THEN RETURN NULL; END IF;
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_page_conflict';
  END IF;
 END IF;
 RETURN NEW;
 END $$;
 CREATE FUNCTION public.{prefix}_head_guard() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE r public.{prefix}_runs%ROWTYPE; a record; c record; run_xmin xid;
 BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_context_invalid'; END IF;
 IF TG_OP='UPDATE' THEN
  IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
  IF ROW(NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.source_kind,NEW.parser_version,NEW.request_checksum)
  IS DISTINCT FROM ROW(OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.source_kind,OLD.parser_version,OLD.request_checksum)
  OR OLD.version=9223372036854775807 OR NEW.version<>OLD.version+1
  OR NEW.published_sequence<=OLD.published_sequence OR NEW.published_at IS DISTINCT FROM OLD.published_at THEN
   RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_current_head_conflict'; END IF;
 ELSE
  IF NEW.version<>1 OR NEW.published_at IS NOT NULL THEN
   RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_current_head_conflict'; END IF;
 END IF;
 SELECT * INTO r FROM public.{prefix}_runs WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.run_id FOR UPDATE;
 IF NOT FOUND OR r.state<>'complete' OR ROW(r.source_kind,r.parser_version,r.request_checksum,r.ingest_sequence)
 IS DISTINCT FROM ROW(NEW.source_kind,NEW.parser_version,NEW.request_checksum,NEW.published_sequence) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_publication_invalid'; END IF;
 -- Publication is part of the run's successful finalization root, never a
 -- second CAS on a previously sealed replay. A separately sealed run stays history.
 SELECT xmin INTO run_xmin FROM public.{prefix}_runs WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.run_id;
 IF run_xmin IS DISTINCT FROM pg_current_xact_id()::text::xid THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_publication_invalid'; END IF;
 {live_binding('r')}
 NEW.published_at:=clock_timestamp();
 RETURN NEW;
 END $$;
 CREATE FUNCTION public.{prefix}_audit_guard() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog AS $$
 BEGIN
 -- The invoker needs column INSERT rights for the automatic event below;
 -- direct caller INSERT is not an accepted event origin.
 IF pg_trigger_depth()<>2 OR NEW.audit_id IS NOT NULL OR NEW.occurred_at IS NOT NULL THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 NEW.audit_id:=gen_random_uuid(); NEW.occurred_at:=clock_timestamp();
 RETURN NEW;
 END $$;
 CREATE FUNCTION public.{prefix}_emit_audit() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE before_r public.{prefix}_runs; after_r public.{prefix}_runs;
 before_h public.{prefix}_current_heads; after_h public.{prefix}_current_heads; kind text;
 BEGIN
 IF TG_TABLE_NAME='{prefix}_runs' THEN
  after_r:=NEW;
  IF TG_OP='INSERT' THEN kind:='run.created';
  ELSE before_r:=OLD; kind:=CASE NEW.state WHEN 'complete' THEN 'run.completed' ELSE 'run.'||NEW.state END; END IF;
 ELSE
  SELECT * INTO after_r FROM public.{prefix}_runs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.run_id;
  before_r:=after_r; after_h:=NEW;
  IF TG_OP='UPDATE' THEN before_h:=OLD; END IF;
  kind:='current.published';
 END IF;
 INSERT INTO public.{prefix}_audit(organization_id,marketplace_account_id,marketplace,run_id,event_kind,
 actor_kind,actor_membership_id,before_run,after_run,before_head,after_head)
 VALUES(NEW.organization_id,NEW.marketplace_account_id,'wb',NEW.run_id,kind,'worker',NULL,
 before_r,after_r,before_h,after_h);
 RETURN NULL;
 END $$;
 """)


def witnesses():
    for family in FAMILIES:
        p = f"wb_{family}"
        fact_table = "wb_price_product_facts" if family == "price" else "wb_stock_observations"
        extra = ""
        if family == "price":
            extra = """
 IF EXISTS(SELECT 1 FROM public.wb_price_product_facts f
 WHERE f.organization_id=r.organization_id AND f.marketplace_account_id=r.marketplace_account_id AND f.run_id=r.run_id
 AND f.source_size_count-f.unresolved_size_count<>(SELECT count(*) FROM public.wb_price_size_facts s
 WHERE s.organization_id=f.organization_id AND s.marketplace_account_id=f.marketplace_account_id
 AND s.run_id=f.run_id AND s.nm_id=f.nm_id)) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_fact_count_invalid'; END IF;
 SELECT EXISTS(SELECT 1 FROM public.wb_price_product_facts f WHERE f.organization_id=r.organization_id
 AND f.marketplace_account_id=r.marketplace_account_id AND f.run_id=r.run_id
 AND (f.source_size_count=0 OR f.unresolved_size_count>0)) INTO unresolved;
 IF (r.state='complete' AND unresolved) OR (r.state='partial' AND unresolved
 AND r.safe_error_code<>'SOURCE_IDENTITY_UNRESOLVED') THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_identity_unresolved'; END IF;
 SELECT facts+count(*) INTO facts FROM public.wb_price_size_facts s WHERE s.organization_id=r.organization_id
 AND s.marketplace_account_id=r.marketplace_account_id AND s.run_id=r.run_id;
 """
        op.execute(f"""
 CREATE FUNCTION public.{p}_graph() RETURNS trigger
 LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 DECLARE r public.{p}_runs%ROWTYPE; pg public.{p}_pages%ROWTYPE;
 pages public.{p}_pages[]; pc bigint:=0; raw_count numeric:=0; facts numeric:=0;
 previous_time timestamptz; previous_terminal boolean:=false; unresolved boolean:=false;
 created public.{p}_audit%ROWTYPE; terminal_event public.{p}_audit%ROWTYPE; e public.{p}_audit%ROWTYPE;
 h public.{p}_current_heads%ROWTYPE; previous_h public.{p}_current_heads;
 published_r public.{p}_runs%ROWTYPE; initial_r public.{p}_runs; event_count bigint;
 BEGIN
 -- Fail closed against changing either RLS GUC between a write and deferred
 -- validation. Earlier pending rows must never disappear into a new context.
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_context_invalid'; END IF;
 SELECT * INTO r FROM public.{p}_runs WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.run_id;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_graph_invalid'; END IF;
 FOR pg IN SELECT * FROM public.{p}_pages WHERE organization_id=r.organization_id
 AND marketplace_account_id=r.marketplace_account_id AND run_id=r.run_id ORDER BY page_no LOOP
  IF pg.page_no<>pc OR pg.page_offset::numeric<>pc::numeric*r.page_limit OR pg.requested_limit<>r.page_limit
  OR previous_terminal OR pg.received_at<r.started_at OR pg.received_at>clock_timestamp()
  OR (previous_time IS NOT NULL AND pg.received_at<previous_time)
  OR pg.raw_row_count<>(SELECT count(*) FROM public.{fact_table} f WHERE f.organization_id=r.organization_id
   AND f.marketplace_account_id=r.marketplace_account_id AND f.run_id=r.run_id AND f.page_no=pg.page_no) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_page_invalid'; END IF;
  pc:=pc+1; raw_count:=raw_count+pg.raw_row_count;
  previous_time:=pg.received_at; previous_terminal:=pg.terminal;
 END LOOP;
 SELECT count(*) INTO facts FROM public.{fact_table} f WHERE f.organization_id=r.organization_id
 AND f.marketplace_account_id=r.marketplace_account_id AND f.run_id=r.run_id;
 {extra}
 IF r.state<>'collecting' THEN
  IF ROW(r.page_count::numeric,r.raw_row_count::numeric,r.fact_count::numeric)
  IS DISTINCT FROM ROW(pc::numeric,raw_count,facts) OR r.received_at IS DISTINCT FROM previous_time
  OR (r.state='complete' AND NOT previous_terminal) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_counts_invalid'; END IF;
  IF pc>0 THEN
   SELECT array_agg(t ORDER BY t.page_no) INTO pages FROM public.{p}_pages t
   WHERE organization_id=r.organization_id AND marketplace_account_id=r.marketplace_account_id AND run_id=r.run_id;
   IF r.manifest_checksum IS DISTINCT FROM encode(sha256(public.{p}_manifest_bytes(r,pages)),'hex') THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_manifest_invalid'; END IF;
  END IF;
 END IF;
 SELECT * INTO created FROM public.{p}_audit WHERE organization_id=r.organization_id
 AND marketplace_account_id=r.marketplace_account_id AND run_id=r.run_id AND event_kind='run.created';
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 initial_r:=r; initial_r.state:='collecting'; initial_r.version:=0; initial_r.finished_at:=NULL;
 initial_r.received_at:=NULL; initial_r.manifest_checksum:=NULL; initial_r.page_count:=0;
 initial_r.raw_row_count:=0; initial_r.fact_count:=0; initial_r.safe_error_code:=NULL;
 IF created.before_run IS DISTINCT FROM NULL::public.{p}_runs OR created.after_run IS DISTINCT FROM initial_r
 OR created.before_head IS DISTINCT FROM NULL::public.{p}_current_heads
 OR created.after_head IS DISTINCT FROM NULL::public.{p}_current_heads
 OR created.occurred_at<r.started_at THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 SELECT count(*) INTO event_count FROM public.{p}_audit WHERE organization_id=r.organization_id
 AND marketplace_account_id=r.marketplace_account_id AND run_id=r.run_id AND event_kind LIKE 'run.%';
 IF event_count<>1+r.version THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 IF r.version=1 THEN
  SELECT * INTO terminal_event FROM public.{p}_audit WHERE organization_id=r.organization_id
  AND marketplace_account_id=r.marketplace_account_id AND run_id=r.run_id
  AND event_kind=CASE r.state WHEN 'complete' THEN 'run.completed' ELSE 'run.'||r.state END;
  IF NOT FOUND OR terminal_event.before_run IS DISTINCT FROM initial_r OR terminal_event.after_run IS DISTINCT FROM r
  OR terminal_event.before_head IS DISTINCT FROM NULL::public.{p}_current_heads
  OR terminal_event.after_head IS DISTINCT FROM NULL::public.{p}_current_heads
  OR terminal_event.occurred_at<created.occurred_at OR terminal_event.occurred_at<r.finished_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 END IF;
 SELECT * INTO h FROM public.{p}_current_heads WHERE organization_id=r.organization_id
 AND marketplace_account_id=r.marketplace_account_id AND source_kind=r.source_kind
 AND parser_version=r.parser_version AND request_checksum=r.request_checksum;
 previous_h:=NULL;
 FOR e IN SELECT * FROM public.{p}_audit WHERE organization_id=r.organization_id
 AND marketplace_account_id=r.marketplace_account_id AND event_kind='current.published'
 AND (after_head).source_kind=r.source_kind AND (after_head).parser_version=r.parser_version
 AND (after_head).request_checksum=r.request_checksum ORDER BY (after_head).version LOOP
  SELECT * INTO published_r FROM public.{p}_runs WHERE organization_id=e.organization_id
  AND marketplace_account_id=e.marketplace_account_id AND run_id=e.run_id;
  IF NOT FOUND OR published_r.state<>'complete' OR e.before_run IS DISTINCT FROM published_r
  OR e.after_run IS DISTINCT FROM published_r OR e.before_head IS DISTINCT FROM previous_h
  OR (e.after_head).version IS DISTINCT FROM coalesce(previous_h.version,0)+1
  OR ROW((e.after_head).organization_id,(e.after_head).marketplace_account_id,(e.after_head).marketplace,
   (e.after_head).source_kind,(e.after_head).parser_version,(e.after_head).request_checksum,
   (e.after_head).run_id,(e.after_head).published_sequence) IS DISTINCT FROM
   ROW(published_r.organization_id,published_r.marketplace_account_id,published_r.marketplace,
   published_r.source_kind,published_r.parser_version,published_r.request_checksum,
   published_r.run_id,published_r.ingest_sequence)
  OR (previous_h.version IS NOT NULL AND ((e.after_head).published_sequence<=previous_h.published_sequence
   OR (e.after_head).published_at<previous_h.published_at))
  OR (e.after_head).published_at<published_r.finished_at OR e.occurred_at<(e.after_head).published_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_head_graph_invalid'; END IF;
  previous_h:=e.after_head;
 END LOOP;
 IF (h.version IS NULL AND previous_h.version IS NOT NULL)
 OR (h.version IS NOT NULL AND h IS DISTINCT FROM previous_h) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_head_graph_invalid'; END IF;
 -- Every publication event on this run must join the exact head chain above.
 IF EXISTS(SELECT 1 FROM public.{p}_audit a WHERE a.organization_id=r.organization_id
 AND a.marketplace_account_id=r.marketplace_account_id AND a.run_id=r.run_id AND a.event_kind='current.published'
 AND (a.after_head IS NOT DISTINCT FROM NULL::public.{p}_current_heads
 OR ROW((a.after_head).source_kind,(a.after_head).parser_version,(a.after_head).request_checksum)
 IS DISTINCT FROM ROW(r.source_kind,r.parser_version,r.request_checksum))) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_audit_invalid'; END IF;
 RETURN NULL;
 END $$;
 """)


def acl():
    relations = TABLES + tuple(f"wb_{f}_ingest_sequence" for f in FAMILIES)
    names = ",".join("'" + t + "'" for t in relations)
    functions = ",".join(f"'public.{f}'::regprocedure" for f in PURE + TRIGGERS)
    op.execute(f"""
 DO $$ DECLARE t record; a record; c record; f record; who text;
 BEGIN
 -- Includes grants inherited at object creation through owner's defaults.
 -- Only this migration's objects are scrubbed, including individual columns.
 FOR t IN SELECT oid,relowner,relname,relacl,relkind FROM pg_class
 WHERE relnamespace='public'::regnamespace AND relname IN ({names}) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(t.relacl) WHERE grantee<>t.relowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON %s public.%I FROM %s',CASE WHEN t.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,t.relname,who);
  END LOOP;
  FOR c IN SELECT at.attname,x.grantee FROM pg_attribute at CROSS JOIN LATERAL aclexplode(at.attacl) x
  WHERE at.attrelid=t.oid AND x.grantee<>t.relowner LOOP
   who:=CASE WHEN c.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(c.grantee)) END;
   EXECUTE format('REVOKE ALL (%I) ON public.%I FROM %s',c.attname,t.relname,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON %s public.%I FROM PUBLIC',CASE WHEN t.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END,t.relname);
 END LOOP;
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE oid IN ({functions}) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(f.proacl) WHERE grantee<>f.proowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
 END $$;
 """)


def upgrade() -> None:
    op.execute(HELPERS)
    models()
    codecs_and_guards()
    witnesses()
    for table in TABLES:
        family = "price" if table.startswith("wb_price_") else "stock"
        predicate = "organization_id::text COLLATE \"C\"=current_setting('app.organization_id',true) COLLATE \"C\" AND marketplace_account_id::text COLLATE \"C\"=current_setting('app.marketplace_account_id',true) COLLATE \"C\""
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_owner ON public.{table} USING({predicate}) WITH CHECK({predicate})")
        op.execute(f"CREATE TRIGGER a_owner_lock BEFORE INSERT OR UPDATE OR DELETE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.wb_current_account_lock()")
        op.execute(f"CREATE TRIGGER z_no_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.wb_current_immutable()")
        if table.endswith("_runs"):
            guard, operations = "run_guard", "INSERT OR UPDATE"
        elif table.endswith("_current_heads"):
            guard, operations = "head_guard", "INSERT OR UPDATE"
        else:
            guard = "audit_guard" if table.endswith("_audit") else "child_guard"
            operations = "INSERT"
            op.execute(f"CREATE TRIGGER z_no_update BEFORE UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_current_immutable()")
        op.execute(f"CREATE TRIGGER b_guard BEFORE {operations} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_{family}_{guard}()")
        op.execute(f"CREATE TRIGGER z_no_delete BEFORE DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_current_immutable()")
        if table.endswith("_runs") or table.endswith("_current_heads"):
            op.execute(f"CREATE TRIGGER c_audit AFTER INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_{family}_emit_audit()")
        op.execute(f"CREATE CONSTRAINT TRIGGER z_graph AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wb_{family}_graph()")
    acl()


def downgrade() -> None:
    # Owner lacking BYPASSRLS gets an error, not a filtered empty result.
    op.execute("SET LOCAL row_security=off")
    op.execute("LOCK TABLE " + ",".join("public." + t for t in sorted(TABLES)) + " IN ACCESS EXCLUSIVE MODE")
    checks = " OR ".join(f"EXISTS(SELECT 1 FROM public.{t})" for t in TABLES)
    op.execute(f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='wb_current_downgrade_history'; END IF; END $$")
    for table in TABLES:
        for trigger in ("a_owner_lock", "b_guard", "z_no_delete", "z_no_truncate", "z_graph"):
            op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        if table.endswith("_runs") or table.endswith("_current_heads"):
            op.execute(f"DROP TRIGGER c_audit ON public.{table}")
        else:
            op.execute(f"DROP TRIGGER z_no_update ON public.{table}")
    for function in TRIGGERS:
        op.execute(f"DROP FUNCTION public.{function}")
    for family in FAMILIES:
        op.execute(f"DROP FUNCTION public.wb_{family}_manifest_bytes(public.wb_{family}_runs,public.wb_{family}_pages[])")
    for table in ("wb_price_audit", "wb_stock_audit", "wb_price_size_facts", "wb_price_product_facts",
                  "wb_stock_observations", "wb_price_pages", "wb_stock_pages", "wb_price_current_heads",
                  "wb_stock_current_heads", "wb_price_runs", "wb_stock_runs"):
        op.execute(f"DROP TABLE public.{table}")
    # Owned ingest sequences disappear with their own run columns, no CASCADE.
    for function in PURE[:4]:
        op.execute(f"DROP FUNCTION public.{function}")
