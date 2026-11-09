"""Account-owned approvals with exact request bytes and durable audit witnesses.

Requires exclusive migration/ACL administration, as the preceding migrations do.
No service authorization or provider execution is introduced by these relations.
"""

from alembic import op

revision = "20260909_0066"
down_revision = "20260909_0065"
branch_labels = None
depends_on = None

TABLES = (
    "wb_repricer_price_approvals",
    "wb_repricer_price_apply_attempts",
    "wb_repricer_price_approval_audit",
)
PURE = (
    "repricer_exact_text",
    "repricer_integral_finite",
    "repricer_safe_code",
    "repricer_ascii_json_string",
    "repricer_integer_decimal",
    "repricer_request_bytes",
    "repricer_action_key",
    "repricer_dispatch_key",
    "repricer_context_id",
)
TRIGGERS = ("repricer_account_lock", "repricer_row_guard", "repricer_validate")

HELPERS = r"""
CREATE FUNCTION public.repricer_exact_text(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
SELECT v IS NOT NULL AND v<>'' AND NOT (
 ascii(left(v,1)) IN (9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,
 8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)
 OR ascii(right(v,1)) IN (9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,
 8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)) $$;
CREATE FUNCTION public.repricer_integral_finite(v numeric) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND v NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
 AND v=trunc(v) $$;
CREATE FUNCTION public.repricer_safe_code(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT v IS NOT NULL AND v COLLATE "C" ~ '^[A-Za-z][A-Za-z0-9_]{0,127}$' $$;
CREATE FUNCTION public.repricer_integer_decimal(v numeric) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$ BEGIN
 IF v IS NULL THEN RETURN 'null'; END IF;
 IF NOT public.repricer_integral_finite(v) THEN RAISE EXCEPTION 'repricer_numeric_invalid'; END IF;
 RETURN trunc(v)::text;
END $$;
CREATE FUNCTION public.repricer_ascii_json_string(v text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE answer text := '"'; ch text; n integer; i integer;
BEGIN
 FOR i IN 1..char_length(v) LOOP
  ch:=substr(v,i,1); n:=ascii(ch);
  answer:=answer || CASE n
   WHEN 34 THEN E'\\"' WHEN 92 THEN E'\\\\' WHEN 8 THEN E'\\b'
   WHEN 12 THEN E'\\f' WHEN 10 THEN E'\\n' WHEN 13 THEN E'\\r' WHEN 9 THEN E'\\t'
   ELSE CASE WHEN n BETWEEN 32 AND 126 THEN ch
    WHEN n<=65535 THEN E'\\u'||lpad(to_hex(n),4,'0')
    ELSE E'\\u'||to_hex(55296+(n-65536)/1024)||E'\\u'||to_hex(56320+(n-65536)%1024)
   END END;
 END LOOP;
 RETURN answer||'"';
END $$;
CREATE FUNCTION public.repricer_request_bytes(
 org integer, account integer, approval text, catalog integer, nm numeric,
 article text, price numeric, discount smallint, size numeric, minimum numeric)
RETURNS bytea LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT convert_to('{"accountId":'||account::text||',"approvalId":'||public.repricer_ascii_json_string(approval)
 ||',"articleId":'||public.repricer_ascii_json_string(article)
 ||',"catalogSkuId":'||coalesce(catalog::text,'null')||',"discountPct":'||discount::text
 ||',"minPriceKopecks":'||public.repricer_integer_decimal(minimum)
 ||',"nmId":'||public.repricer_integer_decimal(nm)||',"organizationId":'||org::text
 ||',"priceKopecks":'||public.repricer_integer_decimal(price)
 ||',"schema":"wb-price-apply/v1","sizeId":'||public.repricer_integer_decimal(size)||'}','UTF8') $$;
CREATE FUNCTION public.repricer_action_key(org integer,account integer,approval text,checksum text)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT encode(sha256(convert_to('['||org::text||','||public.repricer_ascii_json_string(account::text)
 ||','||public.repricer_ascii_json_string(approval)||','||public.repricer_ascii_json_string(checksum)||']','UTF8')),'hex') $$;
CREATE FUNCTION public.repricer_dispatch_key(org integer,account integer,approval text,key text,attempt uuid)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT encode(sha256(convert_to('["wb-price-dispatch/v1",'||org::text||','||account::text||','
 ||public.repricer_ascii_json_string(approval)||','||public.repricer_ascii_json_string(key)||','
 ||public.repricer_ascii_json_string(attempt::text)||']','UTF8')),'hex') $$;
CREATE FUNCTION public.repricer_context_id(setting text) RETURNS integer
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$ DECLARE v text; BEGIN
 v:=current_setting(setting,true);
 IF v IS NULL OR v COLLATE "C" !~ '^[1-9][0-9]{0,9}$' THEN RETURN NULL; END IF;
 IF v::numeric>2147483647 THEN RETURN NULL; END IF;
 RETURN v::integer;
END $$;
"""

