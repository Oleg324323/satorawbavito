\set ON_ERROR_STOP on

-- Run this script as a standalone psql input, not inside an outer transaction
-- or with --single-transaction. Runtime must never see the broad grant interval.
BEGIN;

DO $$
DECLARE
    insecure_tables text;
BEGIN
    SELECT string_agg(required.table_name, ', ' ORDER BY required.table_name)
    INTO insecure_tables
    FROM (
        VALUES
            ('review_sync_runs_v2'),
            ('review_facts'),
            ('review_observations'),
            ('review_sync_run_items'),
            ('order_sync_runs'),
            ('marketplace_orders'),
            ('marketplace_order_items'),
            ('order_observations'),
            ('order_status_observations'),
            ('order_lifecycle_events'),
            ('order_deadlines'),
            ('order_sync_coverage'),
            ('order_sync_memberships'),
            ('order_read_snapshots'),
            ('order_read_snapshot_rows'),
            ('production_work_items'),
            ('production_assignment_receipts'),
            ('production_assignment_history'),
            ('wb_repricing_sku_override_versions'),
            ('wb_repricing_sku_override_heads'),
            ('wb_repricing_sku_override_audit'),
            ('review_policy_versions'),
            ('review_policy_heads'),
            ('review_draft_revisions'),
            ('review_decisions'),
            ('review_workflow_heads'),
            ('review_local_audit'),
            ('review_local_command_receipts'),
            ('review_send_commands'),
            ('review_send_command_authorities'),
            ('review_send_attempts'),
            ('review_answer_evidence'),
            ('review_send_audit'),
            ('review_send_enqueue_intents'),
            ('notification_in_app_events'),
            ('notification_in_app_receipts'),
            ('user_orders_jobs'),
            ('user_orders_job_authorities'),
            ('user_orders_job_attempts'),
            ('user_orders_job_audit'),
            ('wb_repricing_jobs'),
            ('wb_repricing_job_authorities'),
            ('wb_repricing_job_audit'),
            ('wb_repricing_upload_receipts'),
            ('wb_repricing_upload_receipt_audit'),
            ('catalog_cost_versions'),
            ('catalog_economics_override_versions'),
            ('catalog_skus'),
            ('iam_memberships'),
            ('marketplace_accounts'),
            ('marketplace_account_credentials'),
            ('marketplace_account_ingestion_tokens'),
            ('marketplace_offers'),
            ('marketplace_products'),
            ('organization_economics_versions'),
            ('wb_finance_operations'),
            ('wb_finance_sync_run_operations'),
            ('wb_finance_sync_run_sku_daily_pnl_rollups'),
            ('wb_finance_sync_run_sku_pnl_rollups'),
            ('wb_finance_sync_run_sku_rollups'),
            ('wb_finance_sync_runs')
    ) AS required(table_name)
    LEFT JOIN pg_class AS relation
        ON relation.relname = required.table_name
        AND relation.relnamespace = 'public'::regnamespace
    WHERE relation.oid IS NULL
        OR NOT relation.relrowsecurity
        OR NOT relation.relforcerowsecurity;

    IF insecure_tables IS NOT NULL THEN
        RAISE EXCEPTION 'canonical tables without forced RLS: %', insecure_tables;
    END IF;
END
$$;

SELECT format('CREATE ROLE %I', :'runtime_role')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_role')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'runtime_role',
    :'runtime_password'
)
\gexec

GRANT CONNECT, TEMPORARY ON DATABASE :"runtime_database" TO :"runtime_role";
REVOKE CREATE ON DATABASE :"runtime_database" FROM :"runtime_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM :"runtime_role";
GRANT USAGE ON SCHEMA public TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
-- 0076 additive columns inherit the existing table privileges. Column REVOKE
-- cannot narrow a table UPDATE grant: immutable incarnation/binding triggers
-- enforce these invariants, including OLD+1 caller counter assignments.
-- Trigger execution is automatic; runtime may not invoke the functions itself.
REVOKE ALL ON FUNCTION public.ingestion_account_incarnation_guard(),
    public.ingestion_token_binding_guard() FROM PUBLIC, :"runtime_role";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";

ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"runtime_role";

REVOKE ALL ON TABLE public.alembic_version FROM :"runtime_role";
-- Quota admission belongs only to separately approved reader/executor custody.
-- Conditional for older schema fixtures; never grant API quota rights.
SELECT format('REVOKE ALL ON TABLE public.wb_price_quota FROM %I', :'runtime_role')
WHERE to_regclass('public.wb_price_quota') IS NOT NULL
\gexec

-- Orders overrides must follow every broad runtime grant above.
REVOKE ALL ON TABLE public.order_sync_runs, public.marketplace_orders,
    public.marketplace_order_items, public.order_observations,
    public.order_status_observations, public.order_lifecycle_events,
    public.order_deadlines, public.order_sync_coverage, public.order_sync_memberships,
    public.order_read_snapshots, public.order_read_snapshot_rows FROM :"runtime_role";
GRANT SELECT, INSERT ON TABLE public.order_sync_runs, public.marketplace_orders,
    public.marketplace_order_items, public.order_observations,
    public.order_status_observations, public.order_lifecycle_events,
    public.order_deadlines, public.order_sync_coverage, public.order_sync_memberships,
    public.order_read_snapshots, public.order_read_snapshot_rows TO :"runtime_role";
