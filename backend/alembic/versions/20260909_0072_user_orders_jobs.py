"""Session-bound Orders authority; source only, final integration UNVERIFIED."""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0072"
down_revision = "20260909_0071"
branch_labels = depends_on = None

TABLES = ("user_orders_jobs", "user_orders_job_authorities", "user_orders_job_attempts", "user_orders_job_audit")
PURE = ("user_orders_ascii(text)", "user_orders_text(text,integer)",
        "user_orders_request_bytes(public.user_orders_jobs)")
TRIGGERS = ("user_orders_row_guard()", "user_orders_transition_witness()")

SQL = r'''
CREATE FUNCTION public.user_orders_ascii(value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE result text:='"'; c integer; ch text; i integer;
BEGIN
 IF value IS NULL THEN RETURN 'null'; END IF;
 FOR i IN 1..char_length(value) LOOP
  ch:=substr(value,i,1); c:=ascii(ch);
  IF c=0 OR c BETWEEN 55296 AND 57343 THEN RAISE EXCEPTION 'orders_job_invalid';
  ELSIF c=34 THEN result:=result||E'\\"';
  ELSIF c=92 THEN result:=result||E'\\\\';
  ELSIF c=8 THEN result:=result||E'\\b';
  ELSIF c=9 THEN result:=result||E'\\t';
  ELSIF c=10 THEN result:=result||E'\\n';
  ELSIF c=12 THEN result:=result||E'\\f';
  ELSIF c=13 THEN result:=result||E'\\r';
  ELSIF c<32 OR c BETWEEN 127 AND 65535 THEN result:=result||E'\\u'||lpad(to_hex(c),4,'0');
  ELSIF c>65535 THEN c:=c-65536; result:=result||E'\\u'||lpad(to_hex(55296+c/1024),4,'0')||E'\\u'||lpad(to_hex(56320+c%1024),4,'0');
  ELSE result:=result||ch; END IF;
 END LOOP;
 RETURN result||'"';
END $$;
CREATE FUNCTION public.user_orders_text(value text,maximum integer) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(char_length(value) BETWEEN 1 AND maximum AND
 value=btrim(value,U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'),false);
$$;

CREATE TABLE public.user_orders_jobs (
 job_id uuid PRIMARY KEY CHECK (substr(job_id::text,15,1)='4' AND substr(job_id::text,20,1) IN ('8','9','a','b')),
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0), provider text COLLATE "C" NOT NULL,
 operation_kind text COLLATE "C" NOT NULL CHECK(operation_kind='orders.sync.v1'),
 required_permission text COLLATE "C" NOT NULL CHECK(required_permission='sync:run'),
 initiator_user_id varchar(64) NOT NULL REFERENCES public.lk_users(user_id),
 initiator_membership_id integer NOT NULL,
 initiator_session_id varchar(64) NOT NULL REFERENCES public.lk_sessions(session_id),
 idempotency_key uuid NOT NULL CHECK(substr(idempotency_key::text,15,1)='4' AND substr(idempotency_key::text,20,1) IN ('8','9','a','b')),
 request_schema_version smallint NOT NULL CHECK(request_schema_version=1), request_bytes bytea NOT NULL,
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum=encode(sha256(request_bytes),'hex')),
 source_kind text COLLATE "C" NOT NULL, adapter_version text COLLATE "C" NOT NULL CHECK(public.user_orders_text(adapter_version,128)),
 mapping_version text COLLATE "C" NOT NULL, source_contract_version text COLLATE "C" NOT NULL CHECK(public.user_orders_text(source_contract_version,128)),
 source_date_from date, source_statuses text[], source_limit integer, source_page bigint,
 requested_from timestamptz CHECK(requested_from IS NULL), requested_to timestamptz CHECK(requested_to IS NULL),
 expected_external_account_id varchar(128) NOT NULL CHECK(public.user_orders_text(expected_external_account_id,128)),
 expected_credential_ref varchar(255) CHECK(expected_credential_ref IS NULL OR public.user_orders_text(expected_credential_ref,255)),
 created_at timestamptz NOT NULL CHECK(isfinite(created_at)),
 authority_expires_at timestamptz NOT NULL CHECK(isfinite(authority_expires_at) AND authority_expires_at>created_at),
 policy_reference text COLLATE "C" NOT NULL CHECK(public.user_orders_text(policy_reference,128)),
 policy_version integer NOT NULL CHECK(policy_version>0),
 max_attempts integer NOT NULL CHECK(max_attempts BETWEEN 1 AND 1000),
 lease_seconds integer NOT NULL CHECK(lease_seconds>0),
 retry_backoff_seconds integer[] NOT NULL,
 state text COLLATE "C" NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','revoked','expired','blocked')),
 version bigint NOT NULL CHECK(version>0), attempt_count integer NOT NULL CHECK(attempt_count BETWEEN 0 AND max_attempts),
 current_attempt_id uuid, next_attempt_at timestamptz, completed_at timestamptz,
 safe_reason text COLLATE "C", result_sync_run_id bigint, result_coverage_state text COLLATE "C",
 FOREIGN KEY(organization_id,marketplace_account_id,provider) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,initiator_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 FOREIGN KEY(organization_id,marketplace_account_id,result_sync_run_id) REFERENCES public.order_sync_runs(organization_id,marketplace_account_id,sync_run_id),
 UNIQUE(organization_id,marketplace_account_id,job_id),
 UNIQUE(organization_id,marketplace_account_id,initiator_membership_id,operation_kind,idempotency_key),
 CHECK(source_date_from IS NULL OR (isfinite(source_date_from) AND source_date_from BETWEEN DATE '0001-01-01' AND DATE '9999-12-31')),
 CHECK((provider='wb' AND source_kind='wb-statistics-supplier-orders' AND mapping_version='wb-statistics-status-v1' AND source_date_from IS NOT NULL AND source_statuses IS NULL AND source_limit IS NULL AND source_page IS NULL)
 OR (provider='avito' AND source_kind='avito-order-management' AND mapping_version='avito-order-status-v1' AND source_statuses IS NOT NULL AND source_limit IS NOT NULL AND source_page IS NOT NULL AND source_limit BETWEEN 1 AND 20 AND source_page BETWEEN 1 AND 9223372036854775807)),
 CHECK(cardinality(retry_backoff_seconds)=max_attempts-1 AND array_position(retry_backoff_seconds,NULL) IS NULL AND 0<=ALL(retry_backoff_seconds)),
 CHECK(next_attempt_at IS NULL OR (isfinite(next_attempt_at) AND next_attempt_at>=created_at AND next_attempt_at<authority_expires_at)),
 CHECK(completed_at IS NULL OR (isfinite(completed_at) AND completed_at>=created_at)),
 CHECK((state IN ('queued','running') AND completed_at IS NULL AND result_sync_run_id IS NULL AND result_coverage_state IS NULL)
 OR (state NOT IN ('queued','running') AND completed_at IS NOT NULL AND next_attempt_at IS NULL)),
 CHECK((state='queued' AND current_attempt_id IS NULL AND next_attempt_at IS NOT NULL) OR
 (state='running' AND current_attempt_id IS NOT NULL AND next_attempt_at IS NULL AND safe_reason IS NULL) OR state NOT IN ('queued','running')),
 CHECK((state='succeeded' AND current_attempt_id IS NOT NULL AND result_sync_run_id IS NOT NULL AND result_coverage_state IS NOT NULL AND result_coverage_state IN ('complete','partial') AND safe_reason IS NULL)
 OR (state<>'succeeded' AND result_sync_run_id IS NULL AND result_coverage_state IS NULL)),
 CHECK(state<>'failed' OR current_attempt_id IS NOT NULL)
);
CREATE FUNCTION public.user_orders_request_bytes(j public.user_orders_jobs) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE source text; statuses text;
BEGIN
 IF j.provider='wb' THEN source:='{"dateFrom":'||public.user_orders_ascii(to_char(j.source_date_from,'YYYY-MM-DD'))||'}';
 ELSE
  SELECT string_agg(public.user_orders_ascii(s),',' ORDER BY ord) INTO statuses FROM unnest(j.source_statuses) WITH ORDINALITY a(s,ord);
  source:='{"dateFrom":'||public.user_orders_ascii(to_char(j.source_date_from,'YYYY-MM-DD'))||',"limit":'||j.source_limit::text||',"page":'||j.source_page::text||',"statuses":['||COALESCE(statuses,'')||']}';
 END IF;
 RETURN convert_to('{"adapterVersion":'||public.user_orders_ascii(j.adapter_version)||',"mappingVersion":'||public.user_orders_ascii(j.mapping_version)||',"marketplaceAccountId":'||j.marketplace_account_id::text||',"operationKind":"orders.sync.v1","organizationId":'||j.organization_id::text||',"provider":'||public.user_orders_ascii(j.provider)||',"requestedFrom":null,"requestedTo":null,"schemaVersion":1,"sourceContractVersion":'||public.user_orders_ascii(j.source_contract_version)||',"sourceKind":'||public.user_orders_ascii(j.source_kind)||',"sourceRequest":'||source||'}','UTF8');
END $$;
ALTER TABLE public.user_orders_jobs ADD CONSTRAINT ck_user_orders_request CHECK(request_bytes=public.user_orders_request_bytes(user_orders_jobs));

CREATE TABLE public.user_orders_job_authorities (
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 credential_id uuid NOT NULL REFERENCES public.marketplace_account_credentials(credential_id),
 credential_kind text COLLATE "C" NOT NULL, provider text COLLATE "C" NOT NULL,
 generation bigint NOT NULL CHECK(generation>0), payload_schema_version smallint NOT NULL CHECK(payload_schema_version=1),
 expires_at timestamptz,
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,credential_kind),
 UNIQUE(organization_id,marketplace_account_id,job_id,credential_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES public.user_orders_jobs(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,provider) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 CHECK((provider='wb' AND credential_kind='wb_api' AND expires_at IS NULL) OR
 (provider='avito' AND credential_kind='avito_oauth_access' AND expires_at IS NOT NULL AND isfinite(expires_at)))
);
CREATE TABLE public.user_orders_job_attempts (
 attempt_id uuid PRIMARY KEY CHECK(substr(attempt_id::text,15,1)='4' AND substr(attempt_id::text,20,1) IN ('8','9','a','b')),
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 attempt_number integer NOT NULL CHECK(attempt_number>0), claimant_token uuid NOT NULL CHECK(substr(claimant_token::text,15,1)='4' AND substr(claimant_token::text,20,1) IN ('8','9','a','b')),
 state text COLLATE "C" NOT NULL CHECK(state IN ('claimed','succeeded','failed','abandoned','cancelled','revoked','expired','blocked')),
 version bigint NOT NULL CHECK(version>0), job_version_after bigint NOT NULL CHECK(job_version_after>1),
 claimed_at timestamptz NOT NULL CHECK(isfinite(claimed_at)),
 lease_expires_at timestamptz NOT NULL CHECK(isfinite(lease_expires_at) AND lease_expires_at>claimed_at),
 finished_at timestamptz, safe_reason text COLLATE "C", result_sync_run_id bigint, result_coverage_state text COLLATE "C",
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES public.user_orders_jobs(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,result_sync_run_id) REFERENCES public.order_sync_runs(organization_id,marketplace_account_id,sync_run_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,attempt_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,attempt_number),
 UNIQUE(organization_id,marketplace_account_id,job_id,claimant_token),
 CHECK((state='claimed' AND finished_at IS NULL AND safe_reason IS NULL AND result_sync_run_id IS NULL AND result_coverage_state IS NULL)
 OR(state<>'claimed' AND finished_at IS NOT NULL AND isfinite(finished_at) AND finished_at>=claimed_at)),
 CHECK((state='succeeded' AND result_sync_run_id IS NOT NULL AND result_coverage_state IS NOT NULL AND result_coverage_state IN ('complete','partial') AND safe_reason IS NULL)
 OR(state<>'succeeded' AND result_sync_run_id IS NULL AND result_coverage_state IS NULL))
);
CREATE UNIQUE INDEX uq_user_orders_current ON public.user_orders_job_attempts(organization_id,marketplace_account_id,job_id) WHERE state='claimed';
ALTER TABLE public.user_orders_jobs ADD CONSTRAINT fk_user_orders_current FOREIGN KEY(organization_id,marketplace_account_id,job_id,current_attempt_id)
 REFERENCES public.user_orders_job_attempts(organization_id,marketplace_account_id,job_id,attempt_id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX ix_user_orders_due ON public.user_orders_jobs(organization_id,marketplace_account_id,next_attempt_at,job_id) WHERE state='queued';
CREATE TABLE public.user_orders_job_audit (
 event_id uuid PRIMARY KEY CHECK(substr(event_id::text,15,1)='4' AND substr(event_id::text,20,1) IN ('8','9','a','b')),
 organization_id integer NOT NULL, marketplace_account_id integer NOT NULL, job_id uuid NOT NULL,
 job_version_after bigint NOT NULL CHECK(job_version_after>0), event_kind text COLLATE "C" NOT NULL,
 occurred_at timestamptz NOT NULL CHECK(isfinite(occurred_at)),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind IN ('membership','delegated_worker')), actor_membership_id integer,
 attempt_id uuid, before_state text COLLATE "C", after_state text COLLATE "C" NOT NULL, reason text COLLATE "C",
 attempt_version_before bigint, attempt_version_after bigint, attempt_state_before text COLLATE "C", attempt_state_after text COLLATE "C",
 attempt_count_after integer NOT NULL, next_attempt_at timestamptz, lease_expires_at timestamptz,
 result_sync_run_id bigint, result_coverage_state text COLLATE "C",
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES public.user_orders_jobs(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,attempt_id) REFERENCES public.user_orders_job_attempts(organization_id,marketplace_account_id,job_id,attempt_id),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,job_version_after),
 CHECK((actor_kind='membership')=(actor_membership_id IS NOT NULL)),
 CHECK((attempt_id IS NULL AND attempt_version_before IS NULL AND attempt_version_after IS NULL AND attempt_state_before IS NULL AND attempt_state_after IS NULL AND lease_expires_at IS NULL)
 OR(attempt_id IS NOT NULL AND attempt_version_after IS NOT NULL AND attempt_state_after IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE FUNCTION public.user_orders_row_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.user_orders_jobs; c record; now_at timestamptz; s text; mutable text[];
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'orders_job_history_immutable'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' OR NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
 OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN RAISE EXCEPTION 'orders_job_context_invalid'; END IF;
 IF TG_TABLE_NAME='user_orders_jobs' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.state<>'queued' OR NEW.version<>1 OR NEW.attempt_count<>0 OR NEW.current_attempt_id IS NOT NULL OR NEW.safe_reason IS NOT NULL OR NEW.next_attempt_at IS DISTINCT FROM NEW.created_at THEN RAISE EXCEPTION 'orders_job_creation_invalid'; END IF;
   -- Metadata only; service acquires user/membership/session/account/credential locks first.
   IF NOT EXISTS(SELECT 1 FROM public.lk_users u JOIN public.iam_memberships m ON m.user_id=u.user_id AND m.organization_id=u.organization_id
       JOIN public.lk_sessions s ON s.user_id=u.user_id WHERE u.user_id=NEW.initiator_user_id AND u.organization_id=NEW.organization_id
       AND m.membership_id=NEW.initiator_membership_id AND s.session_id=NEW.initiator_session_id AND u.is_active AND m.is_active AND s.revoked_at IS NULL AND s.expires_at>=NEW.authority_expires_at)
   THEN RAISE EXCEPTION 'orders_job_principal_invalid'; END IF;
   now_at:=clock_timestamp();
   IF NEW.created_at>now_at OR NEW.authority_expires_at<=now_at OR NEW.created_at+make_interval(secs=>NEW.lease_seconds)>NEW.authority_expires_at THEN RAISE EXCEPTION 'orders_job_deadline_invalid'; END IF;
   IF NOT EXISTS(SELECT 1 FROM public.marketplace_accounts a WHERE a.organization_id=NEW.organization_id AND a.marketplace_account_id=NEW.marketplace_account_id
       AND a.marketplace=NEW.provider AND a.status='connected' AND a.external_account_id COLLATE "C"=NEW.expected_external_account_id COLLATE "C" AND a.credential_ref COLLATE "C" IS NOT DISTINCT FROM NEW.expected_credential_ref COLLATE "C") THEN RAISE EXCEPTION 'orders_job_binding_invalid'; END IF;
  ELSE
   mutable:=ARRAY['state','version','attempt_count','current_attempt_id','next_attempt_at','completed_at','safe_reason','result_sync_run_id','result_coverage_state'];
   IF OLD.state NOT IN ('queued','running') OR NEW.version<>OLD.version+1 OR (to_jsonb(OLD)-mutable) IS DISTINCT FROM (to_jsonb(NEW)-mutable) THEN RAISE EXCEPTION 'orders_job_history_immutable'; END IF;
  END IF;
  IF array_ndims(NEW.retry_backoff_seconds)>1 OR (cardinality(NEW.retry_backoff_seconds)>0 AND array_lower(NEW.retry_backoff_seconds,1)<>1) THEN RAISE EXCEPTION 'orders_job_policy_invalid'; END IF;
  IF NEW.provider='avito' THEN
   IF array_ndims(NEW.source_statuses)>1 OR (cardinality(NEW.source_statuses)>0 AND array_lower(NEW.source_statuses,1)<>1) THEN RAISE EXCEPTION 'orders_job_source_invalid'; END IF;
   FOREACH s IN ARRAY NEW.source_statuses LOOP IF NOT public.user_orders_text(s,128) OR position(',' IN s)>0 THEN RAISE EXCEPTION 'orders_job_source_invalid'; END IF; END LOOP;
  END IF;
 ELSIF TG_TABLE_NAME='user_orders_job_authorities' THEN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'orders_job_history_immutable'; END IF;
  SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
  SELECT organization_id,marketplace_account_id,provider,credential_kind,generation,payload_schema_version,expires_at,revoked_at INTO c FROM public.marketplace_account_credentials WHERE credential_id=NEW.credential_id;
  IF j.version<>1 OR j.provider<>NEW.provider OR NOT FOUND OR (c.organization_id,c.marketplace_account_id,c.provider,c.credential_kind,c.generation,c.payload_schema_version,c.expires_at,c.revoked_at)
     IS DISTINCT FROM (NEW.organization_id,NEW.marketplace_account_id,NEW.provider,NEW.credential_kind,NEW.generation,NEW.payload_schema_version,NEW.expires_at,NULL::timestamptz)
     OR (NEW.expires_at IS NOT NULL AND NEW.expires_at<j.authority_expires_at) THEN RAISE EXCEPTION 'orders_job_authority_invalid'; END IF;
 ELSIF TG_TABLE_NAME='user_orders_job_attempts' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.state<>'claimed' OR NEW.version<>1 THEN RAISE EXCEPTION 'orders_job_attempt_invalid'; END IF;
  ELSE
   mutable:=ARRAY['state','version','job_version_after','lease_expires_at','finished_at','safe_reason','result_sync_run_id','result_coverage_state'];
   IF OLD.state<>'claimed' OR NEW.version<>OLD.version+1 OR NEW.job_version_after<=OLD.job_version_after OR (to_jsonb(OLD)-mutable) IS DISTINCT FROM (to_jsonb(NEW)-mutable) THEN RAISE EXCEPTION 'orders_job_history_immutable'; END IF;
  END IF;
 ELSE
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'orders_job_history_immutable'; END IF;
  SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
  IF j.version<>NEW.job_version_after OR (NEW.actor_kind='membership' AND NEW.actor_membership_id<>j.initiator_membership_id)
    OR NEW.occurred_at>clock_timestamp() OR EXISTS(SELECT 1 FROM public.user_orders_job_audit a WHERE a.organization_id=NEW.organization_id
      AND a.marketplace_account_id=NEW.marketplace_account_id AND a.job_id=NEW.job_id AND a.job_version_after=NEW.job_version_after-1 AND a.occurred_at>NEW.occurred_at)
    THEN RAISE EXCEPTION 'orders_job_audit_invalid'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.user_orders_transition_witness() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.user_orders_jobs; a public.user_orders_job_audit; t public.user_orders_job_attempts; run public.order_sync_runs;
 old_state text; old_count integer; old_attempt uuid; old_version bigint; old_lease timestamptz; old_attempt_state text;
 expected_event text; valid boolean:=false;
BEGIN
 SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
 IF (SELECT count(*) FROM public.user_orders_job_authorities WHERE organization_id=j.organization_id AND marketplace_account_id=j.marketplace_account_id AND job_id=j.job_id)<>1 THEN RAISE EXCEPTION 'orders_job_authority_required'; END IF;
 IF TG_TABLE_NAME='user_orders_job_authorities' THEN RETURN NULL; END IF;
 IF TG_TABLE_NAME='user_orders_job_audit' THEN
  -- Reciprocal evidence: an audit cannot invent a future version; uniqueness plus
  -- every captured job transition witness prevents filling or reusing old versions.
  IF NEW.job_version_after>j.version THEN RAISE EXCEPTION 'orders_job_audit_orphan'; END IF;
  RETURN NULL;
 END IF;
 IF TG_TABLE_NAME='user_orders_job_attempts' THEN
  SELECT * INTO STRICT a FROM public.user_orders_job_audit WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id AND job_version_after=NEW.job_version_after;
  IF TG_OP='UPDATE' THEN old_version:=OLD.version; old_attempt_state:=OLD.state; END IF;
  IF (a.attempt_id,a.attempt_version_before,a.attempt_version_after,a.attempt_state_before,a.attempt_state_after,a.lease_expires_at,a.result_sync_run_id,a.result_coverage_state)
  IS DISTINCT FROM (NEW.attempt_id,old_version,NEW.version,old_attempt_state,NEW.state,NEW.lease_expires_at,NEW.result_sync_run_id,NEW.result_coverage_state)
  OR (NEW.state<>'claimed' AND (NEW.finished_at,NEW.safe_reason) IS DISTINCT FROM (a.occurred_at,a.reason))
  OR NEW.lease_expires_at>j.authority_expires_at OR NEW.attempt_number>a.attempt_count_after
  THEN RAISE EXCEPTION 'orders_job_attempt_witness_invalid'; END IF;
  IF TG_OP='INSERT' AND (a.event_kind<>'job.claimed' OR NEW.attempt_number<>a.attempt_count_after OR NEW.claimed_at<>a.occurred_at OR NEW.lease_expires_at<>a.occurred_at+make_interval(secs=>j.lease_seconds)) THEN RAISE EXCEPTION 'orders_job_claim_invalid'; END IF;
  IF TG_OP='UPDATE' AND ((a.event_kind='job.lease_renewed' AND (OLD.lease_expires_at<=a.occurred_at OR NEW.lease_expires_at<=OLD.lease_expires_at OR NEW.lease_expires_at<>a.occurred_at+make_interval(secs=>j.lease_seconds)))
    OR(a.event_kind<>'job.lease_renewed' AND NEW.lease_expires_at<>OLD.lease_expires_at)) THEN RAISE EXCEPTION 'orders_job_lease_invalid'; END IF;
  RETURN NULL;
 END IF;
 SELECT * INTO STRICT a FROM public.user_orders_job_audit WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id AND job_version_after=NEW.version;
 IF TG_OP='UPDATE' THEN old_state:=OLD.state; old_count:=OLD.attempt_count; old_attempt:=OLD.current_attempt_id; ELSE old_count:=0; END IF;
 IF (a.before_state,a.after_state,a.attempt_count_after,a.next_attempt_at,a.reason,a.result_sync_run_id,a.result_coverage_state)
 IS DISTINCT FROM (old_state,NEW.state,NEW.attempt_count,NEW.next_attempt_at,NEW.safe_reason,NEW.result_sync_run_id,NEW.result_coverage_state)
 OR a.occurred_at<NEW.created_at OR (NEW.completed_at IS NOT NULL AND NEW.completed_at<>a.occurred_at) THEN RAISE EXCEPTION 'orders_job_transition_witness_invalid'; END IF;
 IF a.attempt_id IS NOT NULL THEN
  SELECT * INTO STRICT t FROM public.user_orders_job_attempts WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id AND attempt_id=a.attempt_id;
  IF a.attempt_version_after>t.version OR a.attempt_count_after<>t.attempt_number THEN RAISE EXCEPTION 'orders_job_attempt_orphan'; END IF;
 END IF;
 CASE a.event_kind
 WHEN 'job.created' THEN valid:=TG_OP='INSERT' AND a.actor_kind='membership' AND a.reason IS NULL AND a.attempt_id IS NULL AND a.occurred_at=NEW.created_at;
 WHEN 'job.claimed' THEN valid:=old_state='queued' AND NEW.state='running' AND NEW.attempt_count=old_count+1 AND a.attempt_id=NEW.current_attempt_id AND a.attempt_state_before IS NULL AND a.attempt_state_after='claimed' AND a.attempt_version_after=1 AND a.reason IS NULL AND a.occurred_at>=OLD.next_attempt_at AND a.lease_expires_at<=NEW.authority_expires_at;
 WHEN 'job.lease_renewed' THEN valid:=old_state='running' AND NEW.state='running' AND a.attempt_id=old_attempt AND NEW.current_attempt_id=old_attempt AND a.attempt_state_after='claimed' AND a.reason IS NULL;
 WHEN 'job.retry_scheduled' THEN valid:=old_state='running' AND NEW.state='queued' AND a.attempt_id=old_attempt AND a.attempt_state_after='failed' AND a.reason IN ('SOURCE_READ_UNAVAILABLE','SOURCE_READ_RATE_LIMITED','LOCAL_TRANSIENT_FAILURE') AND NEW.attempt_count<NEW.max_attempts AND a.occurred_at<a.lease_expires_at AND a.occurred_at<NEW.authority_expires_at;
 WHEN 'job.reclaimed' THEN valid:=old_state='running' AND NEW.state='queued' AND a.attempt_id=old_attempt AND a.attempt_state_after='abandoned' AND a.reason='LEASE_EXPIRED' AND a.lease_expires_at<=a.occurred_at AND NEW.attempt_count<NEW.max_attempts AND a.occurred_at<NEW.authority_expires_at;
 WHEN 'job.succeeded' THEN valid:=old_state='running' AND NEW.state='succeeded' AND a.attempt_id=old_attempt AND NEW.current_attempt_id=old_attempt AND a.attempt_state_after='succeeded' AND a.reason IS NULL AND a.occurred_at<a.lease_expires_at AND a.occurred_at<NEW.authority_expires_at;
 WHEN 'job.failed' THEN valid:=old_state='running' AND NEW.state='failed' AND a.attempt_id=old_attempt AND NEW.current_attempt_id=old_attempt AND a.attempt_state_after IN ('failed','abandoned') AND a.reason IN ('PERMANENT_SOURCE_FAILURE','RETRY_BUDGET_EXHAUSTED') AND (a.reason<>'RETRY_BUDGET_EXHAUSTED' OR NEW.attempt_count=NEW.max_attempts) AND ((a.attempt_state_after='abandoned' AND a.lease_expires_at<=a.occurred_at) OR (a.attempt_state_after='failed' AND a.occurred_at<a.lease_expires_at)) AND a.occurred_at<NEW.authority_expires_at;
 WHEN 'job.cancelled' THEN valid:=old_state IN ('queued','running') AND NEW.state='cancelled' AND a.actor_kind='membership' AND a.reason='USER_CANCELLED';
 WHEN 'job.revoked' THEN valid:=old_state IN ('queued','running') AND NEW.state='revoked' AND a.reason='AUTHORITY_REVOKED';
 WHEN 'job.expired' THEN valid:=old_state IN ('queued','running') AND NEW.state='expired' AND a.reason='AUTHORITY_EXPIRED';
 WHEN 'job.blocked' THEN valid:=old_state IN ('queued','running') AND NEW.state='blocked' AND a.reason IN ('PERMISSION_DENIED','ACCOUNT_SCOPE_DENIED','ACCOUNT_DISCONNECTED','BINDING_CHANGED','SOURCE_CONTRACT_UNAVAILABLE');
 ELSE valid:=false;
 END CASE;
 IF valid IS DISTINCT FROM true THEN RAISE EXCEPTION 'orders_job_transition_invalid'; END IF;
 IF a.event_kind NOT IN ('job.created','job.cancelled','job.revoked') AND a.actor_kind<>'delegated_worker' THEN RAISE EXCEPTION 'orders_job_actor_invalid'; END IF;
 IF a.event_kind NOT IN ('job.created','job.claimed') AND NEW.attempt_count<>old_count THEN RAISE EXCEPTION 'orders_job_count_invalid'; END IF;
 IF a.event_kind NOT IN ('job.created','job.claimed') AND old_state='running' AND (a.attempt_id IS DISTINCT FROM old_attempt OR a.attempt_state_before<>'claimed' OR a.attempt_version_after<>a.attempt_version_before+1) THEN RAISE EXCEPTION 'orders_job_fence_invalid'; END IF;
 IF a.event_kind IN ('job.cancelled','job.revoked','job.expired','job.blocked') THEN
  IF NEW.current_attempt_id IS DISTINCT FROM old_attempt OR (old_state='queued' AND a.attempt_id IS NOT NULL) OR (old_state='running' AND a.attempt_state_after<>NEW.state) THEN RAISE EXCEPTION 'orders_job_closure_invalid'; END IF;
 END IF;
 IF a.event_kind IN ('job.retry_scheduled','job.reclaimed') AND (NEW.next_attempt_at IS DISTINCT FROM a.occurred_at+make_interval(secs=>NEW.retry_backoff_seconds[NEW.attempt_count]) OR NEW.next_attempt_at+make_interval(secs=>NEW.lease_seconds)>NEW.authority_expires_at) THEN RAISE EXCEPTION 'orders_job_retry_invalid'; END IF;
 IF NEW.state='succeeded' THEN
  SELECT * INTO STRICT run FROM public.order_sync_runs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.result_sync_run_id;
  IF (run.marketplace,run.source_kind,run.adapter_version,run.mapping_version,run.source_contract_version,run.source_run_key,run.account_binding_schema_version,run.account_binding_external_account_id,run.account_binding_credential_ref,run.requested_from,run.requested_to,run.state)
    IS DISTINCT FROM (NEW.provider,NEW.source_kind,NEW.adapter_version,NEW.mapping_version,NEW.source_contract_version,'orders-job-v1:'||NEW.job_id::text||':'||a.attempt_id::text,1::smallint,NEW.expected_external_account_id::text,NEW.expected_credential_ref::text,NULL::timestamptz,NULL::timestamptz,NEW.result_coverage_state)
    OR run.completed_at IS NULL OR run.completed_at>a.occurred_at OR (NEW.result_coverage_state='complete' AND run.manifest_state<>'complete') THEN RAISE EXCEPTION 'orders_job_result_invalid'; END IF;
 END IF;
 RETURN NULL;
END $$;
'''