RELATIONS = r"""
CREATE TABLE public.wb_repricer_price_approvals (
 approval_row_id uuid PRIMARY KEY,
 organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0),
 marketplace text COLLATE "C" NOT NULL CHECK(marketplace='wb'),
 approval_id text COLLATE "C" NOT NULL CHECK(public.repricer_exact_text(approval_id)),
 catalog_sku_id integer CHECK(catalog_sku_id>0),
 nm_id numeric NOT NULL CHECK(public.repricer_integral_finite(nm_id) AND nm_id>0),
 article_id text COLLATE "C" NOT NULL CHECK(public.repricer_exact_text(article_id)),
 recommended_price_kopecks numeric NOT NULL CHECK(public.repricer_integral_finite(recommended_price_kopecks) AND recommended_price_kopecks>0),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 action_key text COLLATE "C" NOT NULL CHECK(action_key ~ '^[0-9a-f]{64}$'),
 request_format text COLLATE "C" NOT NULL,
 canonical_request_bytes bytea,
 discount_pct smallint CHECK(discount_pct BETWEEN 0 AND 99),
 size_id numeric CHECK(size_id IS NULL OR (public.repricer_integral_finite(size_id) AND size_id>0)),
 min_price_kopecks numeric CHECK(min_price_kopecks IS NULL OR (public.repricer_integral_finite(min_price_kopecks) AND min_price_kopecks>=50)),
 status text COLLATE "C" NOT NULL,
 version numeric NOT NULL CHECK(public.repricer_integral_finite(version) AND version>=0),
 created_at timestamptz NOT NULL CHECK(isfinite(created_at)),
 updated_at timestamptz NOT NULL CHECK(isfinite(updated_at) AND updated_at>=created_at),
 claimed_by_membership_id integer CHECK(claimed_by_membership_id>0),
 decided_by_membership_id integer CHECK(decided_by_membership_id>0),
 reason_code text COLLATE "C" CHECK(reason_code IS NULL OR public.repricer_safe_code(reason_code)),
 safe_error_code text COLLATE "C" CHECK(safe_error_code IS NULL OR public.repricer_safe_code(safe_error_code)),
 wb_upload_id text COLLATE "C" CHECK(wb_upload_id IS NULL OR public.repricer_exact_text(wb_upload_id)),
 result_code text COLLATE "C" CHECK(result_code IS NULL OR public.repricer_safe_code(result_code)),
 created_audit_id uuid NOT NULL, claimed_audit_id uuid, decision_audit_id uuid, outcome_audit_id uuid,
 UNIQUE(organization_id,marketplace_account_id,approval_row_id),
 UNIQUE(organization_id,marketplace_account_id,action_key),
 FOREIGN KEY(organization_id,marketplace_account_id,marketplace)
 REFERENCES public.marketplace_accounts(organization_id,marketplace_account_id,marketplace),
 FOREIGN KEY(organization_id,catalog_sku_id) REFERENCES public.catalog_skus(organization_id,catalog_sku_id),
 FOREIGN KEY(organization_id,claimed_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 FOREIGN KEY(organization_id,decided_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 CHECK(action_key=public.repricer_action_key(organization_id,marketplace_account_id,approval_id,request_checksum)),
 CONSTRAINT repricer_request_exact CHECK ((
  (request_format='legacy' AND canonical_request_bytes IS NULL AND discount_pct IS NULL AND size_id IS NULL AND min_price_kopecks IS NULL)
  OR (request_format='wb-price-apply/v1' AND discount_pct IS NOT NULL AND canonical_request_bytes IS NOT NULL
   AND octet_length(canonical_request_bytes) BETWEEN 1 AND 4096 AND recommended_price_kopecks>=50
   AND article_id !~ '[\x01-\x1f\x7f]'
   AND canonical_request_bytes=public.repricer_request_bytes(organization_id,marketplace_account_id,approval_id,catalog_sku_id,nm_id,article_id,recommended_price_kopecks,discount_pct,size_id,min_price_kopecks)
   AND request_checksum=encode(sha256(canonical_request_bytes),'hex'))) IS TRUE),
 CONSTRAINT repricer_approval_state CHECK ((
 (status='pending' AND claimed_by_membership_id IS NULL AND decided_by_membership_id IS NULL AND reason_code IS NULL AND safe_error_code IS NULL AND wb_upload_id IS NULL AND result_code IS NULL)
 OR (status='applying' AND claimed_by_membership_id IS NOT NULL AND decided_by_membership_id IS NULL AND reason_code IS NULL AND safe_error_code IS NULL AND wb_upload_id IS NULL AND result_code IS NULL)
 OR (status IN ('rejected','blocked') AND claimed_by_membership_id IS NULL AND decided_by_membership_id IS NOT NULL AND reason_code IS NOT NULL AND safe_error_code IS NULL AND wb_upload_id IS NULL AND result_code IS NULL)
 OR (status='applied' AND claimed_by_membership_id IS NOT NULL AND decided_by_membership_id IS NULL AND reason_code IS NULL AND safe_error_code IS NULL AND wb_upload_id IS NOT NULL)
 OR (status IN ('failed','ambiguous') AND claimed_by_membership_id IS NOT NULL AND decided_by_membership_id IS NULL AND reason_code IS NULL AND safe_error_code IS NOT NULL AND wb_upload_id IS NULL AND result_code IS NULL)) IS TRUE)
);
CREATE TABLE public.wb_repricer_price_apply_attempts (
 attempt_id uuid PRIMARY KEY, organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0), approval_row_id uuid NOT NULL,
 action_key text COLLATE "C" NOT NULL CHECK(action_key ~ '^[0-9a-f]{64}$'),
 request_checksum text COLLATE "C" NOT NULL CHECK(request_checksum ~ '^[0-9a-f]{64}$'),
 dispatch_key text COLLATE "C" NOT NULL CHECK(dispatch_key ~ '^[0-9a-f]{64}$'),
 claim_version numeric NOT NULL CHECK(public.repricer_integral_finite(claim_version) AND claim_version>0),
 claimed_by_membership_id integer NOT NULL CHECK(claimed_by_membership_id>0),
 status text COLLATE "C" NOT NULL, version smallint NOT NULL CHECK(version BETWEEN 0 AND 2),
 reserved_at timestamptz NOT NULL CHECK(isfinite(reserved_at)),
 updated_at timestamptz NOT NULL CHECK(isfinite(updated_at) AND updated_at>=reserved_at),
 dispatch_at timestamptz CHECK(dispatch_at IS NULL OR (isfinite(dispatch_at) AND dispatch_at>=reserved_at)),
 finished_at timestamptz CHECK(finished_at IS NULL OR (isfinite(finished_at) AND finished_at>=coalesce(dispatch_at,reserved_at))),
 safe_error_code text COLLATE "C" CHECK(safe_error_code IS NULL OR safe_error_code IN
 ('INTERNAL_APPLY_ERROR','WB_APPLY_AUTHORIZATION_FAILED','WB_APPLY_RATE_LIMITED','WB_APPLY_REJECTED','WB_APPLY_TIMEOUT','WB_APPLY_TRANSPORT_ERROR','WB_APPLY_VALIDATION_FAILED','WB_RESULT_UNAVAILABLE')),
 wb_upload_id text COLLATE "C" CHECK(wb_upload_id IS NULL OR public.repricer_exact_text(wb_upload_id)),
 result_code text COLLATE "C" CHECK(result_code IS NULL OR public.repricer_safe_code(result_code)),
 reserved_audit_id uuid NOT NULL, dispatched_audit_id uuid, outcome_audit_id uuid,
 UNIQUE(organization_id,marketplace_account_id,approval_row_id),
 UNIQUE(organization_id,marketplace_account_id,attempt_id),
 UNIQUE(organization_id,marketplace_account_id,approval_row_id,attempt_id),
 UNIQUE(organization_id,marketplace_account_id,dispatch_key),
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id) REFERENCES public.wb_repricer_price_approvals(organization_id,marketplace_account_id,approval_row_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(organization_id,claimed_by_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id),
 CONSTRAINT repricer_attempt_state CHECK ((
 (status='reserved' AND version=0 AND dispatch_at IS NULL AND finished_at IS NULL AND updated_at=reserved_at AND safe_error_code IS NULL AND wb_upload_id IS NULL AND result_code IS NULL AND dispatched_audit_id IS NULL AND outcome_audit_id IS NULL)
 OR (status='dispatched' AND version=1 AND dispatch_at IS NOT NULL AND finished_at IS NULL AND updated_at=dispatch_at AND safe_error_code IS NULL AND wb_upload_id IS NULL AND result_code IS NULL AND dispatched_audit_id IS NOT NULL AND outcome_audit_id IS NULL)
 OR (status IN ('applied','failed','ambiguous') AND finished_at IS NOT NULL AND updated_at=finished_at AND outcome_audit_id IS NOT NULL
  AND ((dispatch_at IS NULL AND dispatched_audit_id IS NULL AND version=1 AND status='failed') OR (dispatch_at IS NOT NULL AND dispatched_audit_id IS NOT NULL AND version=2))
  AND ((status='applied' AND wb_upload_id IS NOT NULL AND result_code IS NOT NULL AND safe_error_code IS NULL)
   OR (status IN ('failed','ambiguous') AND safe_error_code IS NOT NULL AND wb_upload_id IS NULL AND result_code IS NULL
    AND (status<>'failed' OR dispatch_at IS NULL OR safe_error_code='WB_APPLY_REJECTED'))))) IS TRUE)
);
CREATE TABLE public.wb_repricer_price_approval_audit (
 audit_event_id uuid PRIMARY KEY, organization_id integer NOT NULL CHECK(organization_id>0),
 marketplace_account_id integer NOT NULL CHECK(marketplace_account_id>0), approval_row_id uuid NOT NULL,
 event_kind text COLLATE "C" NOT NULL, actor_kind text COLLATE "C" NOT NULL,
 actor_membership_id integer CHECK(actor_membership_id>0), before_status text COLLATE "C",
 after_status text COLLATE "C" NOT NULL, before_version numeric,
 after_version numeric NOT NULL CHECK(public.repricer_integral_finite(after_version) AND after_version>=0),
 occurred_at timestamptz NOT NULL CHECK(isfinite(occurred_at)), attempt_id uuid,
 before_attempt_version smallint, after_attempt_version smallint,
 reason_code text COLLATE "C", safe_error_code text COLLATE "C", wb_upload_id text COLLATE "C", result_code text COLLATE "C",
 CHECK(before_version IS NULL OR (public.repricer_integral_finite(before_version) AND before_version>=0)),
 UNIQUE(organization_id,marketplace_account_id,approval_row_id,audit_event_id),
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id) REFERENCES public.wb_repricer_price_approvals(organization_id,marketplace_account_id,approval_row_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id,attempt_id) REFERENCES public.wb_repricer_price_apply_attempts(organization_id,marketplace_account_id,approval_row_id,attempt_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(organization_id,actor_membership_id) REFERENCES public.iam_memberships(organization_id,membership_id)
);
CREATE INDEX repricer_pending ON public.wb_repricer_price_approvals(organization_id,marketplace_account_id,created_at,approval_row_id) WHERE request_format='wb-price-apply/v1' AND status='pending';
CREATE INDEX repricer_recovery ON public.wb_repricer_price_apply_attempts(organization_id,marketplace_account_id,updated_at,attempt_id) WHERE status='dispatched';
CREATE INDEX repricer_audit_lookup ON public.wb_repricer_price_approval_audit(organization_id,marketplace_account_id,approval_row_id,event_kind);
"""

