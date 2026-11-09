"""Review send and account notifications. IMPLEMENTED / UNVERIFIED.

Physical integrity only; authenticated initiation/execution/closing remain T1/T4
service responsibilities. This revision performs no provider or queue operation.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0074"
down_revision = "20260909_0073"
branch_labels = depends_on = None

# Unicode 14.0.0 Nd blocks: matches the repository's python:3.11-slim image.
# Never derive immutable DDL from the machine executing an upgrade. Python's
# regex/strip representation contract must be version-matched by the consumer.
UNICODE_VERSION = "14.0.0"
DECIMAL_STARTS = (
    0x0030, 0x0660, 0x06F0, 0x07C0, 0x0966, 0x09E6, 0x0A66, 0x0AE6,
    0x0B66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66, 0x0DE6, 0x0E50, 0x0ED0,
    0x0F20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80,
    0x1A90, 0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900,
    0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10, 0x104A0, 0x10D30, 0x11066,
    0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650,
    0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0,
    0x16A60, 0x16AC0, 0x16B50, 0x1E140, 0x1E2F0, 0x1E950, 0x1FBF0,
)

TABLES = (
    "review_send_commands", "review_send_command_authorities", "review_send_attempts",
    "review_answer_evidence", "review_send_audit", "review_send_enqueue_intents",
    "notification_in_app_events", "notification_in_app_receipts",
)
C, H, A, E, J, Q, N, R = TABLES
OWNER = "organization_id,marketplace_account_id,marketplace"
OWNER_COLUMNS = "organization_id integer NOT NULL,marketplace_account_id integer NOT NULL,marketplace text COLLATE \"C\" NOT NULL"
COLUMNS = {
    C: """command_id uuid NOT NULL,review_id uuid NOT NULL,draft_id uuid NOT NULL,
      draft_revision numeric NOT NULL,decision_id uuid NOT NULL,operation_kind text NOT NULL,
      idempotency_key uuid NOT NULL,request_payload bytea NOT NULL,request_checksum text NOT NULL,
      binding_checksum text NOT NULL,text_checksum text NOT NULL,creator_membership_id integer NOT NULL,
      created_at timestamptz NOT NULL,state text NOT NULL,version numeric NOT NULL,
      current_attempt_id uuid,completed_at timestamptz,result_evidence_id uuid,reason_code text,
      audit_event_id uuid NOT NULL""",
    H: """command_id uuid NOT NULL,origin_user_id varchar(64) NOT NULL,
      origin_membership_id integer NOT NULL,origin_session_id varchar(64) NOT NULL,
      account_binding_schema_version smallint NOT NULL,expected_external_account_id varchar(128) NOT NULL,
      expected_credential_ref varchar(255),credential_id uuid NOT NULL,generation bigint NOT NULL,
      credential_kind text NOT NULL,payload_schema_version smallint NOT NULL,credential_expires_at timestamptz,
      policy_reference text NOT NULL,policy_version numeric NOT NULL,lease_seconds numeric NOT NULL,
      authority_expires_at timestamptz NOT NULL""",
    A: """attempt_id uuid NOT NULL,command_id uuid NOT NULL,review_id uuid NOT NULL,
      sequence numeric NOT NULL,claimed_at timestamptz NOT NULL,lease_token uuid NOT NULL,
      state text NOT NULL,lease_expires_at timestamptz NOT NULL,dispatched_at timestamptz,
      finished_at timestamptz,result_evidence_id uuid,reason_code text,
      command_version numeric NOT NULL,audit_event_id uuid NOT NULL""",
    E: """evidence_id uuid NOT NULL,review_id uuid NOT NULL,command_id uuid NOT NULL,
      attempt_id uuid NOT NULL,read_id uuid,evidence_kind text NOT NULL,evidence_version text NOT NULL,
      outcome text NOT NULL,observed_at timestamptz NOT NULL,reconciliation_started_at timestamptz,
      provider_answer_id_utf8 bytea,answer_checksum text,verifier_version text NOT NULL,
      evidence_payload bytea NOT NULL,evidence_checksum text NOT NULL""",
    J: """event_id uuid NOT NULL,review_id uuid NOT NULL,aggregate_id uuid NOT NULL,
      aggregate_version numeric NOT NULL,event_kind text NOT NULL,occurred_at timestamptz NOT NULL,
      actor_kind text NOT NULL,actor_membership_id integer,command_id uuid NOT NULL,draft_id uuid,
      policy_id uuid,decision_id uuid,attempt_id uuid,before_state text,after_state text NOT NULL,
      reason_code text,audit_payload bytea NOT NULL,audit_checksum text NOT NULL,
      current_attempt_id uuid,command_completed_at timestamptz,result_evidence_id uuid,
      attempt_sequence numeric,lease_token uuid,attempt_before_command_version numeric,
      attempt_before_state text,attempt_after_state text,lease_before_expires_at timestamptz,
      lease_after_expires_at timestamptz,attempt_claimed_at timestamptz,
      attempt_dispatched_at timestamptz,attempt_finished_at timestamptz""",
    Q: """command_id uuid NOT NULL,command_version numeric NOT NULL,event_kind text NOT NULL,
      audit_event_id uuid NOT NULL,enqueue_payload bytea NOT NULL,enqueue_checksum text NOT NULL""",
    N: """event_id uuid NOT NULL,scope text NOT NULL,producer text NOT NULL,entity_id uuid NOT NULL,
      source_version numeric NOT NULL,kind text NOT NULL,occurred_at timestamptz NOT NULL,
      dedupe_key text NOT NULL,title text NOT NULL,details text NOT NULL,severity text NOT NULL,
      source_review_id uuid NOT NULL,source_draft_id uuid,source_command_id uuid,source_audit_event_id uuid,
      event_payload bytea NOT NULL,event_checksum text NOT NULL""",
    R: """event_id uuid NOT NULL,recipient_membership_id integer NOT NULL,
      read_at timestamptz,dismissed_at timestamptz,version numeric NOT NULL""",
}
MUTABLE = {
    C: "state,version,current_attempt_id,completed_at,result_evidence_id,reason_code,audit_event_id",
    A: "state,lease_expires_at,dispatched_at,finished_at,result_evidence_id,reason_code,command_version,audit_event_id",
    R: "read_at,dismissed_at",
}
KEYS = {
    C: [("pk", OWNER + ",command_id"), ("review", OWNER + ",review_id,command_id"),
        ("idem", "organization_id,marketplace_account_id,operation_kind,idempotency_key")],
    H: [("pk", OWNER + ",command_id")],
    A: [("pk", OWNER + ",attempt_id"), ("command", OWNER + ",review_id,command_id,attempt_id"),
        ("sequence", OWNER + ",command_id,sequence"), ("token", OWNER + ",lease_token")],
    E: [("pk", OWNER + ",evidence_id"), ("attempt", OWNER + ",review_id,command_id,attempt_id,evidence_id"),
        ("read", OWNER + ",read_id")],
    J: [("pk", OWNER + ",event_id"), ("version", OWNER + ",command_id,aggregate_version"),
        ("witness", OWNER + ",command_id,aggregate_version,event_id"),
        ("source", OWNER + ",review_id,command_id,aggregate_version,event_id")],
    Q: [("pk", OWNER + ",command_id,command_version,event_kind")],
    N: [("pk", "organization_id,event_id"), ("scope", OWNER + ",event_id"),
        ("dedupe", "organization_id,marketplace_account_id,dedupe_key")],
    R: [("pk", "organization_id,event_id,recipient_membership_id")],
}
# Explicit child keys only. All referenced parent keys already exist in 0071 or
# earlier; credential/session owner equality is checked against the actual row.
FKS = [(t, "account", OWNER, "marketplace_accounts", OWNER) for t in TABLES] + [
    (C, "draft", OWNER + ",review_id,draft_id,draft_revision,binding_checksum", "review_draft_revisions", OWNER + ",review_id,draft_id,revision,binding_checksum"),
    (C, "decision", OWNER + ",review_id,draft_id,draft_revision,decision_id", "review_decisions", OWNER + ",review_id,draft_id,draft_revision,decision_id"),
    (C, "creator", "organization_id,creator_membership_id", "iam_memberships", "organization_id,membership_id"),
    (C, "authority", OWNER + ",command_id", H, OWNER + ",command_id"),
    (C, "attempt", OWNER + ",review_id,command_id,current_attempt_id", A, OWNER + ",review_id,command_id,attempt_id"),
    (C, "evidence", OWNER + ",review_id,command_id,current_attempt_id,result_evidence_id", E, OWNER + ",review_id,command_id,attempt_id,evidence_id"),
    (C, "audit", OWNER + ",command_id,version,audit_event_id", J, OWNER + ",command_id,aggregate_version,event_id"),
    (H, "command", OWNER + ",command_id", C, OWNER + ",command_id"),
    (H, "member", "organization_id,origin_membership_id", "iam_memberships", "organization_id,membership_id"),
    (H, "user", "origin_user_id", "lk_users", "user_id"),
    (H, "session", "origin_session_id", "lk_sessions", "session_id"),
    (H, "credential", "credential_id", "marketplace_account_credentials", "credential_id"),
    (A, "command", OWNER + ",review_id,command_id", C, OWNER + ",review_id,command_id"),
    (A, "evidence", OWNER + ",review_id,command_id,attempt_id,result_evidence_id", E, OWNER + ",review_id,command_id,attempt_id,evidence_id"),
    (A, "audit", OWNER + ",command_id,command_version,audit_event_id", J, OWNER + ",command_id,aggregate_version,event_id"),
    (E, "attempt", OWNER + ",review_id,command_id,attempt_id", A, OWNER + ",review_id,command_id,attempt_id"),
    (J, "command", OWNER + ",review_id,command_id", C, OWNER + ",review_id,command_id"),
    (J, "attempt", OWNER + ",review_id,command_id,attempt_id", A, OWNER + ",review_id,command_id,attempt_id"),
    (J, "evidence", OWNER + ",review_id,command_id,attempt_id,result_evidence_id", E, OWNER + ",review_id,command_id,attempt_id,evidence_id"),
    (J, "actor", "organization_id,actor_membership_id", "iam_memberships", "organization_id,membership_id"),
    (Q, "audit", OWNER + ",command_id,command_version,audit_event_id", J, OWNER + ",command_id,aggregate_version,event_id"),
    (N, "draft", OWNER + ",source_review_id,source_draft_id,source_version", "review_draft_revisions", OWNER + ",review_id,draft_id,revision"),
    (N, "audit", OWNER + ",source_review_id,source_command_id,source_version,source_audit_event_id", J, OWNER + ",review_id,command_id,aggregate_version,event_id"),
    (R, "event", OWNER + ",event_id", N, OWNER + ",event_id"),
    (R, "member", "organization_id,recipient_membership_id", "iam_memberships", "organization_id,membership_id"),
]


def execute(sql):
    op.execute(sa.text(sql))


def literal(value):
    return "convert_to('" + value.replace("'", "''") + "','UTF8')"


def obj(values):
    parts = []
    for i, (key, value) in enumerate(sorted(values.items())):
        parts.extend((literal(("{" if i == 0 else ",") + '"' + key + '":'), value))
    return "||".join((*parts, literal("}")))


def string(value, nullable=False):
    return f"public.review_local_string({value},{str(nullable).lower()})"


def uid(value, nullable=False):
    return f"public.review_local_uuid({value},{str(nullable).lower()})"


def number(value, nullable=False):
    return f"public.review_local_number({value},false,{str(nullable).lower()})"


def stamp(value, nullable=False):
    result = string(f"public.review_local_timestamp_text({value})")
    return f"CASE WHEN {value} IS NULL THEN {literal('null')} ELSE {result} END" if nullable else result


def digest(value, nullable=False):
    result = f"public.review_local_checksum({value})"
    return f"CASE WHEN {value} IS NULL THEN {literal('null')} ELSE {result} END" if nullable else result


def owner(alias, marketplace=True):
    result = {"organizationId": number(alias + ".organization_id"),
              "marketplaceAccountId": number(alias + ".marketplace_account_id")}
    if marketplace:
        result["marketplace"] = string(alias + ".marketplace")
    return result


INVALID = "RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_invalid';"


def function(name, args, body, declare=""):
    return f"CREATE FUNCTION public.{name}({args}) RETURNS bytea LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ {declare} BEGIN {body} END $$;"


SCALARS = r'''
CREATE FUNCTION public.review_send_unicode_version() RETURNS text LANGUAGE sql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT '14.0.0'::text $$;
CREATE FUNCTION public.review_send_uuid4(v uuid) RETURNS boolean LANGUAGE sql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(substr(v::text,15,1)='4' AND substr(v::text,20,1) IN ('8','9','a','b'),false) $$;
CREATE FUNCTION public.review_send_answer_id(v bytea) RETURNS boolean LANGUAGE plpgsql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE i integer; characters integer:=0;
BEGIN
 IF v IS NULL OR NOT public.review_local_nonblank_utf8(v) THEN RETURN false; END IF;
 FOR i IN 0..octet_length(v)-1 LOOP
  IF get_byte(v,i)<128 OR get_byte(v,i)>=192 THEN characters:=characters+1; END IF;
 END LOOP;
 RETURN characters<=512;
END $$;
CREATE FUNCTION public.review_send_external_id(v bytea) RETURNS boolean LANGUAGE plpgsql IMMUTABLE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE i integer:=0; b integer; width integer; code integer; numeric_text text:='';
 all_numeric_characters boolean:=true; whitespace boolean;
BEGIN
 IF v IS NULL OR octet_length(v)=0 OR NOT public.review_strict_utf8(v) THEN RETURN false; END IF;
 WHILE i<octet_length(v) LOOP
  b:=get_byte(v,i); width:=CASE WHEN b<128 THEN 1 WHEN b<224 THEN 2 WHEN b<240 THEN 3 ELSE 4 END;
  code:=CASE WHEN b<128 THEN b ELSE ascii(convert_from(substr(v,i+1,width),'UTF8')) END;
  whitespace:=code BETWEEN 9 AND 13 OR code BETWEEN 28 AND 32
   OR code IN (133,160,5760,8232,8233,8239,8287,12288) OR code BETWEEN 8192 AND 8202;
  IF (i=0 OR i+width=octet_length(v)) AND whitespace THEN RETURN false; END IF;
  IF __REVIEW_ND_RANGES__ THEN numeric_text:=numeric_text||'0';
  ELSIF code IN (43,45,46,69,101) THEN numeric_text:=numeric_text||chr(code);
  ELSE all_numeric_characters:=false;
  END IF;
  i:=i+width;
 END LOOP;
 -- The original bytes are returned by the serializer, never this classification
 -- projection. Decimal ranges are literal Unicode14 data, independent of host.
 RETURN NOT (all_numeric_characters AND numeric_text COLLATE "C" ~ '^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$'
  AND numeric_text COLLATE "C" !~ '^[0-9]+$');
END $$;
'''


def codecs():
    request = {**owner("c"), "schemaVersion": literal('"review-send-request-v1"'),
        "operationKind": string("c.operation_kind"), "externalReviewId": "public.review_local_utf8_json_string(external_id)",
        "draftId": uid("c.draft_id"), "draftRevision": number("c.draft_revision"),
        "decisionId": uid("c.decision_id"), "bindingChecksum": digest("c.binding_checksum"),
        "textChecksum": digest("c.text_checksum")}
    yield function("review_send_request_bytes", "c public.review_send_commands,external_id bytea",
        "IF c.operation_kind IS DISTINCT FROM 'review.answer.create.v1' OR c.marketplace NOT IN ('wb','avito') "
        "OR NOT public.review_send_external_id(external_id) THEN " + INVALID + " END IF; RETURN " + obj(request) + ";")
    evidence = {**owner("e"), "evidenceVersion": string("e.evidence_version"), "evidenceKind": string("e.evidence_kind"),
        **{key: uid("e." + col) for key, col in (("evidenceId", "evidence_id"), ("reviewId", "review_id"), ("commandId", "command_id"), ("attemptId", "attempt_id"))},
        "readId": uid("e.read_id", True), "reconciliationStartedAt": stamp("e.reconciliation_started_at", True),
        "observedAt": stamp("e.observed_at"), "outcome": string("e.outcome"),
        "providerAnswerId": "CASE WHEN e.provider_answer_id_utf8 IS NULL THEN convert_to('null','UTF8') ELSE public.review_local_utf8_json_string(e.provider_answer_id_utf8) END",
        "answerChecksum": digest("e.answer_checksum", True), "verifierVersion": "public.review_local_label(e.verifier_version)"}
    yield function("review_send_evidence_bytes", "e public.review_answer_evidence", """
     IF (e.evidence_version='review-answer-evidence-v1' AND e.marketplace IN ('wb','avito')
      AND ((e.evidence_kind='dispatch_ack' AND e.read_id IS NULL AND e.reconciliation_started_at IS NULL)
       OR (e.evidence_kind='reconciliation_read' AND e.read_id IS NOT NULL AND e.reconciliation_started_at IS NOT NULL AND e.reconciliation_started_at<=e.observed_at))
      AND (e.provider_answer_id_utf8 IS NULL OR public.review_send_answer_id(e.provider_answer_id_utf8))
      AND ((e.outcome='incomplete' AND (e.provider_answer_id_utf8 IS NULL OR e.answer_checksum IS NULL))
       OR (e.outcome IN ('exact','different') AND e.provider_answer_id_utf8 IS NOT NULL AND e.answer_checksum IS NOT NULL))) IS NOT TRUE THEN
     """ + INVALID + " END IF; RETURN " + obj(evidence) + ";")
    audit = {**owner("j"), "schemaVersion": literal('"review-audit-v1"'),
        **{key: uid("j." + col, nullable) for key, col, nullable in (
            ("eventId", "event_id", False), ("aggregateId", "aggregate_id", False), ("commandId", "command_id", False),
            ("draftId", "draft_id", True), ("policyId", "policy_id", True), ("decisionId", "decision_id", True), ("attemptId", "attempt_id", True))},
        "aggregateVersion": number("j.aggregate_version"), "eventKind": string("j.event_kind"),
        "occurredAt": stamp("j.occurred_at"), "actorKind": string("j.actor_kind"),
        "actorMembershipId": number("j.actor_membership_id", True), "beforeState": string("j.before_state", True),
        "afterState": string("j.after_state"), "reasonCode": string("j.reason_code", True)}
    yield function("review_send_audit_bytes", "j public.review_send_audit", AUDIT_SHAPE + " RETURN " + obj(audit) + ";")
    enqueue = {**owner("q"), "schemaVersion": literal('"review-enqueue-v1"'),
        "commandId": uid("q.command_id"), "commandVersion": number("q.command_version"), "eventKind": string("q.event_kind")}
    yield function("review_send_enqueue_bytes", "q public.review_send_enqueue_intents",
        "IF q.event_kind IS DISTINCT FROM 'send.ready' OR q.marketplace NOT IN ('wb','avito') THEN " + INVALID + " END IF; RETURN " + obj(enqueue) + ";")
    identity = {**owner("n", False), "contract": literal('"notification-event-v1"'), "scope": string("n.scope"),
        "producer": string("n.producer"), "entityId": uid("n.entity_id"), "sourceVersion": number("n.source_version"), "kind": string("n.kind")}
    yield function("notification_in_app_identity_bytes", "n public.notification_in_app_events", "RETURN " + obj(identity) + ";")
    event = {**owner("n", False), "schemaVersion": literal('"notification-event-v1"'), "eventId": uid("n.event_id"),
        "entityId": uid("n.entity_id"), "sourceVersion": number("n.source_version"), "occurredAt": stamp("n.occurred_at"),
        **{key: string("n." + col) for key, col in (("scope", "scope"), ("producer", "producer"), ("kind", "kind"),
            ("dedupeKey", "dedupe_key"), ("title", "title"), ("details", "details"), ("severity", "severity"))}}
    yield function("notification_in_app_event_bytes", "n public.notification_in_app_events", """
      IF (n.scope='account' AND n.producer='reviews' AND n.kind IN ('approval_required','send_blocked','send_ambiguous')
       AND n.title=CASE n.kind WHEN 'approval_required' THEN 'Ответ на отзыв требует подтверждения'
        WHEN 'send_blocked' THEN 'Отправка ответа заблокирована' ELSE 'Результат отправки ответа требует проверки' END
       AND n.details=CASE n.kind WHEN 'approval_required' THEN 'Проверьте текущую версию черновика в разделе отзывов.'
        WHEN 'send_blocked' THEN 'Проверьте актуальность источника, черновика и разрешений.'
        ELSE 'Повторная отправка заблокирована до проверки результата.' END
       AND n.severity=CASE n.kind WHEN 'approval_required' THEN 'info' ELSE 'warning' END
       AND n.dedupe_key=encode(sha256(public.notification_in_app_identity_bytes(n)),'hex')) IS NOT TRUE THEN
      """ + INVALID + " END IF; RETURN " + obj(event) + ";")
    receipt = {**owner("r", False), "schemaVersion": literal('"notification-in-app-receipt-v1"'),
        "eventId": uid("r.event_id"), "recipientMembershipId": number("r.recipient_membership_id"),
        "readAt": stamp("r.read_at", True), "dismissedAt": stamp("r.dismissed_at", True)}
    yield function("notification_in_app_receipt_bytes", "r public.notification_in_app_receipts",
        "IF r.read_at IS NULL AND r.dismissed_at IS NULL THEN " + INVALID + " END IF; RETURN " + obj(receipt) + ";")
    visible = {"schemaVersion": literal('"notification-visible-action-v1"'), "organizationId": number("org"),
        "marketplaceAccountId": number("account"), "recipientMembershipId": number("member"),
        "eventIds": "encoded_ids", "action": string("action")}
    yield function("notification_in_app_visible_action_bytes", "org integer,account integer,member integer,event_ids uuid[],action text", """
     IF action IS NULL OR action NOT IN ('read','dismiss') OR event_ids IS NULL
      OR cardinality(event_ids)=0 OR array_ndims(event_ids)<>1 OR array_position(event_ids,NULL) IS NOT NULL
      OR cardinality(event_ids)<>(SELECT count(DISTINCT x) FROM unnest(event_ids) x) THEN
     """ + INVALID + """ END IF;
     FOREACH identifier IN ARRAY event_ids LOOP
      IF encoded_ids<>convert_to('[','UTF8') THEN encoded_ids:=encoded_ids||convert_to(',','UTF8'); END IF;
      encoded_ids:=encoded_ids||public.review_local_uuid(identifier);
     END LOOP;
     encoded_ids:=encoded_ids||convert_to(']','UTF8'); RETURN """ + obj(visible) + ";",
        "DECLARE encoded_ids bytea:=convert_to('[','UTF8'); identifier uuid;")


AUDIT_SHAPE = """
 IF (j.marketplace IN ('wb','avito') AND j.command_id=j.aggregate_id AND j.policy_id IS NULL
  AND ((j.actor_kind='membership' AND j.actor_membership_id>0) OR (j.actor_kind='worker' AND j.actor_membership_id IS NULL))
  AND ((j.event_kind IN ('send.created','send.cancelled') AND j.actor_kind='membership')
   OR (j.event_kind IN ('send.sent','send.conflict') AND j.actor_kind IN ('worker','membership'))
   OR (j.event_kind IN ('send.claimed','send.lease_renewed','send.dispatched','send.reclaimed','send.ambiguous','send.blocked') AND j.actor_kind='worker'))
  AND ((j.event_kind='send.created' AND j.aggregate_version=1 AND j.before_state IS NULL AND j.after_state='queued'
    AND j.draft_id IS NOT NULL AND j.decision_id IS NOT NULL AND j.attempt_id IS NULL AND j.reason_code IS NULL)
   OR (j.aggregate_version>=2 AND j.draft_id IS NULL AND j.decision_id IS NULL
    AND ((j.event_kind='send.cancelled' OR (j.event_kind='send.blocked' AND j.before_state='queued'))=(j.attempt_id IS NULL))
    AND ((j.event_kind='send.claimed' AND j.before_state='queued' AND j.after_state='leased' AND j.reason_code IS NULL)
     OR (j.event_kind IN ('send.lease_renewed','send.dispatched') AND j.before_state='leased' AND j.after_state='leased' AND j.reason_code IS NULL)
     OR (j.event_kind='send.reclaimed' AND j.before_state='leased' AND j.after_state='queued' AND j.reason_code='LEASE_EXPIRED_UNDISPATCHED')
     OR (j.event_kind='send.ambiguous' AND j.before_state='leased' AND j.after_state='ambiguous' AND j.reason_code='RESULT_UNKNOWN')
     OR (j.event_kind='send.sent' AND j.before_state IN ('leased','ambiguous') AND j.after_state='sent' AND j.reason_code='VERIFIED_EXACT_ANSWER')
     OR (j.event_kind='send.conflict' AND j.before_state IN ('leased','ambiguous') AND j.after_state='conflict' AND j.reason_code='VERIFIED_DIFFERENT_ANSWER')
     OR (j.event_kind='send.blocked' AND j.before_state IN ('queued','leased') AND j.after_state='blocked'
      AND j.reason_code IN ('ACCESS_REVOKED','SOURCE_CHANGED','POLICY_CHANGED','NOT_ANSWERABLE','CREDENTIAL_UNAVAILABLE'))
     OR (j.event_kind='send.cancelled' AND j.before_state='queued' AND j.after_state='cancelled' AND j.reason_code='USER_CANCELLED'))))) IS NOT TRUE THEN
 """ + INVALID + " END IF;"


ACCOUNT_LOCK = r'''
CREATE FUNCTION public.review_send_account_lock() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE org text:=current_setting('app.organization_id',true);
 account text:=current_setting('app.marketplace_account_id',true);
BEGIN
 IF current_setting('transaction_isolation')<>'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='25000',MESSAGE='review_send_isolation_invalid'; END IF;
 IF org IS NULL OR account IS NULL OR org COLLATE "C" !~ '^[1-9][0-9]{0,9}$'
  OR account COLLATE "C" !~ '^[1-9][0-9]{0,9}$' THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_context_invalid'; END IF;
 IF org::bigint>2147483647 OR account::bigint>2147483647 THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_context_invalid'; END IF;
 -- Shared same-account serialization with 0071 source writes. Auth guards must
 -- already have been acquired by the root service before this statement.
 PERFORM 1 FROM public.marketplace_accounts WHERE organization_id=org::integer
  AND marketplace_account_id=account::integer AND marketplace IN ('wb','avito') FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_account_missing'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') OR (TG_OP='UPDATE' AND TG_TABLE_NAME NOT IN
  ('review_send_commands','review_send_attempts','notification_in_app_receipts')) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_immutable'; END IF;
 RETURN NULL;
END $$;
'''

AUDIT_GUARD = r'''
CREATE FUNCTION public.review_send_audit_guard() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE c public.review_send_commands; a public.review_send_attempts;
 h public.review_send_command_authorities; e public.review_answer_evidence;
 expected_token uuid:=NEW.lease_token;
BEGIN
 SELECT * INTO c FROM public.review_send_commands WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
  AND command_id=NEW.command_id FOR UPDATE;
 NEW.occurred_at:=clock_timestamp();
 IF NEW.event_kind='send.created' THEN
  IF c.command_id IS NOT NULL OR NEW.aggregate_version IS DISTINCT FROM 1::numeric THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
 ELSE
  IF c.command_id IS NULL OR NEW.aggregate_version IS DISTINCT FROM c.version+1
   OR NEW.review_id IS DISTINCT FROM c.review_id OR NEW.before_state IS DISTINCT FROM c.state THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
  IF NEW.event_kind<>'send.claimed' AND NEW.attempt_id IS DISTINCT FROM c.current_attempt_id THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
 END IF;
 -- Only result evidence and the canonical event/actor identity are supplied by
 -- the service. Every physical transition witness below is derived here.
 NEW.current_attempt_id:=NULL; NEW.command_completed_at:=NULL;
 NEW.attempt_sequence:=NULL; NEW.lease_token:=NULL;
 NEW.attempt_before_command_version:=NULL; NEW.attempt_before_state:=NULL;
 NEW.attempt_after_state:=NULL; NEW.lease_before_expires_at:=NULL;
 NEW.lease_after_expires_at:=NULL; NEW.attempt_claimed_at:=NULL;
 NEW.attempt_dispatched_at:=NULL; NEW.attempt_finished_at:=NULL;
 IF NEW.event_kind IN ('send.claimed','send.lease_renewed','send.dispatched') THEN
  SELECT * INTO h FROM public.review_send_command_authorities WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND command_id=NEW.command_id;
  IF h.command_id IS NULL OR h.authority_expires_at<=NEW.occurred_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_authority_invalid'; END IF;
 END IF;
 IF NEW.event_kind='send.claimed' THEN
  IF expected_token IS NOT NULL OR NEW.attempt_id IS NOT NULL THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
  NEW.attempt_id:=gen_random_uuid(); NEW.lease_token:=gen_random_uuid();
  NEW.attempt_sequence:=COALESCE((SELECT max(sequence) FROM public.review_send_attempts
   WHERE organization_id=NEW.organization_id AND marketplace_account_id=NEW.marketplace_account_id
    AND marketplace=NEW.marketplace AND command_id=NEW.command_id),0)+1;
  NEW.attempt_claimed_at:=NEW.occurred_at; NEW.attempt_after_state:='claimed';
  NEW.lease_after_expires_at:=least(NEW.occurred_at+(h.lease_seconds::text||' seconds')::interval,h.authority_expires_at);
 ELSIF NEW.attempt_id IS NOT NULL THEN
  SELECT * INTO a FROM public.review_send_attempts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND command_id=NEW.command_id AND review_id=NEW.review_id AND attempt_id=NEW.attempt_id;
  IF a.attempt_id IS NULL THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
  IF expected_token IS DISTINCT FROM a.lease_token THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
  NEW.attempt_sequence:=a.sequence; NEW.lease_token:=a.lease_token;
  NEW.attempt_before_command_version:=a.command_version; NEW.attempt_before_state:=a.state;
  NEW.attempt_after_state:=a.state; NEW.lease_before_expires_at:=a.lease_expires_at;
  NEW.lease_after_expires_at:=a.lease_expires_at; NEW.attempt_claimed_at:=a.claimed_at;
  NEW.attempt_dispatched_at:=a.dispatched_at; NEW.attempt_finished_at:=a.finished_at;
  IF NEW.event_kind IN ('send.lease_renewed','send.dispatched') THEN
   IF a.state<>'claimed' OR a.dispatched_at IS NOT NULL OR NEW.occurred_at>=a.lease_expires_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
   IF NEW.event_kind='send.lease_renewed' THEN
    NEW.lease_after_expires_at:=least(NEW.occurred_at+(h.lease_seconds::text||' seconds')::interval,h.authority_expires_at);
    IF NEW.lease_after_expires_at<=a.lease_expires_at THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
   ELSE NEW.attempt_after_state:='dispatched'; NEW.attempt_dispatched_at:=NEW.occurred_at;
   END IF;
  ELSIF NEW.event_kind='send.reclaimed' THEN
   IF a.state<>'claimed' OR a.dispatched_at IS NOT NULL OR NEW.occurred_at<a.lease_expires_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
   NEW.attempt_after_state:='abandoned'; NEW.attempt_finished_at:=NEW.occurred_at;
  ELSIF NEW.event_kind='send.blocked' THEN
   IF a.state<>'claimed' OR a.dispatched_at IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
   NEW.attempt_after_state:='blocked'; NEW.attempt_finished_at:=NEW.occurred_at;
  ELSIF NEW.event_kind='send.ambiguous' THEN
   IF a.state<>'dispatched' OR a.dispatched_at IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
   NEW.attempt_after_state:='ambiguous';
  ELSIF NEW.event_kind IN ('send.sent','send.conflict') THEN
   IF a.state NOT IN ('dispatched','ambiguous') OR a.dispatched_at IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
   NEW.attempt_after_state:=NEW.after_state; NEW.attempt_finished_at:=NEW.occurred_at;
  END IF;
 ELSIF expected_token IS NOT NULL THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid';
 END IF;
 IF NEW.event_kind NOT IN ('send.ambiguous','send.sent','send.conflict') AND NEW.result_evidence_id IS NOT NULL THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_evidence_invalid'; END IF;
 IF NEW.result_evidence_id IS NOT NULL THEN
  SELECT * INTO e FROM public.review_answer_evidence WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND review_id=NEW.review_id
   AND command_id=NEW.command_id AND attempt_id=NEW.attempt_id AND evidence_id=NEW.result_evidence_id;
  IF e.evidence_id IS NULL OR e.observed_at>NEW.occurred_at
   OR e.outcome IS DISTINCT FROM (CASE NEW.event_kind WHEN 'send.sent' THEN 'exact'
     WHEN 'send.conflict' THEN 'different' ELSE 'incomplete' END) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_evidence_invalid'; END IF;
 END IF;
 IF NEW.event_kind IN ('send.sent','send.conflict') THEN
  IF e.evidence_id IS NULL OR
   (e.evidence_kind='dispatch_ack' AND (NEW.actor_kind<>'worker' OR c.state<>'leased'
     OR a.state<>'dispatched' OR NEW.occurred_at>=a.lease_expires_at)) OR
   (e.evidence_kind='reconciliation_read' AND NEW.occurred_at<a.lease_expires_at) OR
   (NEW.actor_kind='membership' AND e.evidence_kind<>'reconciliation_read') THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_closing_invalid'; END IF;
 END IF;
 IF NEW.after_state IN ('leased','ambiguous','sent','conflict','blocked') THEN
  NEW.current_attempt_id:=NEW.attempt_id;
 END IF;
 IF NEW.after_state IN ('sent','conflict','blocked','cancelled') THEN NEW.command_completed_at:=NEW.occurred_at; END IF;
 NEW.audit_payload:=public.review_send_audit_bytes(NEW);
 NEW.audit_checksum:=encode(sha256(NEW.audit_payload),'hex');
 RETURN NEW;
EXCEPTION WHEN datetime_field_overflow OR interval_field_overflow OR numeric_value_out_of_range THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_time_invalid';
END $$;
'''


def row_guard():
    immutable = []
    for table, mutable in MUTABLE.items():
        columns = [part.strip().split()[0] for part in (OWNER_COLUMNS + "," + COLUMNS[table]).split(",")]
        ignored = set(mutable.split(","))
        # Receipt version is exclusively assigned by the DB merge below.
        if table == R:
            ignored.add("version")
        columns = [col for col in columns if col not in ignored]
        immutable.append("IF TG_TABLE_NAME='" + table + "' THEN IF ROW(" + ",".join("NEW." + x for x in columns)
            + ") IS DISTINCT FROM ROW(" + ",".join("OLD." + x for x in columns) + ") THEN "
            + "RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_immutable'; END IF; END IF;")
    return r'''
CREATE FUNCTION public.review_send_row_guard() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE j public.review_send_audit; c public.review_send_commands; a public.review_send_attempts;
 d public.review_draft_revisions; q public.review_decisions; f public.review_facts;
 binding public.marketplace_accounts; credential record; login record; moment timestamptz;
BEGIN
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_context_invalid'; END IF;
 IF TG_OP='UPDATE' THEN
''' + "\n".join(immutable) + r'''
 END IF;
 IF TG_TABLE_NAME='notification_in_app_receipts' THEN
  moment:=clock_timestamp();
  IF TG_OP='INSERT' THEN
   IF NEW.read_at IS NOT NULL THEN NEW.read_at:=moment; END IF;
   IF NEW.dismissed_at IS NOT NULL THEN NEW.dismissed_at:=moment; END IF;
   NEW.version:=1;
  ELSE
   IF OLD.read_at IS NOT NULL THEN NEW.read_at:=OLD.read_at;
   ELSIF NEW.read_at IS NOT NULL THEN NEW.read_at:=moment; END IF;
   IF OLD.dismissed_at IS NOT NULL THEN NEW.dismissed_at:=OLD.dismissed_at;
   ELSIF NEW.dismissed_at IS NOT NULL THEN NEW.dismissed_at:=moment; END IF;
   NEW.version:=OLD.version+CASE WHEN ROW(NEW.read_at,NEW.dismissed_at)
    IS DISTINCT FROM ROW(OLD.read_at,OLD.dismissed_at) THEN 1 ELSE 0 END;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='review_send_command_authorities' THEN
  SELECT external_account_id,credential_ref,status INTO binding.external_account_id,binding.credential_ref,binding.status
   FROM public.marketplace_accounts WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace;
  IF NOT FOUND OR ROW(binding.external_account_id,binding.credential_ref,binding.status)
   IS DISTINCT FROM ROW(NEW.expected_external_account_id,NEW.expected_credential_ref,'connected') THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_authority_invalid'; END IF;
  PERFORM public.review_run_binding_bytes(NEW.organization_id,NEW.marketplace_account_id,NEW.marketplace,
   NEW.expected_external_account_id,NEW.expected_credential_ref);
  SELECT organization_id,marketplace_account_id,provider,credential_kind,generation,payload_schema_version,expires_at,revoked_at
   INTO credential FROM public.marketplace_account_credentials WHERE credential_id=NEW.credential_id;
  IF NOT FOUND OR ROW(credential.organization_id,credential.marketplace_account_id,credential.provider,
   credential.credential_kind,credential.generation,credential.payload_schema_version,credential.expires_at,credential.revoked_at)
   IS DISTINCT FROM ROW(NEW.organization_id::bigint,NEW.marketplace_account_id::bigint,NEW.marketplace,
   NEW.credential_kind,NEW.generation,NEW.payload_schema_version,NEW.credential_expires_at,NULL::timestamptz) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_authority_invalid'; END IF;
  SELECT user_id,expires_at,revoked_at INTO login FROM public.lk_sessions WHERE session_id=NEW.origin_session_id;
  IF NOT FOUND OR login.user_id<>NEW.origin_user_id OR login.revoked_at IS NOT NULL
   OR NEW.authority_expires_at>login.expires_at OR NEW.authority_expires_at<=clock_timestamp()
   OR (NEW.credential_expires_at IS NOT NULL AND NEW.authority_expires_at>NEW.credential_expires_at)
   OR NOT EXISTS(SELECT 1 FROM public.lk_users u JOIN public.iam_memberships m
    ON m.user_id=u.user_id AND m.organization_id=u.organization_id
    WHERE u.user_id=NEW.origin_user_id AND u.organization_id=NEW.organization_id AND u.is_active
     AND m.membership_id=NEW.origin_membership_id AND m.is_active) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_authority_invalid'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='review_answer_evidence' THEN
  SELECT * INTO a FROM public.review_send_attempts WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND review_id=NEW.review_id AND command_id=NEW.command_id AND attempt_id=NEW.attempt_id;
  SELECT * INTO c FROM public.review_send_commands WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND review_id=NEW.review_id AND command_id=NEW.command_id;
  IF a.attempt_id IS NULL OR c.command_id IS NULL OR a.dispatched_at IS NULL
   OR NEW.observed_at<a.dispatched_at OR NEW.observed_at>clock_timestamp()
   OR (NEW.evidence_kind='reconciliation_read' AND NEW.reconciliation_started_at<a.dispatched_at)
   OR (NEW.outcome='exact' AND NEW.answer_checksum IS DISTINCT FROM c.text_checksum)
   OR (NEW.outcome='different' AND NEW.answer_checksum IS NOT DISTINCT FROM c.text_checksum) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_evidence_invalid'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='notification_in_app_events' THEN
  IF NEW.occurred_at>clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_source_invalid'; END IF;
  IF NEW.kind='approval_required' THEN
   SELECT * INTO d FROM public.review_draft_revisions WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
    AND review_id=NEW.source_review_id AND draft_id=NEW.source_draft_id AND revision=NEW.source_version;
   IF d.draft_id IS NULL OR NEW.occurred_at<d.created_at THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_source_invalid'; END IF;
  ELSE
   SELECT * INTO j FROM public.review_send_audit WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
    AND review_id=NEW.source_review_id AND command_id=NEW.source_command_id
    AND aggregate_version=NEW.source_version AND event_id=NEW.source_audit_event_id;
   IF j.event_id IS NULL OR NEW.occurred_at<j.occurred_at OR j.event_kind IS DISTINCT FROM
    (CASE NEW.kind WHEN 'send_blocked' THEN 'send.blocked' WHEN 'send_ambiguous' THEN 'send.ambiguous' END) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_source_invalid'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='review_send_enqueue_intents' THEN
  SELECT * INTO j FROM public.review_send_audit WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
   AND command_id=NEW.command_id AND aggregate_version=NEW.command_version AND event_id=NEW.audit_event_id;
  IF j.event_id IS NULL OR j.event_kind NOT IN ('send.created','send.reclaimed') THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_enqueue_invalid'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO j FROM public.review_send_audit WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace
  AND command_id=NEW.command_id AND event_id=NEW.audit_event_id;
 IF j.event_id IS NULL OR j.review_id IS DISTINCT FROM NEW.review_id THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
 IF TG_TABLE_NAME='review_send_commands' THEN
  IF TG_OP='INSERT' THEN
   IF j.event_kind<>'send.created' OR NEW.version IS DISTINCT FROM 1::numeric
    OR ROW(NEW.draft_id,NEW.decision_id,NEW.creator_membership_id)
     IS DISTINCT FROM ROW(j.draft_id,j.decision_id,j.actor_membership_id) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
   NEW.created_at:=j.occurred_at;
   SELECT * INTO d FROM public.review_draft_revisions WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND draft_id=NEW.draft_id;
   SELECT * INTO q FROM public.review_decisions WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND decision_id=NEW.decision_id;
   SELECT * INTO f FROM public.review_facts WHERE organization_id=NEW.organization_id
    AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND review_id=NEW.review_id;
   IF d.draft_id IS NULL OR q.decision_id IS NULL OR f.review_id IS NULL OR q.decision_kind<>'approved'
    OR NEW.created_at<q.decided_at OR NEW.text_checksum IS DISTINCT FROM d.text_checksum
    OR NEW.binding_checksum IS DISTINCT FROM d.binding_checksum
    OR NEW.request_payload IS DISTINCT FROM public.review_send_request_bytes(NEW,COALESCE(f.external_review_id_utf8,convert_to(f.external_review_id,'UTF8'))) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_request_invalid'; END IF;
  ELSIF NEW.version IS DISTINCT FROM OLD.version+1 OR OLD.state IS DISTINCT FROM j.before_state THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid';
  END IF;
  IF ROW(NEW.state,NEW.version,NEW.current_attempt_id,NEW.completed_at,NEW.result_evidence_id,NEW.reason_code)
   IS DISTINCT FROM ROW(j.after_state,j.aggregate_version,j.current_attempt_id,j.command_completed_at,j.result_evidence_id,
    CASE WHEN j.event_kind='send.reclaimed' THEN NULL::text ELSE j.reason_code END) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
 ELSIF TG_TABLE_NAME='review_send_attempts' THEN
  IF TG_OP='INSERT' THEN
   IF j.event_kind<>'send.claimed' OR j.attempt_before_state IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
  ELSIF ROW(OLD.state,OLD.lease_expires_at,OLD.command_version) IS DISTINCT FROM
   ROW(j.attempt_before_state,j.lease_before_expires_at,j.attempt_before_command_version) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_transition_invalid'; END IF;
  IF ROW(NEW.attempt_id,NEW.sequence,NEW.lease_token,NEW.claimed_at,NEW.state,NEW.lease_expires_at,
   NEW.dispatched_at,NEW.finished_at,NEW.result_evidence_id,NEW.reason_code,NEW.command_version)
   IS DISTINCT FROM ROW(j.attempt_id,j.attempt_sequence,j.lease_token,j.attempt_claimed_at,j.attempt_after_state,
   j.lease_after_expires_at,j.attempt_dispatched_at,j.attempt_finished_at,j.result_evidence_id,j.reason_code,j.aggregate_version) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
 END IF;
 RETURN NEW;
END $$;
'''


VALIDATE = r'''
CREATE FUNCTION public.review_send_validate() RETURNS trigger LANGUAGE plpgsql VOLATILE
 SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE c public.review_send_commands; h public.review_send_command_authorities;
 j public.review_send_audit; previous_attempt public.review_send_audit;
 last_event public.review_send_audit; a public.review_send_attempts;
 seq numeric:=0; claims numeric:=0; last_state text; last_time timestamptz;
BEGIN
 -- Deferred callbacks retain NEW's real owner; a later SET LOCAL must not hide
 -- an earlier owner's graph. Maintenance does not silently exempt this check.
 IF NEW.organization_id::text IS DISTINCT FROM current_setting('app.organization_id',true)
  OR NEW.marketplace_account_id::text IS DISTINCT FROM current_setting('app.marketplace_account_id',true) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_context_invalid'; END IF;
 IF TG_TABLE_NAME IN ('notification_in_app_events','notification_in_app_receipts') THEN RETURN NULL; END IF;
 SELECT * INTO c FROM public.review_send_commands WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND command_id=NEW.command_id;
 SELECT * INTO h FROM public.review_send_command_authorities WHERE organization_id=NEW.organization_id
  AND marketplace_account_id=NEW.marketplace_account_id AND marketplace=NEW.marketplace AND command_id=NEW.command_id;
 IF c.command_id IS NULL OR h.command_id IS NULL OR h.origin_membership_id<>c.creator_membership_id
  OR h.authority_expires_at<=c.created_at THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
 FOR j IN SELECT * FROM public.review_send_audit WHERE organization_id=c.organization_id
  AND marketplace_account_id=c.marketplace_account_id AND marketplace=c.marketplace AND command_id=c.command_id
  ORDER BY aggregate_version LOOP
  seq:=seq+1;
  IF j.aggregate_version IS DISTINCT FROM seq OR j.before_state IS DISTINCT FROM last_state
   OR j.review_id IS DISTINCT FROM c.review_id OR (last_time IS NOT NULL AND j.occurred_at<last_time)
   OR j.occurred_at>clock_timestamp() THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_history_invalid'; END IF;
  IF seq=1 AND (j.event_kind<>'send.created' OR ROW(j.draft_id,j.decision_id,j.actor_membership_id,j.occurred_at)
   IS DISTINCT FROM ROW(c.draft_id,c.decision_id,c.creator_membership_id,c.created_at)) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_history_invalid'; END IF;
  IF j.event_kind IN ('send.created','send.reclaimed') AND NOT EXISTS(
   SELECT 1 FROM public.review_send_enqueue_intents q WHERE q.organization_id=c.organization_id
    AND q.marketplace_account_id=c.marketplace_account_id AND q.marketplace=c.marketplace
    AND q.command_id=c.command_id AND q.command_version=j.aggregate_version
    AND q.event_kind='send.ready' AND q.audit_event_id=j.event_id) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_enqueue_invalid'; END IF;
  IF j.attempt_id IS NOT NULL THEN
   SELECT * INTO previous_attempt FROM public.review_send_audit WHERE organization_id=c.organization_id
    AND marketplace_account_id=c.marketplace_account_id AND marketplace=c.marketplace AND command_id=c.command_id
    AND attempt_id=j.attempt_id AND aggregate_version<j.aggregate_version ORDER BY aggregate_version DESC LIMIT 1;
   IF j.event_kind='send.claimed' THEN
    claims:=claims+1;
    IF previous_attempt.event_id IS NOT NULL OR j.attempt_sequence IS DISTINCT FROM claims
     OR j.attempt_before_command_version IS NOT NULL OR j.attempt_before_state IS NOT NULL
     OR j.lease_before_expires_at IS NOT NULL THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_history_invalid'; END IF;
   ELSE
    IF previous_attempt.event_id IS NULL OR
     ROW(j.attempt_before_command_version,j.attempt_before_state,j.lease_before_expires_at,
      j.attempt_sequence,j.lease_token,j.attempt_claimed_at)
     IS DISTINCT FROM ROW(previous_attempt.aggregate_version,previous_attempt.attempt_after_state,
      previous_attempt.lease_after_expires_at,previous_attempt.attempt_sequence,previous_attempt.lease_token,
      previous_attempt.attempt_claimed_at) THEN
     RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_history_invalid'; END IF;
   END IF;
   SELECT * INTO a FROM public.review_send_attempts WHERE organization_id=c.organization_id
    AND marketplace_account_id=c.marketplace_account_id AND marketplace=c.marketplace
    AND command_id=c.command_id AND attempt_id=j.attempt_id;
   IF a.attempt_id IS NULL OR a.command_version<j.aggregate_version THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
   IF a.command_version=j.aggregate_version AND ROW(a.review_id,a.sequence,a.claimed_at,a.lease_token,
    a.state,a.lease_expires_at,a.dispatched_at,a.finished_at,a.result_evidence_id,a.reason_code,a.audit_event_id)
    IS DISTINCT FROM ROW(c.review_id,j.attempt_sequence,j.attempt_claimed_at,j.lease_token,j.attempt_after_state,
     j.lease_after_expires_at,j.attempt_dispatched_at,j.attempt_finished_at,j.result_evidence_id,j.reason_code,j.event_id) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
  END IF;
  last_state:=j.after_state; last_time:=j.occurred_at; last_event:=j;
 END LOOP;
 IF seq=0 OR ROW(c.version,c.state,c.current_attempt_id,c.completed_at,c.result_evidence_id,c.reason_code,c.audit_event_id)
  IS DISTINCT FROM ROW(seq,last_event.after_state,last_event.current_attempt_id,last_event.command_completed_at,
   last_event.result_evidence_id,CASE WHEN last_event.event_kind='send.reclaimed' THEN NULL::text ELSE last_event.reason_code END,last_event.event_id)
  OR claims<>(SELECT count(*)::numeric FROM public.review_send_attempts WHERE organization_id=c.organization_id
   AND marketplace_account_id=c.marketplace_account_id AND marketplace=c.marketplace AND command_id=c.command_id) THEN
  RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_witness_invalid'; END IF;
 -- Historical dispatch/ACK rows may outlive their lease; only the newly written
 -- direct/pre-marker transition gets this commit-time check. Later recovery must
 -- neither renew nor revalidate an old login to reconstruct historical success.
 IF TG_TABLE_NAME='review_send_audit' THEN
  IF NEW.event_kind IN ('send.claimed','send.lease_renewed','send.dispatched')
   AND clock_timestamp()>=NEW.lease_after_expires_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_lease_invalid'; END IF;
  IF NEW.event_kind IN ('send.sent','send.conflict') AND EXISTS(
   SELECT 1 FROM public.review_answer_evidence e WHERE e.organization_id=NEW.organization_id
    AND e.marketplace_account_id=NEW.marketplace_account_id AND e.marketplace=NEW.marketplace
    AND e.evidence_id=NEW.result_evidence_id AND e.evidence_kind='dispatch_ack')
   AND clock_timestamp()>=NEW.lease_after_expires_at THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_send_closing_invalid'; END IF;
 END IF;
 RETURN NULL;
END $$;
'''


def fields(table):
    return [part.strip().split()[0] for part in (OWNER_COLUMNS + "," + COLUMNS[table]).split(",")]


def projection(table):
    return "ROW(" + ",".join(fields(table)) + ")::public." + table


def constraint(table, suffix, body):
    execute(f"ALTER TABLE public.{table} ADD CONSTRAINT rs_{TABLES.index(table)}_{suffix} {body}")


SHAPES = {
    C: """operation_kind='review.answer.create.v1' AND
     CASE state
      WHEN 'queued' THEN current_attempt_id IS NULL AND completed_at IS NULL AND result_evidence_id IS NULL AND reason_code IS NULL
      WHEN 'leased' THEN current_attempt_id IS NOT NULL AND completed_at IS NULL AND result_evidence_id IS NULL AND reason_code IS NULL
      WHEN 'ambiguous' THEN current_attempt_id IS NOT NULL AND completed_at IS NULL AND reason_code='RESULT_UNKNOWN'
      WHEN 'sent' THEN current_attempt_id IS NOT NULL AND completed_at IS NOT NULL AND result_evidence_id IS NOT NULL AND reason_code='VERIFIED_EXACT_ANSWER'
      WHEN 'conflict' THEN current_attempt_id IS NOT NULL AND completed_at IS NOT NULL AND result_evidence_id IS NOT NULL AND reason_code='VERIFIED_DIFFERENT_ANSWER'
      WHEN 'blocked' THEN completed_at IS NOT NULL AND result_evidence_id IS NULL AND reason_code IN ('ACCESS_REVOKED','SOURCE_CHANGED','POLICY_CHANGED','NOT_ANSWERABLE','CREDENTIAL_UNAVAILABLE')
      WHEN 'cancelled' THEN current_attempt_id IS NULL AND completed_at IS NOT NULL AND result_evidence_id IS NULL AND reason_code='USER_CANCELLED'
      ELSE false END AND (completed_at IS NULL OR completed_at>=created_at)""",
    H: """account_binding_schema_version=1 AND payload_schema_version=1 AND generation>0
     AND public.review_local_label(policy_reference) IS NOT NULL
     AND ((marketplace='wb' AND credential_kind='wb_api' AND credential_expires_at IS NULL)
      OR (marketplace='avito' AND credential_kind='avito_oauth_access' AND credential_expires_at IS NOT NULL))""",
    A: """public.review_send_uuid4(attempt_id) AND public.review_send_uuid4(lease_token)
     AND lease_expires_at>claimed_at AND (dispatched_at IS NULL OR dispatched_at>=claimed_at)
     AND (finished_at IS NULL OR finished_at>=COALESCE(dispatched_at,claimed_at)) AND
     CASE state
      WHEN 'claimed' THEN dispatched_at IS NULL AND finished_at IS NULL AND result_evidence_id IS NULL AND reason_code IS NULL
      WHEN 'dispatched' THEN dispatched_at IS NOT NULL AND finished_at IS NULL AND result_evidence_id IS NULL AND reason_code IS NULL
      WHEN 'ambiguous' THEN dispatched_at IS NOT NULL AND finished_at IS NULL AND reason_code='RESULT_UNKNOWN'
      WHEN 'abandoned' THEN dispatched_at IS NULL AND finished_at IS NOT NULL AND result_evidence_id IS NULL AND reason_code='LEASE_EXPIRED_UNDISPATCHED'
      WHEN 'blocked' THEN dispatched_at IS NULL AND finished_at IS NOT NULL AND result_evidence_id IS NULL AND reason_code IN ('ACCESS_REVOKED','SOURCE_CHANGED','POLICY_CHANGED','NOT_ANSWERABLE','CREDENTIAL_UNAVAILABLE')
      WHEN 'sent' THEN dispatched_at IS NOT NULL AND finished_at IS NOT NULL AND result_evidence_id IS NOT NULL AND reason_code='VERIFIED_EXACT_ANSWER'
      WHEN 'conflict' THEN dispatched_at IS NOT NULL AND finished_at IS NOT NULL AND result_evidence_id IS NOT NULL AND reason_code='VERIFIED_DIFFERENT_ANSWER'
      ELSE false END""",
    E: "true",  # Full discriminator/outcome shape is in the exact bytes helper.
    J: """(attempt_id IS NULL AND current_attempt_id IS NULL AND attempt_sequence IS NULL AND lease_token IS NULL
       AND attempt_before_command_version IS NULL AND attempt_before_state IS NULL AND attempt_after_state IS NULL
       AND lease_before_expires_at IS NULL AND lease_after_expires_at IS NULL AND attempt_claimed_at IS NULL
       AND attempt_dispatched_at IS NULL AND attempt_finished_at IS NULL)
      OR (attempt_id IS NOT NULL AND attempt_sequence IS NOT NULL AND public.review_send_uuid4(attempt_id)
       AND public.review_send_uuid4(lease_token) AND lease_after_expires_at IS NOT NULL AND attempt_claimed_at IS NOT NULL
       AND lease_after_expires_at>attempt_claimed_at AND attempt_after_state IS NOT NULL)""",
    Q: "event_kind='send.ready'",
    N: """(kind='approval_required' AND source_draft_id=entity_id AND source_draft_id IS NOT NULL
      AND source_command_id IS NULL AND source_audit_event_id IS NULL)
      OR (kind IN ('send_blocked','send_ambiguous') AND source_command_id=entity_id
       AND source_command_id IS NOT NULL AND source_audit_event_id IS NOT NULL AND source_draft_id IS NULL)""",
    R: "read_at IS NOT NULL OR dismissed_at IS NOT NULL",
}


def constraints():
    for table in TABLES:
        for suffix, columns in KEYS[table]:
            constraint(table, suffix, ("PRIMARY KEY" if suffix == "pk" else "UNIQUE") + "(" + columns + ")")
        checks = ["organization_id>0", "marketplace_account_id>0", "marketplace IN ('wb','avito')"]
        for definition in COLUMNS[table].split(","):
            name, typ = definition.strip().split()[:2]
            if typ == "integer":
                checks.append(f"({name} IS NULL OR {name}>0)")
            if typ == "uuid":
                checks.append(f"({name} IS NULL OR {name}<>'00000000-0000-0000-0000-000000000000'::uuid)")
            if typ == "numeric":
                checks.append(f"({name} IS NULL OR public.review_local_integer_text({name},false) IS NOT NULL)")
            if typ == "timestamptz":
                checks.append(f"({name} IS NULL OR public.review_local_timestamp_text({name}) IS NOT NULL)")
            if name.endswith("checksum") or name == "dedupe_key":
                checks.append(f"({name} IS NULL OR {name} COLLATE \"C\" ~ '^[0-9a-f]{{64}}$')")
        constraint(table, "scalars", "CHECK((" + " AND ".join(checks) + ") IS TRUE)")
        constraint(table, "shape", "CHECK((" + SHAPES[table] + ") IS TRUE)")
    for table, suffix, columns, parent, targets in FKS:
        constraint(table, suffix + "_fk", f"FOREIGN KEY({columns}) REFERENCES public.{parent}({targets}) DEFERRABLE INITIALLY DEFERRED")
    constraint(C, "bytes", "CHECK(public.review_strict_utf8(request_payload) AND request_checksum=encode(sha256(request_payload),'hex'))")
    for table, prefix, helper in ((E, "evidence", "review_send_evidence_bytes"), (J, "audit", "review_send_audit_bytes"),
            (Q, "enqueue", "review_send_enqueue_bytes"), (N, "event", "notification_in_app_event_bytes")):
        constraint(table, "bytes", f"CHECK({prefix}_payload=public.{helper}({projection(table)}) AND {prefix}_checksum=encode(sha256({prefix}_payload),'hex'))")
    execute("CREATE UNIQUE INDEX rs_one_create ON public.review_send_commands(organization_id,marketplace_account_id,marketplace,review_id,operation_kind) WHERE state IN ('queued','leased','ambiguous','sent','conflict')")
    # Longest child lookups first; never create a redundant left-prefix index.
    covered = {table: [cols.split(",") for _, cols in KEYS[table]] for table in TABLES}
    # The partial CREATE lock is deliberately excluded from FK index coverage.
    for i, (table, _, columns, _, _) in sorted(enumerate(FKS), key=lambda item: -len(item[1][2].split(","))):
        parts = columns.split(",")
        if not any(key[:len(parts)] == parts for key in covered[table]):
            execute(f"CREATE INDEX rs_child_{i} ON public.{table}({columns})")
            covered[table].append(parts)
    # Replay proof looks up an attempt's immediately preceding audit event.
    execute("CREATE INDEX rs_audit_attempt ON public.review_send_audit(organization_id,marketplace_account_id,marketplace,command_id,attempt_id,aggregate_version)")


PURE = (
    "review_send_unicode_version()", "review_send_uuid4(uuid)", "review_send_answer_id(bytea)", "review_send_external_id(bytea)",
    "review_send_request_bytes(public.review_send_commands,bytea)",
    "review_send_evidence_bytes(public.review_answer_evidence)",
    "review_send_audit_bytes(public.review_send_audit)",
    "review_send_enqueue_bytes(public.review_send_enqueue_intents)",
    "notification_in_app_identity_bytes(public.notification_in_app_events)",
    "notification_in_app_event_bytes(public.notification_in_app_events)",
    "notification_in_app_receipt_bytes(public.notification_in_app_receipts)",
    "notification_in_app_visible_action_bytes(integer,integer,integer,uuid[],text)",
)
TRIGGERS = ("review_send_account_lock()", "review_send_audit_guard()", "review_send_row_guard()", "review_send_validate()")


def sql_list(values):
    return ",".join("'" + value + "'" for value in values)


def acl():
    return """DO $$ DECLARE t record; x record; a record; f record; target text;
    BEGIN
     FOR t IN SELECT oid,relname,relowner,relacl FROM pg_class WHERE relnamespace='public'::regnamespace
      AND relname IN (""" + sql_list(TABLES) + """) LOOP
      FOR x IN SELECT DISTINCT grantee FROM aclexplode(COALESCE(t.relacl,acldefault('r',t.relowner))) WHERE grantee<>t.relowner LOOP
       target:=CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(x.grantee)) END;
       EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s',t.relname,target);
      END LOOP;
      FOR a IN SELECT at.attname,p.grantee FROM pg_attribute at CROSS JOIN LATERAL aclexplode(at.attacl) p
       WHERE at.attrelid=t.oid AND at.attnum>0 AND NOT at.attisdropped AND p.grantee<>t.relowner LOOP
       target:=CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END;
       EXECUTE format('REVOKE ALL (%I) ON TABLE public.%I FROM %s',a.attname,t.relname,target);
      END LOOP;
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',t.relname);
     END LOOP;
     -- No serial/identity columns or sequences are created in this revision.
     FOR f IN SELECT oid,oid::regprocedure signature,proowner,proacl FROM pg_proc WHERE oid IN (
     """ + ",".join("'public." + signature + "'::regprocedure" for signature in (*PURE, *TRIGGERS)) + """) LOOP
      FOR x IN SELECT DISTINCT grantee FROM aclexplode(COALESCE(f.proacl,acldefault('f',f.proowner))) WHERE grantee<>f.proowner LOOP
       target:=CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(x.grantee)) END;
       EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',f.signature,target);
      END LOOP;
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
     END LOOP;
    END $$;"""


def upgrade():
    ranges = [(first, first + 9) for first in DECIMAL_STARTS] + [(0x1D7CE, 0x1D7FF)]
    decimal_class = " OR ".join(f"code BETWEEN {first} AND {last}" for first, last in ranges)
    execute(SCALARS.replace("__REVIEW_ND_RANGES__", decimal_class))
    execute("COMMENT ON FUNCTION public.review_send_external_id(bytea) IS 'Pinned Unicode14.0.0 decimal category; deploy T4 with matching Unicode data'")
    for table in TABLES:
        columns = COLUMNS[table].replace(" text", ' text COLLATE "C"')
        execute(f"CREATE TABLE public.{table} ({OWNER_COLUMNS},{columns})")
    for sql in codecs():
        execute(sql)
    constraints()
    execute(ACCOUNT_LOCK)
    execute(AUDIT_GUARD)
    execute(row_guard())
    execute(VALIDATE)
    for table in TABLES:
        execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        predicate = "organization_id::text COLLATE \"C\"=current_setting('app.organization_id',true) COLLATE \"C\" AND marketplace_account_id::text COLLATE \"C\"=current_setting('app.marketplace_account_id',true) COLLATE \"C\""
        execute(f"CREATE POLICY review_send_scope ON public.{table} USING ({predicate}) WITH CHECK ({predicate})")
        execute(f"CREATE TRIGGER review_send_account_first BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.review_send_account_lock()")
        guard = "review_send_audit_guard" if table == J else "review_send_row_guard"
        execute(f"CREATE TRIGGER review_send_guard BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{guard}()")
        execute(f"CREATE CONSTRAINT TRIGGER review_send_witness AFTER INSERT OR UPDATE ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.review_send_validate()")
    execute(acl())


def downgrade():
    execute("LOCK TABLE " + ",".join("public." + table for table in sorted(TABLES)) + " IN ACCESS EXCLUSIVE MODE")
    # With FORCE RLS, row_security=off raises for an insufficient maintenance
    # role instead of returning a filtered false-empty result. No owner bypass.
    execute("SET LOCAL row_security=off")
    for table in sorted(TABLES):
        execute(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.{table}) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='review_send_downgrade_nonempty'; END IF; END $$")
    for table in TABLES:
        for trigger in ("review_send_witness", "review_send_guard", "review_send_account_first"):
            execute(f"DROP TRIGGER {trigger} ON public.{table}")
    for signature in reversed(TRIGGERS):
        execute("DROP FUNCTION public." + signature)
    for table, suffix, _, _, _ in FKS:
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT rs_{TABLES.index(table)}_{suffix}_fk")
    for table in TABLES:
        for suffix in ("scalars", "shape"):
            execute(f"ALTER TABLE public.{table} DROP CONSTRAINT rs_{TABLES.index(table)}_{suffix}")
    for table in (C, E, J, Q, N):
        execute(f"ALTER TABLE public.{table} DROP CONSTRAINT rs_{TABLES.index(table)}_bytes")
    for signature in reversed(PURE):
        execute("DROP FUNCTION public." + signature)
    for table in reversed(TABLES):
        execute("DROP TABLE public." + table)
