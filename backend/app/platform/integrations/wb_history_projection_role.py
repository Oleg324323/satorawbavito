"""Explicit dormant role contract; identities are configuration, not authority."""
import json
from dataclasses import dataclass

from sqlalchemy import text

from app.platform.integrations.user_orders_job_contract import OrdersJobError


@dataclass(frozen=True, slots=True, repr=False)
class HistoryProjectionRoleIdentity:
    runtime_oid: int
    runtime_name: str
    helper_owner_oid: int
    helper_owner_name: str

    def __post_init__(self):
        for oid in (self.runtime_oid, self.helper_owner_oid):
            if type(oid) is not int or not 1 <= oid < 2**32:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
        for name in (self.runtime_name, self.helper_owner_name):
            if (type(name) is not str or not name or "\x00" in name
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in name) or len(name.encode("utf-8")) > 63):
                raise OrdersJobError("JOB_CONTRACT_INVALID")
        if self.runtime_oid == self.helper_owner_oid or self.runtime_name == self.helper_owner_name:
            raise OrdersJobError("JOB_CONTRACT_INVALID")

    def __repr__(self):
        return "<HistoryProjectionRoleIdentity redacted>"


def _validate_role_identity(identity, rows, *, session_oid, current_oid):
    """Catalog facts only; effective ACL inspection is also required for authority."""
    if type(identity) is not HistoryProjectionRoleIdentity:
        raise OrdersJobError("JOB_FENCE_INVALID")
    actual = {row["oid"]: row for row in rows}
    if (len(rows) != 2 or len(actual) != 2 or actual.keys() != {identity.runtime_oid, identity.helper_owner_oid}
            or session_oid != identity.runtime_oid or current_oid != identity.runtime_oid):
        raise OrdersJobError("JOB_FENCE_INVALID")
    for oid, name, login in ((identity.runtime_oid, identity.runtime_name, True),
            (identity.helper_owner_oid, identity.helper_owner_name, False)):
        row = actual[oid]
        if (row["rolname"] != name or row["rolcanlogin"] is not login
                or any(row[field] is not False for field in ("rolsuper", "rolbypassrls", "rolcreaterole",
                    "rolcreatedb", "rolinherit", "rolreplication"))):
            raise OrdersJobError("JOB_FENCE_INVALID")


_AUTH_READ = {
    "lk_users": "user_id organization_id is_active",
    "lk_sessions": "session_id user_id expires_at revoked_at",
    "iam_memberships": "membership_id organization_id user_id is_active role permissions scope_mode allowed_account_ids",
    "marketplace_accounts": "marketplace_account_id organization_id marketplace external_account_id credential_ref status ingestion_binding_version",
    "marketplace_account_credentials": "credential_id organization_id marketplace_account_id provider credential_kind generation payload_schema_version expires_at revoked_at",
}
_JOB_TABLES = ["user_orders_jobs", "user_orders_job_authorities", "user_orders_job_attempts", "user_orders_job_audit", "user_orders_history_selection_pages"]
_ORDER_INSERT = ["order_sync_runs", "order_observations", "order_sync_memberships", "order_sync_coverage", "order_lifecycle_events", "order_status_observations", "marketplace_order_items"]
_CATALOG = ["marketplace_products", "marketplace_offers", "catalog_skus"]
_HELPER_UPDATE = ["raw_status", "canonical_status", "mapping_state", "mapping_version", "source_updated_at", "last_seen_sync_run_id", "version", "updated_at"]
_ACL = {"runtime": {}, "helper": {}}
for table, columns in _AUTH_READ.items():
    _ACL["runtime"][table] = {"SELECT": columns.split(), "UPDATE": ["last_seen_at" if table == "lk_sessions" else "updated_at"]}
for table in _JOB_TABLES + _ORDER_INSERT:
    _ACL["runtime"][table] = {"SELECT": ["*"], "INSERT": ["*"]}
