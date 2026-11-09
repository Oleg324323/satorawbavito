\set ON_ERROR_STOP on
-- INERT SOURCE: reviewed custody and separately provisioned roles are required.
-- Never run runtime-db-role.sql for this identity. No activation or role creation.
-- Inputs: projection_role, projection_helper_owner. Trusted application inputs
-- additionally pin both actual OIDs; names alone never establish authority.
BEGIN;
SELECT set_config('history_grants.runtime', :'projection_role', true),
       set_config('history_grants.helper_owner', :'projection_helper_owner', true);
DO $$
DECLARE runtime pg_roles; helper pg_roles; r record;
BEGIN
 SELECT * INTO runtime FROM pg_roles WHERE rolname=current_setting('history_grants.runtime');
 SELECT * INTO helper FROM pg_roles WHERE rolname=current_setting('history_grants.helper_owner');
 IF session_user<>current_user OR runtime.oid IS NULL OR helper.oid IS NULL OR runtime.oid=helper.oid
  OR NOT runtime.rolcanlogin OR helper.rolcanlogin
  OR runtime.rolsuper OR runtime.rolcreatedb OR runtime.rolcreaterole OR runtime.rolinherit
  OR runtime.rolreplication OR runtime.rolbypassrls
  OR helper.rolsuper OR helper.rolcreatedb OR helper.rolcreaterole OR helper.rolinherit
  OR helper.rolreplication OR helper.rolbypassrls
  OR EXISTS(SELECT FROM pg_auth_members WHERE member IN (runtime.oid,helper.oid) OR roleid IN (runtime.oid,helper.oid))
  OR EXISTS(SELECT FROM pg_class WHERE relowner IN (runtime.oid,helper.oid))
  OR EXISTS(SELECT FROM pg_namespace WHERE nspowner IN (runtime.oid,helper.oid))
  OR EXISTS(SELECT FROM pg_database WHERE datdba IN (runtime.oid,helper.oid))
  OR has_schema_privilege(runtime.oid,'public','CREATE') OR has_schema_privilege(helper.oid,'public','CREATE')
  OR EXISTS(SELECT FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a
    WHERE a.grantee IN (runtime.oid,helper.oid))
 THEN RAISE EXCEPTION 'history_projection_identity_denied'; END IF;
 IF to_regprocedure('public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)') IS NULL
 THEN RAISE EXCEPTION 'history_projection_schema_invalid'; END IF;
 -- Ownership changes preserve named ACL entries. Reject unintended callers
 -- before enabling SECURITY DEFINER; never silently revoke another identity.
 IF EXISTS(SELECT FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  WHERE p.oid='public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)'::regprocedure
   AND a.grantee NOT IN (0,p.proowner,runtime.oid,helper.oid))
 THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
 FOR r IN SELECT c.oid,c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
   AND a.attnum>0 AND NOT a.attisdropped LOOP
  IF has_table_privilege(runtime.oid,r.oid,'DELETE,TRUNCATE,REFERENCES,TRIGGER')
   OR has_column_privilege(runtime.oid,r.oid,r.attname,'REFERENCES')
   OR has_table_privilege(helper.oid,r.oid,'INSERT,DELETE,TRUNCATE,REFERENCES,TRIGGER')
   OR has_column_privilege(helper.oid,r.oid,r.attname,'INSERT,REFERENCES')
   OR (has_column_privilege(helper.oid,r.oid,r.attname,'SELECT') AND r.relname NOT IN
     ('marketplace_accounts','marketplace_orders','order_sync_runs','order_sync_memberships','order_observations'))
   OR (has_column_privilege(helper.oid,r.oid,r.attname,'UPDATE') AND NOT
     ((r.relname='marketplace_accounts' AND r.attname='updated_at') OR
      (r.relname='marketplace_orders' AND r.attname IN ('raw_status','canonical_status','mapping_state',
       'mapping_version','source_updated_at','last_seen_sync_run_id','version','updated_at'))))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
  IF has_column_privilege(runtime.oid,r.oid,r.attname,'SELECT') AND NOT (
   r.relname IN ('user_orders_jobs','user_orders_job_authorities','user_orders_job_attempts','user_orders_job_audit',
    'user_orders_history_selection_pages','wb_live_history_requests','wb_live_history_pages','wb_live_history_rows',
    'marketplace_orders','marketplace_order_items','order_sync_runs','order_observations',
    'order_status_observations','order_sync_memberships','order_sync_coverage','order_lifecycle_events')
   OR (r.relname='lk_users' AND r.attname IN ('user_id','organization_id','is_active'))
   OR (r.relname='lk_sessions' AND r.attname IN ('session_id','user_id','expires_at','revoked_at'))
   OR (r.relname='iam_memberships' AND r.attname IN ('membership_id','organization_id','user_id','is_active','role','permissions','scope_mode','allowed_account_ids'))
   OR (r.relname='marketplace_accounts' AND r.attname IN ('marketplace_account_id','organization_id','marketplace','external_account_id','credential_ref','status','ingestion_binding_version'))
   OR (r.relname='marketplace_account_credentials' AND r.attname IN ('credential_id','organization_id','marketplace_account_id','provider','credential_kind','generation','payload_schema_version','expires_at','revoked_at'))
   OR (r.relname='marketplace_products' AND r.attname IN ('organization_id','marketplace_account_id','marketplace_product_id','external_product_id','updated_at'))
   OR (r.relname='marketplace_offers' AND r.attname IN ('organization_id','marketplace_account_id','marketplace_product_id','marketplace_offer_id','external_offer_key','catalog_sku_id','updated_at'))
   OR (r.relname='catalog_skus' AND r.attname IN ('organization_id','catalog_sku_id','updated_at'))
   OR (r.relname='lk_audit_events' AND r.attname IN ('event_id','created_at')))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
  IF has_column_privilege(runtime.oid,r.oid,r.attname,'UPDATE') AND NOT (
   r.relname='order_sync_runs'
   OR (r.relname='marketplace_orders' AND r.attname='order_id')
   OR (r.relname='marketplace_order_items' AND r.attname='order_item_id')
   OR (r.relname IN ('lk_users','iam_memberships','marketplace_accounts','marketplace_account_credentials',
     'marketplace_products','marketplace_offers','catalog_skus') AND r.attname='updated_at')
   OR (r.relname='lk_sessions' AND r.attname='last_seen_at')
   OR (r.relname='user_orders_jobs' AND r.attname IN ('state','version','attempt_count','current_attempt_id',
     'next_attempt_at','completed_at','safe_reason','result_sync_run_id','result_coverage_state',
     'history_cursor_page','history_cursor_ordinal','history_progress_version'))
   OR (r.relname='user_orders_job_attempts' AND r.attname IN ('state','version','job_version_after',
     'lease_expires_at','finished_at','safe_reason','result_sync_run_id','result_coverage_state')))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
  IF has_column_privilege(runtime.oid,r.oid,r.attname,'INSERT') AND NOT (
   r.relname IN ('user_orders_jobs','user_orders_job_authorities','user_orders_job_attempts','user_orders_job_audit',
    'user_orders_history_selection_pages','marketplace_order_items','order_sync_runs','order_observations',
    'order_status_observations','order_sync_memberships','order_sync_coverage','order_lifecycle_events')
   OR (r.relname='marketplace_orders' AND r.attname IN ('organization_id','marketplace_account_id','marketplace','external_order_id'))
   OR (r.relname='lk_audit_events' AND r.attname IN ('organization_id','actor_user_id','action','object_type',
     'object_id','details','before_state','after_state','reason','ip_address','user_agent')))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
 END LOOP;
 FOR r IN SELECT unnest(ARRAY['marketplace_accounts','marketplace_account_credentials','iam_memberships',
  'user_orders_jobs','user_orders_job_authorities','user_orders_job_attempts','user_orders_job_audit',
  'user_orders_history_selection_pages','wb_live_history_requests','wb_live_history_pages','wb_live_history_rows',
  'marketplace_orders','marketplace_order_items','order_sync_runs','order_observations',
  'order_sync_memberships','order_sync_coverage','order_lifecycle_events','order_status_observations',
  'marketplace_products','marketplace_offers','catalog_skus']) AS name LOOP
  IF NOT EXISTS(SELECT FROM pg_class WHERE oid=to_regclass('public.'||r.name) AND relrowsecurity AND relforcerowsecurity)
  THEN RAISE EXCEPTION 'history_projection_schema_invalid'; END IF;
 END LOOP;
 -- Do not silently repair a general-purpose or previously overprivileged role.
 FOR r IN SELECT c.oid,c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE c.relnamespace='public'::regnamespace AND a.attnum>0 AND NOT a.attisdropped
  AND c.relname IN ('lk_users','lk_sessions','iam_memberships','marketplace_accounts','marketplace_account_credentials') LOOP
  IF has_column_privilege(runtime.oid,r.oid,r.attname,'INSERT,REFERENCES')
   OR (r.attname<>CASE WHEN r.relname='lk_sessions' THEN 'last_seen_at' ELSE 'updated_at' END
       AND has_column_privilege(runtime.oid,r.oid,r.attname,'UPDATE'))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
 END LOOP;
 FOR r IN SELECT attname FROM pg_attribute WHERE attrelid='public.marketplace_orders'::regclass AND attnum>0 AND NOT attisdropped LOOP
  IF (r.attname<>'order_id' AND has_column_privilege(runtime.oid,'public.marketplace_orders',r.attname,'UPDATE'))
   OR (r.attname NOT IN ('organization_id','marketplace_account_id','marketplace','external_order_id')
       AND has_column_privilege(runtime.oid,'public.marketplace_orders',r.attname,'INSERT'))
  THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
 END LOOP;
 IF has_table_privilege(runtime.oid,'public.marketplace_orders','DELETE,TRUNCATE,REFERENCES,TRIGGER')
 THEN RAISE EXCEPTION 'history_projection_privileges_denied'; END IF;
