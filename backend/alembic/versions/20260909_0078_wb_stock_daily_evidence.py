"""Immutable received-basis WB stock daily history and exact reviewed evidence.

Storage only: no reviewer permission, initial worker bootstrap or provider proof.
Source-first package; SQL parity and disposable PostgreSQL acceptance are deferred.
"""

from alembic import op

revision = "20260909_0078"
down_revision = "20260909_0077"
branch_labels = depends_on = None

TABLES = (
    "wb_stock_daily_revisions", "wb_stock_daily_heads", "wb_stock_revision_evidence",
    "wb_stock_revision_evidence_diffs", "wb_stock_revision_evidence_decisions", "wb_stock_daily_audit",
)
OWNER = "organization_id,marketplace_account_id"
DAY = OWNER + ",stock_scope,request_checksum,business_date_msk"
PURE = (
    "wb_daily_integer(numeric,boolean)", "wb_daily_whitespace(integer)",
    "wb_daily_reference(text)", "wb_daily_ascii(text)",
    "wb_stock_evidence_row_bytes(public.wb_stock_observations)",
    "wb_stock_evidence_diff_bytes(public.wb_stock_revision_evidence_diffs[])",
    "wb_stock_evidence_proposal_bytes(public.wb_stock_revision_evidence)",
    "wb_stock_evidence_expected_diff(integer,integer,uuid,uuid)",
    "wb_daily_run_eligible(integer,integer,uuid,bytea,text,date)",
)
TRIGGERS = (
    "wb_daily_lock()", "wb_daily_immutable()", "wb_daily_insert_guard()",
    "wb_daily_head_guard()", "wb_daily_emit_audit()", "wb_daily_audit_guard()",
    "wb_daily_graph()", "wb_stock_evidence_graph()",
)


def owner_columns():
    return """
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 marketplace text COLLATE "C" NOT NULL CHECK(marketplace='wb'),
 FOREIGN KEY(organization_id,marketplace_account_id,marketplace)
 REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
"""


def daily_columns():
    return """
 stock_scope text COLLATE "C" NOT NULL CHECK(stock_scope='wb_warehouse'),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 business_date_msk date NOT NULL CHECK(business_date_msk BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'),
"""


SCALARS = r"""
CREATE FUNCTION public.wb_daily_integer(v numeric,allow_zero boolean) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT coalesce(v::text NOT IN ('NaN','Infinity','-Infinity') AND v=trunc(v)
 AND (v>0 OR (allow_zero AND v=0)),false) $$;
CREATE FUNCTION public.wb_daily_whitespace(v integer) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT v BETWEEN 9 AND 13 OR v BETWEEN 28 AND 32 OR v BETWEEN 8192 AND 8202
 OR v IN (133,160,5760,8232,8233,8239,8287,12288) $$;
CREATE FUNCTION public.wb_daily_reference(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT coalesce(length(v)>0 AND NOT public.wb_daily_whitespace(ascii(left(v,1)))
 AND NOT public.wb_daily_whitespace(ascii(right(v,1))),false) $$;
CREATE FUNCTION public.wb_daily_ascii(v text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE result text:='"'; c text; n integer; i integer;
BEGIN
 IF v IS NULL THEN RETURN 'null'; END IF;
 -- Python ensure_ascii=True, including DEL, lowercase escapes and surrogate pairs.
 FOR i IN 1..length(v) LOOP
  c:=substr(v,i,1); n:=ascii(c);
  result:=result||CASE n WHEN 34 THEN E'\\"' WHEN 92 THEN E'\\\\'
   WHEN 8 THEN E'\\b' WHEN 9 THEN E'\\t' WHEN 10 THEN E'\\n'
   WHEN 12 THEN E'\\f' WHEN 13 THEN E'\\r'
   ELSE CASE WHEN n<32 OR n>=127 THEN CASE WHEN n<=65535
    THEN E'\\u'||lpad(to_hex(n),4,'0')
    ELSE E'\\u'||lpad(to_hex(55296+(n-65536)/1024),4,'0')
      ||E'\\u'||lpad(to_hex(56320+(n-65536)%1024),4,'0') END ELSE c END END;
 END LOOP;
 RETURN result||'"';
END $$;
CREATE FUNCTION public.wb_daily_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed'
 OR org IS NULL OR account IS NULL OR org !~ '^[1-9][0-9]{0,9}$'
 OR account !~ '^[1-9][0-9]{0,9}$' OR org::numeric>2147483647 OR account::numeric>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_context_invalid'; END IF;
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer
 AND marketplace_account_id=account::integer AND marketplace COLLATE "C"='wb' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_account_missing'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.wb_daily_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_history_immutable'; END $$;
"""