GUARDS = r"""
CREATE FUNCTION public.repricer_account_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog AS $$
DECLARE org integer; account integer;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'repricer_history_immutable'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'repricer_isolation_invalid'; END IF;
 org:=public.repricer_context_id('app.organization_id'); account:=public.repricer_context_id('app.marketplace_account_id');
 IF org IS NULL OR account IS NULL THEN RAISE EXCEPTION 'repricer_context_invalid'; END IF;
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org AND marketplace_account_id=account AND marketplace='wb' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'repricer_context_invalid'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.repricer_row_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog AS $$
DECLARE witness text; owner_id oid; previous_witness uuid; next_witness uuid;
BEGIN
 IF NEW.organization_id IS DISTINCT FROM public.repricer_context_id('app.organization_id') OR NEW.marketplace_account_id IS DISTINCT FROM public.repricer_context_id('app.marketplace_account_id') THEN RAISE EXCEPTION 'repricer_scope_invalid'; END IF;
 IF TG_TABLE_NAME='wb_repricer_price_approval_audit' THEN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'repricer_history_immutable'; END IF;
  IF NEW.event_kind='approval.imported' THEN
   SELECT relowner INTO owner_id FROM pg_class WHERE oid='public.wb_repricer_price_approvals'::regclass;
   IF current_user::regrole::oid<>owner_id THEN RAISE EXCEPTION 'repricer_import_denied'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='wb_repricer_price_approvals' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.claimed_audit_id IS NOT NULL OR NEW.decision_audit_id IS NOT NULL OR NEW.outcome_audit_id IS NOT NULL THEN RAISE EXCEPTION 'repricer_witness_invalid'; END IF;
   IF NEW.request_format='legacy' THEN
    SELECT relowner INTO owner_id FROM pg_class WHERE oid=TG_RELID;
    IF current_user::regrole::oid<>owner_id THEN RAISE EXCEPTION 'repricer_import_denied'; END IF;
   ELSIF NEW.status<>'pending' OR NEW.version<>0 THEN RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
   -- This is a separate VOLATILE SQL command, after the statement lock wait.
   IF EXISTS(SELECT 1 FROM public.wb_repricer_price_approvals a WHERE a.organization_id=NEW.organization_id AND a.marketplace_account_id=NEW.marketplace_account_id AND a.approval_id=NEW.approval_id) THEN RAISE EXCEPTION 'repricer_identity_conflict'; END IF;
   RETURN NEW;
  END IF;
  IF OLD.request_format<>'wb-price-apply/v1' OR OLD.status NOT IN ('pending','applying') OR NEW.version<>OLD.version+1 OR NEW.updated_at<OLD.updated_at THEN RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
  IF OLD.status='pending' AND NEW.status='applying' THEN witness:='claimed_audit_id';
  ELSIF OLD.status='pending' AND NEW.status IN ('rejected','blocked') THEN witness:='decision_audit_id';
  ELSIF OLD.status='applying' AND NEW.status IN ('applied','failed','ambiguous') THEN witness:='outcome_audit_id';
  ELSE RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
  previous_witness:=CASE witness WHEN 'claimed_audit_id' THEN OLD.claimed_audit_id WHEN 'decision_audit_id' THEN OLD.decision_audit_id ELSE OLD.outcome_audit_id END;
  next_witness:=CASE witness WHEN 'claimed_audit_id' THEN NEW.claimed_audit_id WHEN 'decision_audit_id' THEN NEW.decision_audit_id ELSE NEW.outcome_audit_id END;
  IF (OLD.approval_row_id,OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.approval_id,
      OLD.catalog_sku_id,OLD.nm_id,OLD.article_id,OLD.recommended_price_kopecks,OLD.request_checksum,OLD.action_key,
      OLD.request_format,OLD.canonical_request_bytes,OLD.discount_pct,OLD.size_id,OLD.min_price_kopecks,OLD.created_at,OLD.created_audit_id,
      CASE WHEN witness<>'claimed_audit_id' THEN OLD.claimed_audit_id END,
      CASE WHEN witness<>'decision_audit_id' THEN OLD.decision_audit_id END,
      CASE WHEN witness<>'outcome_audit_id' THEN OLD.outcome_audit_id END,
      CASE WHEN OLD.status<>'pending' THEN OLD.claimed_by_membership_id END)
   IS DISTINCT FROM
     (NEW.approval_row_id,NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.approval_id,
      NEW.catalog_sku_id,NEW.nm_id,NEW.article_id,NEW.recommended_price_kopecks,NEW.request_checksum,NEW.action_key,
      NEW.request_format,NEW.canonical_request_bytes,NEW.discount_pct,NEW.size_id,NEW.min_price_kopecks,NEW.created_at,NEW.created_audit_id,
      CASE WHEN witness<>'claimed_audit_id' THEN NEW.claimed_audit_id END,
      CASE WHEN witness<>'decision_audit_id' THEN NEW.decision_audit_id END,
      CASE WHEN witness<>'outcome_audit_id' THEN NEW.outcome_audit_id END,
      CASE WHEN OLD.status<>'pending' THEN NEW.claimed_by_membership_id END)
  THEN RAISE EXCEPTION 'repricer_immutable_invalid'; END IF;
 ELSE
  IF TG_OP='INSERT' THEN
   IF NEW.status<>'reserved' OR NEW.version<>0 THEN RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
   RETURN NEW;
  END IF;
  IF NEW.version<>OLD.version+1 OR NEW.updated_at<OLD.updated_at THEN RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
  IF OLD.status='reserved' AND NEW.status='dispatched' THEN
   witness:='dispatched_audit_id';
  ELSIF (OLD.status='reserved' AND NEW.status='failed') OR (OLD.status='dispatched' AND NEW.status IN ('applied','failed','ambiguous')) THEN
   witness:='outcome_audit_id';
  ELSE RAISE EXCEPTION 'repricer_transition_invalid'; END IF;
  previous_witness:=CASE witness WHEN 'dispatched_audit_id' THEN OLD.dispatched_audit_id ELSE OLD.outcome_audit_id END;
  next_witness:=CASE witness WHEN 'dispatched_audit_id' THEN NEW.dispatched_audit_id ELSE NEW.outcome_audit_id END;
  IF (OLD.attempt_id,OLD.organization_id,OLD.marketplace_account_id,OLD.approval_row_id,OLD.action_key,OLD.request_checksum,
      OLD.dispatch_key,OLD.claim_version,OLD.claimed_by_membership_id,OLD.reserved_at,OLD.reserved_audit_id,
      CASE WHEN witness<>'dispatched_audit_id' THEN OLD.dispatch_at END,
      CASE WHEN witness<>'dispatched_audit_id' THEN OLD.dispatched_audit_id END,
      CASE WHEN witness<>'outcome_audit_id' THEN OLD.outcome_audit_id END)
   IS DISTINCT FROM
     (NEW.attempt_id,NEW.organization_id,NEW.marketplace_account_id,NEW.approval_row_id,NEW.action_key,NEW.request_checksum,
      NEW.dispatch_key,NEW.claim_version,NEW.claimed_by_membership_id,NEW.reserved_at,NEW.reserved_audit_id,
      CASE WHEN witness<>'dispatched_audit_id' THEN NEW.dispatch_at END,
      CASE WHEN witness<>'dispatched_audit_id' THEN NEW.dispatched_audit_id END,
      CASE WHEN witness<>'outcome_audit_id' THEN NEW.outcome_audit_id END)
  THEN RAISE EXCEPTION 'repricer_immutable_invalid'; END IF;
 END IF;
 IF previous_witness IS NOT NULL OR next_witness IS NULL THEN RAISE EXCEPTION 'repricer_immutable_invalid'; END IF;
 RETURN NEW;
END $$;
"""