END $$;

GRANT USAGE ON SCHEMA public TO :"projection_role", :"projection_helper_owner";
GRANT SELECT(user_id,organization_id,is_active) ON public.lk_users TO :"projection_role";
GRANT SELECT(session_id,user_id,expires_at,revoked_at) ON public.lk_sessions TO :"projection_role";
GRANT SELECT(membership_id,organization_id,user_id,is_active,role,permissions,scope_mode,allowed_account_ids)
 ON public.iam_memberships TO :"projection_role";
GRANT SELECT(marketplace_account_id,organization_id,marketplace,external_account_id,credential_ref,status,ingestion_binding_version)
 ON public.marketplace_accounts TO :"projection_role";
-- Metadata only. No ciphertext, nonce, key inventory, verifier, or legacy tokens.
GRANT SELECT(credential_id,organization_id,marketplace_account_id,provider,credential_kind,
 generation,payload_schema_version,expires_at,revoked_at)
 ON public.marketplace_account_credentials TO :"projection_role";
-- These are real bounded timestamp mutation capabilities needed by existing
-- FOR SHARE/UPDATE guards, not an assertion that the columns are immutable.
GRANT UPDATE(updated_at) ON public.lk_users,public.iam_memberships,
 public.marketplace_accounts,public.marketplace_account_credentials TO :"projection_role";
