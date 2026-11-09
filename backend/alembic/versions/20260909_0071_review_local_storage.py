"""Account-owned local Review history and atomic completed command receipts.

Only local persistence. Live authentication, prepared-source verification and
service composition remain consumer responsibilities; no provider delivery.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0071"
down_revision = "20260909_0070"
branch_labels = depends_on = None

TABLES = (
    "review_policy_versions", "review_policy_heads", "review_draft_revisions",
    "review_decisions", "review_workflow_heads", "review_local_audit",
    "review_local_command_receipts",
)
HEADS = ("review_policy_heads", "review_workflow_heads")
OWNER = "organization_id,marketplace_account_id,marketplace"
OWNER_COLUMNS = "organization_id integer NOT NULL,marketplace_account_id integer NOT NULL,marketplace text NOT NULL"
ZERO = "00000000-0000-0000-0000-000000000000"
SCALARS = r'''
CREATE FUNCTION public.review_local_integer_text(value numeric,allow_zero boolean) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL OR allow_zero IS NULL OR value::text IN ('NaN','Infinity','-Infinity')
  OR value<>trunc(value) OR value<0 OR (NOT allow_zero AND value=0) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 RETURN trunc(value)::text;
END $$;
CREATE FUNCTION public.review_local_timestamp_text(value timestamptz) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL OR NOT isfinite(value) OR extract(year FROM value AT TIME ZONE 'UTC') NOT BETWEEN 1 AND 9999 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 RETURN to_char(value AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
END $$;
CREATE FUNCTION public.review_local_utf8_json_string(value bytea) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE result bytea:=convert_to('"','UTF8'); start_at integer:=0; i integer; b integer; escaped text;
BEGIN
 IF value IS NULL OR NOT public.review_strict_utf8(value) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 -- Inspect original bytes; copy disjoint spans, never the remaining suffix.
 FOR i IN 0..octet_length(value)-1 LOOP
  b:=get_byte(value,i);
  IF b<32 OR b IN (34,92) THEN
   escaped:=CASE b WHEN 34 THEN E'\\"' WHEN 92 THEN E'\\\\'
     WHEN 8 THEN E'\\b' WHEN 9 THEN E'\\t' WHEN 10 THEN E'\\n'
     WHEN 12 THEN E'\\f' WHEN 13 THEN E'\\r'
     ELSE E'\\u'||lpad(to_hex(b),4,'0') END;
   result:=result||substr(value,start_at+1,i-start_at)||convert_to(escaped,'UTF8');
   start_at:=i+1;
  END IF;
 END LOOP;
 RETURN result||substr(value,start_at+1,octet_length(value)-start_at)||convert_to('"','UTF8');
END $$;
CREATE FUNCTION public.review_local_nonblank_utf8(value bytea) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE i integer:=0; b integer; width integer; code integer;
BEGIN
 IF value IS NULL OR NOT public.review_strict_utf8(value) THEN RETURN false; END IF;
 WHILE i<octet_length(value) LOOP
  b:=get_byte(value,i);
  IF b<128 THEN code:=b; width:=1;
  ELSE
   width:=CASE WHEN b<224 THEN 2 WHEN b<240 THEN 3 ELSE 4 END;
   code:=ascii(convert_from(substr(value,i+1,width),'UTF8'));
  END IF;
  IF NOT (code BETWEEN 9 AND 13 OR code BETWEEN 28 AND 32 OR code IN (133,160,5760,8232,8233,8239,8287,12288)
          OR code BETWEEN 8192 AND 8202) THEN RETURN true; END IF;
  i:=i+width;
 END LOOP;
 RETURN false;
END $$;
CREATE FUNCTION public.review_local_string(value text,nullable boolean DEFAULT false) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL AND nullable THEN RETURN convert_to('null','UTF8'); END IF;
 RETURN public.review_local_utf8_json_string(convert_to(value,'UTF8'));
END $$;
CREATE FUNCTION public.review_local_uuid(value uuid,nullable boolean DEFAULT false) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value='00000000-0000-0000-0000-000000000000'::uuid THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 RETURN public.review_local_string(value::text,nullable);
END $$;
CREATE FUNCTION public.review_local_number(value numeric,allow_zero boolean DEFAULT false,nullable boolean DEFAULT false) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL AND nullable THEN RETURN convert_to('null','UTF8'); END IF;
 RETURN convert_to(public.review_local_integer_text(value,allow_zero),'UTF8');
END $$;
CREATE FUNCTION public.review_local_label(value text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL OR value COLLATE "C" !~ '^[A-Za-z0-9_.:/-]{1,128}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 RETURN public.review_local_string(value);
END $$;
CREATE FUNCTION public.review_local_checksum(value text) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF value IS NULL OR value COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 RETURN public.review_local_string(value);
END $$;
'''

# Stable physical column order also defines legal explicit composite projections.
COLUMNS = {
    TABLES[0]: """policy_id uuid NOT NULL,version numeric NOT NULL,approval_mode text NOT NULL,
      template_version text NOT NULL,model_version text NOT NULL,policy_payload bytea NOT NULL,
      policy_checksum text NOT NULL,actor_membership_id integer NOT NULL,created_at timestamptz NOT NULL,
      audit_event_id uuid NOT NULL""",
    TABLES[1]: """head_id uuid NOT NULL,version numeric NOT NULL,current_policy_id uuid NOT NULL,
      current_policy_version numeric NOT NULL,current_policy_checksum text NOT NULL,updated_at timestamptz NOT NULL""",
    TABLES[2]: """review_id uuid NOT NULL,draft_id uuid NOT NULL,revision numeric NOT NULL,
      source_observation_id uuid NOT NULL,source_checksum text NOT NULL,policy_id uuid NOT NULL,
      policy_version numeric NOT NULL,policy_checksum text NOT NULL,text_utf8 bytea NOT NULL,
      text_checksum text NOT NULL,binding_payload bytea NOT NULL,binding_checksum text NOT NULL,
      generation_id uuid NOT NULL,generation_mode text NOT NULL,template_version text NOT NULL,
      model_version text NOT NULL,generation_started_at timestamptz NOT NULL,generation_completed_at timestamptz NOT NULL,
      previous_draft_id uuid,generation_payload bytea NOT NULL,generation_checksum text NOT NULL,
      policy_head_id uuid NOT NULL,policy_head_version numeric NOT NULL,policy_selection_event_id uuid NOT NULL,
      actor_membership_id integer NOT NULL,created_at timestamptz NOT NULL,audit_event_id uuid NOT NULL""",
    TABLES[3]: """review_id uuid NOT NULL,decision_id uuid NOT NULL,draft_id uuid NOT NULL,
      draft_revision numeric NOT NULL,binding_checksum text NOT NULL,decision_kind text NOT NULL,
      actor_membership_id integer NOT NULL,decided_at timestamptz NOT NULL,audit_event_id uuid NOT NULL""",
    TABLES[4]: """review_id uuid NOT NULL,head_id uuid NOT NULL,version numeric NOT NULL,
      current_draft_id uuid NOT NULL,current_draft_revision numeric NOT NULL,current_decision_id uuid,
      updated_at timestamptz NOT NULL""",
    TABLES[5]: """event_id uuid NOT NULL,aggregate_kind text NOT NULL,aggregate_id uuid NOT NULL,
      aggregate_version numeric NOT NULL,event_kind text NOT NULL,review_id uuid,policy_id uuid,policy_version numeric,
      draft_id uuid,draft_revision numeric,decision_id uuid,actor_membership_id integer NOT NULL,
      before_state text,after_state text,occurred_at timestamptz NOT NULL,local_command_id uuid NOT NULL,
      audit_payload bytea NOT NULL,audit_checksum text NOT NULL""",
    TABLES[6]: """local_command_id uuid NOT NULL,actor_membership_id integer NOT NULL,operation_kind text NOT NULL,
      request_payload bytea NOT NULL,request_checksum text NOT NULL,account_binding_schema_version smallint NOT NULL,
      account_binding_external_account_id text NOT NULL,account_binding_credential_ref text,
      account_binding_payload bytea NOT NULL,account_binding_checksum text NOT NULL,
      result_payload bytea NOT NULL,result_checksum text NOT NULL,completed_at timestamptz NOT NULL,
      audit_event_id uuid NOT NULL,review_id uuid,expected_head_version numeric,expected_draft_revision numeric,
      expected_policy_head_id uuid,expected_policy_head_version numeric,result_policy_id uuid,result_policy_version numeric,
      result_draft_id uuid,result_draft_revision numeric,result_decision_id uuid,result_head_id uuid,result_head_version numeric""",
}


def fields(table):
    return [part.strip().split()[0] for part in (OWNER_COLUMNS + "," + COLUMNS[table]).split(",")]


def projection(table):
    columns = fields(table)
    if table == TABLES[5]:
        columns += ["policy_head_id", "workflow_head_id"]
    return "ROW(" + ",".join(columns) + ")::public." + table


def quoted(value):
    return "convert_to('" + value.replace("'", "''") + "','UTF8')"


def obj(values):
    parts = []
    for index, (key, value) in enumerate(sorted(values.items())):
        parts.extend((quoted(("{" if index == 0 else ",") + '"' + key + '":'), value))
    return "||".join((*parts, quoted("}")))


def array(*values):
    return quoted("[") + "||" + ("||" + quoted(",") + "||").join(values) + "||" + quoted("]")


def string(value, nullable=False):
    return f"public.review_local_string({value},{str(nullable).lower()})"


def uid(value, nullable=False):
    return f"public.review_local_uuid({value},{str(nullable).lower()})"


def number(value, zero=False, nullable=False):
    return f"public.review_local_number({value},{str(zero).lower()},{str(nullable).lower()})"


def checksum(value):
    return f"public.review_local_checksum({value})"


def label(value):
    return f"public.review_local_label({value})"


def timestamp(value):
    return string(f"public.review_local_timestamp_text({value})")


def function(name, args, body):
    return f"CREATE FUNCTION public.{name}({args}) RETURNS bytea LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ BEGIN {body} END $$;"


INVALID = "RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';"


def codecs():
    policy = obj({"schemaVersion": quoted('"review-policy-v1"'), "organizationId": number("p.organization_id"),
        "marketplaceAccountId": number("p.marketplace_account_id"), "marketplace": string("p.marketplace"),
        "policyId": uid("p.policy_id"), "version": number("p.version"), "approvalMode": string("p.approval_mode"),
        "templateVersion": label("p.template_version"), "modelVersion": label("p.model_version")})
    yield function("review_local_policy_bytes", "p public.review_policy_versions",
                   "IF p.marketplace IS NULL OR p.marketplace COLLATE \"C\" NOT IN ('wb','avito') OR p.approval_mode IS DISTINCT FROM 'manual' THEN " + INVALID + " END IF; RETURN " + policy + ";")
    generation = {"schemaVersion": quoted('"review-generation-v1"'), "actorMembershipId": number("d.actor_membership_id"),
        "generationId": uid("d.generation_id"), "sourceObservationId": uid("d.source_observation_id"),
        "sourceChecksum": checksum("d.source_checksum"), "policyId": uid("d.policy_id"),
        "policyVersion": number("d.policy_version"), "policyChecksum": checksum("d.policy_checksum"),
        "mode": string("d.generation_mode"), "templateVersion": label("d.template_version"),
        "modelVersion": label("d.model_version"), "startedAt": timestamp("d.generation_started_at"),
        "completedAt": timestamp("d.generation_completed_at")}
    yield function("review_local_generation_bytes", "d public.review_draft_revisions",
        "IF d.generation_mode IS NULL OR d.generation_mode COLLATE \"C\" NOT IN ('fake','manual_edit') OR "
        "(d.generation_mode='manual_edit') IS DISTINCT FROM (d.previous_draft_id IS NOT NULL) OR "
        "d.generation_completed_at<d.generation_started_at THEN " + INVALID + " END IF; "
        "IF d.generation_mode='manual_edit' THEN RETURN " + obj(dict(generation, previousDraftId=uid("d.previous_draft_id")))
        + "; END IF; RETURN " + obj(generation) + ";")
    yield function("review_local_decision_binding_bytes", "d public.review_draft_revisions,external_id bytea",
        "IF d.marketplace IS NULL OR d.marketplace COLLATE \"C\" NOT IN ('wb','avito') OR external_id IS NULL OR octet_length(external_id)=0 THEN " + INVALID + " END IF; RETURN " + obj({
        "contract": quoted('"review-decision-v1"'),
        "owner": array(number("d.organization_id"), number("d.marketplace_account_id"), string("d.marketplace"), "public.review_local_utf8_json_string(external_id)"),
        "draft": array(uid("d.draft_id"), number("d.revision"), checksum("d.text_checksum")),
        "source": array(uid("d.source_observation_id"), checksum("d.source_checksum")),
        "policy": array(uid("d.policy_id"), number("d.policy_version"), checksum("d.policy_checksum"), label("d.template_version"), label("d.model_version"))}) + ";")
    audit = obj({"schemaVersion": quoted('"review-audit-v1"'), "organizationId": number("a.organization_id"),
        "marketplaceAccountId": number("a.marketplace_account_id"), "marketplace": string("a.marketplace"),
        "eventId": uid("a.event_id"), "aggregateId": uid("a.aggregate_id"), "aggregateVersion": number("a.aggregate_version"),
        "eventKind": string("a.event_kind"), "occurredAt": timestamp("a.occurred_at"),
        "actorKind": quoted('"membership"'), "actorMembershipId": number("a.actor_membership_id"),
        "commandId": quoted("null"), "attemptId": quoted("null"), "reasonCode": quoted("null"),
        "policyId": uid("a.policy_id", True), "draftId": uid("a.draft_id", True), "decisionId": uid("a.decision_id", True),
        "beforeState": string("a.before_state", True), "afterState": string("a.after_state", True)})
    yield function("review_local_audit_bytes", "a public.review_local_audit", """
      IF a.marketplace IS NULL OR a.marketplace COLLATE "C" NOT IN ('wb','avito') OR a.event_kind IS NULL
       OR a.event_kind NOT IN ('policy.created','policy.selected','draft.published','decision.approved','decision.rejected')
       OR ((a.event_kind IN ('policy.created','policy.selected','draft.published')) IS DISTINCT FROM (a.policy_id IS NOT NULL))
       OR ((a.event_kind IN ('draft.published','decision.approved','decision.rejected')) IS DISTINCT FROM (a.draft_id IS NOT NULL))
       OR ((a.event_kind IN ('decision.approved','decision.rejected')) IS DISTINCT FROM (a.decision_id IS NOT NULL))
       OR (a.before_state='decision_current' AND a.aggregate_version<3) THEN """ + INVALID + """ END IF;
      IF a.event_kind='policy.created' THEN
       IF a.aggregate_id IS DISTINCT FROM a.policy_id OR a.before_state IS NOT NULL OR a.after_state IS NOT NULL THEN """ + INVALID + """ END IF;
      ELSIF a.event_kind='policy.selected' THEN
       IF a.after_state IS DISTINCT FROM 'policy_selected' OR (CASE WHEN a.aggregate_version=1 THEN a.before_state IS NOT NULL ELSE a.before_state IS DISTINCT FROM 'policy_selected' END) THEN """ + INVALID + """ END IF;
      ELSIF a.event_kind='draft.published' THEN
       IF a.after_state IS DISTINCT FROM 'draft_current' OR (CASE WHEN a.aggregate_version=1 THEN a.before_state IS NOT NULL ELSE a.before_state IS NULL OR a.before_state NOT IN ('draft_current','decision_current') END) THEN """ + INVALID + """ END IF;
      ELSE
       IF a.aggregate_version<2 OR a.after_state IS DISTINCT FROM 'decision_current' OR a.before_state IS NULL OR a.before_state NOT IN ('draft_current','decision_current') THEN """ + INVALID + """ END IF;
      END IF; RETURN """ + audit + ";")
    result = obj({"schemaVersion": quoted('"review-local-command-result-v1"'),
        "localCommandId": uid("r.local_command_id"), "operationKind": string("r.operation_kind"),
        "auditEventId": uid("r.audit_event_id"), "completedAt": timestamp("r.completed_at"),
        **{key: uid("r.result_" + column, True) for key, column in (("policyId", "policy_id"), ("draftId", "draft_id"), ("decisionId", "decision_id"), ("headId", "head_id"))},
        **{key: number("r.result_" + column, nullable=True) for key, column in (("policyVersion", "policy_version"), ("draftRevision", "draft_revision"), ("headVersion", "head_version"))}})
    yield function("review_local_result_bytes", "r public.review_local_command_receipts", """
      IF r.operation_kind IS NULL OR r.operation_kind NOT IN ('review.policy.create.v1','review.policy.select.v1','review.draft.publish.v1','review.decision.record.v1')
       OR ((r.operation_kind<>'review.decision.record.v1') IS DISTINCT FROM (r.result_policy_id IS NOT NULL))
       OR ((r.operation_kind<>'review.decision.record.v1') IS DISTINCT FROM (r.result_policy_version IS NOT NULL))
       OR ((r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1')) IS DISTINCT FROM (r.result_draft_id IS NOT NULL))
       OR ((r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1')) IS DISTINCT FROM (r.result_draft_revision IS NOT NULL))
       OR ((r.operation_kind='review.decision.record.v1') IS DISTINCT FROM (r.result_decision_id IS NOT NULL))
       OR ((r.operation_kind<>'review.policy.create.v1') IS DISTINCT FROM (r.result_head_id IS NOT NULL))
       OR ((r.operation_kind<>'review.policy.create.v1') IS DISTINCT FROM (r.result_head_version IS NOT NULL))
       OR (r.operation_kind='review.decision.record.v1' AND r.result_head_version<2) THEN """ + INVALID + " END IF; RETURN " + result + ";")
    inputs = {
        "review.policy.create.v1": obj({"policy": "public.review_local_policy_bytes(p)"}),
        "review.policy.select.v1": obj({"policyId": uid("p.policy_id"), "policyVersion": number("p.version"),
            "policyChecksum": checksum("p.policy_checksum"), "expectedHeadVersion": number("r.expected_head_version", True)}),
        "review.draft.publish.v1": obj({"reviewId": uid("r.review_id"), "externalReviewId": "public.review_local_utf8_json_string(external_id)",
            "draftId": uid("d.draft_id"), "expectedHeadVersion": number("r.expected_head_version", True),
            "expectedDraftRevision": number("r.expected_draft_revision", True), "expectedPolicyHeadId": uid("r.expected_policy_head_id"),
            "expectedPolicyHeadVersion": number("r.expected_policy_head_version"), "generation": "public.review_local_generation_bytes(d)",
            "text": "public.review_local_utf8_json_string(d.text_utf8)"}),
        "review.decision.record.v1": obj({"reviewId": uid("r.review_id"), "externalReviewId": "public.review_local_utf8_json_string(external_id)",
            "draftId": uid("d.draft_id"), "draftRevision": number("d.revision"), "bindingChecksum": checksum("d.binding_checksum"),
            "sourceObservationId": uid("d.source_observation_id"), "expectedHeadVersion": number("r.expected_head_version"),
            "expectedPolicyHeadId": uid("r.expected_policy_head_id"), "expectedPolicyHeadVersion": number("r.expected_policy_head_version"),
            "decisionKind": string("q.decision_kind")}),
    }
    envelope = {"schemaVersion": quoted('"review-local-command-v1"'), "organizationId": number("r.organization_id"),
        "marketplaceAccountId": number("r.marketplace_account_id"), "marketplace": string("r.marketplace"),
        "localCommandId": uid("r.local_command_id"), "actorMembershipId": number("r.actor_membership_id"), "operationKind": string("r.operation_kind")}
    body = """
      IF r.marketplace IS NULL OR r.marketplace COLLATE "C" NOT IN ('wb','avito') THEN """ + INVALID + """ END IF;
      IF r.operation_kind='review.policy.create.v1' AND ROW(p.organization_id,p.marketplace_account_id,p.marketplace) IS DISTINCT FROM ROW(r.organization_id,r.marketplace_account_id,r.marketplace) THEN """ + INVALID + """ END IF;
      IF r.operation_kind='review.draft.publish.v1' AND (d.actor_membership_id IS DISTINCT FROM r.actor_membership_id
       OR NOT public.review_local_nonblank_utf8(d.text_utf8)
       OR (r.expected_head_version=0) IS DISTINCT FROM (r.expected_draft_revision=0)) THEN """ + INVALID + """ END IF;
      IF r.operation_kind='review.decision.record.v1' AND (q.decision_kind IS NULL OR q.decision_kind NOT IN ('approved','rejected')) THEN """ + INVALID + """ END IF;
      IF r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1') AND (external_id IS NULL OR octet_length(external_id)=0) THEN """ + INVALID + """ END IF;
      IF ((r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1')) IS DISTINCT FROM (r.review_id IS NOT NULL))
       OR ((r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1')) IS DISTINCT FROM (r.expected_policy_head_id IS NOT NULL))
       OR ((r.operation_kind IN ('review.draft.publish.v1','review.decision.record.v1')) IS DISTINCT FROM (r.expected_policy_head_version IS NOT NULL))
       OR ((r.operation_kind='review.draft.publish.v1') IS DISTINCT FROM (r.expected_draft_revision IS NOT NULL))
       OR ((r.operation_kind<>'review.policy.create.v1') IS DISTINCT FROM (r.expected_head_version IS NOT NULL)) THEN """ + INVALID + " END IF; "
    for kind, inner in inputs.items():
        body += f"IF r.operation_kind='{kind}' THEN RETURN " + obj(dict(envelope, input=inner)) + "; END IF; "
    yield function("review_local_request_bytes", "r public.review_local_command_receipts,p public.review_policy_versions,d public.review_draft_revisions,q public.review_decisions,external_id bytea", body + INVALID)


def execute(sql):
    op.execute(sa.text(sql))


ALIASES = dict(zip(TABLES, ("policy", "phead", "draft", "decision", "whead", "audit", "receipt"), strict=True))
KEYS = {
    TABLES[0]: [("pk", OWNER + ",policy_id,version"), ("checksum", OWNER + ",policy_id,version,policy_checksum")],
    TABLES[1]: [("pk", OWNER), ("head", OWNER + ",head_id")],
    TABLES[2]: [("pk", OWNER + ",draft_id"), ("revision", OWNER + ",review_id,revision"),
        ("identity", OWNER + ",review_id,draft_id"), ("draft", OWNER + ",review_id,draft_id,revision"),
        ("binding", OWNER + ",review_id,draft_id,revision,binding_checksum"),
        ("generation", "organization_id,marketplace_account_id,generation_id")],
    TABLES[3]: [("pk", OWNER + ",decision_id"), ("draft", OWNER + ",review_id,draft_id,draft_revision,decision_id")],
    TABLES[4]: [("pk", OWNER + ",review_id"), ("head", OWNER + ",head_id")],
    TABLES[5]: [("pk", OWNER + ",event_id"), ("aggregate", OWNER + ",aggregate_kind,aggregate_id,aggregate_version"),
        ("receipt", OWNER + ",event_id,local_command_id"),
        ("selection", OWNER + ",event_id,policy_head_id,aggregate_version,policy_id,policy_version")],
    TABLES[6]: [("pk", "organization_id,marketplace_account_id,local_command_id"),
        ("owner", OWNER + ",local_command_id"), ("audit", OWNER + ",audit_event_id"),
        ("witness", OWNER + ",local_command_id,audit_event_id")],
}
# child, suffix, local columns, parent, referenced columns, deferred
FKS = [
    (table, "account", OWNER, "marketplace_accounts", OWNER, False) for table in TABLES
] + [
    (table, "actor", "organization_id,actor_membership_id", "iam_memberships", "organization_id,membership_id", False)
    for table in (TABLES[0], TABLES[2], TABLES[3], TABLES[5], TABLES[6])
] + [
    (TABLES[1], "policy", OWNER + ",current_policy_id,current_policy_version,current_policy_checksum", TABLES[0], OWNER + ",policy_id,version,policy_checksum", False),
    (TABLES[2], "fact", OWNER + ",review_id", "review_facts", OWNER + ",review_id", False),
    (TABLES[2], "source", OWNER + ",review_id,source_observation_id,source_checksum", "review_observations", OWNER + ",review_id,observation_id,content_checksum", False),
    (TABLES[2], "policy", OWNER + ",policy_id,policy_version,policy_checksum", TABLES[0], OWNER + ",policy_id,version,policy_checksum", False),
    (TABLES[2], "predecessor", OWNER + ",review_id,previous_draft_id", TABLES[2], OWNER + ",review_id,draft_id", False),
    (TABLES[2], "epoch", OWNER + ",policy_head_id", TABLES[1], OWNER + ",head_id", False),
    (TABLES[2], "selection", OWNER + ",policy_selection_event_id,policy_head_id,policy_head_version,policy_id,policy_version", TABLES[5], OWNER + ",event_id,policy_head_id,aggregate_version,policy_id,policy_version", True),
    (TABLES[3], "draft", OWNER + ",review_id,draft_id,draft_revision,binding_checksum", TABLES[2], OWNER + ",review_id,draft_id,revision,binding_checksum", False),
    (TABLES[4], "draft", OWNER + ",review_id,current_draft_id,current_draft_revision", TABLES[2], OWNER + ",review_id,draft_id,revision", False),
    (TABLES[4], "decision", OWNER + ",review_id,current_draft_id,current_draft_revision,current_decision_id", TABLES[3], OWNER + ",review_id,draft_id,draft_revision,decision_id", False),
    (TABLES[5], "policy", OWNER + ",policy_id,policy_version", TABLES[0], OWNER + ",policy_id,version", True),
    (TABLES[5], "draft", OWNER + ",review_id,draft_id,draft_revision", TABLES[2], OWNER + ",review_id,draft_id,revision", True),
    (TABLES[5], "decision", OWNER + ",review_id,draft_id,draft_revision,decision_id", TABLES[3], OWNER + ",review_id,draft_id,draft_revision,decision_id", True),
    (TABLES[5], "phead", OWNER + ",policy_head_id", TABLES[1], OWNER + ",head_id", True),
    (TABLES[5], "whead", OWNER + ",workflow_head_id", TABLES[4], OWNER + ",head_id", True),
    (TABLES[5], "receipt", OWNER + ",local_command_id,event_id", TABLES[6], OWNER + ",local_command_id,audit_event_id", True),
    (TABLES[6], "audit", OWNER + ",audit_event_id,local_command_id", TABLES[5], OWNER + ",event_id,local_command_id", True),
    (TABLES[6], "policy", OWNER + ",result_policy_id,result_policy_version", TABLES[0], OWNER + ",policy_id,version", True),
    (TABLES[6], "draft", OWNER + ",review_id,result_draft_id,result_draft_revision", TABLES[2], OWNER + ",review_id,draft_id,revision", True),
    (TABLES[6], "decision", OWNER + ",review_id,result_draft_id,result_draft_revision,result_decision_id", TABLES[3], OWNER + ",review_id,draft_id,draft_revision,decision_id", True),
] + [
    (table, "audit", OWNER + ",audit_event_id", TABLES[5], OWNER + ",event_id", True)
    for table in (TABLES[0], TABLES[2], TABLES[3])
]


def constraint(table, name, body):
    execute(f"ALTER TABLE public.{table} ADD CONSTRAINT review_local_{ALIASES[table]}_{name} {body}")


def constraints():
    for table in TABLES:
        for suffix, columns in KEYS[table]:
            constraint(table, suffix, ("PRIMARY KEY" if suffix == "pk" else "UNIQUE") + "(" + columns + ")")
        checks = ["organization_id>0", "marketplace_account_id>0", "marketplace COLLATE \"C\" IN ('wb','avito')"]
        for definition in COLUMNS[table].split(","):
            name, typ = definition.strip().split()[:2]
            if typ == "integer":
                checks.append(f"{name}>0")
            if typ == "uuid":
                checks.append(f"({name} IS NULL OR {name}<>'{ZERO}'::uuid)")
            if typ == "numeric":
                checks.append(f"({name} IS NULL OR public.review_local_integer_text({name},{str(name in ('expected_head_version','expected_draft_revision')).lower()}) IS NOT NULL)")
            if typ == "timestamptz":
                checks.append(f"({name} IS NULL OR public.review_local_timestamp_text({name}) IS NOT NULL)")
            if name.endswith("checksum"):
                checks.append(f"({name} IS NULL OR {name} COLLATE \"C\" ~ '^[0-9a-f]{{64}}$')")
        constraint(table, "scalars", "CHECK(" + " AND ".join(checks) + ")")
    for table, suffix, columns, parent, targets, deferred in FKS:
        constraint(table, suffix + "_fk", f"FOREIGN KEY({columns}) REFERENCES public.{parent}({targets})" + (" DEFERRABLE INITIALLY DEFERRED" if deferred else ""))
    constraint(TABLES[0], "bytes", f"CHECK(policy_payload=public.review_local_policy_bytes({projection(TABLES[0])}) AND policy_checksum=encode(sha256(policy_payload),'hex'))")
    constraint(TABLES[2], "bytes", f"""CHECK(public.review_local_nonblank_utf8(text_utf8)
      AND text_checksum=encode(sha256(text_utf8),'hex')
      AND generation_payload=public.review_local_generation_bytes({projection(TABLES[2])})
      AND generation_checksum=encode(sha256(generation_payload),'hex')
      AND public.review_strict_utf8(binding_payload) AND binding_checksum=encode(sha256(binding_payload),'hex'))""")
    constraint(TABLES[3], "kind", "CHECK(decision_kind IN ('approved','rejected'))")
    constraint(TABLES[5], "bytes", f"CHECK(audit_payload=public.review_local_audit_bytes({projection(TABLES[5])}) AND audit_checksum=encode(sha256(audit_payload),'hex'))")
    constraint(TABLES[5], "shape", """CHECK(
      aggregate_kind=CASE event_kind WHEN 'policy.created' THEN 'policy_version' WHEN 'policy.selected' THEN 'policy_head' ELSE 'workflow_head' END
      AND (policy_id IS NULL)=(policy_version IS NULL) AND (draft_id IS NULL)=(draft_revision IS NULL)
      AND (aggregate_kind='workflow_head')=(review_id IS NOT NULL))""")
    constraint(TABLES[6], "bytes", f"""CHECK(
      public.review_strict_utf8(request_payload) AND request_checksum=encode(sha256(request_payload),'hex')
      AND result_payload=public.review_local_result_bytes({projection(TABLES[6])})
      AND result_checksum=encode(sha256(result_payload),'hex') AND account_binding_schema_version=1
      AND account_binding_payload=public.review_run_binding_bytes(organization_id,marketplace_account_id,
        marketplace,account_binding_external_account_id,account_binding_credential_ref)
      AND account_binding_checksum=encode(sha256(account_binding_payload),'hex'))""")
    # Plan longer lookup prefixes first so a later index cannot make an earlier
    # generated nonunique child index redundant. Constraint indexes stay intact.
    covered = {table: [key.split(",") for _, key in KEYS[table]] for table in TABLES}
    for index, (table, _, columns, _, _, _) in sorted(enumerate(FKS), key=lambda item: -len(item[1][2].split(","))):
        parts = columns.split(",")
        if not any(key[:len(parts)] == parts for key in covered[table]):
            execute(f"CREATE INDEX review_local_child_{index} ON public.{table}({columns})")
            covered[table].append(parts)


GUARDS = r'''
CREATE FUNCTION public.review_local_account_lock() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='25000',MESSAGE='review_local_isolation_invalid';
 END IF;
 IF org IS NULL OR account IS NULL OR org COLLATE "C" !~ '^[1-9][0-9]{0,9}$'
  OR account COLLATE "C" !~ '^[1-9][0-9]{0,9}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_context_invalid';
 END IF;
 IF org::bigint>2147483647 OR account::bigint>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_context_invalid';
 END IF;
 -- The canonical two-GUC context has no provider input. Lock the exact account's
 -- canonical provider; NEW's provider is separately anchored by its composite FK.
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer
  AND marketplace_account_id=account::integer AND marketplace IN ('wb','avito') FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_account_missing'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') OR (TG_OP='UPDATE' AND TG_TABLE_NAME NOT IN ('review_policy_heads','review_workflow_heads')) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_immutable';
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.review_local_row_guard() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE f public.review_facts%ROWTYPE; d public.review_draft_revisions%ROWTYPE;
 ph public.review_policy_heads%ROWTYPE; wh public.review_workflow_heads%ROWTYPE;
 account public.marketplace_accounts%ROWTYPE; source record;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_context_invalid';
 END IF;
 IF TG_TABLE_NAME IN ('review_policy_heads','review_workflow_heads') THEN
  IF TG_OP='INSERT' THEN
   IF NEW.version IS DISTINCT FROM 1::numeric THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
   IF TG_TABLE_NAME='review_workflow_heads' THEN
    IF NEW.current_draft_revision IS DISTINCT FROM 1::numeric OR NEW.current_decision_id IS NOT NULL THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
    END IF;
   END IF;
  ELSE
   IF ROW(NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,NEW.head_id)
    IS DISTINCT FROM ROW(OLD.organization_id,OLD.marketplace_account_id,OLD.marketplace,OLD.head_id)
    OR NEW.version IS DISTINCT FROM OLD.version+1 OR NEW.updated_at<OLD.updated_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
   IF TG_TABLE_NAME='review_workflow_heads' THEN
    IF NEW.review_id IS DISTINCT FROM OLD.review_id THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
    END IF;
    IF NEW.current_draft_id IS DISTINCT FROM OLD.current_draft_id THEN
     IF NEW.current_draft_revision IS DISTINCT FROM OLD.current_draft_revision+1 OR NEW.current_decision_id IS NOT NULL THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
     END IF;
    ELSE
     IF NEW.current_draft_revision IS DISTINCT FROM OLD.current_draft_revision OR NEW.current_decision_id IS NULL
      OR NEW.current_decision_id IS NOT DISTINCT FROM OLD.current_decision_id THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
     END IF;
    END IF;
   END IF;
  END IF;
 ELSIF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_immutable';
 END IF;
 IF TG_TABLE_NAME IN ('review_draft_revisions','review_decisions') THEN
  -- Account statement fence is already held. Never reverse fact/head ordering.
  SELECT * INTO f FROM public.review_facts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND review_id=NEW.review_id FOR SHARE;
  IF NOT FOUND OR f.source_order_state<>'current' THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_source_changed';
  END IF;
  IF TG_TABLE_NAME='review_draft_revisions' THEN d:=NEW;
  ELSE
   SELECT * INTO d FROM public.review_draft_revisions WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
    AND review_id=NEW.review_id AND draft_id=NEW.draft_id;
   IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
  END IF;
  IF f.current_observation_id IS DISTINCT FROM d.source_observation_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_source_changed';
  END IF;
  SELECT * INTO ph FROM public.review_policy_heads WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace FOR UPDATE;
  IF NOT FOUND OR ROW(ph.head_id,ph.version,ph.current_policy_id,ph.current_policy_version,ph.current_policy_checksum)
   IS DISTINCT FROM ROW(d.policy_head_id,d.policy_head_version,d.policy_id,d.policy_version,d.policy_checksum) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_policy_changed';
  END IF;
  SELECT * INTO wh FROM public.review_workflow_heads WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND review_id=NEW.review_id FOR UPDATE;
  IF TG_TABLE_NAME='review_draft_revisions' THEN
   IF NEW.revision IS DISTINCT FROM COALESCE(wh.current_draft_revision,0)+1 THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
   IF NEW.generation_mode='manual_edit' AND (wh.head_id IS NULL OR NEW.previous_draft_id IS DISTINCT FROM wh.current_draft_id
    OR NEW.draft_id=NEW.previous_draft_id) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_predecessor_stale';
   END IF;
  ELSE
   IF wh.head_id IS NULL OR ROW(wh.current_draft_id,wh.current_draft_revision)
    IS DISTINCT FROM ROW(NEW.draft_id,NEW.draft_revision) OR NEW.decided_at<d.created_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
  END IF;
  SELECT o.answered,o.can_answer,r.account_binding_payload INTO source FROM public.review_observations o
   JOIN public.review_sync_runs_v2 r ON ROW(r.organization_id,r.marketplace_account_id,r.marketplace,r.sync_run_id)
    =ROW(o.organization_id,o.marketplace_account_id,o.marketplace,o.source_run_id)
   WHERE o.organization_id=NEW.organization_id AND o.marketplace_account_id=NEW.marketplace_account_id
    AND o.marketplace=NEW.marketplace AND o.review_id=NEW.review_id AND o.observation_id=d.source_observation_id;
  IF NOT FOUND OR source.account_binding_payload IS NULL THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_source_unbound';
  END IF;
  SELECT * INTO account FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace;
  IF source.account_binding_payload IS DISTINCT FROM public.review_run_binding_bytes(account.organization_id,
   account.marketplace_account_id,account.marketplace,account.external_account_id,account.credential_ref) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_source_unbound';
  END IF;
  IF TG_TABLE_NAME='review_decisions' THEN
   IF NEW.decision_kind='approved' AND (source.answered IS DISTINCT FROM false OR source.can_answer IS DISTINCT FROM true) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_not_answerable';
   END IF;
  END IF;
 END IF;
 IF TG_TABLE_NAME='review_local_command_receipts' THEN
  SELECT * INTO account FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace;
  IF NOT FOUND OR ROW(NEW.account_binding_external_account_id,NEW.account_binding_credential_ref)
   IS DISTINCT FROM ROW(account.external_account_id,account.credential_ref) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 END IF;
 RETURN NEW;
END $$;
'''


VALIDATION = r'''
CREATE FUNCTION public.review_local_validate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE a public.review_local_audit%ROWTYPE; r public.review_local_command_receipts%ROWTYPE;
 p public.review_policy_versions%ROWTYPE; d public.review_draft_revisions%ROWTYPE;
 q public.review_decisions%ROWTYPE; f public.review_facts%ROWTYPE;
 ph public.review_policy_heads%ROWTYPE; wh public.review_workflow_heads%ROWTYPE;
 event record; expected_kind text; external_id bytea; source_binding bytea;
 seen numeric:=0; previous_time timestamptz; previous_state text;
 last_draft uuid; last_revision numeric:=0; last_decision uuid;
 last_policy uuid; last_policy_version numeric;
BEGIN
 -- A later SET LOCAL cannot hide an earlier owner's deferred callback under RLS.
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_context_invalid';
 END IF;
 IF TG_TABLE_NAME IN ('review_policy_heads','review_workflow_heads') THEN
  SELECT * INTO a FROM public.review_local_audit WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND aggregate_kind=CASE TG_TABLE_NAME WHEN 'review_policy_heads' THEN 'policy_head' ELSE 'workflow_head' END
   AND aggregate_id=NEW.head_id AND aggregate_version=NEW.version;
 ELSIF TG_TABLE_NAME='review_local_audit' THEN a:=NEW;
 ELSE
  SELECT * INTO a FROM public.review_local_audit WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND event_id=NEW.audit_event_id;
 END IF;
 IF a.event_id IS NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
 SELECT * INTO r FROM public.review_local_command_receipts WHERE organization_id=a.organization_id
  AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
  AND local_command_id=a.local_command_id AND audit_event_id=a.event_id;
 IF NOT FOUND OR ROW(r.actor_membership_id,r.completed_at,r.review_id)
  IS DISTINCT FROM ROW(a.actor_membership_id,a.occurred_at,a.review_id) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 expected_kind:=CASE a.event_kind WHEN 'policy.created' THEN 'review.policy.create.v1'
  WHEN 'policy.selected' THEN 'review.policy.select.v1' WHEN 'draft.published' THEN 'review.draft.publish.v1'
  ELSE 'review.decision.record.v1' END;
 IF r.operation_kind IS DISTINCT FROM expected_kind THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 IF a.policy_id IS NOT NULL THEN
  SELECT * INTO p FROM public.review_policy_versions WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
   AND policy_id=a.policy_id AND version=a.policy_version;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
 END IF;
 IF a.draft_id IS NOT NULL THEN
  SELECT * INTO d FROM public.review_draft_revisions WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
   AND review_id=a.review_id AND draft_id=a.draft_id AND revision=a.draft_revision;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
  SELECT * INTO p FROM public.review_policy_versions WHERE organization_id=d.organization_id
   AND marketplace_account_id=d.marketplace_account_id AND marketplace=d.marketplace
   AND policy_id=d.policy_id AND version=d.policy_version;
  IF NOT FOUND OR ROW(d.policy_checksum,d.template_version,d.model_version)
   IS DISTINCT FROM ROW(p.policy_checksum,p.template_version,p.model_version) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
  SELECT * INTO f FROM public.review_facts WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace AND review_id=a.review_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
  external_id:=COALESCE(f.external_review_id_utf8,convert_to(f.external_review_id,'UTF8'));
  IF d.binding_payload IS DISTINCT FROM public.review_local_decision_binding_bytes(d,external_id)
   OR ROW(r.expected_policy_head_id,r.expected_policy_head_version)
    IS DISTINCT FROM ROW(d.policy_head_id,d.policy_head_version) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
  SELECT run.account_binding_payload INTO source_binding FROM public.review_observations o
   JOIN public.review_sync_runs_v2 run ON ROW(run.organization_id,run.marketplace_account_id,run.marketplace,run.sync_run_id)
    =ROW(o.organization_id,o.marketplace_account_id,o.marketplace,o.source_run_id)
   WHERE o.organization_id=d.organization_id AND o.marketplace_account_id=d.marketplace_account_id
    AND o.marketplace=d.marketplace AND o.review_id=d.review_id AND o.observation_id=d.source_observation_id;
  IF NOT FOUND OR source_binding IS NULL OR source_binding IS DISTINCT FROM r.account_binding_payload THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_source_unbound';
  END IF;
 END IF;
 IF a.decision_id IS NOT NULL THEN
  SELECT * INTO q FROM public.review_decisions WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
   AND review_id=a.review_id AND decision_id=a.decision_id;
  IF NOT FOUND OR ROW(q.draft_id,q.draft_revision,q.binding_checksum,q.audit_event_id,q.actor_membership_id,q.decided_at)
   IS DISTINCT FROM ROW(d.draft_id,d.revision,d.binding_checksum,a.event_id,a.actor_membership_id,a.occurred_at)
   OR a.event_kind IS DISTINCT FROM 'decision.'||q.decision_kind THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 END IF;
 IF a.event_kind='policy.created' THEN
  IF ROW(p.audit_event_id,p.actor_membership_id,p.created_at)
   IS DISTINCT FROM ROW(a.event_id,a.actor_membership_id,a.occurred_at)
   OR a.aggregate_version IS DISTINCT FROM p.version THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF a.event_kind='policy.selected' THEN
  IF a.occurred_at<p.created_at THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
 ELSIF a.event_kind='draft.published' THEN
  IF ROW(d.audit_event_id,d.actor_membership_id,d.created_at,d.policy_id,d.policy_version)
   IS DISTINCT FROM ROW(a.event_id,a.actor_membership_id,a.occurred_at,a.policy_id,a.policy_version)
   OR r.expected_draft_revision IS DISTINCT FROM d.revision-1 THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 END IF;
 IF ROW(r.result_policy_id,r.result_policy_version,r.result_draft_id,r.result_draft_revision,r.result_decision_id)
  IS DISTINCT FROM ROW(a.policy_id,a.policy_version,a.draft_id,a.draft_revision,a.decision_id) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 IF a.aggregate_kind<>'policy_version' AND (ROW(r.result_head_id,r.result_head_version)
  IS DISTINCT FROM ROW(a.aggregate_id,a.aggregate_version) OR r.expected_head_version IS DISTINCT FROM a.aggregate_version-1) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 IF r.request_payload IS DISTINCT FROM public.review_local_request_bytes(r,p,d,q,external_id) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
 END IF;
 -- Validate the triggering immutable entity, not just some correctly linked event.
 IF TG_TABLE_NAME='review_policy_versions' THEN
  IF a.event_kind<>'policy.created' OR ROW(NEW.policy_id,NEW.version) IS DISTINCT FROM ROW(p.policy_id,p.version) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF TG_TABLE_NAME='review_draft_revisions' THEN
  IF a.event_kind<>'draft.published' OR NEW.draft_id IS DISTINCT FROM d.draft_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF TG_TABLE_NAME='review_decisions' THEN
  IF NEW.decision_id IS DISTINCT FROM q.decision_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF TG_TABLE_NAME='review_local_command_receipts' THEN
  IF NEW.local_command_id IS DISTINCT FROM r.local_command_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF TG_TABLE_NAME='review_policy_heads' THEN
  IF ROW(NEW.current_policy_id,NEW.current_policy_version,NEW.current_policy_checksum,NEW.updated_at)
   IS DISTINCT FROM ROW(p.policy_id,p.version,p.policy_checksum,a.occurred_at) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
  IF TG_OP='UPDATE' AND a.before_state IS DISTINCT FROM 'policy_selected' THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSIF TG_TABLE_NAME='review_workflow_heads' THEN
  IF ROW(NEW.current_draft_id,NEW.current_draft_revision,NEW.current_decision_id,NEW.updated_at,NEW.review_id)
   IS DISTINCT FROM ROW(a.draft_id,a.draft_revision,a.decision_id,a.occurred_at,a.review_id) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
  IF TG_OP='UPDATE' THEN
   IF a.before_state IS DISTINCT FROM (CASE WHEN OLD.current_decision_id IS NULL THEN 'draft_current' ELSE 'decision_current' END)
    OR (a.event_kind='draft.published' AND d.revision IS DISTINCT FROM OLD.current_draft_revision+1)
    OR (a.event_kind<>'draft.published' AND ROW(OLD.current_draft_id,OLD.current_draft_revision)
       IS DISTINCT FROM ROW(d.draft_id,d.revision)) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
  END IF;
 END IF;
 -- Immutable transition evidence forms a complete chain ending at the final head.
 -- Earlier epochs are never compared to the final policy as new eligibility checks.
 IF a.aggregate_kind='policy_head' THEN
  SELECT * INTO ph FROM public.review_policy_heads WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace AND head_id=a.aggregate_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
 ELSIF a.aggregate_kind='workflow_head' THEN
  SELECT * INTO wh FROM public.review_workflow_heads WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
   AND head_id=a.aggregate_id AND review_id=a.review_id;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid'; END IF;
 ELSE RETURN NULL;
 END IF;
 FOR event IN SELECT * FROM public.review_local_audit WHERE organization_id=a.organization_id
   AND marketplace_account_id=a.marketplace_account_id AND marketplace=a.marketplace
   AND aggregate_kind=a.aggregate_kind AND aggregate_id=a.aggregate_id ORDER BY aggregate_version LOOP
  seen:=seen+1;
  IF event.aggregate_version IS DISTINCT FROM seen OR event.before_state IS DISTINCT FROM previous_state
   OR event.occurred_at<previous_time THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
  IF a.aggregate_kind='workflow_head' THEN
   IF event.review_id IS DISTINCT FROM wh.review_id THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
   END IF;
   IF event.event_kind='draft.published' THEN
    IF event.draft_revision IS DISTINCT FROM last_revision+1 THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
    END IF;
    last_draft:=event.draft_id; last_revision:=event.draft_revision; last_decision:=NULL;
   ELSE
    IF ROW(event.draft_id,event.draft_revision) IS DISTINCT FROM ROW(last_draft,last_revision) THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
    END IF;
    last_decision:=event.decision_id;
   END IF;
  ELSE last_policy:=event.policy_id; last_policy_version:=event.policy_version;
  END IF;
  previous_state:=event.after_state; previous_time:=event.occurred_at;
 END LOOP;
 IF a.aggregate_kind='policy_head' THEN
  IF ROW(ph.version,ph.current_policy_id,ph.current_policy_version,ph.updated_at)
   IS DISTINCT FROM ROW(seen,last_policy,last_policy_version,previous_time) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 ELSE
  IF ROW(wh.version,wh.current_draft_id,wh.current_draft_revision,wh.current_decision_id,wh.updated_at)
   IS DISTINCT FROM ROW(seen,last_draft,last_revision,last_decision,previous_time) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
  END IF;
 END IF;
 RETURN NULL;
END $$;
'''


PURE = (
    "review_local_integer_text(numeric,boolean)", "review_local_timestamp_text(timestamp with time zone)",
    "review_local_utf8_json_string(bytea)", "review_local_nonblank_utf8(bytea)",
    "review_local_string(text,boolean)", "review_local_uuid(uuid,boolean)",
    "review_local_number(numeric,boolean,boolean)", "review_local_label(text)", "review_local_checksum(text)",
    "review_local_policy_bytes(review_policy_versions)", "review_local_generation_bytes(review_draft_revisions)",
    "review_local_decision_binding_bytes(review_draft_revisions,bytea)", "review_local_audit_bytes(review_local_audit)",
    "review_local_result_bytes(review_local_command_receipts)",
    "review_local_request_bytes(review_local_command_receipts,review_policy_versions,review_draft_revisions,review_decisions,bytea)",
)
TRIGGERS = ("review_local_account_lock()", "review_local_row_guard()", "review_local_validate()")


def sql_list(values):
    return ",".join("'" + value + "'" for value in values)


def function_oids(signatures):
    qualified = []
    for signature in signatures:
        name, _, arguments = signature.partition("(")
        for table in TABLES:
            arguments = arguments.replace(table, "public." + table)
        qualified.append("'public." + name + "(" + arguments + "'::regprocedure")
    return ",".join(qualified)


def acl():
    return """DO $$ DECLARE t record; a record; col record; f record; entitled record;
      allowed text[]; rights text; grantee text;
    BEGIN
     FOR t IN SELECT oid,relname,relowner,relacl FROM pg_class WHERE relnamespace='public'::regnamespace
      AND relname IN (""" + sql_list(TABLES) + """) LOOP
      allowed:=CASE WHEN t.relname IN ('review_policy_heads','review_workflow_heads') THEN ARRAY['SELECT','INSERT','UPDATE'] ELSE ARRAY['SELECT','INSERT'] END;
      FOR a IN SELECT x.grantee,array_agg(DISTINCT x.privilege_type) privileges
       FROM aclexplode(COALESCE(t.relacl,acldefault('r',t.relowner))) x WHERE x.grantee<>t.relowner GROUP BY x.grantee LOOP
       grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
       EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s',t.relname,grantee);
       IF a.grantee<>0 THEN
        FOREACH rights IN ARRAY a.privileges LOOP
         IF rights=ANY(allowed) THEN EXECUTE format('GRANT %s ON TABLE public.%I TO %s',rights,t.relname,grantee); END IF;
        END LOOP;
       END IF;
      END LOOP;
      FOR col IN SELECT at.attname,x.grantee,array_agg(DISTINCT x.privilege_type) privileges
       FROM pg_attribute at CROSS JOIN LATERAL aclexplode(at.attacl) x
       WHERE at.attrelid=t.oid AND at.attnum>0 AND NOT at.attisdropped AND x.grantee<>t.relowner
       GROUP BY at.attname,x.grantee LOOP
       grantee:=CASE WHEN col.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(col.grantee)) END;
       EXECUTE format('REVOKE ALL (%I) ON TABLE public.%I FROM %s',col.attname,t.relname,grantee);
       IF col.grantee<>0 THEN
        FOREACH rights IN ARRAY col.privileges LOOP
         IF rights=ANY(allowed) THEN EXECUTE format('GRANT %s (%I) ON TABLE public.%I TO %s',rights,col.attname,t.relname,grantee); END IF;
        END LOOP;
       END IF;
      END LOOP;
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',t.relname);
     END LOOP;
     FOR f IN SELECT oid,proowner,proacl,oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
      AND oid IN (""" + function_oids((*PURE, *TRIGGERS)) + """) LOOP
      FOR a IN SELECT DISTINCT x.grantee FROM aclexplode(COALESCE(f.proacl,acldefault('f',f.proowner))) x WHERE x.grantee<>f.proowner LOOP
       grantee:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
       EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,grantee);
      END LOOP;
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
     END LOOP;
     FOR entitled IN SELECT DISTINCT entitlement.grantee FROM (
      SELECT c.relowner grantee FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relname IN (""" + sql_list(TABLES) + """)
      UNION SELECT x.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
       WHERE c.relnamespace='public'::regnamespace AND c.relname IN (""" + sql_list(TABLES) + """)
      UNION SELECT x.grantee FROM pg_class c JOIN pg_attribute at ON at.attrelid=c.oid CROSS JOIN LATERAL aclexplode(at.attacl) x
       WHERE c.relnamespace='public'::regnamespace AND c.relname IN (""" + sql_list(TABLES) + """)
     ) entitlement WHERE entitlement.grantee<>0 LOOP
      FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
       AND oid IN (""" + function_oids(PURE) + """) LOOP
       EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,pg_get_userbyid(entitled.grantee));
      END LOOP;
     END LOOP;
    END $$;"""


def upgrade():
    execute(SCALARS)
    for table, columns in COLUMNS.items():
        extra = ""
        if table == TABLES[5]:
            extra = ",policy_head_id uuid GENERATED ALWAYS AS (CASE WHEN aggregate_kind='policy_head' THEN aggregate_id END) STORED,workflow_head_id uuid GENERATED ALWAYS AS (CASE WHEN aggregate_kind='workflow_head' THEN aggregate_id END) STORED"
        execute(f"CREATE TABLE public.{table} ({OWNER_COLUMNS},{columns}{extra})")
    for sql in codecs():
        execute(sql)
    constraints()
    execute(GUARDS)
    execute(VALIDATION)
    for table in TABLES:
        execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        predicate = 'organization_id::text COLLATE "C"=current_setting(\'app.organization_id\',true) COLLATE "C" AND marketplace_account_id::text COLLATE "C"=current_setting(\'app.marketplace_account_id\',true) COLLATE "C"'
        execute(f"CREATE POLICY review_local_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})")
        execute(f"CREATE TRIGGER review_local_account_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.review_local_account_lock()")
        execute(f"CREATE TRIGGER review_local_guard BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.review_local_row_guard()")
        execute(f"CREATE CONSTRAINT TRIGGER review_local_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.review_local_validate()")
    execute(acl())


def downgrade():
    execute("LOCK TABLE " + ",".join("public." + table for table in TABLES) + " IN ACCESS EXCLUSIVE MODE")
    execute("SET LOCAL row_security=off")
    for table in TABLES:
        execute(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='review_local_downgrade_nonempty'; END IF; END $$")
    for table in TABLES:
        for trigger in ("review_local_witness", "review_local_guard", "review_local_account_first"):
            execute(f"DROP TRIGGER {trigger} ON public.{table}")
    for signature in TRIGGERS:
        execute("DROP FUNCTION public." + signature)
    # Drop explicit new FK/check dependencies before composite-typed helper functions.
    for table, suffix, _, _, _, _ in FKS:
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT review_local_{ALIASES[table]}_{suffix}_fk")
    for table in TABLES:
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT review_local_{ALIASES[table]}_scalars")
    for table in (TABLES[0], TABLES[2], TABLES[5], TABLES[6]):
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT review_local_{ALIASES[table]}_bytes")
    for signature in reversed(PURE):
        execute("DROP FUNCTION public." + signature)
    for table in reversed(TABLES):
        execute("DROP TABLE public." + table)