# One trigger carries both captured OLD/NEW evidence and current-row alignment.
# No SECURITY DEFINER routine, mutable GUC bypass, or fourth relation is needed.
VALIDATE = r"""
CREATE FUNCTION public.repricer_validate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog AS $$
DECLARE a public.wb_repricer_price_approvals%ROWTYPE;
 t public.wb_repricer_price_apply_attempts%ROWTYPE;
 e public.wb_repricer_price_approval_audit%ROWTYPE;
 claim public.wb_repricer_price_approval_audit%ROWTYPE;
 witness uuid; kind text; actor text; member integer;
 prev_status text; prev_version numeric; after_status text; after_version numeric;
 before_attempt smallint; after_attempt smallint; attempt uuid; moment timestamptz;
 reason text; error text; upload text; result text;
BEGIN
 SELECT * INTO a FROM public.wb_repricer_price_approvals WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND approval_row_id=NEW.approval_row_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'repricer_approval_missing'; END IF;
 SELECT * INTO t FROM public.wb_repricer_price_apply_attempts WHERE organization_id=a.organization_id AND marketplace_account_id=a.marketplace_account_id AND approval_row_id=a.approval_row_id;
 IF TG_TABLE_NAME='wb_repricer_price_approvals' THEN
  after_status:=NEW.status; after_version:=NEW.version; moment:=NEW.updated_at;
  IF TG_OP='INSERT' THEN
   witness:=NEW.created_audit_id; moment:=NEW.created_at;
   kind:=CASE WHEN NEW.request_format='legacy' THEN 'approval.imported' ELSE 'approval.created' END;
   actor:=CASE WHEN NEW.request_format='legacy' THEN 'backfill' ELSE 'membership' END;
  ELSE
   prev_status:=OLD.status; prev_version:=OLD.version;
   IF NEW.status='applying' THEN witness:=NEW.claimed_audit_id; kind:='approval.claimed'; actor:='membership'; member:=NEW.claimed_by_membership_id;
   ELSIF NEW.status IN ('rejected','blocked') THEN witness:=NEW.decision_audit_id; kind:='approval.'||NEW.status; actor:='membership'; member:=NEW.decided_by_membership_id; reason:=NEW.reason_code;
   ELSE
    witness:=NEW.outcome_audit_id; kind:=CASE WHEN NEW.status='applied' THEN 'apply.succeeded' ELSE 'apply.'||NEW.status END;
    actor:='repricer_worker'; attempt:=t.attempt_id; after_attempt:=t.version; before_attempt:=t.version-1;
    error:=NEW.safe_error_code; upload:=NEW.wb_upload_id; result:=NEW.result_code;
   END IF;
  END IF;
 ELSIF TG_TABLE_NAME='wb_repricer_price_apply_attempts' THEN
  prev_status:='applying'; after_status:='applying'; prev_version:=NEW.claim_version; after_version:=NEW.claim_version;
  actor:='membership'; member:=NEW.claimed_by_membership_id; attempt:=NEW.attempt_id; after_attempt:=NEW.version; moment:=NEW.updated_at;
  IF TG_OP='INSERT' THEN witness:=NEW.reserved_audit_id; kind:='attempt.reserved';
  ELSE
   before_attempt:=OLD.version;
   IF NEW.status='dispatched' THEN witness:=NEW.dispatched_audit_id; kind:='attempt.dispatched';
   ELSE
    witness:=NEW.outcome_audit_id; kind:=CASE WHEN NEW.status='applied' THEN 'apply.succeeded' ELSE 'apply.'||NEW.status END;
    actor:='repricer_worker'; member:=NULL; after_status:=NEW.status; after_version:=NEW.claim_version+1;
    error:=NEW.safe_error_code; upload:=NEW.wb_upload_id; result:=NEW.result_code;
   END IF;
  END IF;
 ELSE
  -- Inverse ownership rejects extra historical events, including correct-looking ghosts.
  e:=NEW;
  witness:=CASE e.event_kind WHEN 'approval.created' THEN a.created_audit_id WHEN 'approval.imported' THEN a.created_audit_id
   WHEN 'approval.claimed' THEN a.claimed_audit_id WHEN 'approval.rejected' THEN a.decision_audit_id WHEN 'approval.blocked' THEN a.decision_audit_id
   WHEN 'attempt.reserved' THEN t.reserved_audit_id WHEN 'attempt.dispatched' THEN t.dispatched_audit_id
   WHEN 'apply.succeeded' THEN a.outcome_audit_id WHEN 'apply.failed' THEN a.outcome_audit_id WHEN 'apply.ambiguous' THEN a.outcome_audit_id END;
  IF witness IS DISTINCT FROM e.audit_event_id THEN RAISE EXCEPTION 'repricer_audit_unowned'; END IF;
  IF EXISTS(SELECT 1 FROM public.wb_repricer_price_approval_audit x WHERE x.organization_id=e.organization_id AND x.marketplace_account_id=e.marketplace_account_id AND x.approval_row_id=e.approval_row_id AND x.audit_event_id<>e.audit_event_id AND x.event_kind=e.event_kind AND x.after_version=e.after_version AND x.attempt_id IS NOT DISTINCT FROM e.attempt_id AND x.after_attempt_version IS NOT DISTINCT FROM e.after_attempt_version) THEN RAISE EXCEPTION 'repricer_audit_duplicate'; END IF;
 END IF;
 IF TG_TABLE_NAME<>'wb_repricer_price_approval_audit' THEN
  SELECT * INTO e FROM public.wb_repricer_price_approval_audit WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id AND approval_row_id=NEW.approval_row_id AND audit_event_id=witness;
  IF NOT FOUND THEN RAISE EXCEPTION 'repricer_audit_missing'; END IF;
  IF kind='approval.created' THEN member:=e.actor_membership_id; IF member IS NULL THEN RAISE EXCEPTION 'repricer_audit_invalid'; END IF; END IF;
  IF (e.event_kind,e.actor_kind,e.actor_membership_id,e.before_status,e.after_status,e.before_version,e.after_version,
      e.occurred_at,e.attempt_id,e.before_attempt_version,e.after_attempt_version,e.reason_code,e.safe_error_code,e.wb_upload_id,e.result_code)
   IS DISTINCT FROM (kind,actor,member,prev_status,after_status,prev_version,after_version,moment,attempt,before_attempt,after_attempt,reason,error,upload,result)
  THEN RAISE EXCEPTION 'repricer_audit_invalid'; END IF;
 END IF;
 -- Final-state invariants are checked for roots, attempts AND inverse audit inserts.
 IF EXISTS(SELECT 1 FROM public.wb_repricer_price_approvals x WHERE x.organization_id=a.organization_id AND x.marketplace_account_id=a.marketplace_account_id AND x.approval_id=a.approval_id AND x.approval_row_id<>a.approval_row_id) THEN RAISE EXCEPTION 'repricer_identity_conflict'; END IF;
 IF a.request_format='legacy' THEN
  IF t.attempt_id IS NOT NULL THEN RAISE EXCEPTION 'repricer_legacy_attempt'; END IF;
  RETURN NULL;
 END IF;
 IF a.status IN ('pending','rejected','blocked') THEN
  IF t.attempt_id IS NOT NULL THEN RAISE EXCEPTION 'repricer_attempt_alignment'; END IF;
 ELSE
  SELECT * INTO claim FROM public.wb_repricer_price_approval_audit WHERE organization_id=a.organization_id AND marketplace_account_id=a.marketplace_account_id AND approval_row_id=a.approval_row_id AND audit_event_id=a.claimed_audit_id;
  IF claim.event_kind IS DISTINCT FROM 'approval.claimed' OR claim.actor_membership_id IS DISTINCT FROM a.claimed_by_membership_id OR a.version IS DISTINCT FROM (claim.after_version+CASE WHEN a.status='applying' THEN 0 ELSE 1 END) THEN RAISE EXCEPTION 'repricer_claim_alignment'; END IF;
  IF t.attempt_id IS NOT NULL THEN
   IF t.action_key<>a.action_key OR t.request_checksum<>a.request_checksum OR t.claim_version<>claim.after_version OR t.claimed_by_membership_id<>a.claimed_by_membership_id OR t.reserved_at<claim.occurred_at OR t.dispatch_key<>public.repricer_dispatch_key(a.organization_id,a.marketplace_account_id,a.approval_id,a.action_key,t.attempt_id) THEN RAISE EXCEPTION 'repricer_attempt_alignment'; END IF;
  END IF;
  IF a.status='applying' THEN
   IF t.attempt_id IS NOT NULL AND t.status NOT IN ('reserved','dispatched') THEN RAISE EXCEPTION 'repricer_attempt_alignment'; END IF;
  ELSIF t.attempt_id IS NULL OR (a.status,a.safe_error_code,a.wb_upload_id,a.result_code,a.updated_at,a.outcome_audit_id) IS DISTINCT FROM (t.status,t.safe_error_code,t.wb_upload_id,t.result_code,t.finished_at,t.outcome_audit_id) THEN RAISE EXCEPTION 'repricer_outcome_alignment'; END IF;
 END IF;
 RETURN NULL;
END $$;
"""