GRANT UPDATE(last_seen_at) ON public.lk_sessions TO :"projection_role";
GRANT SELECT(organization_id,marketplace_account_id,marketplace_product_id,external_product_id,updated_at)
 ON public.marketplace_products TO :"projection_role";
GRANT SELECT(organization_id,marketplace_account_id,marketplace_product_id,marketplace_offer_id,external_offer_key,catalog_sku_id,updated_at)
 ON public.marketplace_offers TO :"projection_role";
GRANT SELECT(organization_id,catalog_sku_id,updated_at) ON public.catalog_skus TO :"projection_role";
-- Existing Catalog resolution takes row locks. These timestamps ARE mutable;
-- business fields, INSERT and DELETE remain outside the capability.
GRANT UPDATE(updated_at) ON public.marketplace_products,public.marketplace_offers,public.catalog_skus TO :"projection_role";
GRANT SELECT ON public.wb_live_history_requests,public.wb_live_history_pages,public.wb_live_history_rows TO :"projection_role";
GRANT SELECT,INSERT ON public.user_orders_jobs,public.user_orders_job_authorities,
 public.user_orders_job_attempts,public.user_orders_job_audit,public.user_orders_history_selection_pages TO :"projection_role";
GRANT UPDATE(state,version,attempt_count,current_attempt_id,next_attempt_at,completed_at,safe_reason,
 result_sync_run_id,result_coverage_state,history_cursor_page,history_cursor_ordinal,history_progress_version)
 ON public.user_orders_jobs TO :"projection_role";
GRANT UPDATE(state,version,job_version_after,lease_expires_at,finished_at,safe_reason,result_sync_run_id,result_coverage_state)
 ON public.user_orders_job_attempts TO :"projection_role";
GRANT SELECT ON public.marketplace_orders TO :"projection_role";
GRANT INSERT(organization_id,marketplace_account_id,marketplace,external_order_id)
 ON public.marketplace_orders TO :"projection_role";
