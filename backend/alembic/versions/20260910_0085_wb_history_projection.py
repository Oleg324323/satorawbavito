"""Explicit positive-partial history projection; no runtime authority defaults."""
import hashlib
import re
import runpy
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "20260910_0085"
down_revision = "20260910_0084"
branch_labels = depends_on = None

OPERATION = "orders.wb-history.project.v1"
TABLE = "user_orders_history_selection_pages"
JOB_COLUMNS = ("history_job_id", "history_run_id", "history_selection_digest", "history_page_count",
    "history_terminal_page_id", "history_cursor_page", "history_cursor_ordinal", "history_progress_version")
AUDIT_COLUMNS = ("history_page_index", "history_page_id", "history_first_ordinal", "history_next_ordinal",
    "history_input_checksum", "history_result_sync_run_id", "history_reconciliation_count")
DECISION_COLUMNS = ("history_decision_version", "history_pre_order_version", "history_pre_sync_run_id",
    "history_pre_observation_id", "history_outcome")
CODECS = r'''
CREATE FUNCTION public.wb_history_projection_header_bytes(p public.wb_live_history_pages) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF p.state IS DISTINCT FROM 'published' OR p.receipt IS NULL OR p.published_at IS NULL OR NOT isfinite(p.published_at)
  OR p.source IS DISTINCT FROM 'wb-statistics-supplier-orders' THEN RAISE EXCEPTION 'orders_history_invalid'; END IF;
 RETURN convert_to(public.wb_state_json(jsonb_build_object(
  'organization_id',p.organization_id,'marketplace_account_id',p.marketplace_account_id,
  'job_id',p.job_id::text,'page_id',p.page_id::text,'run_id',p.run_id::text,
  'credential_id',p.credential_id::text,'credential_generation',p.credential_generation,
  'account_incarnation',p.account_incarnation,'request_checksum',p.request_checksum,
  'raw_checksum',p.receipt->>'raw_checksum','input_date_from',p.input_date_from,
  'next_date_from',p.receipt->>'next_date_from','row_count',(p.receipt->>'row_count')::integer,
  'terminal',(p.receipt->>'terminal')::boolean,'state',p.state,
  'published_at',to_char(p.published_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')||'+00:00')),'UTF8');
END $$;
CREATE FUNCTION public.wb_history_projection_chunk_bytes(org integer,account integer,job uuid,page uuid,first_ordinal integer) RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.wb_live_history_pages; n integer; expected integer; rows jsonb; actual integer; lo integer; hi integer;
BEGIN
 SELECT * INTO STRICT p FROM public.wb_live_history_pages WHERE organization_id=org AND marketplace_account_id=account
  AND job_id=job AND source='wb-statistics-supplier-orders' AND page_id=page AND state='published';
 n:=(p.receipt->>'row_count')::integer;
 IF first_ordinal IS NULL OR first_ordinal<0 OR first_ordinal%1000<>0 OR first_ordinal>n
  OR (first_ordinal=n AND n<>0) THEN RAISE EXCEPTION 'orders_history_invalid'; END IF;
 expected:=least(1000,n-first_ordinal);
 SELECT COALESCE(jsonb_agg(jsonb_build_object('ordinal',r.ordinal,'observation',r.observation,
  'semantic_checksum',r.semantic_checksum,'source_row_checksum',r.source_row_checksum,
  'nm_id',r.nm_id,'barcode',r.barcode) ORDER BY r.ordinal),'[]'::jsonb),count(*),min(r.ordinal),max(r.ordinal)
 INTO rows,actual,lo,hi FROM public.wb_live_history_rows r WHERE r.organization_id=org AND r.marketplace_account_id=account
  AND r.job_id=job AND r.source='wb-statistics-supplier-orders' AND r.page_id=page
  AND r.ordinal>=first_ordinal AND r.ordinal<first_ordinal+expected;
 IF actual<>expected OR (expected>0 AND (lo<>first_ordinal OR hi<>first_ordinal+expected-1)) THEN
  RAISE EXCEPTION 'orders_history_invalid'; END IF;
 RETURN convert_to(public.wb_state_json(jsonb_build_object('version','wb-history-chunk-v1',
  'page',convert_from(public.wb_history_projection_header_bytes(p),'UTF8')::jsonb,
  'first_ordinal',first_ordinal,'rows',rows)),'UTF8');
END $$;
'''