def models():
    op.execute(f"""
 CREATE TABLE public.wb_stock_daily_revisions (
 {owner_columns()}{daily_columns()}
 revision numeric NOT NULL CHECK(public.wb_daily_integer(revision,false)),
 daily_revision_id uuid NOT NULL CHECK(public.wb_current_uuid(daily_revision_id)),
 command_id uuid NOT NULL CHECK(public.wb_current_uuid(command_id)),
 source_kind text COLLATE "C" NOT NULL CHECK(source_kind='wb_warehouse_v1'),
 parser_version text COLLATE "C" NOT NULL CHECK(parser_version='wb-warehouse-stocks/v1'),
 source_run_id uuid NOT NULL,
 request_bytes bytea NOT NULL CHECK(request_checksum=encode(sha256(request_bytes),'hex')),
 basis text COLLATE "C" NOT NULL CHECK(basis='received'),
 effective_observation_at timestamptz NOT NULL CHECK(public.wb_current_time(effective_observation_at)),
 created_at timestamptz NOT NULL CHECK(public.wb_current_time(created_at)),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind IN ('worker','membership')),
 actor_membership_id integer CHECK(actor_membership_id>0),
 correction_reason text COLLATE "C" CHECK(correction_reason ~ '^[A-Za-z][A-Za-z0-9_]{{0,127}}$'),
 supersedes_revision numeric CHECK(supersedes_revision IS NULL OR public.wb_daily_integer(supersedes_revision,false)),
 evidence_id uuid, decision_id uuid,
 proposal_checksum text COLLATE "C" CHECK(proposal_checksum ~ '^[0-9a-f]{{64}}$'),
 PRIMARY KEY({DAY},revision),
 UNIQUE({OWNER},daily_revision_id), UNIQUE({OWNER},command_id),
 UNIQUE({OWNER},evidence_id), UNIQUE({OWNER},decision_id),
 FOREIGN KEY({OWNER},source_kind,parser_version,request_checksum,source_run_id)
 REFERENCES public.wb_stock_runs({OWNER},source_kind,parser_version,request_checksum,run_id),
 FOREIGN KEY({DAY},supersedes_revision) REFERENCES public.wb_stock_daily_revisions({DAY},revision),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 CHECK((revision=1 AND actor_kind='worker' AND actor_membership_id IS NULL AND correction_reason IS NULL
  AND supersedes_revision IS NULL AND evidence_id IS NULL AND decision_id IS NULL AND proposal_checksum IS NULL)
 OR (revision>1 AND actor_kind='membership' AND actor_membership_id IS NOT NULL AND correction_reason IS NOT NULL
  AND supersedes_revision IS NOT NULL AND revision=supersedes_revision+1
  AND evidence_id IS NOT NULL AND decision_id IS NOT NULL AND proposal_checksum IS NOT NULL))
 );
 CREATE TABLE public.wb_stock_daily_heads (
 {owner_columns()}{daily_columns()}
 current_revision numeric NOT NULL CHECK(public.wb_daily_integer(current_revision,false)),
 version numeric NOT NULL CHECK(version=current_revision),
 updated_at timestamptz NOT NULL CHECK(public.wb_current_time(updated_at)),
 PRIMARY KEY({DAY}),
 FOREIGN KEY({DAY},current_revision) REFERENCES public.wb_stock_daily_revisions({DAY},revision)
 );
 CREATE TABLE public.wb_stock_revision_evidence (
 {owner_columns()}{daily_columns()}
 evidence_id uuid NOT NULL CHECK(public.wb_current_uuid(evidence_id)),
 source_kind text COLLATE "C" NOT NULL CHECK(source_kind='wb_warehouse_v1'),
 parser_version text COLLATE "C" NOT NULL CHECK(parser_version='wb-warehouse-stocks/v1'),
 grain_version text COLLATE "C" NOT NULL CHECK(grain_version='nmId/chrtId?/warehouseId'),
 request_bytes bytea NOT NULL CHECK(request_checksum=encode(sha256(request_bytes),'hex')),
 before_run_id uuid NOT NULL, after_run_id uuid NOT NULL CHECK(after_run_id<>before_run_id),
 before_manifest_checksum text COLLATE "C" NOT NULL CHECK(before_manifest_checksum ~ '^[0-9a-f]{{64}}$'),
 after_manifest_checksum text COLLATE "C" NOT NULL CHECK(after_manifest_checksum ~ '^[0-9a-f]{{64}}$'),
 before_daily_revision numeric NOT NULL CHECK(public.wb_daily_integer(before_daily_revision,false)),
 proposed_by_membership_id integer NOT NULL CHECK(proposed_by_membership_id>0),
 proposed_at timestamptz NOT NULL CHECK(public.wb_current_time(proposed_at)),
 proposal_command_id uuid NOT NULL CHECK(public.wb_current_uuid(proposal_command_id)),
 proposal_bytes bytea NOT NULL,
 proposal_checksum text COLLATE "C" NOT NULL CHECK(proposal_checksum ~ '^[0-9a-f]{{64}}$'
  AND proposal_checksum=encode(sha256(proposal_bytes),'hex')),
 evidence_document_bytes bytea NOT NULL CHECK(public.review_local_nonblank_utf8(evidence_document_bytes)),
 evidence_document_checksum text COLLATE "C" NOT NULL CHECK(evidence_document_checksum ~ '^[0-9a-f]{{64}}$'
  AND evidence_document_checksum=encode(sha256(evidence_document_bytes),'hex')),
 reviewed_evidence_reference text COLLATE "C" NOT NULL CHECK(public.wb_daily_reference(reviewed_evidence_reference)),
 diff_checksum text COLLATE "C" NOT NULL CHECK(diff_checksum ~ '^[0-9a-f]{{64}}$'),
 added_count numeric NOT NULL CHECK(public.wb_daily_integer(added_count,true)),
 removed_count numeric NOT NULL CHECK(public.wb_daily_integer(removed_count,true)),
 changed_count numeric NOT NULL CHECK(public.wb_daily_integer(changed_count,true)),
 CHECK(added_count+removed_count+changed_count>0),
 PRIMARY KEY({OWNER},evidence_id), UNIQUE({OWNER},proposal_command_id),
 FOREIGN KEY({OWNER},source_kind,parser_version,request_checksum,before_run_id)
 REFERENCES public.wb_stock_runs({OWNER},source_kind,parser_version,request_checksum,run_id),
 FOREIGN KEY({OWNER},source_kind,parser_version,request_checksum,after_run_id)
 REFERENCES public.wb_stock_runs({OWNER},source_kind,parser_version,request_checksum,run_id),
 FOREIGN KEY({DAY},before_daily_revision) REFERENCES public.wb_stock_daily_revisions({DAY},revision),
 FOREIGN KEY(organization_id,proposed_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
 );
 CREATE TABLE public.wb_stock_revision_evidence_diffs (
 {owner_columns()}
 evidence_id uuid NOT NULL,
 diff_row_id uuid NOT NULL CHECK(public.wb_current_uuid(diff_row_id)),
 nm_id bigint NOT NULL CHECK(nm_id>0), chrt_id bigint CHECK(chrt_id>0),
 warehouse_id bigint NOT NULL CHECK(warehouse_id>0),
 change_kind text COLLATE "C" NOT NULL CHECK(change_kind IN ('added','removed','changed')),
 before_payload_checksum text COLLATE "C" CHECK(before_payload_checksum ~ '^[0-9a-f]{{64}}$'),
 after_payload_checksum text COLLATE "C" CHECK(after_payload_checksum ~ '^[0-9a-f]{{64}}$'),
 PRIMARY KEY({OWNER},diff_row_id),
 UNIQUE NULLS NOT DISTINCT({OWNER},evidence_id,nm_id,chrt_id,warehouse_id),
 FOREIGN KEY({OWNER},evidence_id) REFERENCES public.wb_stock_revision_evidence({OWNER},evidence_id),
 CHECK((change_kind='added' AND before_payload_checksum IS NULL AND after_payload_checksum IS NOT NULL)
 OR (change_kind='removed' AND before_payload_checksum IS NOT NULL AND after_payload_checksum IS NULL)
 OR (change_kind='changed' AND before_payload_checksum IS NOT NULL AND after_payload_checksum IS NOT NULL
  AND before_payload_checksum<>after_payload_checksum))
 );
 CREATE TABLE public.wb_stock_revision_evidence_decisions (
 {owner_columns()}
 evidence_id uuid NOT NULL,
 decision_id uuid NOT NULL CHECK(public.wb_current_uuid(decision_id)),
 decision_command_id uuid NOT NULL CHECK(public.wb_current_uuid(decision_command_id)),
 outcome text COLLATE "C" NOT NULL CHECK(outcome IN ('accepted','rejected')),
 reviewed_by_membership_id integer NOT NULL CHECK(reviewed_by_membership_id>0),
 decided_at timestamptz NOT NULL CHECK(public.wb_current_time(decided_at)),
 reviewed_proposal_checksum text COLLATE "C" NOT NULL CHECK(reviewed_proposal_checksum ~ '^[0-9a-f]{{64}}$'),
 reason_code text COLLATE "C" NOT NULL CHECK(reason_code ~ '^[A-Za-z][A-Za-z0-9_]{{0,127}}$'),
 PRIMARY KEY({OWNER},evidence_id), UNIQUE({OWNER},decision_id), UNIQUE({OWNER},decision_command_id),
 UNIQUE({OWNER},evidence_id,decision_id),
 FOREIGN KEY({OWNER},evidence_id) REFERENCES public.wb_stock_revision_evidence({OWNER},evidence_id),
 FOREIGN KEY(organization_id,reviewed_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
 );
 ALTER TABLE public.wb_stock_daily_revisions ADD CONSTRAINT wb_daily_accepted_decision_fk
 FOREIGN KEY({OWNER},evidence_id,decision_id)
 REFERENCES public.wb_stock_revision_evidence_decisions({OWNER},evidence_id,decision_id);
 CREATE TABLE public.wb_stock_daily_audit (
 {owner_columns()}{daily_columns()}
 audit_id uuid NOT NULL CHECK(public.wb_current_uuid(audit_id)),
 revision numeric NOT NULL CHECK(public.wb_daily_integer(revision,false)),
 source_run_id uuid NOT NULL,
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind IN ('daily.created','daily.corrected')),
 occurred_at timestamptz NOT NULL CHECK(public.wb_current_time(occurred_at)),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind IN ('worker','membership')),
 actor_membership_id integer CHECK(actor_membership_id>0),
 before_revision public.wb_stock_daily_revisions,
 after_revision public.wb_stock_daily_revisions NOT NULL,
 before_head public.wb_stock_daily_heads,
 after_head public.wb_stock_daily_heads NOT NULL,
 PRIMARY KEY({OWNER},audit_id), UNIQUE({DAY},revision),
 FOREIGN KEY({DAY},revision) REFERENCES public.wb_stock_daily_revisions({DAY},revision),
 FOREIGN KEY({OWNER},source_run_id) REFERENCES public.wb_stock_runs({OWNER},run_id),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 CHECK((event_kind='daily.created' AND revision=1 AND actor_kind='worker' AND actor_membership_id IS NULL)
 OR (event_kind='daily.corrected' AND revision>1 AND actor_kind='membership' AND actor_membership_id IS NOT NULL))
 );
 """)