-- Lock privilege only: identity guards reject changes, and the role cannot
-- supply the version increment required even for an otherwise no-op UPDATE.
GRANT UPDATE(order_id) ON public.marketplace_orders TO :"projection_role";
GRANT SELECT,INSERT ON public.order_sync_runs,public.order_observations,
 public.order_sync_memberships,public.order_sync_coverage,public.order_lifecycle_events,public.order_status_observations,
 public.marketplace_order_items TO :"projection_role";
GRANT UPDATE ON public.order_sync_runs TO :"projection_role";
GRANT UPDATE(order_item_id) ON public.marketplace_order_items TO :"projection_role";
-- ORM emits nullable audit fields as NULL; no audit UPDATE/DELETE is admitted.
GRANT INSERT(organization_id,actor_user_id,action,object_type,object_id,details,
 before_state,after_state,reason,ip_address,user_agent)
 ON public.lk_audit_events TO :"projection_role";
GRANT SELECT(event_id,created_at) ON public.lk_audit_events TO :"projection_role";
SELECT format('GRANT USAGE,SELECT ON SEQUENCE %s TO %I',pg_get_serial_sequence(t,c),:'projection_role')
 FROM (VALUES ('public.order_sync_runs','sync_run_id'),('public.marketplace_orders','order_id'),
 ('public.marketplace_order_items','order_item_id'),('public.order_observations','observation_id'),
 ('public.order_sync_memberships','membership_id'),('public.order_sync_coverage','coverage_id'),
 ('public.order_lifecycle_events','lifecycle_event_id'),('public.order_status_observations','status_observation_id'),
 ('public.lk_audit_events','event_id')) AS identities(t,c)
\gexec

GRANT SELECT ON public.marketplace_accounts,public.marketplace_orders,public.order_sync_runs,
 public.order_sync_memberships,public.order_observations TO :"projection_helper_owner";
GRANT UPDATE(updated_at) ON public.marketplace_accounts TO :"projection_helper_owner";
GRANT UPDATE(raw_status,canonical_status,mapping_state,mapping_version,source_updated_at,
 last_seen_sync_run_id,version,updated_at) ON public.marketplace_orders TO :"projection_helper_owner";

GRANT EXECUTE ON FUNCTION public.user_orders_ascii(text),public.user_orders_text(text,integer),
 public.user_orders_request_bytes(public.user_orders_jobs),public.wb_state_json(jsonb),
 public.repricer_ascii_json_string(text),public.wb_sku_override_decimal(numeric),
 public.orders_binding_text(text,integer),public.orders_binding_ascii_string(text),
 public.orders_run_binding_bytes(integer,integer,text,text,text),
 public.wb_history_projection_header_bytes(public.wb_live_history_pages),
 public.wb_history_projection_chunk_bytes(integer,integer,uuid,uuid,integer),
 public.wb_history_projection_selection_valid(public.user_orders_jobs),
 public.wb_history_projection_receipt_valid(public.user_orders_jobs,public.user_orders_job_audit),
 public.wb_history_projection_chunk_transition(public.user_orders_jobs,public.user_orders_jobs,public.user_orders_job_audit),
 public.wb_history_projection_keys(jsonb,text[]),public.wb_history_projection_semantic(jsonb)
 TO :"projection_role";
GRANT EXECUTE ON FUNCTION public.user_orders_text(text,integer),public.wb_state_json(jsonb),
 public.repricer_ascii_json_string(text),public.wb_sku_override_decimal(numeric),
 public.wb_history_projection_keys(jsonb,text[]),public.wb_history_projection_semantic(jsonb)
 TO :"projection_helper_owner";

-- Ownership handoff is transactional and requires an authorized administrator.
-- The helper owner cannot log in or have members; temporary CREATE is removed
-- before commit. No table ownership or general-purpose definer grant is given.
REVOKE ALL ON FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint) FROM PUBLIC;
GRANT CREATE ON SCHEMA public TO :"projection_helper_owner";
ALTER FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint) OWNER TO :"projection_helper_owner";
REVOKE CREATE ON SCHEMA public FROM :"projection_helper_owner";
ALTER FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint) SECURITY DEFINER;
ALTER FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint) SET search_path TO pg_catalog,public;
GRANT EXECUTE ON FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint) TO :"projection_role";
-- Runtime admission and final fences must independently check pinned OIDs,
-- effective ACLs, public/inherited reachability, function ownership and RLS.
-- Grant-script success alone never enables a worker, API route, or account.
COMMIT;