SCHEMA = r'''
ALTER TABLE public.user_orders_jobs
 ADD COLUMN history_job_id uuid, ADD COLUMN history_run_id uuid, ADD COLUMN history_selection_digest text COLLATE "C",
 ADD COLUMN history_page_count integer, ADD COLUMN history_terminal_page_id uuid,
 ADD COLUMN history_cursor_page integer, ADD COLUMN history_cursor_ordinal integer, ADD COLUMN history_progress_version bigint;
ALTER TABLE public.user_orders_jobs DROP CONSTRAINT user_orders_jobs_operation_kind_check;
ALTER TABLE public.user_orders_jobs ADD CONSTRAINT user_orders_jobs_operation_kind_check
 CHECK(operation_kind IN ('orders.sync.v1','orders.wb-history.project.v1'));
DO $$ DECLARE c record; matches integer:=0; BEGIN
 FOR c IN SELECT conname,pg_get_constraintdef(oid) def FROM pg_constraint WHERE conrelid='public.user_orders_jobs'::regclass
  AND contype='c' AND pg_get_constraintdef(oid) LIKE '%source_date_from IS NOT NULL%' LOOP
  matches:=matches+1;
  EXECUTE format('ALTER TABLE public.user_orders_jobs DROP CONSTRAINT %I',c.conname);
  EXECUTE format('ALTER TABLE public.user_orders_jobs ADD CONSTRAINT %I CHECK ((operation_kind=''orders.sync.v1'' AND (%s)) OR
   (operation_kind=''orders.wb-history.project.v1'' AND provider=''wb'' AND source_kind=''wb-statistics-supplier-orders''
    AND adapter_version=''wb-statistics-orders-stream-v1'' AND mapping_version=''wb-statistics-status-v1''
    AND source_contract_version=''wb-history-positive-partial-v1'' AND source_date_from IS NULL
    AND source_statuses IS NULL AND source_limit IS NULL AND source_page IS NULL))',c.conname,substr(c.def,8,length(c.def)-8));
 END LOOP;
 IF matches<>1 THEN RAISE EXCEPTION 'orders_history_schema_mismatch'; END IF;
END $$;
ALTER TABLE public.user_orders_jobs ADD CONSTRAINT ck_orders_history_shape CHECK(
 (operation_kind='orders.sync.v1' AND num_nonnulls(history_job_id,history_run_id,history_selection_digest,history_page_count,
  history_terminal_page_id,history_cursor_page,history_cursor_ordinal,history_progress_version)=0)
 OR (operation_kind='orders.wb-history.project.v1' AND num_nonnulls(history_job_id,history_run_id,history_selection_digest,history_page_count,
  history_terminal_page_id,history_cursor_page,history_cursor_ordinal,history_progress_version)=8
  AND history_selection_digest ~ '^[0-9a-f]{64}$' AND history_page_count>0
  AND history_cursor_page BETWEEN 0 AND history_page_count AND history_cursor_ordinal BETWEEN 0 AND 99000
  AND history_cursor_ordinal%1000=0 AND history_progress_version>=0
  AND (history_cursor_page<history_page_count OR (state='succeeded' AND history_cursor_ordinal=0))));
CREATE TABLE public.user_orders_history_selection_pages (
 organization_id integer NOT NULL,marketplace_account_id integer NOT NULL,job_id uuid NOT NULL,
 page_index integer NOT NULL CHECK(page_index>=0),history_job_id uuid NOT NULL,
 history_source text COLLATE "C" NOT NULL CHECK(history_source='wb-statistics-supplier-orders'),
 history_page_id uuid NOT NULL,history_run_id uuid NOT NULL,header_checksum text COLLATE "C" NOT NULL CHECK(header_checksum ~ '^[0-9a-f]{64}$'),
 PRIMARY KEY(organization_id,marketplace_account_id,job_id,page_index),
 UNIQUE(organization_id,marketplace_account_id,job_id,history_page_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,page_index,history_page_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES public.user_orders_jobs(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,history_job_id,history_source,history_page_id)
 REFERENCES public.wb_live_history_pages(organization_id,marketplace_account_id,job_id,source,page_id)
);
ALTER TABLE public.user_orders_history_selection_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_orders_history_selection_pages FORCE ROW LEVEL SECURITY;
CREATE POLICY user_orders_scope ON public.user_orders_history_selection_pages
 USING(organization_id::text=current_setting('app.organization_id',true) AND marketplace_account_id::text=current_setting('app.marketplace_account_id',true))
 WITH CHECK(organization_id::text=current_setting('app.organization_id',true) AND marketplace_account_id::text=current_setting('app.marketplace_account_id',true));
ALTER TABLE public.user_orders_job_audit ADD COLUMN history_page_index integer,ADD COLUMN history_page_id uuid,
 ADD COLUMN history_first_ordinal integer,ADD COLUMN history_next_ordinal integer,ADD COLUMN history_input_checksum text COLLATE "C",
 ADD COLUMN history_result_sync_run_id bigint,ADD COLUMN history_reconciliation_count integer;
ALTER TABLE public.user_orders_job_audit ADD CONSTRAINT ck_orders_history_receipt CHECK(
 (event_kind<>'job.chunk_committed' AND num_nonnulls(history_page_index,history_page_id,history_first_ordinal,history_next_ordinal,
  history_input_checksum,history_result_sync_run_id,history_reconciliation_count)=0)
 OR (event_kind='job.chunk_committed' AND num_nonnulls(history_page_index,history_page_id,history_first_ordinal,history_next_ordinal,
  history_input_checksum,history_result_sync_run_id,history_reconciliation_count)=7
  AND history_page_index>=0 AND history_first_ordinal BETWEEN 0 AND 99000 AND history_first_ordinal%1000=0
  AND history_next_ordinal BETWEEN history_first_ordinal AND history_first_ordinal+1000
  AND history_next_ordinal<=100000 AND history_input_checksum ~ '^[0-9a-f]{64}$' AND history_reconciliation_count>=0));
ALTER TABLE public.user_orders_job_audit ADD CONSTRAINT fk_orders_history_receipt_selection
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,history_page_index,history_page_id)
 REFERENCES public.user_orders_history_selection_pages(organization_id,marketplace_account_id,job_id,page_index,history_page_id);
ALTER TABLE public.user_orders_job_audit ADD CONSTRAINT fk_orders_history_receipt_run
 FOREIGN KEY(organization_id,marketplace_account_id,history_result_sync_run_id)
 REFERENCES public.order_sync_runs(organization_id,marketplace_account_id,sync_run_id);
CREATE UNIQUE INDEX uq_orders_history_chunk_receipt ON public.user_orders_job_audit
 (organization_id,marketplace_account_id,job_id,history_page_index,history_first_ordinal) WHERE event_kind='job.chunk_committed';
CREATE INDEX ix_orders_history_receipt_run ON public.user_orders_job_audit
 (organization_id,marketplace_account_id,history_result_sync_run_id,job_id,history_page_index,history_first_ordinal)
 WHERE event_kind='job.chunk_committed';
'''

SELECTION = r'''
CREATE FUNCTION public.wb_history_projection_selection_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.user_orders_jobs;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'orders_history_immutable'; END IF;
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true)
  OR current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'orders_history_context_invalid'; END IF;
 SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id FOR UPDATE;
 IF j.operation_kind<>'orders.wb-history.project.v1' OR j.version<>1 OR NEW.page_index>=j.history_page_count
  OR NEW.history_job_id<>j.history_job_id OR NEW.history_run_id<>j.history_run_id THEN RAISE EXCEPTION 'orders_history_invalid'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.wb_history_projection_selection_valid(j public.user_orders_jobs) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public SET timezone='Etc/GMT-3' AS $$
DECLARE c public.user_orders_job_authorities; pages jsonb; n integer; bad boolean; digest text;
BEGIN
 IF j.operation_kind<>'orders.wb-history.project.v1' THEN RETURN false; END IF;
 SELECT * INTO STRICT c FROM public.user_orders_job_authorities WHERE organization_id=j.organization_id
  AND marketplace_account_id=j.marketplace_account_id AND job_id=j.job_id;
 SELECT count(*),COALESCE(bool_or(h.state<>'published' OR h.run_id<>j.history_run_id
   OR h.credential_id<>c.credential_id OR h.credential_generation<>c.generation
   OR h.account_incarnation<>a.ingestion_binding_version OR p.history_job_id<>j.history_job_id
   OR p.history_run_id<>j.history_run_id OR p.header_checksum<>encode(sha256(public.wb_history_projection_header_bytes(h)),'hex')
   OR p.page_index>=j.history_page_count
   OR ((h.receipt->>'terminal')::boolean IS DISTINCT FROM (p.page_index=j.history_page_count-1))
   OR (p.page_index=j.history_page_count-1 AND h.page_id<>j.history_terminal_page_id)
   OR (p.page_index>0 AND NOT EXISTS(SELECT FROM public.user_orders_history_selection_pages prev
       JOIN public.wb_live_history_pages hp ON (hp.organization_id,hp.marketplace_account_id,hp.job_id,hp.source,hp.page_id)=
        (prev.organization_id,prev.marketplace_account_id,prev.history_job_id,prev.history_source,prev.history_page_id)
       WHERE prev.organization_id=p.organization_id AND prev.marketplace_account_id=p.marketplace_account_id
        AND prev.job_id=p.job_id AND prev.page_index=p.page_index-1 AND hp.receipt->>'next_date_from'=h.input_date_from))
   OR (NOT (h.receipt->>'terminal')::boolean AND (h.receipt->>'next_date_from')::timestamptz<=h.input_date_from::timestamptz)),true),
  jsonb_agg(jsonb_build_object('pageId',p.history_page_id::text,'headerChecksum',p.header_checksum) ORDER BY p.page_index)
 INTO n,bad,pages FROM public.user_orders_history_selection_pages p JOIN public.wb_live_history_pages h
 ON (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)=
 (p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id)
 JOIN public.marketplace_accounts a ON (a.organization_id,a.marketplace_account_id)=(p.organization_id,p.marketplace_account_id)
 WHERE p.organization_id=j.organization_id AND p.marketplace_account_id=j.marketplace_account_id AND p.job_id=j.job_id;
 IF n<>j.history_page_count OR bad THEN RETURN false; END IF;
 -- The frozen selection is the entire published run, not an arbitrary valid
 -- suffix. Read at most one row beyond its declared cardinality.
 IF (SELECT count(*) FROM (SELECT h.page_id FROM public.wb_live_history_pages h
  WHERE h.organization_id=j.organization_id AND h.marketplace_account_id=j.marketplace_account_id
   AND h.job_id=j.history_job_id AND h.source='wb-statistics-supplier-orders' AND h.run_id=j.history_run_id
   AND h.state='published' LIMIT j.history_page_count::bigint+1) bounded)<>j.history_page_count THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT FROM public.user_orders_history_selection_pages first_page
  JOIN public.wb_live_history_pages h ON (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)=
   (first_page.organization_id,first_page.marketplace_account_id,first_page.history_job_id,first_page.history_source,first_page.history_page_id)
  JOIN public.wb_live_history_requests request ON request.organization_id=h.organization_id
   AND request.marketplace_account_id=h.marketplace_account_id AND request.job_id=h.job_id AND request.source=h.source
   AND request.date_from=h.input_date_from
  WHERE first_page.organization_id=j.organization_id AND first_page.marketplace_account_id=j.marketplace_account_id
   AND first_page.job_id=j.job_id AND first_page.page_index=0) THEN RETURN false; END IF;
 digest:=encode(sha256(convert_to(public.wb_state_json(jsonb_build_object('version','wb-history-selection-v1',
  'organizationId',j.organization_id,'marketplaceAccountId',j.marketplace_account_id,'historyJobId',j.history_job_id::text,
  'sourceRunId',j.history_run_id::text,'pages',pages)),'UTF8')),'hex');
 RETURN digest=j.history_selection_digest;
END $$;
CREATE FUNCTION public.wb_history_projection_selection_witness() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.user_orders_jobs;
BEGIN
 SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
 IF NOT public.wb_history_projection_selection_valid(j) THEN RAISE EXCEPTION 'orders_history_selection_invalid'; END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER orders_history_selection_guard BEFORE INSERT OR UPDATE OR DELETE ON public.user_orders_history_selection_pages
 FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_selection_guard();
CREATE TRIGGER orders_history_selection_no_truncate BEFORE TRUNCATE ON public.user_orders_history_selection_pages
 FOR EACH STATEMENT EXECUTE FUNCTION public.wb_history_projection_selection_guard();
CREATE CONSTRAINT TRIGGER orders_history_selection_witness AFTER INSERT ON public.user_orders_history_selection_pages
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_selection_witness();
'''