def upgrade():
    # CREATE (never OR REPLACE/IF NOT EXISTS) rejects collisions and schema drift.
    # DDL preserves literal JSON colons instead of interpreting them as binds.
    # Escape percent signs for DDL's context interpolation, including SQL modulo.
    op.execute(sa.DDL(SQL.replace("%", "%%")))
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        predicate = "organization_id::text COLLATE \"C\"=current_setting('app.organization_id',true) COLLATE \"C\" AND marketplace_account_id::text COLLATE \"C\"=current_setting('app.marketplace_account_id',true) COLLATE \"C\""
        op.execute(f"CREATE POLICY user_orders_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})")
        op.execute(f"CREATE TRIGGER user_orders_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.user_orders_row_guard()")
        op.execute(f"CREATE TRIGGER user_orders_no_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.user_orders_row_guard()")
        op.execute(f"CREATE CONSTRAINT TRIGGER user_orders_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.user_orders_transition_witness()")
        # Remove inherited grants on new objects only; runtime script grants exact rights.
        op.execute(sa.text("""DO $$ DECLARE a record; BEGIN FOR a IN
          SELECT DISTINCT x.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
          WHERE c.oid='public.""" + table + """'::regclass AND x.grantee NOT IN (0,c.relowner)
          LOOP EXECUTE format('REVOKE ALL ON TABLE public.""" + table + """ FROM %I',pg_get_userbyid(a.grantee)); END LOOP; END $$;"""))
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
    for signature in (*PURE, *TRIGGERS):
        op.execute(sa.text("""DO $$ DECLARE a record; BEGIN FOR a IN
          SELECT DISTINCT x.grantee FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x
          WHERE p.oid='public.""" + signature + """'::regprocedure AND x.grantee NOT IN (0,p.proowner)
          LOOP EXECUTE format('REVOKE ALL ON FUNCTION public.""" + signature + """ FROM %I',pg_get_userbyid(a.grantee)); END LOOP; END $$;"""))
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")


def downgrade():
    op.execute("LOCK TABLE " + ",".join("public." + t for t in TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security=off")
    for table in TABLES:
        op.execute(sa.text(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION 'orders_job_history_present'; END IF; END $$;"))
    for table in TABLES:
        for trigger in ("user_orders_guard", "user_orders_no_truncate", "user_orders_witness"):
            op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
    for signature in TRIGGERS:
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("ALTER TABLE public.user_orders_jobs DROP CONSTRAINT fk_user_orders_current")
    op.execute("ALTER TABLE public.user_orders_jobs DROP CONSTRAINT ck_user_orders_request")
    op.execute("DROP FUNCTION public.user_orders_request_bytes(public.user_orders_jobs)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE public.{table}")
    for signature in reversed(PURE[:2]):
        op.execute(f"DROP FUNCTION public.{signature}")