CODECS = r"""
CREATE FUNCTION public.wb_stock_evidence_row_bytes(r public.wb_stock_observations) RETURNS bytea
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to('["wb-stock-evidence-row/v1",['||public.wb_daily_ascii(r.quantity_presence)||','
 ||coalesce(r.quantity::text,'null')||'],['||public.wb_daily_ascii(r.in_way_to_client_presence)||','
 ||coalesce(r.in_way_to_client::text,'null')||'],['||public.wb_daily_ascii(r.in_way_from_client_presence)||','
 ||coalesce(r.in_way_from_client::text,'null')||']]','UTF8') $$;
CREATE FUNCTION public.wb_stock_evidence_diff_bytes(rows public.wb_stock_revision_evidence_diffs[]) RETURNS bytea
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to('["wb-stock-evidence-diff/v1",['||coalesce(string_agg(
 '[["nmId","'||d.nm_id::text||'","chrtId","'||coalesce(d.chrt_id::text,'null')
 ||'","warehouseId","'||d.warehouse_id::text||'"],'
 ||public.wb_daily_ascii(d.change_kind)||','||public.wb_daily_ascii(d.before_payload_checksum)
 ||','||public.wb_daily_ascii(d.after_payload_checksum)||']',','
 ORDER BY d.nm_id::text COLLATE "C",coalesce(d.chrt_id::text,'null') COLLATE "C",d.warehouse_id::text COLLATE "C"),'')
 ||']]','UTF8') FROM unnest(rows) d $$;
CREATE FUNCTION public.wb_stock_evidence_proposal_bytes(p public.wb_stock_revision_evidence) RETURNS bytea
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT convert_to('["wb-stock-evidence-proposal/v1",'||p.organization_id::text||','||p.marketplace_account_id::text
 ||','||public.wb_daily_ascii(p.evidence_id::text)||','||public.wb_daily_ascii(p.before_run_id::text)
 ||','||public.wb_daily_ascii(p.before_manifest_checksum)||','||public.wb_daily_ascii(p.after_run_id::text)
 ||','||public.wb_daily_ascii(p.after_manifest_checksum)||','||public.wb_daily_ascii(p.source_kind)
 ||','||public.wb_daily_ascii(p.parser_version)||','||public.wb_daily_ascii(p.grain_version)
 ||','||public.wb_daily_ascii(p.request_checksum)||','||public.wb_daily_ascii(to_char(p.business_date_msk,'YYYY-MM-DD'))
 ||','||trunc(p.before_daily_revision)::text||','||p.proposed_by_membership_id::text
 ||','||public.wb_daily_ascii(p.proposal_command_id::text)||','||public.wb_daily_ascii(p.evidence_document_checksum)
 ||','||public.wb_daily_ascii(p.reviewed_evidence_reference)||','||public.wb_daily_ascii(p.diff_checksum)
 ||','||trunc(p.added_count)::text||','||trunc(p.removed_count)::text||','||trunc(p.changed_count)::text||']','UTF8') $$;
CREATE FUNCTION public.wb_stock_evidence_expected_diff(org integer,account integer,before_id uuid,after_id uuid)
RETURNS TABLE(nm_id bigint,chrt_id bigint,warehouse_id bigint,change_kind text,before_payload_checksum text,after_payload_checksum text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 WITH b AS (SELECT o.nm_id,o.chrt_id,o.warehouse_id,public.wb_stock_evidence_row_bytes(o) payload
 FROM public.wb_stock_observations o WHERE o.organization_id=org AND o.marketplace_account_id=account AND o.run_id=before_id),
 a AS (SELECT o.nm_id,o.chrt_id,o.warehouse_id,public.wb_stock_evidence_row_bytes(o) payload
 FROM public.wb_stock_observations o WHERE o.organization_id=org AND o.marketplace_account_id=account AND o.run_id=after_id)
 SELECT coalesce(b.nm_id,a.nm_id),coalesce(b.chrt_id,a.chrt_id),coalesce(b.warehouse_id,a.warehouse_id),
 CASE WHEN b.nm_id IS NULL THEN 'added' WHEN a.nm_id IS NULL THEN 'removed' ELSE 'changed' END,
 encode(sha256(b.payload),'hex'),encode(sha256(a.payload),'hex')
 FROM b FULL OUTER JOIN a ON b.nm_id=a.nm_id AND b.warehouse_id=a.warehouse_id
 -- Zero is only a join sentinel outside the positive ID domain, never stored or encoded.
 AND coalesce(b.chrt_id,0)=coalesce(a.chrt_id,0)
 WHERE b.payload IS DISTINCT FROM a.payload $$;
CREATE FUNCTION public.wb_daily_run_eligible(org integer,account integer,id uuid,request bytea,checksum text,day date)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM public.wb_stock_runs r WHERE r.organization_id=org AND r.marketplace_account_id=account
 AND r.run_id=id AND r.state='complete' AND r.version=1 AND r.source_kind='wb_warehouse_v1'
 AND r.parser_version='wb-warehouse-stocks/v1' AND r.request_bytes=request AND r.request_checksum=checksum
 AND r.source_observed_at IS NULL AND (r.received_at AT TIME ZONE 'Europe/Moscow')::date=day
 AND r.page_count>0 AND r.page_count=(SELECT count(*) FROM public.wb_stock_pages p
 WHERE p.organization_id=org AND p.marketplace_account_id=account AND p.run_id=id)
 AND NOT EXISTS(SELECT 1 FROM public.wb_stock_pages p WHERE p.organization_id=org AND p.marketplace_account_id=account
 AND p.run_id=id AND ((p.received_at AT TIME ZONE 'Europe/Moscow')::date<>day OR p.source_observed_at IS NOT NULL))) $$;
"""