RECEIPTS = r'''
CREATE FUNCTION public.wb_history_projection_receipt_valid(j public.user_orders_jobs,a public.user_orders_job_audit) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.wb_live_history_pages; selected public.user_orders_history_selection_pages; run public.order_sync_runs;
 c public.user_orders_job_authorities; n integer; checksum text; members integer; valid_members integer;
BEGIN
 IF j.operation_kind<>'orders.wb-history.project.v1' OR a.event_kind<>'job.chunk_committed' THEN RETURN false; END IF;
 SELECT * INTO STRICT selected FROM public.user_orders_history_selection_pages WHERE organization_id=a.organization_id
  AND marketplace_account_id=a.marketplace_account_id AND job_id=a.job_id AND page_index=a.history_page_index
  AND history_page_id=a.history_page_id;
 SELECT * INTO STRICT p FROM public.wb_live_history_pages WHERE organization_id=selected.organization_id
  AND marketplace_account_id=selected.marketplace_account_id AND job_id=selected.history_job_id
  AND source=selected.history_source AND page_id=selected.history_page_id AND run_id=selected.history_run_id AND state='published';
 SELECT * INTO STRICT c FROM public.user_orders_job_authorities WHERE organization_id=j.organization_id
  AND marketplace_account_id=j.marketplace_account_id AND job_id=j.job_id;
 IF p.job_id<>j.history_job_id OR p.run_id<>j.history_run_id OR p.credential_id<>c.credential_id
  OR p.credential_generation<>c.generation OR selected.header_checksum<>encode(sha256(public.wb_history_projection_header_bytes(p)),'hex')
  THEN RETURN false; END IF;
 n:=least(1000,(p.receipt->>'row_count')::integer-a.history_first_ordinal);
 IF a.history_next_ordinal<>a.history_first_ordinal+n THEN RETURN false; END IF;
 checksum:=encode(sha256(public.wb_history_projection_chunk_bytes(j.organization_id,j.marketplace_account_id,
  p.job_id,p.page_id,a.history_first_ordinal)),'hex');
 IF checksum<>a.history_input_checksum THEN RETURN false; END IF;
 SELECT * INTO STRICT run FROM public.order_sync_runs WHERE organization_id=j.organization_id
  AND marketplace_account_id=j.marketplace_account_id AND sync_run_id=a.history_result_sync_run_id;
 IF (run.marketplace,run.source_kind,run.adapter_version,run.mapping_version,run.source_contract_version,run.source_run_key,
  run.source_snapshot,run.payload_checksum,run.state,run.manifest_state,run.page_count,run.order_count,run.item_count,
  run.expected_order_count,run.requested_from,run.requested_to,run.account_binding_schema_version,
  run.account_binding_external_account_id,run.account_binding_credential_ref)
 IS DISTINCT FROM ('wb'::text,'wb-statistics-supplier-orders'::text,'wb-statistics-orders-stream-v1'::text,
  'wb-statistics-status-v1'::text,'wb-history-positive-partial-v1'::text,
  'wb-history-chunk-v1:'||p.job_id::text||':'||p.page_id::text||':'||a.history_first_ordinal::text||':'||a.history_next_ordinal::text,
  'wb-history-run-v1:'||p.job_id::text||':'||p.run_id::text,checksum,'partial'::text,'partial'::text,
  1::integer,n::integer,n::integer,NULL::integer,NULL::timestamptz,NULL::timestamptz,1::smallint,
  j.expected_external_account_id::text,j.expected_credential_ref::text)
 OR run.completed_at IS NULL OR NOT isfinite(run.completed_at) OR run.completed_at<run.started_at
 OR run.completed_at>a.occurred_at THEN RETURN false; END IF;
 -- Counts describe normalized observations, not child memberships. A semantic
 -- replay may reference an observation captured earlier under another run.
 SELECT count(*) INTO members FROM public.order_sync_memberships m WHERE m.organization_id=j.organization_id
  AND m.marketplace_account_id=j.marketplace_account_id AND m.sync_run_id=run.sync_run_id AND m.order_item_id IS NULL;
 SELECT count(*) INTO valid_members FROM public.wb_live_history_rows h
 JOIN public.marketplace_orders o ON o.organization_id=h.organization_id AND o.marketplace_account_id=h.marketplace_account_id
  AND o.marketplace='wb' AND o.external_order_id=h.srid
 JOIN public.order_sync_memberships m ON m.organization_id=o.organization_id AND m.marketplace_account_id=o.marketplace_account_id
  AND m.order_id=o.order_id AND m.sync_run_id=run.sync_run_id AND m.order_item_id IS NULL AND m.coverage_role='observed'
 JOIN public.order_observations v ON v.organization_id=m.organization_id AND v.marketplace_account_id=m.marketplace_account_id
  AND v.order_id=m.order_id AND v.observation_id=m.observation_id AND v.order_item_id IS NULL
 WHERE h.organization_id=j.organization_id AND h.marketplace_account_id=j.marketplace_account_id AND h.job_id=p.job_id
  AND h.source=p.source AND h.page_id=p.page_id AND h.ordinal>=a.history_first_ordinal AND h.ordinal<a.history_next_ordinal
  AND v.source_kind=p.source AND v.adapter_version=j.adapter_version AND v.payload_checksum=h.semantic_checksum
  AND public.wb_history_projection_semantic(v.normalized_evidence)=public.wb_history_projection_semantic(h.observation)
  AND jsonb_array_length(v.normalized_evidence->'observation'->'items')=1
  AND encode(sha256(convert_to(public.wb_state_json((v.normalized_evidence #- '{observation,observed_at}')
    ||jsonb_build_object('checksum_version','orders-observation-v1')),'UTF8')),'hex')=h.semantic_checksum
  AND m.observed_at=(h.observation->'observation'->>'observed_at')::timestamptz
  AND m.history_decision_version=1 AND m.history_outcome IN ('initial_projection','semantic_replay','reconciliation_required');
 RETURN members=n AND valid_members=n AND a.history_reconciliation_count=(SELECT count(*) FROM public.order_sync_memberships m
  WHERE m.organization_id=j.organization_id AND m.marketplace_account_id=j.marketplace_account_id AND m.sync_run_id=run.sync_run_id
   AND m.order_item_id IS NULL AND m.history_decision_version=1 AND m.history_outcome='reconciliation_required');
END $$;
CREATE FUNCTION public.wb_history_projection_chunk_transition(newj public.user_orders_jobs,oldj public.user_orders_jobs,a public.user_orders_job_audit) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.wb_live_history_pages; stop integer; next_page integer; next_ordinal integer; terminal boolean;
BEGIN
 IF newj.operation_kind<>'orders.wb-history.project.v1' OR oldj.state<>'running'
  OR newj.state NOT IN ('running','succeeded') OR newj.attempt_count<>oldj.attempt_count
  OR newj.current_attempt_id IS DISTINCT FROM oldj.current_attempt_id OR a.attempt_id IS DISTINCT FROM oldj.current_attempt_id
  OR a.reason IS NOT NULL OR a.occurred_at>=a.lease_expires_at OR a.occurred_at>=newj.authority_expires_at
  OR a.history_page_index<>oldj.history_cursor_page OR a.history_first_ordinal<>oldj.history_cursor_ordinal
  OR newj.history_progress_version<>oldj.history_progress_version+1
  OR NOT public.wb_history_projection_receipt_valid(newj,a) THEN RETURN false; END IF;
 SELECT h.* INTO STRICT p FROM public.user_orders_history_selection_pages s JOIN public.wb_live_history_pages h
  ON (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)=
  (s.organization_id,s.marketplace_account_id,s.history_job_id,s.history_source,s.history_page_id)
  WHERE s.organization_id=a.organization_id AND s.marketplace_account_id=a.marketplace_account_id AND s.job_id=a.job_id
   AND s.page_index=a.history_page_index AND s.history_page_id=a.history_page_id;
 stop:=(p.receipt->>'row_count')::integer; terminal:=(p.receipt->>'terminal')::boolean;
 next_page:=oldj.history_cursor_page+CASE WHEN a.history_next_ordinal=stop THEN 1 ELSE 0 END;
 next_ordinal:=CASE WHEN next_page<>oldj.history_cursor_page THEN 0 ELSE a.history_next_ordinal END;
 RETURN newj.history_cursor_page=next_page AND newj.history_cursor_ordinal=next_ordinal
  AND (terminal IS NOT DISTINCT FROM (newj.state='succeeded'))
  AND (a.attempt_state_after IS NOT DISTINCT FROM CASE WHEN terminal THEN 'succeeded' ELSE 'claimed' END)
  AND (NOT terminal OR (next_page=newj.history_page_count AND newj.result_sync_run_id=a.history_result_sync_run_id
    AND newj.result_coverage_state='partial'));
END $$;
CREATE FUNCTION public.wb_history_projection_run_witness() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.order_sync_runs; a public.user_orders_job_audit; j public.user_orders_jobs;
BEGIN
 SELECT * INTO STRICT r FROM public.order_sync_runs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.sync_run_id;
 IF r.source_contract_version<>'wb-history-positive-partial-v1' THEN RETURN NULL; END IF;
 SELECT * INTO a FROM public.user_orders_job_audit WHERE organization_id=r.organization_id
  AND marketplace_account_id=r.marketplace_account_id AND history_result_sync_run_id=r.sync_run_id
  AND event_kind='job.chunk_committed' ORDER BY job_id,history_page_index,history_first_ordinal LIMIT 1;
 IF NOT FOUND THEN RAISE EXCEPTION 'orders_history_receipt_required'; END IF;
 SELECT * INTO STRICT j FROM public.user_orders_jobs WHERE organization_id=a.organization_id
  AND marketplace_account_id=a.marketplace_account_id AND job_id=a.job_id;
 IF NOT public.wb_history_projection_receipt_valid(j,a) THEN RAISE EXCEPTION 'orders_history_receipt_invalid'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER orders_history_run_witness AFTER INSERT OR UPDATE ON public.order_sync_runs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_run_witness();
'''