GRANT UPDATE ON TABLE public.order_sync_runs, public.marketplace_orders,
    public.marketplace_order_items TO :"runtime_role";

-- Immutable Orders binding: only pure CHECK/codec helpers are callable.
REVOKE ALL ON FUNCTION public.orders_run_binding_insert_guard(),
    public.orders_run_binding_update_guard() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.orders_binding_text(text,integer),
    public.orders_binding_ascii_string(text),
    public.orders_run_binding_bytes(integer,integer,text,text,text)
    FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.orders_binding_text(text,integer),
    public.orders_binding_ascii_string(text),
    public.orders_run_binding_bytes(integer,integer,text,text,text) TO :"runtime_role";

-- Only the identity sequences belonging to these Orders tables are narrowed.
SELECT format('REVOKE ALL ON SEQUENCE %s FROM %I; GRANT USAGE ON SEQUENCE %s TO %I',
    pg_get_serial_sequence(format('public.%I', c.relname), a.attname), :'runtime_role',
    pg_get_serial_sequence(format('public.%I', c.relname), a.attname), :'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attidentity<>''
WHERE c.relnamespace='public'::regnamespace AND c.relname IN (
    'order_sync_runs','marketplace_orders','marketplace_order_items','order_observations',
    'order_status_observations','order_lifecycle_events','order_deadlines','order_sync_coverage',
    'order_sync_memberships','order_read_snapshots','order_read_snapshot_rows')
\gexec

-- Review Facts overrides also follow all broad grants within this transaction.
REVOKE ALL ON TABLE public.review_sync_runs_v2, public.review_facts,
    public.review_observations, public.review_sync_run_items FROM :"runtime_role";
-- Explicitly clear column rights before reinstating the INSERT allowlist.
SELECT format('REVOKE ALL (%I) ON TABLE public.review_sync_runs_v2 FROM %I',
    a.attname, :'runtime_role')
FROM pg_attribute a WHERE a.attrelid='public.review_sync_runs_v2'::regclass
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, UPDATE ON TABLE public.review_sync_runs_v2 TO :"runtime_role";
GRANT INSERT (sync_run_id, organization_id, marketplace_account_id, marketplace,
    source_run_id, request_checksum, status, completeness, started_at, completed_at,
    observed_count, manifest_checksum, coverage, error_code, source_run_id_utf8, coverage_utf8,
    account_binding_schema_version, account_binding_external_account_id,
    account_binding_credential_ref, account_binding_payload, account_binding_checksum)
    ON public.review_sync_runs_v2 TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON TABLE public.review_facts TO :"runtime_role";
GRANT SELECT, INSERT ON TABLE public.review_observations, public.review_sync_run_items
    TO :"runtime_role";
GRANT EXECUTE ON FUNCTION public.review_strict_utf8(bytea),
    public.review_coverage_json_object_utf8(bytea) TO :"runtime_role";
-- Review binding adds only pure descriptor/CHECK evaluation authority.
GRANT EXECUTE ON FUNCTION public.review_binding_ascii_string(text),
    public.review_run_binding_bytes(integer,integer,text,text,text) TO :"runtime_role";
SELECT format('REVOKE ALL ON SEQUENCE %s FROM %I; GRANT USAGE ON SEQUENCE %s TO %I',
    pg_get_serial_sequence('public.review_sync_runs_v2','run_sequence'), :'runtime_role',
    pg_get_serial_sequence('public.review_sync_runs_v2','run_sequence'), :'runtime_role')
\gexec

-- Repricer overrides follow all broad grants inside the same atomic transaction.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM (VALUES ('wb_repricer_price_approvals'),
        ('wb_repricer_price_apply_attempts'),('wb_repricer_price_approval_audit')) t(name)
        LEFT JOIN pg_class c ON c.oid=to_regclass('public.'||t.name)
        WHERE c.oid IS NULL OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
    THEN RAISE EXCEPTION 'repricer_forced_rls_required'; END IF;
END $$;
REVOKE ALL ON TABLE public.wb_repricer_price_approvals,
    public.wb_repricer_price_apply_attempts, public.wb_repricer_price_approval_audit
    FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I',
    a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('wb_repricer_price_approvals','wb_repricer_price_apply_attempts','wb_repricer_price_approval_audit')
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT, UPDATE ON TABLE public.wb_repricer_price_approvals,
    public.wb_repricer_price_apply_attempts TO :"runtime_role";
GRANT SELECT, INSERT ON TABLE public.wb_repricer_price_approval_audit TO :"runtime_role";
REVOKE ALL ON FUNCTION public.repricer_account_lock(), public.repricer_row_guard(),
    public.repricer_validate() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.repricer_exact_text(text),
    public.repricer_integral_finite(numeric), public.repricer_safe_code(text),
    public.repricer_ascii_json_string(text), public.repricer_integer_decimal(numeric),
    public.repricer_request_bytes(integer,integer,text,integer,numeric,text,numeric,smallint,numeric,numeric),
    public.repricer_action_key(integer,integer,text,text),
    public.repricer_dispatch_key(integer,integer,text,text,uuid), public.repricer_context_id(text)
    FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.repricer_exact_text(text),
    public.repricer_integral_finite(numeric), public.repricer_safe_code(text),
    public.repricer_ascii_json_string(text), public.repricer_integer_decimal(numeric),
    public.repricer_request_bytes(integer,integer,text,integer,numeric,text,numeric,smallint,numeric,numeric),
    public.repricer_action_key(integer,integer,text,text),
    public.repricer_dispatch_key(integer,integer,text,text,uuid), public.repricer_context_id(text)
    TO :"runtime_role";

-- Dormant Production assignment overrides, after every broad grant.
REVOKE ALL ON TABLE public.production_work_items,
    public.production_assignment_receipts, public.production_assignment_history
    FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I',
    a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('production_work_items','production_assignment_receipts','production_assignment_history')
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT, UPDATE ON TABLE public.production_work_items TO :"runtime_role";
GRANT SELECT, INSERT ON TABLE public.production_assignment_receipts,
    public.production_assignment_history TO :"runtime_role";
SELECT format('REVOKE ALL ON SEQUENCE %s FROM PUBLIC, %I; GRANT USAGE ON SEQUENCE %s TO %I',
    pg_get_serial_sequence(format('public.%I',c.relname),a.attname), :'runtime_role',
    pg_get_serial_sequence(format('public.%I',c.relname),a.attname), :'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attidentity<>''
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('production_work_items','production_assignment_receipts','production_assignment_history')
\gexec
REVOKE ALL ON FUNCTION public.production_account_lock(), public.production_row_guard(),
    public.production_validate() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.production_exact_text(text),
    public.production_ascii_json_string(text),
    public.production_assignment_bytes(bigint,bigint,integer,text,text)
    FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.production_exact_text(text),
    public.production_ascii_json_string(text),
    public.production_assignment_bytes(bigint,bigint,integer,text,text) TO :"runtime_role";

-- Account-scoped SKU override rights, after the atomic broad-grant interval.
REVOKE ALL ON TABLE public.wb_repricing_sku_override_versions,
    public.wb_repricing_sku_override_heads, public.wb_repricing_sku_override_audit
    FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I',
    a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('wb_repricing_sku_override_versions','wb_repricing_sku_override_heads','wb_repricing_sku_override_audit')
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT ON TABLE public.wb_repricing_sku_override_versions,
    public.wb_repricing_sku_override_audit TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON TABLE public.wb_repricing_sku_override_heads TO :"runtime_role";
REVOKE ALL ON FUNCTION public.wb_sku_override_account_lock(), public.wb_sku_override_row_guard(),
    public.wb_sku_override_validate() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.wb_sku_override_decimal(numeric),
    public.wb_sku_override_integral(numeric),
    public.wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)
    FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.wb_sku_override_decimal(numeric),
    public.wb_sku_override_integral(numeric),
    public.wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)
    TO :"runtime_role";