def upgrade():
    op.execute(HELPERS)
    op.execute(RELATIONS)
    for table in TABLES:
        columns = (
            (
                "approval_row_id",
                "created_audit_id",
                "claimed_audit_id",
                "decision_audit_id",
                "outcome_audit_id",
            )
            if table == TABLES[0]
            else (
                "attempt_id",
                "approval_row_id",
                "reserved_audit_id",
                "dispatched_audit_id",
                "outcome_audit_id",
            )
            if table == TABLES[1]
            else ("audit_event_id", "approval_row_id", "attempt_id")
        )
        for column in columns:
            op.execute(
                f"ALTER TABLE public.{table} ADD CHECK ({column} IS NULL OR {column}::text ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$')"
            )
        for column in (c for c in columns if c.endswith("audit_id")):
            op.execute(
                f"ALTER TABLE public.{table} ADD CONSTRAINT {table.removeprefix('wb_repricer_price_')}_{column}_fk FOREIGN KEY(organization_id,marketplace_account_id,approval_row_id,{column}) REFERENCES public.{TABLES[2]}(organization_id,marketplace_account_id,approval_row_id,audit_event_id) DEFERRABLE INITIALLY DEFERRED"
            )
        op.execute(
            f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY"
        )
        predicate = "organization_id=public.repricer_context_id('app.organization_id') AND marketplace_account_id=public.repricer_context_id('app.marketplace_account_id')"
        op.execute(
            f"CREATE POLICY repricer_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute(GUARDS)
    op.execute(VALIDATE)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER repricer_account_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.repricer_account_lock()"
        )
        op.execute(
            f"CREATE TRIGGER repricer_guard BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.repricer_row_guard()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER repricer_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.repricer_validate()"
        )
    op.execute(ACL)