DECISIONS = r'''
ALTER TABLE public.order_sync_memberships ADD COLUMN history_decision_version smallint,
 ADD COLUMN history_pre_order_version bigint,ADD COLUMN history_pre_sync_run_id bigint,
 ADD COLUMN history_pre_observation_id bigint,ADD COLUMN history_outcome text COLLATE "C";
ALTER TABLE public.order_sync_memberships ADD CONSTRAINT ck_orders_history_decision CHECK(
 num_nonnulls(history_decision_version,history_pre_order_version,history_pre_sync_run_id,history_pre_observation_id,history_outcome)=0
 OR (order_item_id IS NULL AND history_decision_version=1 AND history_pre_order_version>=1
  AND history_outcome IN ('initial_projection','semantic_replay','reconciliation_required')
  AND num_nonnulls(history_decision_version,history_pre_order_version,history_outcome)=3
  AND (history_pre_sync_run_id IS NULL)=(history_pre_observation_id IS NULL)));
ALTER TABLE public.order_sync_memberships ADD CONSTRAINT fk_orders_history_pre_run
 FOREIGN KEY(organization_id,marketplace_account_id,history_pre_sync_run_id)
 REFERENCES public.order_sync_runs(organization_id,marketplace_account_id,sync_run_id);
ALTER TABLE public.order_sync_memberships ADD CONSTRAINT fk_orders_history_pre_observation
 FOREIGN KEY(organization_id,marketplace_account_id,order_id,history_pre_observation_id)
 REFERENCES public.order_observations(organization_id,marketplace_account_id,order_id,observation_id);

CREATE FUNCTION public.wb_history_projection_keys(value jsonb,keys text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(jsonb_typeof(value)='object' AND value ?& keys AND value-keys='{}'::jsonb,false);
$$;
CREATE FUNCTION public.wb_history_projection_semantic(value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r jsonb; identity jsonb; status jsonb; item jsonb; item_identity jsonb; sorted jsonb; instant text;
 cancelled boolean; keys text[]; seen text[]:=ARRAY[]::text[];
BEGIN
 -- Closed existing serialize_observation v1, with canonical UTC6 and sorted
 -- items. Only then is ignoring observed_at equivalent to _semantic_row.
 IF NOT public.wb_history_projection_keys(value,ARRAY['evidence_schema_version','observation'])
  OR value->>'evidence_schema_version'<>'1' OR jsonb_typeof(value->'evidence_schema_version')<>'number' THEN
  RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 r:=value->'observation';
 IF NOT public.wb_history_projection_keys(r,ARRAY['identity','source_kind','adapter_version','source_revision','effective_at',
  'observed_at','status','items','wb_is_cancelled','wb_cancel_evidence_present']) THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 identity:=r->'identity';
 IF NOT public.wb_history_projection_keys(identity,ARRAY['organization_id','marketplace_account_id','marketplace','external_order_id'])
  OR identity->>'marketplace'<>'wb' OR jsonb_typeof(identity->'marketplace')<>'string'
  OR jsonb_typeof(identity->'organization_id')<>'number' OR (identity->>'organization_id') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(identity->'marketplace_account_id')<>'number' OR (identity->>'marketplace_account_id') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(identity->'external_order_id')<>'string' OR NOT public.user_orders_text(identity->>'external_order_id',2147483647)
  OR r->>'source_kind'<>'wb-statistics-supplier-orders' OR jsonb_typeof(r->'source_kind')<>'string'
  OR jsonb_typeof(r->'adapter_version')<>'string' OR NOT public.user_orders_text(r->>'adapter_version',2147483647)
  OR (r->'source_revision'<>'null'::jsonb AND (jsonb_typeof(r->'source_revision')<>'string' OR NOT public.user_orders_text(r->>'source_revision',2147483647)))
  OR jsonb_typeof(r->'wb_is_cancelled')<>'boolean' OR jsonb_typeof(r->'wb_cancel_evidence_present')<>'boolean'
  OR jsonb_typeof(r->'items')<>'array' THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 FOREACH instant IN ARRAY ARRAY[r->>'effective_at',r->>'observed_at'] LOOP
  IF instant IS NOT NULL AND (instant !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$'
   OR NOT isfinite(instant::timestamptz) OR instant<>to_char(instant::timestamptz AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')||'+00:00')
   THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 END LOOP;
 IF jsonb_typeof(r->'observed_at')<>'string'
  OR (r->'effective_at'<>'null'::jsonb AND jsonb_typeof(r->'effective_at')<>'string') THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 cancelled:=(r->>'wb_is_cancelled')::boolean OR (r->>'wb_cancel_evidence_present')::boolean;
 status:=r->'status';
 IF NOT public.wb_history_projection_keys(status,ARRAY['raw_status','canonical_status','mapping_state','mapping_version','evidence_source'])
  OR (status->'raw_status'<>'null'::jsonb AND (jsonb_typeof(status->'raw_status')<>'string' OR NOT public.user_orders_text(status->>'raw_status',2147483647)))
  OR status->>'mapping_version'<>'wb-statistics-status-v1' OR status->>'evidence_source'<>'wb-statistics-supplier-orders'
  OR jsonb_typeof(status->'mapping_version')<>'string' OR jsonb_typeof(status->'evidence_source')<>'string'
  OR jsonb_typeof(status->'mapping_state')<>'string'
  OR status->>'mapping_state' IS DISTINCT FROM (CASE WHEN cancelled THEN 'mapped' ELSE 'unmapped' END)
  OR status->'canonical_status' IS DISTINCT FROM (CASE WHEN cancelled THEN '"cancelled"'::jsonb ELSE 'null'::jsonb END)
  THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(r->'items') LOOP
  item_identity:=item->'identity';
  IF NOT public.wb_history_projection_keys(item,ARRAY['identity','quantity','stable_order_line_id','stable_unit_id'])
   OR NOT public.wb_history_projection_keys(item_identity,ARRAY['order_identity','source_line_key','external_item_id','occurrence_index'])
   OR item_identity->'order_identity' IS DISTINCT FROM identity OR jsonb_typeof(item->'quantity')<>'number'
   OR (item->>'quantity') !~ '^[1-9][0-9]*$' OR item->'stable_order_line_id'<>'null'::jsonb
   OR jsonb_typeof(item->'stable_unit_id')<>'string' OR NOT public.user_orders_text(item->>'stable_unit_id',2147483647)
   OR item_identity->>'source_line_key' IS DISTINCT FROM 'wb:unit:'||char_length(item->>'stable_unit_id')::text||':'||(item->>'stable_unit_id')
   OR jsonb_typeof(item_identity->'source_line_key')<>'string'
   OR (item_identity->'external_item_id'<>'null'::jsonb AND (jsonb_typeof(item_identity->'external_item_id')<>'string'
    OR NOT public.user_orders_text(item_identity->>'external_item_id',2147483647)))
   OR jsonb_typeof(item_identity->'occurrence_index')<>'number' OR (item_identity->>'occurrence_index') !~ '^(0|[1-9][0-9]*)$'
   OR (item_identity->>'source_line_key')=ANY(seen) THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
  seen:=array_append(seen,item_identity->>'source_line_key');
 END LOOP;
 SELECT COALESCE(jsonb_agg(i ORDER BY i->'identity'->>'source_line_key' COLLATE "C"),'[]'::jsonb) INTO sorted
  FROM jsonb_array_elements(r->'items') i;
 IF sorted IS DISTINCT FROM r->'items' THEN RAISE EXCEPTION 'orders_history_semantic_invalid'; END IF;
 RETURN r-'observed_at';
END $$;

CREATE FUNCTION public.wb_history_projection_decision_capture() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.order_sync_runs; p public.marketplace_orders; v public.order_observations; prior public.order_observations;
 current_semantic jsonb; incoming jsonb;
BEGIN
 IF num_nonnulls(NEW.history_decision_version,NEW.history_pre_order_version,NEW.history_pre_sync_run_id,
  NEW.history_pre_observation_id,NEW.history_outcome)<>0 THEN RAISE EXCEPTION 'orders_history_proof_input'; END IF;
 SELECT * INTO STRICT r FROM public.order_sync_runs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.sync_run_id;
 IF r.source_contract_version<>'wb-history-positive-partial-v1' THEN RETURN NEW; END IF;
 IF (r.marketplace,r.source_kind,r.adapter_version,r.mapping_version,r.state) IS DISTINCT FROM
  ('wb'::text,'wb-statistics-supplier-orders'::text,'wb-statistics-orders-stream-v1'::text,'wb-statistics-status-v1'::text,'staging'::text)
  OR NEW.coverage_role<>'observed' THEN RAISE EXCEPTION 'orders_history_decision_invalid'; END IF;
 IF NEW.order_item_id IS NOT NULL THEN
  IF NOT EXISTS(SELECT FROM public.order_sync_memberships m WHERE m.organization_id=NEW.organization_id
   AND m.marketplace_account_id=NEW.marketplace_account_id AND m.sync_run_id=NEW.sync_run_id AND m.order_id=NEW.order_id
   AND m.order_item_id IS NULL AND m.history_outcome='initial_projection') THEN RAISE EXCEPTION 'orders_history_decision_invalid'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO STRICT p FROM public.marketplace_orders WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND order_id=NEW.order_id FOR UPDATE;
 SELECT * INTO STRICT v FROM public.order_observations WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND order_id=NEW.order_id AND observation_id=NEW.observation_id AND order_item_id IS NULL;
 incoming:=public.wb_history_projection_semantic(v.normalized_evidence);
 IF (v.source_kind,v.adapter_version,v.normalized_evidence->'observation'->'identity') IS DISTINCT FROM
  (r.source_kind,r.adapter_version,jsonb_build_object('organization_id',NEW.organization_id,'marketplace_account_id',NEW.marketplace_account_id,
    'marketplace','wb','external_order_id',p.external_order_id))
  OR v.payload_checksum<>encode(sha256(convert_to(public.wb_state_json((v.normalized_evidence #- '{observation,observed_at}')
   ||jsonb_build_object('checksum_version','orders-observation-v1')),'UTF8')),'hex')
  OR (v.source_revision,v.source_effective_at,v.observed_at) IS DISTINCT FROM
   (v.normalized_evidence->'observation'->>'source_revision',(v.normalized_evidence->'observation'->>'effective_at')::timestamptz,
    (v.normalized_evidence->'observation'->>'observed_at')::timestamptz) THEN RAISE EXCEPTION 'orders_history_decision_invalid'; END IF;
 NEW.history_decision_version:=1; NEW.history_pre_order_version:=p.version; NEW.history_pre_sync_run_id:=p.last_seen_sync_run_id;
 IF p.last_seen_sync_run_id IS NOT NULL THEN
  SELECT v2.* INTO STRICT prior FROM public.order_sync_memberships m JOIN public.order_observations v2
   ON (v2.organization_id,v2.marketplace_account_id,v2.order_id,v2.observation_id)=
    (m.organization_id,m.marketplace_account_id,m.order_id,m.observation_id)
   WHERE m.organization_id=NEW.organization_id AND m.marketplace_account_id=NEW.marketplace_account_id
    AND m.order_id=NEW.order_id AND m.sync_run_id=p.last_seen_sync_run_id AND m.order_item_id IS NULL AND v2.order_item_id IS NULL;
  current_semantic:=public.wb_history_projection_semantic(prior.normalized_evidence);
  IF prior.source_kind<>r.source_kind OR prior.normalized_evidence->'observation'->'identity' IS DISTINCT FROM incoming->'identity'
   OR prior.adapter_version IS DISTINCT FROM prior.normalized_evidence->'observation'->>'adapter_version'
   OR prior.payload_checksum<>encode(sha256(convert_to(public.wb_state_json((prior.normalized_evidence #- '{observation,observed_at}')
    ||jsonb_build_object('checksum_version','orders-observation-v1')),'UTF8')),'hex')
   OR (prior.source_revision,prior.source_effective_at,prior.observed_at) IS DISTINCT FROM
    (prior.normalized_evidence->'observation'->>'source_revision',(prior.normalized_evidence->'observation'->>'effective_at')::timestamptz,
     (prior.normalized_evidence->'observation'->>'observed_at')::timestamptz) THEN RAISE EXCEPTION 'orders_history_decision_invalid'; END IF;
  NEW.history_pre_observation_id:=prior.observation_id;
  NEW.history_outcome:=CASE WHEN current_semantic=incoming THEN 'semantic_replay' ELSE 'reconciliation_required' END;
 ELSIF p.version=1 AND v.sync_run_id=NEW.sync_run_id
  AND NOT EXISTS(SELECT FROM public.order_observations other WHERE other.organization_id=NEW.organization_id
   AND other.marketplace_account_id=NEW.marketplace_account_id AND other.order_id=NEW.order_id
   AND other.order_item_id IS NULL AND other.observation_id<>NEW.observation_id)
  AND NOT EXISTS(SELECT FROM public.order_sync_memberships other WHERE other.organization_id=NEW.organization_id
   AND other.marketplace_account_id=NEW.marketplace_account_id AND other.order_id=NEW.order_id AND other.order_item_id IS NULL)
 THEN NEW.history_outcome:='initial_projection';
 ELSE NEW.history_outcome:='reconciliation_required'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER orders_history_decision_capture BEFORE INSERT ON public.order_sync_memberships
 FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_decision_capture();

CREATE FUNCTION public.wb_history_projection_parent_admission() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.order_sync_runs; m public.order_sync_memberships; v public.order_observations;
BEGIN
 IF NEW.last_seen_sync_run_id IS NULL THEN RETURN NEW; END IF;
 SELECT * INTO STRICT r FROM public.order_sync_runs WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.last_seen_sync_run_id;
 IF r.source_contract_version<>'wb-history-positive-partial-v1' THEN RETURN NEW; END IF;
 SELECT * INTO m FROM public.order_sync_memberships WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.last_seen_sync_run_id AND order_id=NEW.order_id
  AND order_item_id IS NULL AND history_decision_version=1 AND history_outcome='initial_projection';
 IF NOT FOUND OR r.state<>'staging' OR NEW.version<>OLD.version+1 OR OLD.version<>m.history_pre_order_version
  OR OLD.last_seen_sync_run_id IS DISTINCT FROM m.history_pre_sync_run_id THEN RAISE EXCEPTION 'orders_history_initial_witness_required'; END IF;
 SELECT * INTO STRICT v FROM public.order_observations WHERE organization_id=m.organization_id
  AND marketplace_account_id=m.marketplace_account_id AND observation_id=m.observation_id AND order_id=m.order_id AND order_item_id IS NULL;
 IF (NEW.raw_status,NEW.canonical_status,NEW.mapping_state,NEW.mapping_version,NEW.source_updated_at) IS DISTINCT FROM
  (v.normalized_evidence->'observation'->'status'->>'raw_status',v.normalized_evidence->'observation'->'status'->>'canonical_status',
   v.normalized_evidence->'observation'->'status'->>'mapping_state',v.normalized_evidence->'observation'->'status'->>'mapping_version',v.source_effective_at)
  THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER orders_history_parent_admission BEFORE UPDATE ON public.marketplace_orders
 FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_parent_admission();

CREATE FUNCTION public.wb_history_projection_decision_witness() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE p public.marketplace_orders; v public.order_observations; n integer;
BEGIN
 IF NEW.history_decision_version IS NULL THEN RETURN NULL; END IF;
 SELECT * INTO STRICT p FROM public.marketplace_orders WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND order_id=NEW.order_id;
 IF NEW.history_outcome='initial_projection' THEN
  IF (p.version,p.last_seen_sync_run_id) IS DISTINCT FROM (NEW.history_pre_order_version+1,NEW.sync_run_id)
   THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
  SELECT * INTO STRICT v FROM public.order_observations WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND order_id=NEW.order_id AND observation_id=NEW.observation_id;
  SELECT count(*) INTO n FROM public.order_sync_memberships m JOIN public.marketplace_order_items i
   ON (i.organization_id,i.marketplace_account_id,i.order_id,i.order_item_id)=(m.organization_id,m.marketplace_account_id,m.order_id,m.order_item_id)
   JOIN public.order_observations child ON (child.organization_id,child.marketplace_account_id,child.order_id,child.observation_id)=
    (m.organization_id,m.marketplace_account_id,m.order_id,m.observation_id) AND child.order_item_id=m.order_item_id
   WHERE m.organization_id=NEW.organization_id AND m.marketplace_account_id=NEW.marketplace_account_id
    AND m.sync_run_id=NEW.sync_run_id AND m.order_id=NEW.order_id AND m.order_item_id IS NOT NULL
    AND m.coverage_role='observed' AND m.observed_at=NEW.observed_at AND i.version=1
    AND child.payload_checksum=v.payload_checksum AND child.normalized_evidence=v.normalized_evidence
    AND EXISTS(SELECT FROM jsonb_array_elements(v.normalized_evidence->'observation'->'items') item
     WHERE i.source_line_key=item->'identity'->>'source_line_key'
      AND i.external_item_id IS NOT DISTINCT FROM item->'identity'->>'external_item_id'
      AND i.occurrence_index=(item->'identity'->>'occurrence_index')::integer AND i.quantity=(item->>'quantity')::integer
      AND i.source_updated_at IS NOT DISTINCT FROM v.source_effective_at);
  IF n<>jsonb_array_length(v.normalized_evidence->'observation'->'items')
   OR n<>(SELECT count(*) FROM public.order_sync_memberships WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND sync_run_id=NEW.sync_run_id AND order_id=NEW.order_id AND order_item_id IS NOT NULL)
   THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 ELSIF (p.version,p.last_seen_sync_run_id) IS DISTINCT FROM (NEW.history_pre_order_version,NEW.history_pre_sync_run_id)
  THEN RAISE EXCEPTION 'orders_history_prestate_changed'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER orders_history_decision_witness AFTER INSERT ON public.order_sync_memberships
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wb_history_projection_decision_witness();
'''