-- Local Review completed receipts/history: exact rights after broad grants.
REVOKE ALL ON TABLE public.review_policy_versions, public.review_policy_heads,
    public.review_draft_revisions, public.review_decisions, public.review_workflow_heads,
    public.review_local_audit, public.review_local_command_receipts FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I',
    a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('review_policy_versions','review_policy_heads','review_draft_revisions',
     'review_decisions','review_workflow_heads','review_local_audit','review_local_command_receipts')
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT ON TABLE public.review_policy_versions, public.review_draft_revisions,
    public.review_decisions, public.review_local_audit, public.review_local_command_receipts TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON TABLE public.review_policy_heads, public.review_workflow_heads TO :"runtime_role";
REVOKE ALL ON FUNCTION public.review_local_account_lock(), public.review_local_row_guard(),
    public.review_local_validate() FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, %I; GRANT EXECUTE ON FUNCTION %s TO %I',
    p.oid::regprocedure,:'runtime_role',p.oid::regprocedure,:'runtime_role')
FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.oid IN
    ('public.review_local_integer_text(numeric,boolean)'::regprocedure,
     'public.review_local_timestamp_text(timestamp with time zone)'::regprocedure,
     'public.review_local_utf8_json_string(bytea)'::regprocedure,
     'public.review_local_nonblank_utf8(bytea)'::regprocedure,
     'public.review_local_string(text,boolean)'::regprocedure,
     'public.review_local_uuid(uuid,boolean)'::regprocedure,
     'public.review_local_number(numeric,boolean,boolean)'::regprocedure,
     'public.review_local_label(text)'::regprocedure,
     'public.review_local_checksum(text)'::regprocedure,
     'public.review_local_policy_bytes(public.review_policy_versions)'::regprocedure,
     'public.review_local_generation_bytes(public.review_draft_revisions)'::regprocedure,
     'public.review_local_decision_binding_bytes(public.review_draft_revisions,bytea)'::regprocedure,
     'public.review_local_audit_bytes(public.review_local_audit)'::regprocedure,
     'public.review_local_result_bytes(public.review_local_command_receipts)'::regprocedure,
     'public.review_local_request_bytes(public.review_local_command_receipts,public.review_policy_versions,public.review_draft_revisions,public.review_decisions,bytea)'::regprocedure)
\gexec

