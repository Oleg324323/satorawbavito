"""Private, explicit0074 bindings. No reflection/create_all/DDL ownership."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, LargeBinary, MetaData, Numeric, SmallInteger, Table, Text, Uuid

_metadata = MetaData()
_types = {"id": Uuid(), "int": Integer(), "big": BigInteger(), "small": SmallInteger(),
          "n": Numeric(), "s": Text(), "b": LargeBinary(), "t": DateTime(timezone=True)}


def _table(name, fields):
    pairs = "organization_id:int marketplace_account_id:int marketplace:s " + fields
    return Table(name, _metadata, *(Column(key, _types[kind])
                 for key, kind in (field.split(":") for field in pairs.split())))


COMMAND = _table("review_send_commands", """
command_id:id review_id:id draft_id:id draft_revision:n decision_id:id operation_kind:s
idempotency_key:id request_payload:b request_checksum:s binding_checksum:s text_checksum:s
creator_membership_id:int created_at:t state:s version:n current_attempt_id:id completed_at:t
result_evidence_id:id reason_code:s audit_event_id:id
""")
AUTHORITY = _table("review_send_command_authorities", """
command_id:id origin_user_id:s origin_membership_id:int origin_session_id:s
account_binding_schema_version:small expected_external_account_id:s expected_credential_ref:s
credential_id:id generation:big credential_kind:s payload_schema_version:small credential_expires_at:t
policy_reference:s policy_version:n lease_seconds:n authority_expires_at:t
""")
ATTEMPT = _table("review_send_attempts", """
attempt_id:id command_id:id review_id:id sequence:n claimed_at:t lease_token:id state:s
lease_expires_at:t dispatched_at:t finished_at:t result_evidence_id:id reason_code:s
command_version:n audit_event_id:id
""")
EVIDENCE = _table("review_answer_evidence", """
evidence_id:id review_id:id command_id:id attempt_id:id read_id:id evidence_kind:s evidence_version:s
outcome:s observed_at:t reconciliation_started_at:t provider_answer_id_utf8:b answer_checksum:s
verifier_version:s evidence_payload:b evidence_checksum:s
""")
AUDIT = _table("review_send_audit", """
event_id:id review_id:id aggregate_id:id aggregate_version:n event_kind:s occurred_at:t actor_kind:s
actor_membership_id:int command_id:id draft_id:id policy_id:id decision_id:id attempt_id:id
before_state:s after_state:s reason_code:s audit_payload:b audit_checksum:s current_attempt_id:id
command_completed_at:t result_evidence_id:id attempt_sequence:n lease_token:id
attempt_before_command_version:n attempt_before_state:s attempt_after_state:s lease_before_expires_at:t
lease_after_expires_at:t attempt_claimed_at:t attempt_dispatched_at:t attempt_finished_at:t
""")
ENQUEUE = _table("review_send_enqueue_intents", """
command_id:id command_version:n event_kind:s audit_event_id:id enqueue_payload:b enqueue_checksum:s
""")
EVENT = _table("notification_in_app_events", """
event_id:id scope:s producer:s entity_id:id source_version:n kind:s occurred_at:t dedupe_key:s
title:s details:s severity:s source_review_id:id source_draft_id:id source_command_id:id
source_audit_event_id:id event_payload:b event_checksum:s
""")
RECEIPT = _table("notification_in_app_receipts", """
event_id:id recipient_membership_id:int read_at:t dismissed_at:t version:n
""")