INITIAL_HELPER = r'''
-- Private/invoker until the separately reviewed transactional role handoff.
CREATE FUNCTION public.wb_history_projection_apply_parent(p_org integer,p_account integer,p_run bigint,p_order bigint)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE r public.order_sync_runs; m public.order_sync_memberships; p public.marketplace_orders;
 v public.order_observations; result bigint;
BEGIN
 IF p_org IS NULL OR p_org<1 OR p_account IS NULL OR p_account<1 OR p_run IS NULL OR p_run<1
  OR p_order IS NULL OR p_order<1
  OR current_setting('app.organization_id',true) IS DISTINCT FROM p_org::text
  OR current_setting('app.marketplace_account_id',true) IS DISTINCT FROM p_account::text
 THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 -- Same account-before-parent order as the genuine publication guard.
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=p_org AND marketplace_account_id=p_account
  AND marketplace='wb' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 SELECT * INTO STRICT p FROM public.marketplace_orders WHERE organization_id=p_org AND marketplace_account_id=p_account
  AND order_id=p_order AND marketplace='wb' FOR UPDATE;
 SELECT * INTO STRICT r FROM public.order_sync_runs WHERE organization_id=p_org AND marketplace_account_id=p_account AND sync_run_id=p_run;
 IF (r.marketplace,r.source_kind,r.adapter_version,r.mapping_version,r.source_contract_version,r.state) IS DISTINCT FROM
  ('wb','wb-statistics-supplier-orders','wb-statistics-orders-stream-v1','wb-statistics-status-v1','wb-history-positive-partial-v1','staging')
 THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 SELECT * INTO STRICT m FROM public.order_sync_memberships WHERE organization_id=p_org AND marketplace_account_id=p_account
  AND sync_run_id=p_run AND order_id=p_order AND order_item_id IS NULL AND coverage_role='observed'
  AND history_decision_version=1 AND history_outcome='initial_projection';
 IF p.version<>1 OR m.history_pre_order_version<>p.version OR p.last_seen_sync_run_id IS NOT NULL
  OR m.history_pre_sync_run_id IS NOT NULL OR m.history_pre_observation_id IS NOT NULL
 THEN RAISE EXCEPTION 'orders_history_initial_witness_required'; END IF;
 SELECT * INTO STRICT v FROM public.order_observations WHERE organization_id=p_org AND marketplace_account_id=p_account
  AND order_id=p_order AND observation_id=m.observation_id AND order_item_id IS NULL AND sync_run_id=p_run;
 PERFORM public.wb_history_projection_semantic(v.normalized_evidence);
 UPDATE public.marketplace_orders SET raw_status=v.normalized_evidence->'observation'->'status'->>'raw_status',
  canonical_status=v.normalized_evidence->'observation'->'status'->>'canonical_status',
  mapping_state=v.normalized_evidence->'observation'->'status'->>'mapping_state',
  mapping_version=v.normalized_evidence->'observation'->'status'->>'mapping_version',source_updated_at=v.source_effective_at,
  last_seen_sync_run_id=p_run,version=version+1,updated_at=clock_timestamp()
 WHERE organization_id=p_org AND marketplace_account_id=p_account AND order_id=p_order
  AND version=m.history_pre_order_version AND last_seen_sync_run_id IS NULL RETURNING version INTO result;
 IF result IS NULL THEN RAISE EXCEPTION 'orders_history_projection_invalid'; END IF;
 RETURN result;
END $$;
'''