-- Session-bound Orders jobs: new-object-only overrides, no deletion or helper dispatch.
REVOKE ALL ON TABLE public.user_orders_jobs, public.user_orders_job_authorities,
    public.user_orders_job_attempts, public.user_orders_job_audit FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I',
    a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
    ('user_orders_jobs','user_orders_job_authorities','user_orders_job_attempts','user_orders_job_audit')
    AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT ON TABLE public.user_orders_jobs, public.user_orders_job_authorities,
    public.user_orders_job_attempts, public.user_orders_job_audit TO :"runtime_role";
GRANT UPDATE (state,version,attempt_count,current_attempt_id,next_attempt_at,completed_at,
    safe_reason,result_sync_run_id,result_coverage_state) ON public.user_orders_jobs TO :"runtime_role";
GRANT UPDATE (state,version,job_version_after,lease_expires_at,finished_at,safe_reason,
    result_sync_run_id,result_coverage_state) ON public.user_orders_job_attempts TO :"runtime_role";
REVOKE ALL ON FUNCTION public.user_orders_row_guard(), public.user_orders_transition_witness()
    FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.user_orders_ascii(text), public.user_orders_text(text,integer),
    public.user_orders_request_bytes(public.user_orders_jobs) FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.user_orders_ascii(text), public.user_orders_text(text,integer),
    public.user_orders_request_bytes(public.user_orders_jobs) TO :"runtime_role";

-- Repricer immutable jobs: API creation only; executor receipts use a separate login.
REVOKE ALL ON TABLE public.wb_repricing_jobs, public.wb_repricing_job_authorities,
    public.wb_repricing_job_audit, public.wb_repricing_upload_receipts,
    public.wb_repricing_upload_receipt_audit FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I', a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
 ('wb_repricing_jobs','wb_repricing_job_authorities','wb_repricing_job_audit',
  'wb_repricing_upload_receipts','wb_repricing_upload_receipt_audit')
 AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT ON public.wb_repricing_jobs, public.wb_repricing_job_authorities,
 public.wb_repricing_job_audit TO :"runtime_role";
GRANT SELECT ON public.wb_repricing_upload_receipts, public.wb_repricing_upload_receipt_audit TO :"runtime_role";
REVOKE ALL ON FUNCTION public.repricing_job_guard(), public.repricing_job_witness() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.repricing_job_uuid(uuid), public.repricing_job_time(timestamptz),
 public.repricing_job_text(text,integer) FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.repricing_job_uuid(uuid), public.repricing_job_time(timestamptz),
 public.repricing_job_text(text,integer) TO :"runtime_role";

-- Review send/in-app: exact new-object capabilities. These SQL grants do not
-- authenticate a worker or prove current multi-member/closing authority.
REVOKE ALL ON TABLE public.review_send_commands, public.review_send_command_authorities,
 public.review_send_attempts, public.review_answer_evidence, public.review_send_audit,
 public.review_send_enqueue_intents, public.notification_in_app_events,
 public.notification_in_app_receipts FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON TABLE public.%I FROM PUBLIC, %I', a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
 ('review_send_commands','review_send_command_authorities','review_send_attempts',
  'review_answer_evidence','review_send_audit','review_send_enqueue_intents',
  'notification_in_app_events','notification_in_app_receipts')
 AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT, INSERT ON TABLE public.review_send_commands, public.review_send_command_authorities,
 public.review_send_attempts, public.review_answer_evidence, public.review_send_audit,
 public.review_send_enqueue_intents, public.notification_in_app_events,
 public.notification_in_app_receipts TO :"runtime_role";
GRANT UPDATE (state,version,current_attempt_id,completed_at,result_evidence_id,reason_code,audit_event_id)
 ON public.review_send_commands TO :"runtime_role";
GRANT UPDATE (state,lease_expires_at,dispatched_at,finished_at,result_evidence_id,reason_code,command_version,audit_event_id)
 ON public.review_send_attempts TO :"runtime_role";
GRANT UPDATE (read_at,dismissed_at) ON public.notification_in_app_receipts TO :"runtime_role";
REVOKE ALL ON FUNCTION public.review_send_account_lock(), public.review_send_audit_guard(),
 public.review_send_row_guard(), public.review_send_validate() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.review_send_unicode_version(), public.review_send_uuid4(uuid),
 public.review_send_answer_id(bytea), public.review_send_external_id(bytea),
 public.review_send_request_bytes(public.review_send_commands,bytea),
 public.review_send_evidence_bytes(public.review_answer_evidence),
 public.review_send_audit_bytes(public.review_send_audit),
 public.review_send_enqueue_bytes(public.review_send_enqueue_intents),
 public.notification_in_app_identity_bytes(public.notification_in_app_events),
 public.notification_in_app_event_bytes(public.notification_in_app_events),
 public.notification_in_app_receipt_bytes(public.notification_in_app_receipts),
 public.notification_in_app_visible_action_bytes(integer,integer,integer,uuid[],text)
 FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.review_send_unicode_version(), public.review_send_uuid4(uuid),
 public.review_send_answer_id(bytea), public.review_send_external_id(bytea),
 public.review_send_request_bytes(public.review_send_commands,bytea),
 public.review_send_evidence_bytes(public.review_answer_evidence),
 public.review_send_audit_bytes(public.review_send_audit),
 public.review_send_enqueue_bytes(public.review_send_enqueue_intents),
 public.notification_in_app_identity_bytes(public.notification_in_app_events),
 public.notification_in_app_event_bytes(public.notification_in_app_events),
 public.notification_in_app_receipt_bytes(public.notification_in_app_receipts),
 public.notification_in_app_visible_action_bytes(integer,integer,integer,uuid[],text)
 TO :"runtime_role";