for table, columns in (("marketplace_products", "organization_id marketplace_account_id marketplace_product_id external_product_id updated_at"),
        ("marketplace_offers", "organization_id marketplace_account_id marketplace_product_id marketplace_offer_id external_offer_key catalog_sku_id updated_at"),
        ("catalog_skus", "organization_id catalog_sku_id updated_at")):
    _ACL["runtime"][table] = {"SELECT": columns.split(), "UPDATE": ["updated_at"]}
for table in ["wb_live_history_requests", "wb_live_history_pages", "wb_live_history_rows"]:
    _ACL["runtime"][table] = {"SELECT": ["*"]}
_ACL["runtime"]["user_orders_jobs"]["UPDATE"] = ["state", "version", "attempt_count", "current_attempt_id", "next_attempt_at", "completed_at", "safe_reason", "result_sync_run_id", "result_coverage_state", "history_cursor_page", "history_cursor_ordinal", "history_progress_version"]
_ACL["runtime"]["user_orders_job_attempts"]["UPDATE"] = ["state", "version", "job_version_after", "lease_expires_at", "finished_at", "safe_reason", "result_sync_run_id", "result_coverage_state"]
_ACL["runtime"]["marketplace_orders"] = {"SELECT": ["*"], "UPDATE": ["order_id"],
    "INSERT": ["organization_id", "marketplace_account_id", "marketplace", "external_order_id"]}
_ACL["runtime"]["order_sync_runs"]["UPDATE"] = ["*"]
_ACL["runtime"]["marketplace_order_items"]["UPDATE"] = ["order_item_id"]
_ACL["runtime"]["lk_audit_events"] = {"SELECT": ["event_id", "created_at"],
    "INSERT": ["organization_id", "actor_user_id", "action", "object_type", "object_id", "details", "before_state", "after_state", "reason", "ip_address", "user_agent"]}
for table in ["marketplace_accounts", "marketplace_orders", "order_sync_runs", "order_sync_memberships", "order_observations"]:
    _ACL["helper"][table] = {"SELECT": ["*"]}
_ACL["helper"]["marketplace_accounts"]["UPDATE"] = ["updated_at"]
_ACL["helper"]["marketplace_orders"]["UPDATE"] = _HELPER_UPDATE
_SEQUENCES = tuple((table, column) for table, column in (
    ("order_sync_runs", "sync_run_id"), ("marketplace_orders", "order_id"),
    ("marketplace_order_items", "order_item_id"), ("order_observations", "observation_id"),
    ("order_sync_memberships", "membership_id"), ("order_sync_coverage", "coverage_id"),
    ("order_lifecycle_events", "lifecycle_event_id"), ("order_status_observations", "status_observation_id"),
    ("lk_audit_events", "event_id")))


def _allowed_column(role, table, column, privilege):
    columns = _ACL.get(role, {}).get(table, {}).get(privilege, ())
    return "*" in columns or column in columns