ACL = r"""
DO $$ DECLARE r record; p record; col record; grantee text; allowed text[]; privileges text;
BEGIN
 FOR r IN SELECT oid,relname,relowner FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ('wb_repricer_price_approvals','wb_repricer_price_apply_attempts','wb_repricer_price_approval_audit') LOOP
  allowed:=CASE WHEN r.relname='wb_repricer_price_approval_audit' THEN ARRAY['SELECT','INSERT'] ELSE ARRAY['SELECT','INSERT','UPDATE'] END;
  FOR p IN SELECT x.grantee,array_agg(DISTINCT x.privilege_type) AS rights FROM aclexplode((SELECT relacl FROM pg_class WHERE oid=r.oid)) x WHERE x.grantee<>r.relowner GROUP BY x.grantee LOOP
   grantee:=CASE WHEN p.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(p.grantee)) END;
   EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s',r.relname,grantee);
   SELECT string_agg(v,',') INTO privileges FROM unnest(p.rights) v WHERE v=ANY(allowed);
   IF p.grantee<>0 AND privileges IS NOT NULL THEN
    EXECUTE format('GRANT %s ON TABLE public.%I TO %s',privileges,r.relname,grantee);
   END IF;
  END LOOP;
  FOR col IN SELECT a.attname,x.grantee,array_agg(DISTINCT x.privilege_type) rights FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid=r.oid AND x.grantee<>r.relowner GROUP BY a.attname,x.grantee LOOP
   grantee:=CASE WHEN col.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(col.grantee)) END;
   EXECUTE format('REVOKE ALL (%I) ON TABLE public.%I FROM %s',col.attname,r.relname,grantee);
   IF col.grantee<>0 THEN
    FOREACH privileges IN ARRAY col.rights LOOP
     IF privileges=ANY(allowed) THEN EXECUTE format('GRANT %s (%I) ON TABLE public.%I TO %s',privileges,col.attname,r.relname,grantee); END IF;
    END LOOP;
   END IF;
  END LOOP;
  EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',r.relname);
 END LOOP;
 -- Remove default function grants, including grant options, before restoring only
 -- required pure executions for already admitted new-table grantees.
 FOR r IN SELECT oid,proowner,oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN ('repricer_exact_text','repricer_integral_finite','repricer_safe_code','repricer_ascii_json_string','repricer_integer_decimal','repricer_request_bytes','repricer_action_key','repricer_dispatch_key','repricer_context_id','repricer_account_lock','repricer_row_guard','repricer_validate') LOOP
  FOR p IN SELECT DISTINCT x.grantee FROM aclexplode((SELECT proacl FROM pg_proc WHERE oid=r.oid)) x WHERE x.grantee<>r.proowner LOOP
   grantee:=CASE WHEN p.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(p.grantee)) END;
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',r.signature,grantee);
  END LOOP;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',r.signature);
 END LOOP;
 FOR p IN SELECT DISTINCT x.grantee FROM pg_class c
  CROSS JOIN LATERAL (
   SELECT acl.grantee FROM aclexplode(c.relacl) acl
   UNION SELECT x.grantee FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid=c.oid
  ) x WHERE c.relnamespace='public'::regnamespace AND c.relname IN ('wb_repricer_price_approvals','wb_repricer_price_apply_attempts','wb_repricer_price_approval_audit') AND x.grantee NOT IN (0,c.relowner) LOOP
  FOR r IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN ('repricer_exact_text','repricer_integral_finite','repricer_safe_code','repricer_ascii_json_string','repricer_integer_decimal','repricer_request_bytes','repricer_action_key','repricer_dispatch_key','repricer_context_id') LOOP
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',r.signature,pg_get_userbyid(p.grantee));
  END LOOP;
 END LOOP;
END $$;
"""