-- Repricer typed state: exact new objects only. Org settings mutation has no
-- approved application permission yet, so this login receives read access only
-- to that family. Account-owned assignment/liquidation can be implemented now.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM (VALUES ('wb_repricing_settings_versions'),
 ('wb_repricing_settings_heads'),('wb_repricing_basket_norm_defaults'),
 ('wb_repricing_assignment_versions'),('wb_repricing_assignment_heads'),
 ('wb_repricing_liquidation_campaigns'),('wb_repricing_liquidation_versions'),
 ('wb_repricing_liquidation_heads'),('wb_repricing_state_audit')) t(name)
 LEFT JOIN pg_class c ON c.oid=to_regclass('public.'||t.name)
 WHERE c.oid IS NULL OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
 THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_state_forced_rls_required'; END IF;
END $$;
REVOKE ALL ON TABLE public.wb_repricing_settings_versions, public.wb_repricing_settings_heads,
 public.wb_repricing_basket_norm_defaults, public.wb_repricing_assignment_versions,
 public.wb_repricing_assignment_heads, public.wb_repricing_liquidation_campaigns,
 public.wb_repricing_liquidation_versions, public.wb_repricing_liquidation_heads,
 public.wb_repricing_state_audit FROM PUBLIC, :"runtime_role";
SELECT format('REVOKE ALL (%I) ON public.%I FROM PUBLIC, %I',a.attname,c.relname,:'runtime_role')
FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
WHERE c.relnamespace='public'::regnamespace AND c.relname IN
 ('wb_repricing_settings_versions','wb_repricing_settings_heads','wb_repricing_basket_norm_defaults',
  'wb_repricing_assignment_versions','wb_repricing_assignment_heads','wb_repricing_liquidation_campaigns',
  'wb_repricing_liquidation_versions','wb_repricing_liquidation_heads','wb_repricing_state_audit')
 AND a.attnum>0 AND NOT a.attisdropped
\gexec
GRANT SELECT ON public.wb_repricing_settings_versions, public.wb_repricing_settings_heads,
 public.wb_repricing_basket_norm_defaults TO :"runtime_role";
GRANT SELECT, INSERT ON public.wb_repricing_assignment_versions, public.wb_repricing_assignment_heads,
 public.wb_repricing_liquidation_campaigns, public.wb_repricing_liquidation_versions,
 public.wb_repricing_liquidation_heads, public.wb_repricing_state_audit TO :"runtime_role";
GRANT UPDATE (current_revision,version,updated_at) ON public.wb_repricing_assignment_heads TO :"runtime_role";
GRANT UPDATE (current_revision,version,updated_at,state,current_price_kopecks,target_price_kopecks,
 step_pct,hold_orders_to,next_step_at,requires_negative_margin_confirm,confirmed_by_membership_id,
 confirmed_at,resulting_approval_id,resulting_approval_row_id)
 ON public.wb_repricing_liquidation_heads TO :"runtime_role";
REVOKE ALL ON FUNCTION public.wb_state_lock(), public.wb_state_guard(),
 public.wb_state_settings_witness(), public.wb_state_assignment_witness(),
 public.wb_state_liquidation_witness() FROM PUBLIC, :"runtime_role";
REVOKE ALL ON FUNCTION public.wb_state_uuid(uuid), public.wb_state_time(timestamptz),
 public.wb_state_timestamp(timestamptz), public.wb_state_json(jsonb),
 public.wb_state_settings_bytes(public.wb_repricing_settings_versions,public.wb_repricing_basket_norm_defaults[]),
 public.wb_state_assignment_bytes(public.wb_repricing_assignment_versions),
 public.wb_state_liquidation_bytes(public.wb_repricing_liquidation_versions,public.wb_repricing_liquidation_campaigns)
 FROM PUBLIC, :"runtime_role";
GRANT EXECUTE ON FUNCTION public.wb_state_uuid(uuid), public.wb_state_time(timestamptz),
 public.wb_state_timestamp(timestamptz), public.wb_state_json(jsonb),
 public.wb_state_settings_bytes(public.wb_repricing_settings_versions,public.wb_repricing_basket_norm_defaults[]),
 public.wb_state_assignment_bytes(public.wb_repricing_assignment_versions),
 public.wb_state_liquidation_bytes(public.wb_repricing_liquidation_versions,public.wb_repricing_liquidation_campaigns)
 TO :"runtime_role";
-- Exact preexisting pure codec dependencies; no old table/index or policy change.
GRANT EXECUTE ON FUNCTION public.repricer_exact_text(text), public.repricer_ascii_json_string(text),
 public.wb_sku_override_decimal(numeric), public.wb_sku_override_integral(numeric) TO :"runtime_role";

