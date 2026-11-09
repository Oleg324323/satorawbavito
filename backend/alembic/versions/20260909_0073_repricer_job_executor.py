"""Immutable repricer job and provider receipt graph. IMPLEMENTED / UNVERIFIED."""
import sqlalchemy as sa
from alembic import op

revision = "20260909_0073"
down_revision = "20260909_0072"
branch_labels = depends_on = None

TABLES = ("wb_repricing_jobs", "wb_repricing_job_authorities", "wb_repricing_job_audit",
          "wb_repricing_upload_receipts", "wb_repricing_upload_receipt_audit")
PURE = ("repricing_job_uuid(uuid)", "repricing_job_time(timestamptz)", "repricing_job_text(text,integer)")
TRIGGERS = ("repricing_job_guard()", "repricing_job_witness()")

SQL = r'''
CREATE FUNCTION public.repricing_job_uuid(v uuid) RETURNS boolean LANGUAGE sql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(substr(v::text,15,1)='4' AND substr(v::text,20,1) IN ('8','9','a','b'),false) $$;
CREATE FUNCTION public.repricing_job_time(v timestamptz) RETURNS boolean LANGUAGE sql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(isfinite(v) AND v>=TIMESTAMPTZ '0001-01-01 00:00:00+00'
 AND v<TIMESTAMPTZ '10000-01-01 00:00:00+00',false) $$;
CREATE FUNCTION public.repricing_job_text(v text,n integer) RETURNS boolean LANGUAGE sql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(char_length(v) BETWEEN 1 AND n AND v !~ '[[:cntrl:]]' AND
 v=btrim(v,U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'),false) $$;
CREATE TABLE public.wb_repricing_jobs (
 job_id uuid PRIMARY KEY CHECK(public.repricing_job_uuid(job_id)),
 organization_id integer NOT NULL CHECK(organization_id>0), marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 provider text COLLATE "C" NOT NULL CHECK(provider='wb'),
 approval_row_id uuid NOT NULL CHECK(public.repricing_job_uuid(approval_row_id)),
 action_key text COLLATE "C" NOT NULL CHECK(action_key ~ '^[0-9a-f]{64}$'),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 operation_kind text COLLATE "C" NOT NULL CHECK(operation_kind='wb.price_apply.v1'),
 initiator_user_id varchar(64) NOT NULL REFERENCES public.lk_users(user_id),
 initiator_session_id varchar(64) NOT NULL REFERENCES public.lk_sessions(session_id),
 initiator_membership_id integer NOT NULL CHECK(initiator_membership_id>0),
 created_at timestamptz NOT NULL CHECK(public.repricing_job_time(created_at)),
 created_audit_id uuid NOT NULL CHECK(public.repricing_job_uuid(created_audit_id)),
 UNIQUE(organization_id,marketplace_account_id,job_id),
 UNIQUE(organization_id,marketplace_account_id,approval_row_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,approval_row_id),
 FOREIGN KEY(organization_id,marketplace_account_id,provider) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id) REFERENCES public.wb_repricer_price_approvals(organization_id,marketplace_account_id,approval_row_id),
 FOREIGN KEY(organization_id,initiator_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
);
CREATE TABLE public.wb_repricing_job_authorities (
 organization_id integer NOT NULL CHECK(organization_id>0), marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 job_id uuid NOT NULL CHECK(public.repricing_job_uuid(job_id)), provider text COLLATE "C" NOT NULL CHECK(provider='wb'),
 expected_external_account_id varchar(128) NOT NULL CHECK(public.repricing_job_text(expected_external_account_id,128)),
 expected_credential_ref varchar(255) CHECK(expected_credential_ref IS NULL OR public.repricing_job_text(expected_credential_ref,255)),
 credential_id uuid NOT NULL REFERENCES public.marketplace_account_credentials(credential_id),
 credential_kind text COLLATE "C" NOT NULL CHECK(credential_kind='wb_api'),
 generation bigint NOT NULL CHECK(generation>0), payload_schema_version smallint NOT NULL CHECK(payload_schema_version=1),
 expires_at timestamptz CHECK(expires_at IS NULL),
 authority_expires_at timestamptz NOT NULL CHECK(public.repricing_job_time(authority_expires_at)),
 policy_reference text COLLATE "C" NOT NULL CHECK(public.repricing_job_text(policy_reference,128)),
 policy_version integer NOT NULL CHECK(policy_version>0),
 PRIMARY KEY(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id) REFERENCES public.wb_repricing_jobs(organization_id,marketplace_account_id,job_id),
 FOREIGN KEY(organization_id,marketplace_account_id,provider) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace)
);
CREATE TABLE public.wb_repricing_job_audit (
 audit_event_id uuid PRIMARY KEY CHECK(public.repricing_job_uuid(audit_event_id)),
 organization_id integer NOT NULL CHECK(organization_id>0), marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 job_id uuid NOT NULL, approval_row_id uuid NOT NULL,
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind='repricer_job.created'),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind='membership'),
 actor_membership_id integer NOT NULL CHECK(actor_membership_id>0),
 occurred_at timestamptz NOT NULL CHECK(public.repricing_job_time(occurred_at)),
 UNIQUE(organization_id,marketplace_account_id,job_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,audit_event_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,approval_row_id) REFERENCES public.wb_repricing_jobs(organization_id,marketplace_account_id,job_id,approval_row_id),
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
);
ALTER TABLE public.wb_repricing_jobs ADD CONSTRAINT repricing_job_creation_witness
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,created_audit_id)
 REFERENCES public.wb_repricing_job_audit(organization_id,marketplace_account_id,job_id,audit_event_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE public.wb_repricing_jobs ADD CONSTRAINT repricing_job_authority_witness
 FOREIGN KEY(organization_id,marketplace_account_id,job_id)
 REFERENCES public.wb_repricing_job_authorities(organization_id,marketplace_account_id,job_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE public.wb_repricing_upload_receipts (
 receipt_id uuid PRIMARY KEY CHECK(public.repricing_job_uuid(receipt_id)),
 organization_id integer NOT NULL CHECK(organization_id>0), marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 provider text COLLATE "C" NOT NULL CHECK(provider='wb'), job_id uuid NOT NULL,
 approval_row_id uuid NOT NULL, attempt_id uuid NOT NULL CHECK(public.repricing_job_uuid(attempt_id)),
 action_key text COLLATE "C" NOT NULL CHECK(action_key ~ '^[0-9a-f]{64}$'),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 dispatch_key text COLLATE "C" NOT NULL CHECK(dispatch_key ~ '^[0-9a-f]{64}$'),
 wb_upload_id text COLLATE "C" NOT NULL CHECK(wb_upload_id ~ '^[1-9][0-9]*$'),
 observed_at timestamptz NOT NULL CHECK(public.repricing_job_time(observed_at)),
 recorded_at timestamptz NOT NULL CHECK(public.repricing_job_time(recorded_at)),
 created_audit_id uuid NOT NULL CHECK(public.repricing_job_uuid(created_audit_id)),
 UNIQUE(organization_id,marketplace_account_id,attempt_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id),
 FOREIGN KEY(organization_id,marketplace_account_id,provider) REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,approval_row_id) REFERENCES public.wb_repricing_jobs(organization_id,marketplace_account_id,job_id,approval_row_id),
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id,attempt_id) REFERENCES public.wb_repricer_price_apply_attempts(organization_id,marketplace_account_id,approval_row_id,attempt_id)
);
CREATE TABLE public.wb_repricing_upload_receipt_audit (
 audit_event_id uuid PRIMARY KEY CHECK(public.repricing_job_uuid(audit_event_id)),
 organization_id integer NOT NULL CHECK(organization_id>0), marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 job_id uuid NOT NULL, approval_row_id uuid NOT NULL, attempt_id uuid NOT NULL, receipt_id uuid NOT NULL,
 event_kind text COLLATE "C" NOT NULL CHECK(event_kind='provider_upload.receipt_observed'),
 actor_kind text COLLATE "C" NOT NULL CHECK(actor_kind='repricer_worker'), actor_membership_id integer CHECK(actor_membership_id IS NULL),
 occurred_at timestamptz NOT NULL CHECK(public.repricing_job_time(occurred_at)),
 UNIQUE(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id),
 UNIQUE(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id,audit_event_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id)
 REFERENCES public.wb_repricing_upload_receipts(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id),
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,approval_row_id) REFERENCES public.wb_repricing_jobs(organization_id,marketplace_account_id,job_id,approval_row_id),
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id,attempt_id) REFERENCES public.wb_repricer_price_apply_attempts(organization_id,marketplace_account_id,approval_row_id,attempt_id)
);
ALTER TABLE public.wb_repricing_upload_receipts ADD CONSTRAINT repricing_receipt_creation_witness
 FOREIGN KEY(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id,created_audit_id)
 REFERENCES public.wb_repricing_upload_receipt_audit(organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id,audit_event_id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX repricing_jobs_user ON public.wb_repricing_jobs(initiator_user_id);
CREATE INDEX repricing_jobs_session ON public.wb_repricing_jobs(initiator_session_id);
CREATE INDEX repricing_jobs_member ON public.wb_repricing_jobs(organization_id,initiator_membership_id);
CREATE INDEX repricing_authority_credential ON public.wb_repricing_job_authorities(credential_id);
CREATE INDEX repricing_job_audit_member ON public.wb_repricing_job_audit(organization_id,actor_membership_id);
CREATE INDEX repricing_receipt_approval ON public.wb_repricing_upload_receipts(organization_id,marketplace_account_id,approval_row_id,attempt_id);
CREATE INDEX repricing_receipt_audit_attempt ON public.wb_repricing_upload_receipt_audit(organization_id,marketplace_account_id,approval_row_id,attempt_id);
CREATE FUNCTION public.repricing_job_guard() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.wb_repricing_jobs; a public.wb_repricer_price_approvals;
 login public.lk_sessions; c public.marketplace_account_credentials; account public.marketplace_accounts;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'repricing_history_immutable'; END IF;
 IF TG_TABLE_NAME='wb_repricing_jobs' THEN
  PERFORM 1 FROM public.lk_users WHERE user_id=NEW.initiator_user_id AND organization_id=NEW.organization_id AND is_active FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricing_origin_invalid'; END IF;
  PERFORM 1 FROM public.iam_memberships WHERE membership_id=NEW.initiator_membership_id AND user_id=NEW.initiator_user_id AND organization_id=NEW.organization_id AND is_active FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricing_origin_invalid'; END IF;
  SELECT session_id,user_id,expires_at,revoked_at INTO login.session_id,login.user_id,login.expires_at,login.revoked_at
  FROM public.lk_sessions WHERE session_id=NEW.initiator_session_id FOR SHARE;
  IF NOT FOUND OR login.user_id<>NEW.initiator_user_id OR login.revoked_at IS NOT NULL OR login.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'repricing_origin_invalid'; END IF;
  PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND marketplace='wb' AND status='connected' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricing_origin_invalid'; END IF;
  -- Preserve metadata-before-domain order even for direct creation INSERTs.
  -- The authority row subsequently captures and checks this exact active kind.
  PERFORM credential_id FROM public.marketplace_account_credentials WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND provider='wb' AND credential_kind='wb_api'
   AND revoked_at IS NULL AND payload_schema_version=1 AND expires_at IS NULL FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricing_authority_invalid'; END IF;
  SELECT * INTO a FROM public.wb_repricer_price_approvals WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND approval_row_id=NEW.approval_row_id FOR UPDATE;
  IF NOT FOUND OR a.request_format<>'wb-price-apply/v1' OR a.status<>'pending'
    OR (a.action_key,a.request_checksum) IS DISTINCT FROM (NEW.action_key,NEW.request_checksum) THEN RAISE EXCEPTION 'repricing_job_invalid'; END IF;
  NEW.created_at:=clock_timestamp();
 ELSIF TG_TABLE_NAME='wb_repricing_job_authorities' THEN
  SELECT * INTO j FROM public.wb_repricing_jobs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricing_job_invalid'; END IF;
  SELECT expires_at,revoked_at,user_id INTO login.expires_at,login.revoked_at,login.user_id FROM public.lk_sessions WHERE session_id=j.initiator_session_id FOR SHARE;
  IF NOT FOUND OR login.user_id<>j.initiator_user_id OR login.revoked_at IS NOT NULL OR NEW.authority_expires_at>login.expires_at OR NEW.authority_expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'repricing_authority_invalid'; END IF;
  SELECT external_account_id,credential_ref,status INTO account.external_account_id,account.credential_ref,account.status FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND marketplace='wb' FOR UPDATE;
  IF NOT FOUND OR (account.external_account_id,account.credential_ref,account.status) IS DISTINCT FROM (NEW.expected_external_account_id,NEW.expected_credential_ref,'connected') THEN RAISE EXCEPTION 'repricing_authority_invalid'; END IF;
  SELECT organization_id,marketplace_account_id,provider,credential_kind,generation,payload_schema_version,expires_at,revoked_at
  INTO c.organization_id,c.marketplace_account_id,c.provider,c.credential_kind,c.generation,c.payload_schema_version,c.expires_at,c.revoked_at
  FROM public.marketplace_account_credentials WHERE credential_id=NEW.credential_id FOR SHARE;
  IF NOT FOUND OR (c.organization_id,c.marketplace_account_id,c.provider,c.credential_kind,c.generation,c.payload_schema_version,c.expires_at,c.revoked_at)
  IS DISTINCT FROM (NEW.organization_id,NEW.marketplace_account_id,'wb','wb_api',NEW.generation,1,NULL::timestamptz,NULL::timestamptz) THEN RAISE EXCEPTION 'repricing_authority_invalid'; END IF;
 ELSIF TG_TABLE_NAME='wb_repricing_upload_receipts' THEN
  NEW.recorded_at:=clock_timestamp();
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.repricing_job_witness() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.wb_repricing_jobs; authority public.wb_repricing_job_authorities;
 e public.wb_repricing_job_audit; r public.wb_repricing_upload_receipts;
 re public.wb_repricing_upload_receipt_audit; a public.wb_repricer_price_approvals;
 t public.wb_repricer_price_apply_attempts;
BEGIN
 SELECT * INTO j FROM public.wb_repricing_jobs WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND job_id=NEW.job_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 SELECT * INTO authority FROM public.wb_repricing_job_authorities WHERE organization_id=j.organization_id AND marketplace_account_id=j.marketplace_account_id AND job_id=j.job_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 SELECT * INTO e FROM public.wb_repricing_job_audit WHERE organization_id=j.organization_id AND marketplace_account_id=j.marketplace_account_id AND job_id=j.job_id;
 IF NOT FOUND OR (e.audit_event_id,e.approval_row_id,e.actor_membership_id,e.occurred_at) IS DISTINCT FROM (j.created_audit_id,j.approval_row_id,j.initiator_membership_id,j.created_at) THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 SELECT * INTO a FROM public.wb_repricer_price_approvals WHERE organization_id=j.organization_id AND marketplace_account_id=j.marketplace_account_id AND approval_row_id=j.approval_row_id;
 IF NOT FOUND OR (a.action_key,a.request_checksum) IS DISTINCT FROM (j.action_key,j.request_checksum) THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 IF TG_TABLE_NAME IN ('wb_repricing_jobs','wb_repricing_job_authorities','wb_repricing_job_audit') THEN
  IF authority.authority_expires_at<=clock_timestamp() OR authority.authority_expires_at<=j.created_at THEN RAISE EXCEPTION 'repricing_authority_invalid'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.iam_memberships WHERE organization_id=j.organization_id AND membership_id=j.initiator_membership_id AND user_id=j.initiator_user_id AND is_active)
   OR NOT EXISTS(SELECT 1 FROM public.lk_users WHERE user_id=j.initiator_user_id AND organization_id=j.organization_id AND is_active)
   OR NOT EXISTS(SELECT 1 FROM public.lk_sessions WHERE session_id=j.initiator_session_id AND user_id=j.initiator_user_id
     AND revoked_at IS NULL AND expires_at>=authority.authority_expires_at)
   THEN RAISE EXCEPTION 'repricing_origin_invalid'; END IF;
  RETURN NULL;
 END IF;
 SELECT * INTO r FROM public.wb_repricing_upload_receipts WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND receipt_id=NEW.receipt_id;
 IF NOT FOUND OR (r.job_id,r.approval_row_id,r.action_key,r.request_checksum) IS DISTINCT FROM (j.job_id,j.approval_row_id,j.action_key,j.request_checksum) THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 SELECT * INTO t FROM public.wb_repricer_price_apply_attempts WHERE organization_id=r.organization_id AND marketplace_account_id=r.marketplace_account_id AND approval_row_id=r.approval_row_id AND attempt_id=r.attempt_id;
 IF NOT FOUND OR t.dispatch_at IS NULL OR t.dispatched_audit_id IS NULL OR t.version<1
 OR (t.action_key,t.request_checksum,t.dispatch_key,t.claimed_by_membership_id,a.claimed_by_membership_id)
 IS DISTINCT FROM (r.action_key,r.request_checksum,r.dispatch_key,j.initiator_membership_id,j.initiator_membership_id) THEN RAISE EXCEPTION 'repricing_receipt_binding_invalid'; END IF;
 SELECT * INTO re FROM public.wb_repricing_upload_receipt_audit WHERE organization_id=r.organization_id AND marketplace_account_id=r.marketplace_account_id AND receipt_id=r.receipt_id;
 IF NOT FOUND OR (re.audit_event_id,re.job_id,re.approval_row_id,re.attempt_id,re.occurred_at)
 IS DISTINCT FROM (r.created_audit_id,r.job_id,r.approval_row_id,r.attempt_id,r.recorded_at) THEN RAISE EXCEPTION 'repricing_graph_invalid'; END IF;
 RETURN NULL;
END $$;
'''


