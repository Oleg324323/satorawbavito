"""Read-only duplicate-safe ready-intent discovery; no broker or scheduler.

Trusted bootstrap supplies one exact account, dedicated executor factory/identity
and explicit batch limit. Polling may return the same intent repeatedly: the real
claim CAS, not deleting/acknowledging this immutable row, elects execution.
"""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context, set_tenant_context
from app.platform.integrations.publication_guard import _physical_connection
from app.platform.integrations.review_job_contract import ReviewJobError
from app.platform.integrations.worker_identity import (
    ExecutorRoleIdentity,
    verify_executor_login,
)
from app.reviews.send_payloads import encode_review_enqueue
from app.reviews.send_repository import _decoded
from app.reviews.send_tables import COMMAND, ENQUEUE


def scan_ready_commands(*, executor_session_factory, identity: ExecutorRoleIdentity,
                        organization_id: int, marketplace_account_id: int, marketplace: str,
                        limit: int):
    """Return exact existing EncodedStoragePayload tuples; no credentials/content.

    Not a completeness/snapshot API or authority grant. Subsequent polls rescan
    from the oldest current queued rows; no cursor can strand a reclaimed intent.
    The broker consumer must reload real expected state and use the T1 executor.
    """
    if (not callable(executor_session_factory) or type(identity) is not ExecutorRoleIdentity
            or any(type(value) is not int or not 0 < value <= 2**31 - 1
                   for value in (organization_id, marketplace_account_id, limit))
            or type(marketplace) is not str or marketplace not in {"wb", "avito"}):
        raise ReviewJobError("REVIEW_CONTRACT_INVALID")
    try:
        session = executor_session_factory()
    except Exception:  # noqa: BLE001 - trusted factory failures still need a safe boundary.
        raise ReviewJobError("REVIEW_PERSISTENCE_FAILED") from None
    try:
        valid = (isinstance(session, Session) and session.is_active and not session.in_transaction()
                 and not session.in_nested_transaction() and not session.new and not session.dirty and not session.deleted
                 and isinstance(session.get_bind(), Engine) and session.get_bind().dialect.name == "postgresql")
    except Exception:  # noqa: BLE001 - reject foreign/broken root without taking ownership.
        valid = False
    if not valid:
        # Foreign/dirty root is not ours to commit/rollback/close.
        raise ReviewJobError("REVIEW_FENCE_INVALID")
    code, result = None, None
    try:
        session.begin()
        connection = _physical_connection(session)
        root = connection.get_transaction()
        verify_executor_login(session, identity=identity)
        set_tenant_context(session, organization_id)
        set_marketplace_account_context(session, organization_id=organization_id,
                                        marketplace_account_id=marketplace_account_id)
        keys = ("organization_id", "marketplace_account_id", "marketplace", "command_id")
        query = select(ENQUEUE).join(COMMAND,
            (ENQUEUE.c.organization_id == COMMAND.c.organization_id)
            & (ENQUEUE.c.marketplace_account_id == COMMAND.c.marketplace_account_id)
            & (ENQUEUE.c.marketplace == COMMAND.c.marketplace)
            & (ENQUEUE.c.command_id == COMMAND.c.command_id)
            & (ENQUEUE.c.command_version == COMMAND.c.version)).where(
                *(table.c.organization_id == organization_id for table in (ENQUEUE, COMMAND)),
                *(table.c.marketplace_account_id == marketplace_account_id for table in (ENQUEUE, COMMAND)),
                *(table.c.marketplace == marketplace for table in (ENQUEUE, COMMAND)),
                ENQUEUE.c.event_kind == "send.ready", COMMAND.c.state == "queued",
                COMMAND.c.current_attempt_id.is_(None),
            ).order_by(COMMAND.c.created_at, COMMAND.c.command_id).limit(limit)
        rows = session.execute(query).mappings().all()
        values = []
        for row in rows:
            value = _decoded(row, "enqueue", encode_review_enqueue)
            if ((value["organizationId"], value["marketplaceAccountId"], value["marketplace"], value["commandId"])
                    != (row[keys[0]], row[keys[1]], row[keys[2]], str(row[keys[3]]))):
                raise ReviewJobError("REVIEW_FENCE_INVALID")
            if value["commandVersion"] != row["command_version"]:
                raise ReviewJobError("REVIEW_FENCE_INVALID")
            values.append(encode_review_enqueue(value))
        verify_executor_login(session, identity=identity)
        if (_physical_connection(session) is not connection or connection.get_transaction() is not root
                or root is None or not root.is_active):
            raise ReviewJobError("REVIEW_FENCE_INVALID")
        result = tuple(values)
    except ReviewJobError as error:
        code = error.code
    except Exception:  # noqa: BLE001 - stored corruption and SQL errors are not public diagnostics.
        code = "REVIEW_PERSISTENCE_FAILED"
    finally:
        failed = False
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - still attempt close when rollback fails.
            failed = True
        try:
            session.close()
        except Exception:  # noqa: BLE001 - never return rows after uncertain cleanup.
            failed = True
        if failed:
            code = "REVIEW_PERSISTENCE_FAILED"
    if code is not None:
        raise ReviewJobError(code)
    return result