-- 0077: only current WB source storage objects. These database privileges do
-- not authenticate a worker, supply a source permission or enable a collector.
DO $$
DECLARE t record; a record; c record; f record; who text;
BEGIN
 FOR t IN SELECT x.name,relation.oid,relation.relowner,relation.relname,relation.relacl,relation.relkind,relation.relrowsecurity,relation.relforcerowsecurity
 FROM (VALUES ('wb_price_runs'),('wb_price_pages'),('wb_price_product_facts'),('wb_price_size_facts'),
 ('wb_price_current_heads'),('wb_price_audit'),('wb_stock_runs'),('wb_stock_pages'),
 ('wb_stock_observations'),('wb_stock_current_heads'),('wb_stock_audit'),
 ('wb_price_ingest_sequence'),('wb_stock_ingest_sequence')) x(name)
 LEFT JOIN pg_class relation ON relation.oid=to_regclass('public.'||x.name) LOOP
  IF t.oid IS NULL OR (t.relkind<>'S' AND (NOT t.relrowsecurity OR NOT t.relforcerowsecurity)) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_current_forced_rls_required'; END IF;
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
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE oid IN (
 'public.wb_current_uuid(uuid)'::regprocedure,'public.wb_current_time(timestamptz)'::regprocedure,
 'public.wb_current_presence(text,bigint)'::regprocedure,
 'public.wb_current_request_bytes(integer,integer,text,text,bigint)'::regprocedure,
 'public.wb_price_manifest_bytes(public.wb_price_runs,public.wb_price_pages[])'::regprocedure,
 'public.wb_stock_manifest_bytes(public.wb_stock_runs,public.wb_stock_pages[])'::regprocedure,
 'public.wb_current_account_lock()'::regprocedure,'public.wb_current_immutable()'::regprocedure,
 'public.wb_price_run_guard()'::regprocedure,'public.wb_price_child_guard()'::regprocedure,
 'public.wb_price_head_guard()'::regprocedure,'public.wb_price_audit_guard()'::regprocedure,
 'public.wb_price_emit_audit()'::regprocedure,'public.wb_price_graph()'::regprocedure,
 'public.wb_stock_run_guard()'::regprocedure,'public.wb_stock_child_guard()'::regprocedure,
 'public.wb_stock_head_guard()'::regprocedure,'public.wb_stock_audit_guard()'::regprocedure,
 'public.wb_stock_emit_audit()'::regprocedure,'public.wb_stock_graph()'::regprocedure) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(f.proacl) WHERE grantee<>f.proowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
END $$;
GRANT SELECT ON public.wb_price_runs, public.wb_price_pages, public.wb_price_product_facts,
 public.wb_price_size_facts, public.wb_price_current_heads, public.wb_price_audit,
 public.wb_stock_runs, public.wb_stock_pages, public.wb_stock_observations,
 public.wb_stock_current_heads, public.wb_stock_audit TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,request_key,source_kind,parser_version,
 started_at,page_limit,request_bytes,request_checksum,account_binding_schema_version,account_binding_external_account_id,
 account_binding_credential_ref,account_binding_payload,account_binding_checksum,credential_id,credential_kind,
 credential_generation,credential_payload_schema_version,credential_expires_at)
 ON public.wb_price_runs, public.wb_stock_runs TO :"runtime_role";
GRANT UPDATE (state,version,received_at,manifest_checksum,page_count,raw_row_count,fact_count,safe_error_code)
 ON public.wb_price_runs, public.wb_stock_runs TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,run_id,page_no,page_offset,requested_limit,
 received_at,source_observed_at,http_status,raw_checksum,raw_row_count,terminal,request_id)
 ON public.wb_price_pages, public.wb_stock_pages TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,run_id,page_no,nm_id,source_size_count,
 unresolved_size_count,discount_presence,discount,club_discount_presence,club_discount)
 ON public.wb_price_product_facts TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,run_id,nm_id,size_id,currency,
 list_price_kopecks_presence,list_price_kopecks,discounted_price_kopecks_presence,discounted_price_kopecks,
 club_price_kopecks_presence,club_price_kopecks) ON public.wb_price_size_facts TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,run_id,page_no,stock_scope,warehouse_id,
 nm_id,chrt_id,warehouse_display_name,quantity_presence,quantity,in_way_to_client_presence,in_way_to_client,
 in_way_from_client_presence,in_way_from_client) ON public.wb_stock_observations TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,source_kind,parser_version,request_checksum,
 run_id,published_sequence,version) ON public.wb_price_current_heads, public.wb_stock_current_heads TO :"runtime_role";
GRANT UPDATE (run_id,published_sequence,version)
 ON public.wb_price_current_heads, public.wb_stock_current_heads TO :"runtime_role";
-- Used by SECURITY INVOKER automatic audit triggers; the audit insert guard
-- rejects direct INSERT. IDs and timestamps are always supplied by the DB.
GRANT INSERT (organization_id,marketplace_account_id,marketplace,run_id,event_kind,actor_kind,
 actor_membership_id,before_run,after_run,before_head,after_head)
 ON public.wb_price_audit, public.wb_stock_audit TO :"runtime_role";