GUARDS = r"""
CREATE FUNCTION public.wb_daily_insert_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.wb_stock_revision_evidence; d public.wb_stock_revision_evidence_decisions;
 h public.wb_stock_daily_heads; prev public.wb_stock_daily_revisions; r public.wb_stock_runs; parent_xmin xid;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_context_invalid'; END IF;
 IF TG_TABLE_NAME='wb_stock_revision_evidence' THEN
  IF NEW.proposed_at IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_time_invalid'; END IF;
  NEW.proposed_at:=clock_timestamp();
 ELSIF TG_TABLE_NAME='wb_stock_revision_evidence_diffs' THEN
  SELECT xmin INTO parent_xmin FROM public.wb_stock_revision_evidence WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND evidence_id=NEW.evidence_id;
  IF NOT FOUND OR parent_xmin IS DISTINCT FROM pg_current_xact_id()::text::xid THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_already_sealed'; END IF;
 ELSIF TG_TABLE_NAME='wb_stock_revision_evidence_decisions' THEN
  SELECT * INTO p FROM public.wb_stock_revision_evidence WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND evidence_id=NEW.evidence_id;
  IF NOT FOUND OR NEW.decided_at IS NOT NULL OR NEW.reviewed_proposal_checksum IS DISTINCT FROM p.proposal_checksum THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_decision_invalid'; END IF;
  NEW.decided_at:=clock_timestamp();
  IF NEW.decided_at<p.proposed_at THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_time_invalid'; END IF;
 ELSE
  IF NEW.created_at IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_time_invalid'; END IF;
  -- The statement account lock precedes these immutable SELECTs and mutable head lock.
  IF NEW.revision>1 THEN
   SELECT * INTO p FROM public.wb_stock_revision_evidence WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND evidence_id=NEW.evidence_id;
   IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_evidence_invalid'; END IF;
   SELECT * INTO d FROM public.wb_stock_revision_evidence_decisions WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND evidence_id=NEW.evidence_id AND decision_id=NEW.decision_id;
   IF NOT FOUND OR d.outcome<>'accepted' OR d.reviewed_proposal_checksum IS DISTINCT FROM p.proposal_checksum
   OR ROW(p.stock_scope,p.request_checksum,p.request_bytes,p.business_date_msk,p.before_daily_revision,p.after_run_id,p.proposal_checksum)
   IS DISTINCT FROM ROW(NEW.stock_scope,NEW.request_checksum,NEW.request_bytes,NEW.business_date_msk,
    NEW.supersedes_revision,NEW.source_run_id,NEW.proposal_checksum) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_evidence_invalid'; END IF;
  END IF;
  SELECT * INTO h FROM public.wb_stock_daily_heads WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
  AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk FOR UPDATE;
  IF (NEW.revision=1 AND FOUND) OR (NEW.revision>1 AND (NOT FOUND OR h.current_revision IS DISTINCT FROM NEW.supersedes_revision)) THEN
   RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_daily_head_conflict'; END IF;
  IF NEW.revision>1 THEN
   SELECT * INTO prev FROM public.wb_stock_daily_revisions WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
   AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk AND revision=NEW.supersedes_revision;
   IF NOT FOUND OR prev.source_run_id IS DISTINCT FROM p.before_run_id OR prev.source_run_id=NEW.source_run_id THEN
    RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_daily_head_conflict'; END IF;
  END IF;
  SELECT * INTO r FROM public.wb_stock_runs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND run_id=NEW.source_run_id;
  IF NOT FOUND OR NOT public.wb_daily_run_eligible(NEW.organization_id,NEW.marketplace_account_id,
   NEW.source_run_id,NEW.request_bytes,NEW.request_checksum,NEW.business_date_msk)
  OR NEW.effective_observation_at IS DISTINCT FROM r.received_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_run_ineligible'; END IF;
  NEW.created_at:=clock_timestamp();
  IF NEW.revision>1 AND NEW.created_at<d.decided_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_time_invalid'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.wb_daily_head_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE revision_xmin xid; r public.wb_stock_daily_revisions;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_context_invalid'; END IF;
 IF TG_OP='UPDATE' THEN
  IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
  IF ROW(NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.stock_scope,NEW.request_checksum,NEW.business_date_msk)
  IS DISTINCT FROM ROW(OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.stock_scope,OLD.request_checksum,OLD.business_date_msk)
  OR NEW.current_revision<>OLD.current_revision+1 OR NEW.version<>OLD.version+1
  OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
   RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_daily_head_conflict'; END IF;
 ELSE
  IF NEW.current_revision<>1 OR NEW.version<>1 OR NEW.updated_at IS NOT NULL THEN
   RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='wb_daily_head_conflict'; END IF;
 END IF;
 SELECT t.xmin INTO revision_xmin FROM public.wb_stock_daily_revisions t WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
 AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk AND revision=NEW.current_revision;
 IF NOT FOUND OR revision_xmin IS DISTINCT FROM pg_current_xact_id()::text::xid THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_transition_invalid'; END IF;
 SELECT * INTO r FROM public.wb_stock_daily_revisions WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
 AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk AND revision=NEW.current_revision;
 NEW.updated_at:=clock_timestamp();
 IF NEW.updated_at<r.created_at THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_time_invalid'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.wb_daily_audit_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF pg_trigger_depth()<>2 OR NEW.audit_id IS NOT NULL OR NEW.occurred_at IS NOT NULL THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_audit_invalid'; END IF;
 NEW.audit_id:=gen_random_uuid(); NEW.occurred_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE FUNCTION public.wb_daily_emit_audit() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.wb_stock_daily_revisions; prev public.wb_stock_daily_revisions;
 before_h public.wb_stock_daily_heads;
BEGIN
 SELECT * INTO r FROM public.wb_stock_daily_revisions WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
 AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk AND revision=NEW.current_revision;
 IF TG_OP='UPDATE' THEN
  before_h:=OLD;
  SELECT * INTO prev FROM public.wb_stock_daily_revisions WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
  AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk AND revision=OLD.current_revision;
 END IF;
 INSERT INTO public.wb_stock_daily_audit(organization_id,marketplace_account_id,marketplace,stock_scope,
 request_checksum,business_date_msk,revision,source_run_id,event_kind,actor_kind,actor_membership_id,
 before_revision,after_revision,before_head,after_head)
 VALUES(NEW.organization_id,NEW.marketplace_account_id,'wb',NEW.stock_scope,NEW.request_checksum,
 NEW.business_date_msk,r.revision,r.source_run_id,CASE WHEN TG_OP='INSERT' THEN 'daily.created' ELSE 'daily.corrected' END,
 r.actor_kind,r.actor_membership_id,prev,r,before_h,NEW);
 RETURN NULL;
END $$;
"""


