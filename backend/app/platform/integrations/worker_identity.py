"""Actual dedicated PostgreSQL login check. No domain permission registry."""
from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.integrations.publication_guard import _physical_connection


class ExecutorIdentityDenied(ValueError):
    def __init__(self):
        super().__init__("executor_identity_denied")


def role_name(value):
    if type(value) is not str or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value) is None:
        raise ExecutorIdentityDenied()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ExecutorRoleIdentity:
    marketplace_executor_role: str | None
    marketplace_api_runtime_role: str | None

    def __repr__(self):
        return "<ExecutorRoleIdentity redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("executor_identity_denied")


def verify_executor_login(session, *, identity: ExecutorRoleIdentity) -> None:
    """Check the actual current connection, never SET ROLE/GUC/queue assertions."""
    try:
        if (type(identity) is not ExecutorRoleIdentity or not isinstance(session, Session)
                or not session.in_transaction() or not session.is_active or session.in_nested_transaction()
                or _physical_connection(session).get_isolation_level() != "READ COMMITTED"):
            raise ExecutorIdentityDenied()
        executor = role_name(identity.marketplace_executor_role)
        api = role_name(identity.marketplace_api_runtime_role)
        if executor == api:
            raise ExecutorIdentityDenied()
        valid = session.scalar(text("""
          SELECT session_user=:executor AND current_user=:executor
           AND e.rolcanlogin AND NOT e.rolsuper AND NOT e.rolcreatedb
           AND NOT e.rolcreaterole AND NOT e.rolinherit AND NOT e.rolreplication AND NOT e.rolbypassrls
           AND NOT a.rolsuper AND NOT a.rolcreaterole AND NOT a.rolbypassrls
           AND NOT pg_has_role(a.oid,e.oid,'MEMBER')
           AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=e.oid OR roleid=e.oid)
           AND NOT EXISTS(SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace AND relowner IN(e.oid,a.oid))
           AND NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public' AND nspowner IN(e.oid,a.oid))
          FROM pg_roles e CROSS JOIN pg_roles a WHERE e.rolname=:executor AND a.rolname=:api
        """), {"executor": executor, "api": api})
        if valid is not True:
            raise ExecutorIdentityDenied()
    except Exception:
        raise ExecutorIdentityDenied() from None