def downgrade():
    op.execute(
        "LOCK TABLE public.wb_repricer_price_approvals, public.wb_repricer_price_apply_attempts, public.wb_repricer_price_approval_audit IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security=off")
    op.execute("""DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM public.wb_repricer_price_approvals)
      OR EXISTS(SELECT 1 FROM public.wb_repricer_price_apply_attempts)
      OR EXISTS(SELECT 1 FROM public.wb_repricer_price_approval_audit)
    THEN RAISE EXCEPTION 'repricer_downgrade_nonempty'; END IF;
    END $$""")
    for table in TABLES:
        op.execute(
            f"DROP TRIGGER repricer_witness ON public.{table}; DROP TRIGGER repricer_guard ON public.{table}; DROP TRIGGER repricer_account_first ON public.{table}"
        )
    for name in TRIGGERS:
        op.execute(f"DROP FUNCTION public.{name}()")
    for table, columns in (
        (
            TABLES[0],
            (
                "created_audit_id",
                "claimed_audit_id",
                "decision_audit_id",
                "outcome_audit_id",
            ),
        ),
        (TABLES[1], ("reserved_audit_id", "dispatched_audit_id", "outcome_audit_id")),
    ):
        for column in columns:
            op.execute(
                f"ALTER TABLE public.{table} DROP CONSTRAINT {table.removeprefix('wb_repricer_price_')}_{column}_fk"
            )
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE public.{table}")
    for name, args in (
        ("repricer_dispatch_key", "integer,integer,text,text,uuid"),
        ("repricer_action_key", "integer,integer,text,text"),
        (
            "repricer_request_bytes",
            "integer,integer,text,integer,numeric,text,numeric,smallint,numeric,numeric",
        ),
        ("repricer_integer_decimal", "numeric"),
        ("repricer_ascii_json_string", "text"),
        ("repricer_integral_finite", "numeric"),
        ("repricer_exact_text", "text"),
        ("repricer_safe_code", "text"),
        ("repricer_context_id", "text"),
    ):
        op.execute(f"DROP FUNCTION public.{name}({args})")