WITNESSES = r"""
CREATE FUNCTION public.wb_stock_evidence_graph() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.wb_stock_revision_evidence; d public.wb_stock_revision_evidence_decisions;
 b public.wb_stock_runs; a public.wb_stock_runs; prev public.wb_stock_daily_revisions;
 children public.wb_stock_revision_evidence_diffs[]; added numeric; removed numeric; changed numeric;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_context_invalid'; END IF;
 SELECT * INTO p FROM public.wb_stock_revision_evidence WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND evidence_id=NEW.evidence_id;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_graph_invalid'; END IF;
 SELECT * INTO b FROM public.wb_stock_runs WHERE organization_id=p.organization_id
 AND marketplace_account_id=p.marketplace_account_id AND run_id=p.before_run_id;
 SELECT * INTO a FROM public.wb_stock_runs WHERE organization_id=p.organization_id
 AND marketplace_account_id=p.marketplace_account_id AND run_id=p.after_run_id;
 SELECT * INTO prev FROM public.wb_stock_daily_revisions WHERE organization_id=p.organization_id
 AND marketplace_account_id=p.marketplace_account_id AND stock_scope=p.stock_scope
 AND request_checksum=p.request_checksum AND business_date_msk=p.business_date_msk AND revision=p.before_daily_revision;
 IF NOT FOUND OR prev.source_run_id IS DISTINCT FROM p.before_run_id OR prev.request_bytes IS DISTINCT FROM p.request_bytes
 OR ROW(b.manifest_checksum,a.manifest_checksum) IS DISTINCT FROM ROW(p.before_manifest_checksum,p.after_manifest_checksum)
 OR NOT public.wb_daily_run_eligible(p.organization_id,p.marketplace_account_id,p.before_run_id,p.request_bytes,p.request_checksum,p.business_date_msk)
 OR NOT public.wb_daily_run_eligible(p.organization_id,p.marketplace_account_id,p.after_run_id,p.request_bytes,p.request_checksum,p.business_date_msk)
 OR p.proposed_at<b.finished_at OR p.proposed_at<a.finished_at OR p.proposed_at<prev.created_at THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_parent_invalid'; END IF;
 -- Bidirectional set equality checks every identity and both hashes. A zero net
 -- aggregate does not remove changed identities. Missing and NULL remain distinct.
 IF EXISTS(
  WITH actual AS (SELECT nm_id,chrt_id,warehouse_id,change_kind,before_payload_checksum,after_payload_checksum
   FROM public.wb_stock_revision_evidence_diffs WHERE organization_id=p.organization_id
   AND marketplace_account_id=p.marketplace_account_id AND evidence_id=p.evidence_id),
  expected AS (SELECT * FROM public.wb_stock_evidence_expected_diff(p.organization_id,p.marketplace_account_id,p.before_run_id,p.after_run_id))
  (SELECT * FROM actual EXCEPT SELECT * FROM expected) UNION ALL (SELECT * FROM expected EXCEPT SELECT * FROM actual)
 ) THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_diff_invalid'; END IF;
 SELECT array_agg(t),count(*) FILTER(WHERE change_kind='added'),count(*) FILTER(WHERE change_kind='removed'),
 count(*) FILTER(WHERE change_kind='changed') INTO children,added,removed,changed
 FROM public.wb_stock_revision_evidence_diffs t WHERE organization_id=p.organization_id
 AND marketplace_account_id=p.marketplace_account_id AND evidence_id=p.evidence_id;
 IF ROW(added,removed,changed) IS DISTINCT FROM ROW(p.added_count,p.removed_count,p.changed_count)
 OR added+removed+changed=0 OR p.diff_checksum IS DISTINCT FROM encode(sha256(public.wb_stock_evidence_diff_bytes(children)),'hex')
 OR p.proposal_bytes IS DISTINCT FROM public.wb_stock_evidence_proposal_bytes(p)
 OR p.proposal_checksum IS DISTINCT FROM encode(sha256(public.wb_stock_evidence_proposal_bytes(p)),'hex') THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_bytes_invalid'; END IF;
 SELECT * INTO d FROM public.wb_stock_revision_evidence_decisions WHERE organization_id=p.organization_id
 AND marketplace_account_id=p.marketplace_account_id AND evidence_id=p.evidence_id;
 IF FOUND AND (d.reviewed_proposal_checksum IS DISTINCT FROM p.proposal_checksum OR d.decided_at<p.proposed_at) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_evidence_decision_invalid'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.wb_daily_graph() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.wb_stock_daily_revisions; prev public.wb_stock_daily_revisions;
 h public.wb_stock_daily_heads; prev_h public.wb_stock_daily_heads; a public.wb_stock_daily_audit;
 p public.wb_stock_revision_evidence; d public.wb_stock_revision_evidence_decisions;
 run public.wb_stock_runs; next_revision numeric:=1;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_context_invalid'; END IF;
 FOR r IN SELECT * FROM public.wb_stock_daily_revisions WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
 AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk ORDER BY revision LOOP
  SELECT * INTO run FROM public.wb_stock_runs WHERE organization_id=r.organization_id
  AND marketplace_account_id=r.marketplace_account_id AND run_id=r.source_run_id;
  IF NOT FOUND OR r.revision<>next_revision OR r.effective_observation_at IS DISTINCT FROM run.received_at
  OR r.created_at<run.finished_at OR NOT public.wb_daily_run_eligible(r.organization_id,r.marketplace_account_id,
   r.source_run_id,r.request_bytes,r.request_checksum,r.business_date_msk) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_graph_invalid'; END IF;
  IF r.revision>1 THEN
   SELECT * INTO p FROM public.wb_stock_revision_evidence WHERE organization_id=r.organization_id
   AND marketplace_account_id=r.marketplace_account_id AND evidence_id=r.evidence_id;
   IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_evidence_invalid'; END IF;
   SELECT * INTO d FROM public.wb_stock_revision_evidence_decisions WHERE organization_id=r.organization_id
   AND marketplace_account_id=r.marketplace_account_id AND evidence_id=r.evidence_id AND decision_id=r.decision_id;
   IF NOT FOUND OR d.outcome<>'accepted' OR d.reviewed_proposal_checksum IS DISTINCT FROM p.proposal_checksum
   OR r.created_at<d.decided_at OR r.created_at<prev.created_at OR r.source_run_id=prev.source_run_id
   OR ROW(p.stock_scope,p.request_checksum,p.request_bytes,p.business_date_msk,p.before_daily_revision,
    p.before_run_id,p.after_run_id,p.proposal_checksum)
   IS DISTINCT FROM ROW(r.stock_scope,r.request_checksum,r.request_bytes,r.business_date_msk,
    prev.revision,prev.source_run_id,r.source_run_id,r.proposal_checksum) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_evidence_invalid'; END IF;
  END IF;
  SELECT * INTO a FROM public.wb_stock_daily_audit WHERE organization_id=r.organization_id
  AND marketplace_account_id=r.marketplace_account_id AND stock_scope=r.stock_scope
  AND request_checksum=r.request_checksum AND business_date_msk=r.business_date_msk AND revision=r.revision;
  IF NOT FOUND OR a.before_revision IS DISTINCT FROM prev OR a.after_revision IS DISTINCT FROM r
  OR a.before_head IS DISTINCT FROM prev_h
  OR ROW(a.source_run_id,a.actor_kind,a.actor_membership_id) IS DISTINCT FROM ROW(r.source_run_id,r.actor_kind,r.actor_membership_id)
  OR ROW((a.after_head).organization_id,(a.after_head).marketplace_account_id,(a.after_head).marketplace,
   (a.after_head).stock_scope,(a.after_head).request_checksum,(a.after_head).business_date_msk,
   (a.after_head).current_revision,(a.after_head).version)
  IS DISTINCT FROM ROW(r.organization_id,r.marketplace_account_id,r.marketplace,r.stock_scope,r.request_checksum,
   r.business_date_msk,r.revision,r.revision)
  OR (a.after_head).updated_at<r.created_at OR a.occurred_at<(a.after_head).updated_at
  OR (prev_h.current_revision IS NOT NULL AND (a.after_head).updated_at<prev_h.updated_at) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_audit_invalid'; END IF;
  prev:=r; prev_h:=a.after_head; next_revision:=next_revision+1;
 END LOOP;
 SELECT * INTO h FROM public.wb_stock_daily_heads WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id AND stock_scope=NEW.stock_scope
 AND request_checksum=NEW.request_checksum AND business_date_msk=NEW.business_date_msk;
 IF NOT FOUND OR next_revision=1 OR h IS DISTINCT FROM prev_h THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_unwitnessed_transition'; END IF;
 RETURN NULL;
END $$;
"""


