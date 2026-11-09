"""Guarded persisted reads; authenticated principal comes from the calling adapter."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.bindings import validate_snapshot_binding
from app.orders.contracts import OrderReadPage
from app.orders.cursor import OrdersCursorCodec
from app.orders.http_contracts import OrdersSavedSnapshotResponse
from app.orders.query import OrdersReadFilters
from app.orders.snapshot_repository import OrdersSnapshotRepository, SnapshotChunk
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)


def discover_saved_orders_snapshot(session, *, principal, accounts):
    """Select an opaque saved view, never assert it represents all source orders."""
    if not isinstance(session, Session) or session.in_transaction():
        raise PublicationGuardError("publication_context_invalid")
    account_ids = tuple(sorted(account.marketplace_account_id for account in accounts))
    try:
        with session.begin():
            guard = acquire_publication_guard(
                session,
                principal=principal,
                required_permissions=frozenset({"cabinet:read"}),
                accounts=accounts,
                authorities=(),
            )
            header = (
                session.execute(
                    text("""SELECT snapshot_id,query_checksum,row_count FROM order_read_snapshots
                WHERE organization_id=:org AND account_scope=:accounts
                AND (expires_at IS NULL OR expires_at>clock_timestamp())
                ORDER BY snapshot_id DESC LIMIT 1"""),
                    {"org": principal.organization_id, "accounts": list(account_ids)},
                )
                .mappings()
                .one_or_none()
            )
            if header is None:
                raise OrderContractValidationError("Published snapshot missing")
            # Reuse stored coverage/scope validation; only one row is decoded.
            chunk = OrdersSnapshotRepository(session, principal.organization_id).read(
                header["snapshot_id"],
                account_ids,
                header["query_checksum"],
                limit=1,
            )
            validate_snapshot_binding(
                chunk.high_water_mark, principal.organization_id, accounts
            )
            result = OrdersSavedSnapshotResponse(
                organization_id=principal.organization_id,
                marketplace_account_ids=account_ids,
                snapshot_id=str(chunk.snapshot_id),
                query_checksum=header["query_checksum"],
                high_water_mark=chunk.high_water_mark,
                published_at=chunk.published_at,
                coverage_state=chunk.coverage_state,
                account_coverage=chunk.account_coverage,
                row_count=str(header["row_count"]),
            )
            guard.revalidate_before_write()
        return result
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None


def read_orders_snapshot(
    session: Session,
    *,
    principal: UserSessionPrincipal,
    accounts: tuple[ExpectedAccountBinding, ...],
    snapshot_id: int | None,
    query_checksum: str,
    after_position: int = 0,
    limit: int = 100,
    filters: OrdersReadFilters | None = None,
) -> SnapshotChunk:
    """Own one clean transaction and return only after final authorization succeeds.

    The caller supplies authenticated session/membership identity and exact account
    bindings, not permissions. No credentials are needed to read persisted facts.
    This is not an HTTP endpoint or a signed cursor parser.
    """
    if not isinstance(session, Session) or session.in_transaction():
        raise PublicationGuardError("publication_context_invalid")
    try:
        with session.begin():
            guard = acquire_publication_guard(
                session,
                principal=principal,
                required_permissions=frozenset({"cabinet:read"}),
                accounts=accounts,
                authorities=(),
            )
            if snapshot_id is None:
                snapshot_id = session.execute(
                    text("""SELECT snapshot_id FROM order_read_snapshots
                    WHERE organization_id=:org AND account_scope=:accounts AND query_checksum=:query
                    AND (expires_at IS NULL OR expires_at>clock_timestamp())
                    ORDER BY snapshot_id DESC LIMIT 1"""),
                    {
                        "org": principal.organization_id,
                        "accounts": sorted(
                            account.marketplace_account_id for account in accounts
                        ),
                        "query": query_checksum,
                    },
                ).scalar_one_or_none()
                if snapshot_id is None:
                    raise OrderContractValidationError("Published snapshot missing")
            result = OrdersSnapshotRepository(session, principal.organization_id).read(
                snapshot_id,
                tuple(sorted(account.marketplace_account_id for account in accounts)),
                query_checksum,
                after_position=after_position,
                limit=limit,
                filters=filters,
            )
            validate_snapshot_binding(
                result.high_water_mark, principal.organization_id, accounts
            )
            guard.revalidate_before_write()
        return result
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None


def read_orders_page(
    session: Session,
    *,
    principal: UserSessionPrincipal,
    accounts: tuple[ExpectedAccountBinding, ...],
    query_checksum: str,
    codec: OrdersCursorCodec,
    snapshot_id: int | None = None,
    cursor: str | None = None,
    latest: bool = False,
    limit: int = 100,
    filters: OrdersReadFilters | None = None,
) -> OrderReadPage:
    """Read an existing immutable snapshot; the token never replaces live access."""
    if (
        type(latest) is not bool
        or (latest and (snapshot_id is not None or cursor is not None))
        or (not latest and (snapshot_id is None) == (cursor is None))
    ):
        raise OrderContractValidationError("orders_snapshot_selection_invalid")
    account_ids = tuple(sorted(account.marketplace_account_id for account in accounts))
    filters = filters or OrdersReadFilters()
    cursor_checksum = filters.cursor_checksum(query_checksum)
    position = 0
    if cursor is not None:
        snapshot_id, position = codec.parse(
            cursor,
            principal=principal,
            accounts=account_ids,
            query_checksum=cursor_checksum,
        )
    chunk = read_orders_snapshot(
        session,
        principal=principal,
        accounts=accounts,
        snapshot_id=snapshot_id,
        query_checksum=query_checksum,
        after_position=position,
        limit=limit,
        filters=filters,
    )
    next_cursor = None
    if chunk.next_position is not None:
        next_cursor = codec.issue(
            principal=principal,
            accounts=account_ids,
            snapshot_id=chunk.snapshot_id,
            after_position=chunk.next_position,
            query_checksum=cursor_checksum,
        )
    return OrderReadPage(
        principal.organization_id,
        account_ids,
        str(chunk.snapshot_id),
        chunk.high_water_mark,
        chunk.published_at,
        chunk.coverage_state,
        chunk.rows,
        next_cursor,
        chunk.account_coverage,
    )