def inspect_projection_role(session, identity):
    """Fresh catalog admission; no credential values, role discovery or mutation."""
    if type(identity) is not HistoryProjectionRoleIdentity:
        raise OrdersJobError("JOB_FENCE_INVALID")
    values = {"runtime": identity.runtime_oid, "helper": identity.helper_owner_oid}
    rows = session.execute(text("SELECT oid,rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,"
        "rolinherit,rolreplication FROM pg_catalog.pg_roles WHERE oid IN (:runtime,:helper)"), values).mappings().all()
    actual = session.execute(text("SELECT session_user::regrole::oid,current_user::regrole::oid")).one()
    _validate_role_identity(identity, rows, session_oid=actual[0], current_oid=actual[1])
    helper = session.execute(text("SELECT oid,proowner,prosecdef,proconfig FROM pg_catalog.pg_proc WHERE oid="
        "to_regprocedure('public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)')")).one_or_none()
    if (helper is None or helper.proowner != identity.helper_owner_oid or not helper.prosecdef
            or helper.proconfig != ["search_path=pg_catalog, public"]):
        raise OrdersJobError("JOB_FENCE_INVALID")
    values["routine"] = helper.oid
    unsafe = session.scalar(text("""SELECT
 EXISTS(SELECT FROM pg_catalog.pg_auth_members WHERE member IN (:runtime,:helper) OR roleid IN (:runtime,:helper))
 OR EXISTS(SELECT FROM pg_catalog.pg_class WHERE relowner IN (:runtime,:helper))
 OR EXISTS(SELECT FROM pg_catalog.pg_namespace WHERE nspowner IN (:runtime,:helper))
 OR EXISTS(SELECT FROM pg_catalog.pg_database WHERE datdba IN (:runtime,:helper))
 OR EXISTS(SELECT FROM pg_catalog.pg_proc WHERE proowner=:runtime OR (proowner=:helper AND oid<>:routine))
 OR EXISTS(SELECT FROM pg_catalog.pg_namespace n WHERE n.nspname NOT LIKE 'pg_%' AND
   (has_schema_privilege(:runtime,n.oid,'CREATE') OR has_schema_privilege(:helper,n.oid,'CREATE')))
 OR has_database_privilege(:runtime,current_database(),'CREATE') OR has_database_privilege(:helper,current_database(),'CREATE')
 OR EXISTS(SELECT FROM pg_catalog.pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a
   WHERE a.grantee IN (0,:runtime,:helper))
 OR EXISTS(SELECT FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosecdef AND p.oid<>:routine
   AND (has_function_privilege(:runtime,p.oid,'EXECUTE') OR has_function_privilege(:helper,p.oid,'EXECUTE')))
 OR EXISTS(SELECT FROM pg_catalog.pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
   WHERE p.oid=:routine AND (a.grantee NOT IN (:runtime,:helper) OR (a.grantee=:runtime AND a.is_grantable)))
 OR EXISTS(SELECT FROM pg_catalog.pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
   WHERE a.grantee IN (:runtime,:helper) AND a.is_grantable)
 OR EXISTS(SELECT FROM pg_catalog.pg_attribute c CROSS JOIN LATERAL aclexplode(c.attacl) a
   WHERE a.grantee IN (:runtime,:helper) AND a.is_grantable)
 OR NOT has_function_privilege(:runtime,:routine,'EXECUTE')"""), values)
    if unsafe:
        raise OrdersJobError("JOB_FENCE_INVALID")
    required_rls = sorted(set(_ACL["runtime"]) - {"lk_users", "lk_sessions", "lk_audit_events"})
    if session.scalar(text("SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND c.relrowsecurity AND c.relforcerowsecurity"),
        {"tables": required_rls}) != len(required_rls):
        raise OrdersJobError("JOB_FENCE_INVALID")
    for kind, oid in (("runtime", identity.runtime_oid), ("helper", identity.helper_owner_oid)):
        args = {"role": oid, "acl": json.dumps(_ACL[kind])}
        if session.scalar(text("""SELECT EXISTS(SELECT FROM pg_catalog.pg_class c
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
 CROSS JOIN (VALUES('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) privileges(name)
 WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','f')
 AND a.attnum>0 AND NOT a.attisdropped AND has_column_privilege(:role,c.oid,a.attnum,privileges.name)
 AND NOT coalesce(n.nspname='public' AND ((CAST(:acl AS jsonb)->c.relname->privileges.name ? '*')
  OR (CAST(:acl AS jsonb)->c.relname->privileges.name ? a.attname)),false))
 OR EXISTS(SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','f')
 AND has_table_privilege(:role,c.oid,'DELETE,TRUNCATE,TRIGGER'))"""), args):
            raise OrdersJobError("JOB_FENCE_INVALID")
        sequences = [] if kind == "helper" else [session.scalar(text("SELECT pg_get_serial_sequence(:table,:column)::regclass::oid"),
            {"table": "public." + table, "column": column}) for table, column in _SEQUENCES]
        if session.scalar(text("""SELECT EXISTS(SELECT FROM pg_catalog.pg_class c
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S'
 AND n.nspname NOT IN ('pg_catalog','information_schema') AND
 (has_sequence_privilege(:role,c.oid,'UPDATE') OR (has_sequence_privilege(:role,c.oid,'USAGE,SELECT')
 AND NOT c.oid=ANY(CAST(:sequences AS oid[])))))"""), {"role": oid, "sequences": sequences}):
            raise OrdersJobError("JOB_FENCE_INVALID")