def acl():
    names = ",".join("'" + t + "'" for t in TABLES)
    signatures = ",".join(f"'public.{f}'::regprocedure" for f in PURE + TRIGGERS)
    op.execute(f"""
 DO $$ DECLARE t record; a record; c record; f record; who text;
 BEGIN
 FOR t IN SELECT oid,relowner,relname,relacl FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ({names}) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(t.relacl) WHERE grantee<>t.relowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s',t.relname,who);
  END LOOP;
  FOR c IN SELECT at.attname,x.grantee FROM pg_attribute at CROSS JOIN LATERAL aclexplode(at.attacl) x
  WHERE at.attrelid=t.oid AND x.grantee<>t.relowner LOOP
   who:=CASE WHEN c.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(c.grantee)) END;
   EXECUTE format('REVOKE ALL (%I) ON public.%I FROM %s',c.attname,t.relname,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',t.relname);
 END LOOP;
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE oid IN ({signatures}) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(f.proacl) WHERE grantee<>f.proowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
 END $$;
 """)


def upgrade() -> None:
    op.execute(SCALARS)
    models()
    op.execute(CODECS)
    op.execute(GUARDS)
    op.execute(WITNESSES)
    for table in TABLES:
        predicate = "organization_id::text COLLATE \"C\"=current_setting('app.organization_id',true) COLLATE \"C\" AND marketplace_account_id::text COLLATE \"C\"=current_setting('app.marketplace_account_id',true) COLLATE \"C\""
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_owner ON public.{table} USING({predicate}) WITH CHECK({predicate})")
        op.execute(f"CREATE TRIGGER a_owner_lock BEFORE INSERT OR UPDATE OR DELETE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.wb_daily_lock()")
        for operation in ("DELETE", "TRUNCATE"):
            each = "STATEMENT" if operation == "TRUNCATE" else "ROW"
            op.execute(f"CREATE TRIGGER z_no_{operation.lower()} BEFORE {operation} ON public.{table} FOR EACH {each} EXECUTE FUNCTION public.wb_daily_immutable()")
        if table == "wb_stock_daily_heads":
            guard, operations = "wb_daily_head_guard", "INSERT OR UPDATE"
            op.execute(f"CREATE TRIGGER c_audit AFTER INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_daily_emit_audit()")
        else:
            guard = "wb_daily_audit_guard" if table.endswith("_audit") else "wb_daily_insert_guard"
            operations = "INSERT"
            op.execute(f"CREATE TRIGGER z_no_update BEFORE UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.wb_daily_immutable()")
        op.execute(f"CREATE TRIGGER b_guard BEFORE {operations} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{guard}()")
        graph = "wb_stock_evidence_graph" if table.startswith("wb_stock_revision_evidence") else "wb_daily_graph"
        op.execute(f"CREATE CONSTRAINT TRIGGER z_graph AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.{graph}()")
    acl()