-- USAGE permits burning nextval; no SELECT/UPDATE, setval, ALTER or ownership.
-- An explicit ingest_sequence is rejected before INSERT even for table owners.
GRANT USAGE ON SEQUENCE public.wb_price_ingest_sequence, public.wb_stock_ingest_sequence TO :"runtime_role";
GRANT EXECUTE ON FUNCTION public.wb_current_uuid(uuid), public.wb_current_time(timestamptz),
 public.wb_current_presence(text,bigint), public.wb_current_request_bytes(integer,integer,text,text,bigint),
 public.wb_price_manifest_bytes(public.wb_price_runs,public.wb_price_pages[]),
 public.wb_stock_manifest_bytes(public.wb_stock_runs,public.wb_stock_pages[]) TO :"runtime_role";
-- Exact preexisting schema-1 account descriptor dependencies, no parent edits.
GRANT EXECUTE ON FUNCTION public.orders_binding_text(text,integer),
 public.review_run_binding_bytes(integer,integer,text,text,text), public.review_binding_ascii_string(text)
 TO :"runtime_role";

-- 0078: immutable daily stock/evidence storage only. These privileges are not
-- reviewer permissions, trusted worker identity or an activation instruction.
DO $$
DECLARE t record; a record; c record; f record; who text;
BEGIN
 FOR t IN SELECT x.name,relation.oid,relation.relowner,relation.relname,relation.relacl,relation.relrowsecurity,relation.relforcerowsecurity
 FROM (VALUES ('wb_stock_daily_revisions'),('wb_stock_daily_heads'),('wb_stock_revision_evidence'),
 ('wb_stock_revision_evidence_diffs'),('wb_stock_revision_evidence_decisions'),('wb_stock_daily_audit')) x(name)
 LEFT JOIN pg_class relation ON relation.oid=to_regclass('public.'||x.name) LOOP
  IF t.oid IS NULL OR NOT t.relrowsecurity OR NOT t.relforcerowsecurity THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_daily_forced_rls_required'; END IF;
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
 FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE oid IN (
 'public.wb_daily_integer(numeric,boolean)'::regprocedure,'public.wb_daily_whitespace(integer)'::regprocedure,
 'public.wb_daily_reference(text)'::regprocedure,'public.wb_daily_ascii(text)'::regprocedure,
 'public.wb_stock_evidence_row_bytes(public.wb_stock_observations)'::regprocedure,
 'public.wb_stock_evidence_diff_bytes(public.wb_stock_revision_evidence_diffs[])'::regprocedure,
 'public.wb_stock_evidence_proposal_bytes(public.wb_stock_revision_evidence)'::regprocedure,
 'public.wb_stock_evidence_expected_diff(integer,integer,uuid,uuid)'::regprocedure,
 'public.wb_daily_run_eligible(integer,integer,uuid,bytea,text,date)'::regprocedure,
 'public.wb_daily_lock()'::regprocedure,'public.wb_daily_immutable()'::regprocedure,
 'public.wb_daily_insert_guard()'::regprocedure,'public.wb_daily_head_guard()'::regprocedure,
 'public.wb_daily_emit_audit()'::regprocedure,'public.wb_daily_audit_guard()'::regprocedure,
 'public.wb_daily_graph()'::regprocedure,'public.wb_stock_evidence_graph()'::regprocedure) LOOP
  FOR a IN SELECT DISTINCT grantee FROM aclexplode(f.proacl) WHERE grantee<>f.proowner LOOP
   who:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,who);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
END $$;
GRANT SELECT ON public.wb_stock_daily_revisions, public.wb_stock_daily_heads,
 public.wb_stock_revision_evidence, public.wb_stock_revision_evidence_diffs,
 public.wb_stock_revision_evidence_decisions, public.wb_stock_daily_audit TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,stock_scope,request_checksum,
 business_date_msk,revision,daily_revision_id,command_id,source_kind,parser_version,source_run_id,
 request_bytes,basis,effective_observation_at,actor_kind,actor_membership_id,correction_reason,
 supersedes_revision,evidence_id,decision_id,proposal_checksum)
 ON public.wb_stock_daily_revisions TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,stock_scope,request_checksum,
 business_date_msk,current_revision,version) ON public.wb_stock_daily_heads TO :"runtime_role";
GRANT UPDATE (current_revision,version) ON public.wb_stock_daily_heads TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,stock_scope,request_checksum,
 business_date_msk,evidence_id,source_kind,parser_version,grain_version,request_bytes,before_run_id,
 after_run_id,before_manifest_checksum,after_manifest_checksum,before_daily_revision,
 proposed_by_membership_id,proposal_command_id,proposal_bytes,proposal_checksum,evidence_document_bytes,
 evidence_document_checksum,reviewed_evidence_reference,diff_checksum,added_count,removed_count,changed_count)
 ON public.wb_stock_revision_evidence TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,evidence_id,diff_row_id,nm_id,
 chrt_id,warehouse_id,change_kind,before_payload_checksum,after_payload_checksum)
 ON public.wb_stock_revision_evidence_diffs TO :"runtime_role";
GRANT INSERT (organization_id,marketplace_account_id,marketplace,evidence_id,decision_id,decision_command_id,
 outcome,reviewed_by_membership_id,reviewed_proposal_checksum,reason_code)
 ON public.wb_stock_revision_evidence_decisions TO :"runtime_role";