def upgrade():
    # DDL keeps the POSIX [:cntrl:] class literal instead of parsing a bind.
    # Percent escaping preserves SQL bytes through DDL's context interpolation.
    op.execute(sa.DDL(SQL.replace("%", "%%")))
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY repricing_scope ON public.{table} USING
            (organization_id=public.repricer_context_id('app.organization_id') AND
             marketplace_account_id=public.repricer_context_id('app.marketplace_account_id'))
            WITH CHECK (organization_id=public.repricer_context_id('app.organization_id') AND
             marketplace_account_id=public.repricer_context_id('app.marketplace_account_id'))""")
        op.execute(f"CREATE TRIGGER repricing_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.repricing_job_guard()")
        op.execute(f"CREATE TRIGGER repricing_no_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.repricing_job_guard()")
        op.execute(f"CREATE CONSTRAINT TRIGGER repricing_witness AFTER INSERT ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.repricing_job_witness()")
        # Strip every inherited/default grant on these new objects only. Explicit
        # API/executor role scripts establish privileges after owner approval.
        op.execute(sa.text(f"""DO $$ DECLARE x record; BEGIN
          FOR x IN SELECT DISTINCT a.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
          WHERE c.oid='public.{table}'::regclass AND a.grantee NOT IN (0,c.relowner)
          LOOP EXECUTE format('REVOKE ALL ON TABLE public.{table} FROM %I',pg_get_userbyid(x.grantee)); END LOOP;
          END $$"""))
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
    for signature in PURE + TRIGGERS:
        op.execute(sa.text(f"""DO $$ DECLARE x record; BEGIN
          FOR x IN SELECT DISTINCT a.grantee FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
          WHERE p.oid='public.{signature}'::regprocedure AND a.grantee NOT IN (0,p.proowner)
          LOOP EXECUTE format('REVOKE ALL ON FUNCTION public.{signature} FROM %I',pg_get_userbyid(x.grantee)); END LOOP;
          END $$"""))
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")


def downgrade():
    op.execute("LOCK TABLE " + ",".join("public." + t for t in TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL row_security=off")
    for table in TABLES:
        op.execute(sa.text(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION 'repricing_history_present'; END IF; END $$"))
    for table in TABLES:
        for trigger in ("repricing_immutable", "repricing_no_truncate", "repricing_witness"):
            op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
    for signature in TRIGGERS:
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("ALTER TABLE public.wb_repricing_upload_receipts DROP CONSTRAINT repricing_receipt_creation_witness")
    op.execute("ALTER TABLE public.wb_repricing_jobs DROP CONSTRAINT repricing_job_creation_witness")
    op.execute("ALTER TABLE public.wb_repricing_jobs DROP CONSTRAINT repricing_job_authority_witness")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE public.{table}")
    for signature in reversed(PURE):
        op.execute(f"DROP FUNCTION public.{signature}")