def _once(value, old, new):
    if value.count(old) != 1:
        raise RuntimeError("orders_history_frozen_definition_mismatch")
    return value.replace(old, new, 1)


def _prior():
    path = Path(__file__).with_name("20260909_0072_user_orders_jobs.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != "7a06c497b8a5c87987930acde5c7094ee03738e497e86717e170db0fde6ab7fc":
        raise RuntimeError("orders_history_frozen_definition_mismatch")
    sql = runpy.run_path(str(path))["SQL"]
    names = ("user_orders_request_bytes", "user_orders_row_guard", "user_orders_transition_witness")
    result = []
    for name in names:
        matches = re.findall(r"CREATE FUNCTION public\." + name + r"\(.*?END \$\$;", sql, re.DOTALL)
        if len(matches) != 1:
            raise RuntimeError("orders_history_frozen_definition_mismatch")
        result.append(_once(matches[0], "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    return tuple(result)


def _execute(sql):
    op.execute(sa.DDL(sql.replace("%", "%%")))


def _extended():
    request, guard, witness = _prior()
    request = _once(request, "BEGIN\n IF j.provider='wb'", """BEGIN
 IF j.operation_kind='orders.wb-history.project.v1' THEN
  RETURN convert_to(public.wb_state_json(jsonb_build_object('schemaVersion',1,'operationKind',j.operation_kind,
   'organizationId',j.organization_id,'marketplaceAccountId',j.marketplace_account_id,'provider',j.provider,
   'sourceKind',j.source_kind,'adapterVersion',j.adapter_version,'mappingVersion',j.mapping_version,
   'sourceContractVersion',j.source_contract_version,'requestedFrom',NULL,'requestedTo',NULL,
   'sourceRequest',jsonb_build_object('historyJobId',j.history_job_id::text,'sourceRunId',j.history_run_id::text,
    'selectionDigest',j.history_selection_digest,'pageCount',j.history_page_count,'terminalPageId',j.history_terminal_page_id::text))),'UTF8');
 END IF;
 IF j.provider='wb'""")
    old = "mutable:=ARRAY['state','version','attempt_count','current_attempt_id','next_attempt_at','completed_at','safe_reason','result_sync_run_id','result_coverage_state'];"
    guard = _once(guard, old, old + "\n   IF NEW.operation_kind='orders.wb-history.project.v1' THEN mutable:=mutable||ARRAY['history_cursor_page','history_cursor_ordinal','history_progress_version']; END IF;")
    witness = _once(witness, " CASE a.event_kind", """ IF NEW.operation_kind='orders.wb-history.project.v1' THEN
  IF TG_OP='INSERT' THEN
   IF (NEW.history_cursor_page,NEW.history_cursor_ordinal,NEW.history_progress_version) IS DISTINCT FROM (0,0,0::bigint)
    OR NOT public.wb_history_projection_selection_valid(NEW) THEN RAISE EXCEPTION 'orders_history_selection_invalid'; END IF;
  ELSIF a.event_kind<>'job.chunk_committed' AND
   (NEW.history_cursor_page,NEW.history_cursor_ordinal,NEW.history_progress_version) IS DISTINCT FROM
   (OLD.history_cursor_page,OLD.history_cursor_ordinal,OLD.history_progress_version) THEN RAISE EXCEPTION 'orders_history_progress_invalid';
  END IF;
  IF NEW.state='succeeded' AND a.event_kind<>'job.chunk_committed' THEN RAISE EXCEPTION 'orders_history_eof_required'; END IF;
 END IF;
 CASE a.event_kind
 WHEN 'job.chunk_committed' THEN valid:=TG_OP='UPDATE' AND public.wb_history_projection_chunk_transition(NEW,OLD,a);""")
    witness = _once(witness, " IF NEW.state='succeeded' THEN", " IF NEW.state='succeeded' AND NEW.operation_kind='orders.sync.v1' THEN")
    return request, guard, witness


def _private(signature):
    _execute("""DO $$ DECLARE a record; BEGIN FOR a IN
 SELECT DISTINCT x.grantee FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x
 WHERE p.oid='public.""" + signature + """'::regprocedure AND x.grantee NOT IN (0,p.proowner)
 LOOP EXECUTE format('REVOKE ALL ON FUNCTION public.""" + signature + """ FROM %I',pg_get_userbyid(a.grantee)); END LOOP; END $$;""")
    op.execute("REVOKE ALL ON FUNCTION public." + signature + " FROM PUBLIC")


def upgrade():
    _prior()
    _execute(SCHEMA)
    _execute(CODECS)
    _execute(SELECTION)
    _execute(DECISIONS)
    _execute(INITIAL_HELPER)
    _execute(RECEIPTS)
    for sql in _extended():
        _execute(sql)
    _execute("""DO $$ DECLARE a record; BEGIN FOR a IN
 SELECT DISTINCT x.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
 WHERE c.oid='public.user_orders_history_selection_pages'::regclass AND x.grantee NOT IN (0,c.relowner)
 LOOP EXECUTE format('REVOKE ALL ON TABLE public.user_orders_history_selection_pages FROM %I',pg_get_userbyid(a.grantee)); END LOOP; END $$;""")
    op.execute("REVOKE ALL ON TABLE public.user_orders_history_selection_pages FROM PUBLIC")
    for signature in ("wb_history_projection_header_bytes(public.wb_live_history_pages)",
            "wb_history_projection_chunk_bytes(integer,integer,uuid,uuid,integer)",
            "wb_history_projection_selection_guard()", "wb_history_projection_selection_valid(public.user_orders_jobs)",
            "wb_history_projection_selection_witness()",
            "wb_history_projection_receipt_valid(public.user_orders_jobs,public.user_orders_job_audit)",
            "wb_history_projection_chunk_transition(public.user_orders_jobs,public.user_orders_jobs,public.user_orders_job_audit)",
            "wb_history_projection_run_witness()", "wb_history_projection_keys(jsonb,text[])",
            "wb_history_projection_semantic(jsonb)", "wb_history_projection_decision_capture()",
            "wb_history_projection_parent_admission()", "wb_history_projection_decision_witness()",
            "wb_history_projection_apply_parent(integer,integer,bigint,bigint)"):
        _private(signature)


def downgrade():
    op.execute("LOCK TABLE public.user_orders_jobs,public.user_orders_history_selection_pages,public.user_orders_job_audit,public.order_sync_memberships,public.order_sync_runs IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security=off")
    _execute("""DO $$ BEGIN IF EXISTS(SELECT FROM public.user_orders_jobs WHERE operation_kind='orders.wb-history.project.v1')
 OR EXISTS(SELECT FROM public.user_orders_history_selection_pages)
 OR EXISTS(SELECT FROM public.user_orders_job_audit WHERE event_kind='job.chunk_committed')
 OR EXISTS(SELECT FROM public.order_sync_runs WHERE source_contract_version='wb-history-positive-partial-v1')
 OR EXISTS(SELECT FROM public.order_sync_memberships WHERE history_decision_version IS NOT NULL)
 THEN RAISE EXCEPTION 'orders_history_downgrade_nonempty'; END IF; END $$;""")
    for sql in _prior():
        _execute(sql)
    op.execute("DROP TRIGGER orders_history_run_witness ON public.order_sync_runs")
    op.execute("DROP TRIGGER orders_history_decision_capture ON public.order_sync_memberships")
    op.execute("DROP TRIGGER orders_history_decision_witness ON public.order_sync_memberships")
    op.execute("DROP TRIGGER orders_history_parent_admission ON public.marketplace_orders")
    op.execute("ALTER TABLE public.user_orders_job_audit DROP CONSTRAINT fk_orders_history_receipt_selection")
    op.execute("DROP TABLE public.user_orders_history_selection_pages")
    for signature in ("wb_history_projection_apply_parent(integer,integer,bigint,bigint)", "wb_history_projection_run_witness()",
            "wb_history_projection_chunk_transition(public.user_orders_jobs,public.user_orders_jobs,public.user_orders_job_audit)",
            "wb_history_projection_receipt_valid(public.user_orders_jobs,public.user_orders_job_audit)",
            "wb_history_projection_selection_witness()", "wb_history_projection_selection_valid(public.user_orders_jobs)",
            "wb_history_projection_selection_guard()",
            "wb_history_projection_decision_witness()", "wb_history_projection_parent_admission()",
            "wb_history_projection_decision_capture()", "wb_history_projection_semantic(jsonb)",
            "wb_history_projection_keys(jsonb,text[])",
            "wb_history_projection_chunk_bytes(integer,integer,uuid,uuid,integer)",
            "wb_history_projection_header_bytes(public.wb_live_history_pages)"):
        op.execute("DROP FUNCTION public." + signature)
    for name in DECISION_COLUMNS:
        op.execute("ALTER TABLE public.order_sync_memberships DROP COLUMN " + name)
    for name in AUDIT_COLUMNS:
        op.execute("ALTER TABLE public.user_orders_job_audit DROP COLUMN " + name)
    op.execute("ALTER TABLE public.user_orders_jobs DROP CONSTRAINT ck_orders_history_shape")
    _execute("""DO $$ DECLARE c record; matches integer:=0; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='public.user_orders_jobs'::regclass AND contype='c'
  AND pg_get_constraintdef(oid) LIKE '%source_date_from IS NOT NULL%' LOOP
  matches:=matches+1; EXECUTE format('ALTER TABLE public.user_orders_jobs DROP CONSTRAINT %I',c.conname);
  EXECUTE format('ALTER TABLE public.user_orders_jobs ADD CONSTRAINT %I CHECK
   ((provider=''wb'' AND source_kind=''wb-statistics-supplier-orders'' AND mapping_version=''wb-statistics-status-v1''
    AND source_date_from IS NOT NULL AND source_statuses IS NULL AND source_limit IS NULL AND source_page IS NULL)
    OR (provider=''avito'' AND source_kind=''avito-order-management'' AND mapping_version=''avito-order-status-v1''
    AND source_statuses IS NOT NULL AND source_limit IS NOT NULL AND source_page IS NOT NULL
    AND source_limit BETWEEN 1 AND 20 AND source_page BETWEEN 1 AND 9223372036854775807))',c.conname);
 END LOOP; IF matches<>1 THEN RAISE EXCEPTION 'orders_history_schema_mismatch'; END IF; END $$;""")
    for name in JOB_COLUMNS:
        op.execute("ALTER TABLE public.user_orders_jobs DROP COLUMN " + name)
    op.execute("ALTER TABLE public.user_orders_jobs DROP CONSTRAINT user_orders_jobs_operation_kind_check")
    op.execute("ALTER TABLE public.user_orders_jobs ADD CONSTRAINT user_orders_jobs_operation_kind_check CHECK(operation_kind='orders.sync.v1')")
