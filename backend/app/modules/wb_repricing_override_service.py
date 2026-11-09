"""Dormant committed SKU override commands; no formula, route or provider wiring.

The trusted caller supplies an authenticated principal, exact binding and resource
budget. Every call owns a fresh physical PostgreSQL root, including final live
authorization. SQL errors never fall back to memory/files or expose raw payloads.
"""

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.wb_repricing_override_access import (
    acquire_override_command_guard,
    acquire_override_read_guard,
    lock_override_mapping,
)
from app.modules.wb_repricing_overrides import (
    OverrideChange,
    OverrideCommandValidationError,
    OverrideValues,
)


class OverrideConflictError(ValueError):
    def __init__(self):
        super().__init__("override_conflict")


class OverridePersistenceError(ValueError):
    def __init__(self):
        super().__init__("override_persistence_failed")


@dataclass(frozen=True, slots=True)
class OverrideScope:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int

    def __post_init__(self):
        if any(
            type(value) is not int or not 0 < value < 2**31
            for value in (
                self.organization_id,
                self.marketplace_account_id,
                self.catalog_sku_id,
            )
        ):
            raise OverrideCommandValidationError()


@dataclass(frozen=True, slots=True)
class OverrideRevision:
    change: OverrideChange
    revision: int
    parent_revision: int | None
    created_at: datetime
    canonical_request_bytes: bytes
    request_checksum: str


def _stored_integer(value):
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value != value.to_integral_value()
    ):
        raise OverridePersistenceError()
    return int(value)


def _decode_revision(row, max_bytes):
    try:
        revision = _stored_integer(row["revision"])
        parent = (
            None
            if row["parent_revision"] is None
            else _stored_integer(row["parent_revision"])
        )
        values = {}
        for field in fields(OverrideValues):
            value = row[field.name]
            values[field.name] = (
                _stored_integer(value)
                if value is not None and field.name.endswith("_kopecks")
                else value
            )
        change = OverrideChange(
            row["organization_id"],
            row["marketplace_account_id"],
            row["catalog_sku_id"],
            row["actor_membership_id"],
            str(row["command_id"]),
            revision - 1,
            OverrideValues(**values),
        )
        canonical = bytes(row["canonical_request_bytes"])
        now = row["created_at"]
        if (
            revision < 1
            or parent != (revision - 1 or None)
            or row["marketplace"] != "wb"
            or type(now) is not datetime
            or now.utcoffset() is None
            or canonical != change.canonical_bytes(max_bytes=max_bytes)
            or sha256(canonical).hexdigest() != row["request_checksum"]
        ):
            raise OverridePersistenceError()
        return OverrideRevision(
            change, revision, parent, now, canonical, row["request_checksum"]
        )
    except (TypeError, ValueError, ArithmeticError, KeyError):
        raise OverridePersistenceError() from None