def downgrade() -> None:
    # Forced-RLS owners without BYPASSRLS fail rather than seeing filtered emptiness.
    op.execute("SET LOCAL row_security=off")
    op.execute("LOCK TABLE " + ",".join("public." + t for t in sorted(TABLES)) + " IN ACCESS EXCLUSIVE MODE")
    checks = " OR ".join(f"EXISTS(SELECT 1 FROM public.{t})" for t in TABLES)
    op.execute(f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='wb_daily_downgrade_history'; END IF; END $$")
    for table in TABLES:
        for trigger in ("a_owner_lock", "b_guard", "z_no_delete", "z_no_truncate", "z_graph"):
            op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        op.execute(f"DROP TRIGGER {'c_audit' if table == 'wb_stock_daily_heads' else 'z_no_update'} ON public.{table}")
    for function in TRIGGERS:
        op.execute(f"DROP FUNCTION public.{function}")
    # Composite-typed helpers must go before their table row types.
    for function in reversed(PURE[4:]):
        op.execute(f"DROP FUNCTION public.{function}")
    op.execute("ALTER TABLE public.wb_stock_daily_revisions DROP CONSTRAINT wb_daily_accepted_decision_fk")
    for table in ("wb_stock_daily_audit", "wb_stock_revision_evidence_decisions", "wb_stock_revision_evidence_diffs",
                  "wb_stock_revision_evidence", "wb_stock_daily_heads", "wb_stock_daily_revisions"):
        op.execute(f"DROP TABLE public.{table}")
    for function in reversed(PURE[:4]):
        op.execute(f"DROP FUNCTION public.{function}")
