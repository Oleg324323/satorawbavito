"""Explicit runtime column mappings for T1's 0071 contract, never schema bootstrap.

Separate metadata deliberately cannot cause these tables to be installed by an
application create_all. Alembic owns constraints, generated columns and RLS.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    Uuid,
)

_metadata = MetaData()
_types = {"id": Uuid(), "int": Integer(), "small": SmallInteger(), "n": Numeric(),
          "s": Text(), "b": LargeBinary(), "t": DateTime(timezone=True)}


def _table(name, fields):
    # Fixed source literals only; no reflection, user-supplied identifiers or DDL.
    pairs = "organization_id:int marketplace_account_id:int marketplace:s " + fields
    return Table(name, _metadata, *(Column(key, _types[kind])
                 for key, kind in (field.split(":") for field in pairs.split())))


POLICY = _table("review_policy_versions", """
policy_id:id version:n approval_mode:s template_version:s model_version:s
policy_payload:b policy_checksum:s actor_membership_id:int created_at:t audit_event_id:id
""")
POLICY_HEAD = _table("review_policy_heads", """
head_id:id version:n current_policy_id:id current_policy_version:n current_policy_checksum:s updated_at:t
""")
DRAFT = _table("review_draft_revisions", """
review_id:id draft_id:id revision:n source_observation_id:id source_checksum:s
policy_id:id policy_version:n policy_checksum:s text_utf8:b text_checksum:s
binding_payload:b binding_checksum:s generation_id:id generation_mode:s template_version:s model_version:s
generation_started_at:t generation_completed_at:t previous_draft_id:id generation_payload:b generation_checksum:s
policy_head_id:id policy_head_version:n policy_selection_event_id:id actor_membership_id:int created_at:t audit_event_id:id
""")
DECISION = _table("review_decisions", """
review_id:id decision_id:id draft_id:id draft_revision:n binding_checksum:s decision_kind:s
actor_membership_id:int decided_at:t audit_event_id:id
""")
WORKFLOW_HEAD = _table("review_workflow_heads", """
review_id:id head_id:id version:n current_draft_id:id current_draft_revision:n current_decision_id:id updated_at:t
""")
AUDIT = _table("review_local_audit", """
event_id:id aggregate_kind:s aggregate_id:id aggregate_version:n event_kind:s review_id:id policy_id:id
policy_version:n draft_id:id draft_revision:n decision_id:id actor_membership_id:int before_state:s after_state:s
occurred_at:t local_command_id:id audit_payload:b audit_checksum:s
""")
RECEIPT = _table("review_local_command_receipts", """
local_command_id:id actor_membership_id:int operation_kind:s request_payload:b request_checksum:s
account_binding_schema_version:small account_binding_external_account_id:s account_binding_credential_ref:s
account_binding_payload:b account_binding_checksum:s result_payload:b result_checksum:s completed_at:t audit_event_id:id
review_id:id expected_head_version:n expected_draft_revision:n expected_policy_head_id:id expected_policy_head_version:n
result_policy_id:id result_policy_version:n result_draft_id:id result_draft_revision:n result_decision_id:id
result_head_id:id result_head_version:n
""")