class SkuOverrideService:
    """Owns physical commit; never accepts a caller's partially committed Session."""

    def __init__(self, engine: Engine, *, max_request_bytes: int):
        if (
            not isinstance(engine, Engine)
            or engine.dialect.name != "postgresql"
            or type(max_request_bytes) is not int
            or max_request_bytes <= 0
        ):
            raise OverrideCommandValidationError()
        self._engine = engine
        self._max_bytes = max_request_bytes

    def _read(self, scope, principal, binding, operation):
        if type(scope) is not OverrideScope:
            raise OverrideCommandValidationError()
        scope.__post_init__()
        params = {
            "org": scope.organization_id,
            "account": scope.marketplace_account_id,
            "sku": scope.catalog_sku_id,
        }
        predicate = "organization_id=:org AND marketplace_account_id=:account AND catalog_sku_id=:sku"
        try:
            with Session(self._engine) as session, session.begin():
                guard = acquire_override_read_guard(
                    session,
                    organization_id=scope.organization_id,
                    marketplace_account_id=scope.marketplace_account_id,
                    principal=principal,
                    binding=binding,
                )
                result = operation(session, params, predicate)
                guard.revalidate_before_write()
            return result
        except SQLAlchemyError:
            raise OverridePersistenceError() from None

    def get_current(self, scope, principal, binding):
        def read(session, params, predicate):
            head = (
                session.execute(
                    text(
                        "SELECT version,current_revision,updated_at FROM wb_repricing_sku_override_heads WHERE "
                        + predicate
                    ),
                    params,
                )
                .mappings()
                .one_or_none()
            )
            if head is None:
                return None
            version = _stored_integer(head["version"])
            if version < 1 or _stored_integer(head["current_revision"]) != version:
                raise OverridePersistenceError()
            row = (
                session.execute(
                    text(
                        "SELECT * FROM wb_repricing_sku_override_versions WHERE "
                        + predicate
                        + " AND revision=:revision"
                    ),
                    {**params, "revision": Decimal(version)},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise OverridePersistenceError()
            result = _decode_revision(row, self._max_bytes)
            if result.created_at != head["updated_at"]:
                raise OverridePersistenceError()
            return result

        return self._read(scope, principal, binding, read)

    def history(self, scope, principal, binding, *, limit, before_revision=None):
        """Explicit bounded page; trusted caller owns the pagination/resource policy."""
        if (
            type(limit) is not int
            or not 0 < limit < 2**31
            or (
                before_revision is not None
                and (type(before_revision) is not int or before_revision < 1)
            )
        ):
            raise OverrideCommandValidationError()

        def read(session, params, predicate):
            params = {**params, "limit": limit}
            if before_revision is not None:
                predicate += " AND revision<:before"
                params["before"] = Decimal(before_revision)
            rows = (
                session.execute(
                    text(
                        "SELECT * FROM wb_repricing_sku_override_versions WHERE "
                        + predicate
                        + " ORDER BY revision DESC LIMIT :limit"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            return tuple(_decode_revision(row, self._max_bytes) for row in rows)

        return self._read(scope, principal, binding, read)

    def replace(self, change, principal, binding, *, now):
        if type(change) is not OverrideChange:
            raise OverrideCommandValidationError()
        change.__post_init__()
        change.values.__post_init__()
        try:
            if type(now) is not datetime or now.utcoffset() is None:
                raise OverrideCommandValidationError()
            now = now.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            raise OverrideCommandValidationError() from None
        canonical = change.canonical_bytes(max_bytes=self._max_bytes)
        checksum = change.checksum(max_bytes=self._max_bytes)
        scope = {
            "org": change.organization_id,
            "account": change.marketplace_account_id,
            "sku": change.catalog_sku_id,
        }
        predicate = "organization_id=:org AND marketplace_account_id=:account AND catalog_sku_id=:sku"
        try:
            with Session(self._engine) as session, session.begin():
                guard = acquire_override_command_guard(
                    session, change=change, principal=principal, binding=binding
                )
                receipt = (
                    session.execute(
                        text(
                            "SELECT * FROM wb_repricing_sku_override_versions WHERE "
                            + predicate
                            + " AND command_id=:command"
                        ),
                        {**scope, "command": change.command_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if receipt is not None:
                    result = _decode_revision(receipt, self._max_bytes)
                    if (
                        result.change.actor_membership_id != change.actor_membership_id
                        or result.canonical_request_bytes != canonical
                    ):
                        raise OverrideConflictError()
                    guard.revalidate_before_write()
                else:
                    result = self._insert(
                        session,
                        guard,
                        change,
                        scope,
                        predicate,
                        now,
                        canonical,
                        checksum,
                    )
            return result
        except SQLAlchemyError:
            raise OverridePersistenceError() from None

    def _insert(
        self, session, guard, change, scope, predicate, now, canonical, checksum
    ):
        lock_override_mapping(session, change=change)
        head = session.execute(
            text(
                "SELECT version FROM wb_repricing_sku_override_heads WHERE "
                + predicate
                + " FOR UPDATE"
            ),
            scope,
        ).scalar_one_or_none()
        if (0 if head is None else head) != change.expected_version:
            raise OverrideConflictError()
        revision = change.expected_version + 1
        params = {
            **scope,
            "revision": Decimal(revision),
            "parent": Decimal(change.expected_version) if head is not None else None,
            "expected": Decimal(change.expected_version),
            "command": change.command_id,
            "actor": change.actor_membership_id,
            "now": now,
            "canonical": canonical,
            "checksum": checksum,
            "event": "created" if head is None else "replaced",
        }
        names = tuple(field.name for field in fields(change.values))
        for name in names:
            value = getattr(change.values, name)
            params[name] = (
                Decimal(value)
                if value is not None and name.endswith("_kopecks")
                else value
            )
        session.execute(
            text(
                "INSERT INTO wb_repricing_sku_override_versions(organization_id,marketplace_account_id,"
                "catalog_sku_id,marketplace,revision,parent_revision,command_id,actor_membership_id,"
                "created_at,canonical_request_bytes,request_checksum,"
                + ",".join(names)
                + ") "
                "VALUES(:org,:account,:sku,'wb',:revision,:parent,:command,:actor,:now,:canonical,:checksum,"
                + ",".join(":" + name for name in names)
                + ")"
            ),
            params,
        )
        if head is None:
            session.execute(
                text(
                    "INSERT INTO wb_repricing_sku_override_heads(organization_id,marketplace_account_id,"
                    "catalog_sku_id,current_revision,version,updated_at) "
                    "VALUES(:org,:account,:sku,:revision,:revision,:now)"
                ),
                params,
            )
        else:
            updated = session.execute(
                text(
                    "UPDATE wb_repricing_sku_override_heads SET current_revision=:revision,"
                    "version=:revision,updated_at=:now WHERE "
                    + predicate
                    + " AND version=:expected RETURNING version"
                ),
                params,
            ).scalar_one_or_none()
            if updated != revision:
                raise OverrideConflictError()
        session.execute(
            text(
                "INSERT INTO wb_repricing_sku_override_audit(organization_id,marketplace_account_id,"
                "catalog_sku_id,before_revision,after_revision,command_id,actor_membership_id,event_kind,occurred_at) "
                "VALUES(:org,:account,:sku,:parent,:revision,:command,:actor,:event,:now)"
            ),
            params,
        )
        guard.revalidate_before_write()
        result = OverrideRevision(
            change,
            revision,
            change.expected_version or None,
            now,
            canonical,
            checksum,
        )
        return result
