\set ON_ERROR_STOP on
-- INERT SOURCE ONLY. Infrastructure owner supplies already provisioned distinct
-- executor_role and api_runtime_role psql variables, approving isolated custody
-- and the timestamp UPDATE capabilities below. No role/password provisioning.
BEGIN;
SELECT set_config('repricer.grant_executor_role', :'executor_role', true),
       set_config('repricer.grant_api_role', :'api_runtime_role', true);
DO $$
DECLARE executor pg_roles; api pg_roles; item record;
BEGIN
 SELECT * INTO executor FROM pg_roles WHERE rolname=current_setting('repricer.grant_executor_role');
 SELECT * INTO api FROM pg_roles WHERE rolname=current_setting('repricer.grant_api_role');
 IF executor.oid IS NULL OR api.oid IS NULL OR executor.oid=api.oid
 OR NOT executor.rolcanlogin OR executor.rolsuper OR executor.rolcreatedb OR executor.rolcreaterole
 OR executor.rolinherit OR executor.rolreplication OR executor.rolbypassrls
 OR api.rolsuper OR api.rolcreaterole OR api.rolbypassrls
 OR pg_has_role(api.oid,executor.oid,'MEMBER')
 OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=executor.oid OR roleid=executor.oid)
 THEN RAISE EXCEPTION 'executor_identity_denied'; END IF;
 IF EXISTS(SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace
 AND relowner IN (executor.oid,api.oid))
 OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public' AND nspowner IN (executor.oid,api.oid))
 THEN RAISE EXCEPTION 'executor_identity_denied'; END IF;
 FOR item IN SELECT unnest(ARRAY['marketplace_accounts','marketplace_account_credentials','iam_memberships',
 'wb_repricer_price_approvals','wb_repricer_price_apply_attempts','wb_repricer_price_approval_audit',
 'wb_repricing_jobs','wb_repricing_job_authorities','wb_repricing_job_audit',
 'wb_repricing_upload_receipts','wb_repricing_upload_receipt_audit']) AS name LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass('public.'||item.name) AND relrowsecurity AND relforcerowsecurity)
  THEN RAISE EXCEPTION 'executor_schema_invalid'; END IF;
 END LOOP;
 -- Existing broad rights cannot be silently repaired by this inert narrow script.
 FOR item IN SELECT unnest(ARRAY['lk_users','lk_sessions','iam_memberships','marketplace_accounts','marketplace_account_credentials']) AS name LOOP
  IF has_table_privilege(executor.oid,'public.'||item.name,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
 FOR item IN SELECT unnest(ARRAY['wb_repricing_jobs','wb_repricing_job_authorities','wb_repricing_job_audit']) AS name LOOP
  IF has_table_privilege(executor.oid,'public.'||item.name,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
   OR has_any_column_privilege(executor.oid,'public.'||item.name,'INSERT,UPDATE,REFERENCES')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
 FOR item IN SELECT unnest(ARRAY['wb_repricing_upload_receipts','wb_repricing_upload_receipt_audit']) AS name LOOP
  IF has_table_privilege(executor.oid,'public.'||item.name,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
   OR has_any_column_privilege(executor.oid,'public.'||item.name,'UPDATE,REFERENCES')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
 -- Deny existing identity/expiry/permission/key mutation at column level too.
 FOR item IN SELECT c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE c.relnamespace='public'::regnamespace AND c.relname IN
   ('lk_users','lk_sessions','iam_memberships','marketplace_accounts','marketplace_account_credentials')
  AND a.attnum>0 AND NOT a.attisdropped AND
  a.attname<>CASE WHEN c.relname='lk_sessions' THEN 'last_seen_at' ELSE 'updated_at' END LOOP
  IF has_column_privilege(executor.oid,'public.'||item.relname,item.attname,'UPDATE,INSERT,REFERENCES')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO :"executor_role";
GRANT SELECT (user_id,organization_id,is_active) ON public.lk_users TO :"executor_role";
GRANT SELECT (membership_id,organization_id,user_id,is_active,role,permissions,scope_mode,allowed_account_ids)
 ON public.iam_memberships TO :"executor_role";
GRANT SELECT (session_id,user_id,revoked_at,expires_at) ON public.lk_sessions TO :"executor_role";
GRANT SELECT (marketplace_account_id,organization_id,marketplace,external_account_id,credential_ref,status,ingestion_binding_version)
 ON public.marketplace_accounts TO :"executor_role";
-- Existing resolver selects the full encrypted ORM row: list every required column
-- explicitly; there is no plaintext source, keyring, password or verifier grant.
GRANT SELECT (credential_id,organization_id,marketplace_account_id,provider,credential_kind,
 algorithm,key_version,aad_version,payload_schema_version,nonce,ciphertext,generation,
 expires_at,revoked_at,revocation_reason_code,created_at,updated_at)
 ON public.marketplace_account_credentials TO :"executor_role";
GRANT UPDATE (updated_at) ON public.lk_users, public.iam_memberships,
 public.marketplace_accounts, public.marketplace_account_credentials TO :"executor_role";
GRANT UPDATE (last_seen_at) ON public.lk_sessions TO :"executor_role";
GRANT SELECT ON public.wb_repricing_jobs, public.wb_repricing_job_authorities,
 public.wb_repricing_job_audit, public.wb_repricing_upload_receipts,
 public.wb_repricing_upload_receipt_audit TO :"executor_role";
GRANT INSERT ON public.wb_repricing_upload_receipts, public.wb_repricing_upload_receipt_audit TO :"executor_role";
GRANT SELECT ON public.wb_repricer_price_approvals, public.wb_repricer_price_apply_attempts,
 public.wb_repricer_price_approval_audit TO :"executor_role";
GRANT INSERT ON public.wb_repricer_price_apply_attempts, public.wb_repricer_price_approval_audit TO :"executor_role";
-- T2 _mutable sends these decision columns as NULL for claim/outcome as well.
-- Executor action fence still forbids reject/block and participant scope changes.
GRANT UPDATE (status,version,updated_at,claimed_by_membership_id,decided_by_membership_id,reason_code,claimed_audit_id,
 safe_error_code,wb_upload_id,result_code,outcome_audit_id)
 ON public.wb_repricer_price_approvals TO :"executor_role";
GRANT UPDATE (status,version,updated_at,dispatch_at,finished_at,safe_error_code,
 wb_upload_id,result_code,dispatched_audit_id,outcome_audit_id)
 ON public.wb_repricer_price_apply_attempts TO :"executor_role";
GRANT EXECUTE ON FUNCTION public.repricing_job_uuid(uuid), public.repricing_job_time(timestamptz),
 public.repricing_job_text(text,integer), public.repricer_exact_text(text),
 public.repricer_integral_finite(numeric), public.repricer_safe_code(text),
 public.repricer_ascii_json_string(text), public.repricer_integer_decimal(numeric),
 public.repricer_request_bytes(integer,integer,text,integer,numeric,text,numeric,smallint,numeric,numeric),
 public.repricer_action_key(integer,integer,text,text),
 public.repricer_dispatch_key(integer,integer,text,text,uuid), public.repricer_context_id(text)
 TO :"executor_role";
-- Trigger functions have no direct EXECUTE grants. Missing columns/functions abort
-- this transaction. No default privileges or unrelated ACLs are modified.
-- Additive quota custody; older isolated schema fixtures remain supported.
-- This does not provision a role or confer dispatch authorization.
DO $$
DECLARE executor_name text := current_setting('repricer.grant_executor_role'); item record;
BEGIN
 IF to_regclass('public.wb_price_quota') IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid='public.wb_price_quota'::regclass
    AND relrowsecurity AND relforcerowsecurity)
  THEN RAISE EXCEPTION 'executor_schema_invalid'; END IF;
  IF has_table_privilege(executor_name,'public.wb_price_quota','UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
  FOR item IN SELECT attname FROM pg_attribute WHERE attrelid='public.wb_price_quota'::regclass
    AND attnum>0 AND NOT attisdropped AND attname NOT IN ('next_allowed_at','updated_at') LOOP
   IF has_column_privilege(executor_name,'public.wb_price_quota',item.attname,'UPDATE')
   THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
  END LOOP;
  EXECUTE format('GRANT SELECT,INSERT ON public.wb_price_quota TO %I',executor_name);
  EXECUTE format('GRANT UPDATE(next_allowed_at,updated_at) ON public.wb_price_quota TO %I',executor_name);
 END IF;
END $$;
COMMIT;