-- Supports the SECURITY INVOKER head audit trigger only; direct INSERT is denied
-- by the audit origin guard. Generated IDs/timestamps are excluded from grants.
GRANT INSERT (organization_id,marketplace_account_id,marketplace,stock_scope,request_checksum,
 business_date_msk,revision,source_run_id,event_kind,actor_kind,actor_membership_id,
 before_revision,after_revision,before_head,after_head) ON public.wb_stock_daily_audit TO :"runtime_role";
GRANT EXECUTE ON FUNCTION public.wb_daily_integer(numeric,boolean), public.wb_daily_whitespace(integer),
 public.wb_daily_reference(text), public.wb_daily_ascii(text),
 public.wb_stock_evidence_row_bytes(public.wb_stock_observations),
 public.wb_stock_evidence_diff_bytes(public.wb_stock_revision_evidence_diffs[]),
 public.wb_stock_evidence_proposal_bytes(public.wb_stock_revision_evidence),
 public.wb_stock_evidence_expected_diff(integer,integer,uuid,uuid),
 public.wb_daily_run_eligible(integer,integer,uuid,bytea,text,date) TO :"runtime_role";
-- Exact existing validation dependencies; parent tables, indexes and policies unchanged.
GRANT EXECUTE ON FUNCTION public.wb_current_uuid(uuid), public.wb_current_time(timestamptz),
 public.review_local_nonblank_utf8(bytea), public.review_strict_utf8(bytea) TO :"runtime_role";

-- Known 0080/0082 WB objects: API creates intents/retries, never publications.
-- Current-object override only. Broad future defaults remain a separate risk.
SELECT set_config('wb_live.grant_api_role', :'runtime_role', true);
DO $$ DECLARE t record; a record; target text:=current_setting('wb_live.grant_api_role'); BEGIN
 IF to_regclass('public.wb_live_sync_jobs') IS NOT NULL THEN
  IF EXISTS(SELECT 1 FROM unnest(ARRAY['wb_live_sync_jobs','wb_live_sync_sources','wb_live_sync_requests',
   'wb_live_products','wb_live_product_sizes','wb_live_pages']) n
   LEFT JOIN pg_class c ON c.oid=to_regclass('public.'||n)
   WHERE c.oid IS NULL OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
  THEN RAISE EXCEPTION 'wb_live_schema_invalid'; END IF;
  IF to_regclass('public.wb_live_history_requests') IS NOT NULL AND EXISTS(
   SELECT 1 FROM unnest(ARRAY['wb_live_history_requests','wb_live_history_pages','wb_live_history_rows']) n
   LEFT JOIN pg_class c ON c.oid=to_regclass('public.'||n)
   WHERE c.oid IS NULL OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
  THEN RAISE EXCEPTION 'wb_live_schema_invalid'; END IF;
  FOR t IN SELECT oid,relname FROM pg_class WHERE relnamespace='public'::regnamespace
   AND relname IN ('wb_live_sync_jobs','wb_live_sync_sources','wb_live_sync_requests','wb_live_products',
    'wb_live_product_sizes','wb_live_pages','wb_live_history_requests','wb_live_history_pages','wb_live_history_rows') LOOP
   EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,%I',t.relname,target);
   FOR a IN SELECT attname FROM pg_attribute WHERE attrelid=t.oid AND attnum>0 AND NOT attisdropped LOOP
    EXECUTE format('REVOKE ALL (%I) ON public.%I FROM PUBLIC,%I',a.attname,t.relname,target);
   END LOOP;
  END LOOP;
  EXECUTE format('GRANT SELECT ON wb_live_sync_jobs,wb_live_sync_sources,wb_live_sync_requests,wb_live_products,wb_live_product_sizes,wb_live_pages TO %I',target);
  EXECUTE format('GRANT INSERT(organization_id,marketplace_account_id,job_id,credential_id,credential_generation,account_incarnation,external_account_id,credential_ref,user_id,membership_id,session_id,state,created_at,updated_at) ON wb_live_sync_jobs TO %I',target);
  EXECUTE format('GRANT UPDATE(state,updated_at) ON wb_live_sync_jobs TO %I',target);
  EXECUTE format('GRANT INSERT(organization_id,marketplace_account_id,job_id,source,run_id,state,checkpoint,processed,revision,attempt,lease_token,lease_expires_at,next_due_at,updated_at,error_code) ON wb_live_sync_sources TO %I',target);
  EXECUTE format('GRANT UPDATE(state,attempt,error_code,lease_token,lease_expires_at,next_due_at,updated_at) ON wb_live_sync_sources TO %I',target);
  EXECUTE format('GRANT INSERT(organization_id,marketplace_account_id,idempotency_key,job_id) ON wb_live_sync_requests TO %I',target);
  EXECUTE format('REVOKE ALL ON FUNCTION public.wb_live_due_jobs(integer) FROM PUBLIC,%I',target);
  IF to_regclass('public.wb_live_history_requests') IS NOT NULL THEN
   EXECUTE format('GRANT SELECT ON wb_live_history_requests TO %I',target);
   EXECUTE format('GRANT INSERT(organization_id,marketplace_account_id,idempotency_key,job_id,date_from) ON wb_live_history_requests TO %I',target);
   EXECUTE format('REVOKE ALL ON FUNCTION public.wb_live_history_immutable() FROM PUBLIC,%I',target);
  END IF;
 END IF;
END $$;
COMMIT;
