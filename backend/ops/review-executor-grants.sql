\set ON_ERROR_STOP on
-- INERT SOURCE. Explicit already-provisioned executor_role/api_runtime_role.
-- Never execute as the API login or use SET ROLE as worker authentication.
-- Does not create/alter roles, passwords, defaults, ownership or source data.
BEGIN;
SELECT set_config('review.grant_executor_role', :'executor_role', true),
       set_config('review.grant_api_role', :'api_runtime_role', true);
DO $$
DECLARE executor pg_roles; api pg_roles; item record; allowed_update text[];
BEGIN
 IF current_setting('review.grant_executor_role') !~ '^[a-z_][a-z0-9_]{0,62}$'
 OR current_setting('review.grant_api_role') !~ '^[a-z_][a-z0-9_]{0,62}$'
 THEN RAISE EXCEPTION 'executor_identity_denied'; END IF;
 SELECT * INTO executor FROM pg_roles WHERE rolname=current_setting('review.grant_executor_role');
 SELECT * INTO api FROM pg_roles WHERE rolname=current_setting('review.grant_api_role');
 IF executor.oid IS NULL OR api.oid IS NULL OR executor.oid=api.oid
 OR NOT executor.rolcanlogin OR executor.rolsuper OR executor.rolcreatedb OR executor.rolcreaterole
 OR executor.rolinherit OR executor.rolreplication OR executor.rolbypassrls
 OR api.rolsuper OR api.rolcreaterole OR api.rolbypassrls
 OR pg_has_role(api.oid,executor.oid,'MEMBER')
 OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=executor.oid OR roleid=executor.oid)
 OR EXISTS(SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace AND relowner IN(executor.oid,api.oid))
 OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public' AND nspowner IN(executor.oid,api.oid))
 THEN RAISE EXCEPTION 'executor_identity_denied'; END IF;
 FOR item IN SELECT unnest(ARRAY['iam_memberships','marketplace_accounts','marketplace_account_credentials',
  'review_facts','review_observations','review_policy_versions','review_policy_heads','review_draft_revisions',
  'review_decisions','review_workflow_heads','review_send_commands','review_send_command_authorities',
  'review_send_attempts','review_send_audit','review_answer_evidence','review_send_enqueue_intents',
  'notification_in_app_events']) AS name LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass('public.'||item.name)
   AND relrowsecurity AND relforcerowsecurity) THEN RAISE EXCEPTION 'executor_schema_invalid'; END IF;
 END LOOP;
 -- Refuse broad pre-existing writes instead of silently changing other ACLs.
 FOR item IN SELECT unnest(ARRAY['lk_users','lk_sessions','iam_memberships','marketplace_accounts',
  'marketplace_account_credentials','review_facts','review_observations','review_policy_versions',
  'review_policy_heads','review_draft_revisions','review_decisions','review_workflow_heads',
  'review_send_commands','review_send_command_authorities','review_send_attempts','review_send_audit',
  'review_answer_evidence','review_send_enqueue_intents','notification_in_app_events']) AS name LOOP
  IF has_table_privilege(executor.oid,'public.'||item.name,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
   OR has_any_column_privilege(executor.oid,'public.'||item.name,'REFERENCES')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
  IF item.name NOT IN ('review_send_attempts','review_send_audit','review_answer_evidence',
   'review_send_enqueue_intents','notification_in_app_events')
   AND (has_table_privilege(executor.oid,'public.'||item.name,'INSERT')
    OR has_any_column_privilege(executor.oid,'public.'||item.name,'INSERT'))
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
 FOR item IN SELECT c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE c.relnamespace='public'::regnamespace AND a.attnum>0 AND NOT a.attisdropped AND c.relname IN
  ('lk_users','lk_sessions','iam_memberships','marketplace_accounts','marketplace_account_credentials',
   'review_facts','review_observations','review_policy_versions','review_policy_heads','review_draft_revisions',
   'review_decisions','review_workflow_heads','review_send_commands','review_send_command_authorities',
   'review_send_attempts','review_send_audit','review_answer_evidence','review_send_enqueue_intents','notification_in_app_events') LOOP
  allowed_update:=CASE
   WHEN item.relname IN ('lk_users','iam_memberships','marketplace_accounts','marketplace_account_credentials') THEN ARRAY['updated_at']
   WHEN item.relname='lk_sessions' THEN ARRAY['last_seen_at']
   WHEN item.relname='review_send_commands' THEN ARRAY['state','version','current_attempt_id','completed_at','result_evidence_id','reason_code','audit_event_id']
   WHEN item.relname='review_send_attempts' THEN ARRAY['state','lease_expires_at','dispatched_at','finished_at','result_evidence_id','reason_code','command_version','audit_event_id']
   ELSE ARRAY[]::text[] END;
  IF NOT item.attname=ANY(allowed_update)
   AND has_column_privilege(executor.oid,'public.'||item.relname,item.attname,'UPDATE')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
 -- Identity SELECT must not inherit passwords, session verifiers or other fields.
 FOR item IN SELECT c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE c.relnamespace='public'::regnamespace AND a.attnum>0 AND NOT a.attisdropped
  AND c.relname IN ('lk_users','lk_sessions') LOOP
  IF NOT item.attname=ANY(CASE WHEN item.relname='lk_users' THEN ARRAY['user_id','organization_id','is_active']
   ELSE ARRAY['session_id','user_id','revoked_at','expires_at'] END)
   AND has_column_privilege(executor.oid,'public.'||item.relname,item.attname,'SELECT')
  THEN RAISE EXCEPTION 'executor_privileges_invalid'; END IF;
 END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO :"executor_role";
GRANT SELECT (user_id,organization_id,is_active) ON public.lk_users TO :"executor_role";
GRANT SELECT (session_id,user_id,revoked_at,expires_at) ON public.lk_sessions TO :"executor_role";
GRANT SELECT (membership_id,organization_id,user_id,is_active,role,permissions,scope_mode,allowed_account_ids)
 ON public.iam_memberships TO :"executor_role";
GRANT SELECT (marketplace_account_id,organization_id,marketplace,external_account_id,credential_ref,status)
 ON public.marketplace_accounts TO :"executor_role";
-- Actual existing paired resolver selects this full encrypted ORM row. No keyring,
-- plaintext, password or session verifier is granted or copied into authority.
GRANT SELECT (credential_id,organization_id,marketplace_account_id,provider,credential_kind,
 algorithm,key_version,aad_version,payload_schema_version,nonce,ciphertext,generation,expires_at,
 revoked_at,revocation_reason_code,created_at,updated_at)
 ON public.marketplace_account_credentials TO :"executor_role";
-- Row locks require UPDATE privilege. These are genuine timestamp-write
-- capabilities, not read-only permissions; identity/expiry/payload mutation denied.
GRANT UPDATE (updated_at) ON public.lk_users, public.iam_memberships,
 public.marketplace_accounts, public.marketplace_account_credentials TO :"executor_role";
GRANT UPDATE (last_seen_at) ON public.lk_sessions TO :"executor_role";
-- Account serialization protects these reads. No source/history row-lock UPDATE
-- grant; a consumer needing a different lock must name its exact query first.
GRANT SELECT ON public.review_facts, public.review_observations, public.review_policy_versions,
 public.review_policy_heads, public.review_draft_revisions, public.review_decisions,
 public.review_workflow_heads, public.review_send_commands, public.review_send_command_authorities,
 public.review_send_attempts, public.review_send_audit, public.review_answer_evidence,
 public.review_send_enqueue_intents, public.notification_in_app_events TO :"executor_role";
GRANT INSERT ON public.review_send_attempts, public.review_send_audit, public.review_answer_evidence,
 public.review_send_enqueue_intents, public.notification_in_app_events TO :"executor_role";
GRANT UPDATE (state,version,current_attempt_id,completed_at,result_evidence_id,reason_code,audit_event_id)
 ON public.review_send_commands TO :"executor_role";
GRANT UPDATE (state,lease_expires_at,dispatched_at,finished_at,result_evidence_id,reason_code,command_version,audit_event_id)
 ON public.review_send_attempts TO :"executor_role";
GRANT EXECUTE ON FUNCTION public.review_strict_utf8(bytea), public.review_binding_ascii_string(text),
 public.review_run_binding_bytes(integer,integer,text,text,text),
 public.review_local_integer_text(numeric,boolean), public.review_local_timestamp_text(timestamptz),
 public.review_local_utf8_json_string(bytea), public.review_local_nonblank_utf8(bytea),
 public.review_local_string(text,boolean), public.review_local_uuid(uuid,boolean),
 public.review_local_number(numeric,boolean,boolean), public.review_local_label(text), public.review_local_checksum(text),
 public.review_send_unicode_version(), public.review_send_uuid4(uuid), public.review_send_answer_id(bytea),
 public.review_send_external_id(bytea), public.review_send_request_bytes(public.review_send_commands,bytea),
 public.review_send_evidence_bytes(public.review_answer_evidence), public.review_send_audit_bytes(public.review_send_audit),
 public.review_send_enqueue_bytes(public.review_send_enqueue_intents),
 public.notification_in_app_identity_bytes(public.notification_in_app_events),
 public.notification_in_app_event_bytes(public.notification_in_app_events) TO :"executor_role";
-- No direct trigger-function grants, receipt write, source mutation, history
-- rewrite, DELETE/TRUNCATE, default privileges, role creation or API fallback.
COMMIT;
