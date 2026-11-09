"""0075 account-state SQL participant, not authentication or committed service.

Caller owns a live-authorized PostgreSQL root and its final commit/rollback.
No default permission mapping, org settings writer, provider or route wiring.
"""

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context
from app.modules.wb_repricing_assignments import AssignmentChange
from app.modules.wb_repricing_state_commands import (
    LiquidationCampaign, LiquidationChange, LiquidationRevision, LiquidationValues,
    validate_liquidation_transition,
)
from app.platform.integrations.publication_guard import _physical_connection


class RepricingStateError(ValueError):
    def __init__(self, code):
        allowed = ("state_invalid", "state_conflict", "state_persistence_failed",
                   "state_mapping_unresolved", "state_confirmation_policy_required")
        self.code = code if code in allowed else "state_persistence_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AccountStateScope:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int

    def __post_init__(self):
        if any(type(v) is not int or not 0 < v < 2**31 for v in (
            self.organization_id, self.marketplace_account_id, self.catalog_sku_id,
        )):
            raise RepricingStateError("state_invalid")


@dataclass(frozen=True, slots=True)
class StoredStateRevision:
    change: AssignmentChange | LiquidationChange
    revision: int
    audit_id: UUID
    created_at: datetime
    request_payload: bytes
    request_checksum: str


def _number(value):
    if type(value) is not Decimal or not value.is_finite() or value != value.to_integral_value():
        raise RepricingStateError("state_persistence_failed")
    return int(value)


def _campaign_id(value):
    try:
        parsed = UUID(value)
        if type(value) is not str or str(parsed) != value or parsed.version != 4:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise RepricingStateError("state_invalid") from None


class AccountStateTransaction:
    """Bounded assignment/liquidation tuples. Never commit or retry here.

    SQL participant only: membership identity is NOT proof of authorization.
    The future committed service must authorize before construction/replay and
    hold its live guard through commit. On any exception roll back the whole root.
    """

    def __init__(self, session, scope: AccountStateScope, *, max_request_bytes: int):
        if (type(scope) is not AccountStateScope or not isinstance(session, Session)
                or not session.in_transaction() or not session.is_active
                or session.in_nested_transaction() or session.new or session.dirty or session.deleted
                or type(max_request_bytes) is not int or max_request_bytes <= 0):
            raise RepricingStateError("state_invalid")
        scope.__post_init__()
        self._session, self._scope, self._budget = session, scope, max_request_bytes
        self._root = session.get_transaction()
        self._connection = _physical_connection(session)
        self._physical_root = self._connection.get_transaction()
        self._check_root()
        set_marketplace_account_context(session, organization_id=scope.organization_id,
                                        marketplace_account_id=scope.marketplace_account_id)
        self._params = {"org": scope.organization_id, "account": scope.marketplace_account_id,
                        "sku": scope.catalog_sku_id}
        self._predicate = "organization_id=:org AND marketplace_account_id=:account AND catalog_sku_id=:sku"
        found = self._execute("SELECT marketplace_account_id FROM marketplace_accounts "
                              "WHERE organization_id=:org AND marketplace_account_id=:account "
                              "AND marketplace='wb' FOR UPDATE", self._params).scalar_one_or_none()
        if found is None:
            raise RepricingStateError("state_mapping_unresolved")

    def _check_root(self):
        if (self._session.get_transaction() is not self._root or not self._root.is_active
                or not self._session.is_active or self._session.in_nested_transaction()
                or self._session.new or self._session.dirty or self._session.deleted
                or _physical_connection(self._session) is not self._connection
                or self._connection.get_transaction() is not self._physical_root
                or self._connection.get_isolation_level() != "READ COMMITTED"):
            raise RepricingStateError("state_invalid")

    def _execute(self, sql, params):
        self._check_root()
        try:
            return self._session.execute(text(sql), params)
        except SQLAlchemyError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None) if hasattr(exc, "orig") else None
            raise RepricingStateError("state_conflict" if code in ("23505", "40001", "40P01")
                                      else "state_persistence_failed") from None

    def _owner(self, campaign_id):
        if campaign_id is None:
            return "assignment", self._params, self._predicate
        return ("liquidation", {**self._params, "campaign": _campaign_id(campaign_id)},
                self._predicate + " AND campaign_id=:campaign")

    def _bytes(self, change):
        raw = (change.canonical_bytes if type(change) is AssignmentChange
               else change.canonical_bytes(max_bytes=self._budget))
        if len(raw) > self._budget:
            raise RepricingStateError("state_invalid")
        return raw

    def _decode(self, row, family):
        try:
            revision = _number(row["revision"])
            if family == "assignment":
                change = AssignmentChange(row["organization_id"], row["marketplace_account_id"],
                    row["catalog_sku_id"], row["actor_membership_id"], str(row["command_id"]), revision - 1,
                    row["strategy_id"], row["interval_hours"], row["assigned_at"], row["source"])
            else:
                campaign = self._execute("SELECT * FROM wb_repricing_liquidation_campaigns WHERE "
                    + self._predicate + " AND campaign_id=:campaign",
                    {**self._params, "campaign": row["campaign_id"]}).mappings().one()
                origin = LiquidationCampaign(campaign["organization_id"], campaign["marketplace_account_id"],
                    campaign["catalog_sku_id"], str(campaign["campaign_id"]), campaign["started_by_membership_id"],
                    campaign["created_at"], _number(campaign["start_price_kopecks"]))
                values = {f.name: row[f.name] for f in fields(LiquidationValues)}
                for name in ("current_price_kopecks", "target_price_kopecks"):
                    values[name] = _number(values[name])
                change = LiquidationChange(origin, row["actor_membership_id"], str(row["command_id"]),
                                           revision - 1, LiquidationValues(**values))
            raw = bytes(row["request_payload"])
            parent = None if row["parent_revision"] is None else _number(row["parent_revision"])
            if (revision < 1 or parent != (revision - 1 or None) or row["marketplace"] != "wb"
                    or raw != self._bytes(change) or sha256(raw).hexdigest() != row["request_checksum"]
                    or row["created_at"].utcoffset() is None):
                raise ValueError
            return StoredStateRevision(change, revision, row["audit_id"], row["created_at"], raw, row["request_checksum"])
        except (TypeError, ValueError, KeyError, ArithmeticError, AttributeError):
            raise RepricingStateError("state_persistence_failed") from None

    def get_current(self, *, campaign_id=None):
        family, params, predicate = self._owner(campaign_id)
        head = self._execute(f"SELECT * FROM wb_repricing_{family}_heads WHERE " + predicate, params).mappings().one_or_none()
        if head is None:
            return None
        row = self._execute(f"SELECT * FROM wb_repricing_{family}_versions WHERE " + predicate
                            + " AND revision=:revision", {**params, "revision": head["current_revision"]}).mappings().one()
        result = self._decode(row, family)
        if result.revision != _number(head["version"]) or result.created_at != head["updated_at"]:
            raise RepricingStateError("state_persistence_failed")
        return result

    def history(self, *, limit, before_revision=None, campaign_id=None):
        if (type(limit) is not int or not 0 < limit < 2**31 or
                (before_revision is not None and (type(before_revision) is not int or before_revision < 1))):
            raise RepricingStateError("state_invalid")
        family, params, predicate = self._owner(campaign_id)
        params = {**params, "limit": limit}
        if before_revision is not None:
            predicate += " AND revision<:before"
            params["before"] = Decimal(before_revision)
        rows = self._execute(f"SELECT * FROM wb_repricing_{family}_versions WHERE " + predicate
                             + " ORDER BY revision DESC LIMIT :limit", params).mappings().all()
        return tuple(self._decode(row, family) for row in rows)

    def replace(self, change):
        if type(change) not in (AssignmentChange, LiquidationChange):
            raise RepricingStateError("state_invalid")
        change.__post_init__()
        owner = change if type(change) is AssignmentChange else change.campaign
        if AccountStateScope(owner.organization_id, owner.marketplace_account_id, owner.catalog_sku_id) != self._scope:
            raise RepricingStateError("state_invalid")
        if type(change) is LiquidationChange:
            change.campaign.__post_init__()
            change.values.__post_init__()
        campaign_id = change.campaign.campaign_id if type(change) is LiquidationChange else None
        family, params, predicate = self._owner(campaign_id)
        raw = self._bytes(change)
        receipt = self._execute(f"SELECT * FROM wb_repricing_{family}_versions WHERE " + predicate
            + " AND command_id=:command", {**params, "command": change.command_id}).mappings().one_or_none()
        if receipt is not None:
            stored = self._decode(receipt, family)
            if stored.request_payload != raw or stored.change.actor_membership_id != change.actor_membership_id:
                raise RepricingStateError("state_conflict")
            return stored
        # Historical replay above remains valid after mapping/terminal changes.
        mapped = self._execute("SELECT marketplace_offer_id FROM marketplace_offers WHERE "
            + self._predicate + " ORDER BY marketplace_offer_id FOR SHARE", self._params).all()
        if not mapped:
            raise RepricingStateError("state_mapping_unresolved")
        previous = self.get_current(campaign_id=campaign_id)
        if (0 if previous is None else previous.revision) != change.expected_version:
            raise RepricingStateError("state_conflict")
        if family == "liquidation":
            old = None if previous is None else LiquidationRevision(previous.change.campaign, previous.revision, previous.change.values)
            validate_liquidation_transition(old, change)
            confirmation = (change.values.confirmed_by_membership_id, change.values.confirmed_at)
            old_confirmation = (None, None) if old is None else (old.values.confirmed_by_membership_id, old.values.confirmed_at)
            if confirmation != (None, None) and confirmation != old_confirmation:
                raise RepricingStateError("state_confirmation_policy_required")
        now = self._execute("SELECT clock_timestamp()", {}).scalar_one()
        revision, audit = change.expected_version + 1, uuid4()
        params = {**params, "revision": Decimal(revision), "parent": Decimal(change.expected_version) if previous else None,
                  "expected": Decimal(change.expected_version), "command": change.command_id,
                  "actor": change.actor_membership_id, "audit": audit, "now": now,
                  "payload": raw, "checksum": sha256(raw).hexdigest()}
        if family == "assignment":
            values = {key: getattr(change, key) for key in ("strategy_id", "interval_hours", "assigned_at", "source")}
            event = "cleared" if change.strategy_id is None else "created" if previous is None else "replaced"
        else:
            if previous is None:
                c = change.campaign
                self._execute("INSERT INTO wb_repricing_liquidation_campaigns(organization_id,marketplace_account_id,"
                    "catalog_sku_id,marketplace,campaign_id,created_at,started_by_membership_id,start_price_kopecks) "
                    "VALUES(:org,:account,:sku,'wb',:campaign,:created,:starter,:price)",
                    {**params, "created": c.created_at, "starter": c.started_by_membership_id, "price": Decimal(c.start_price_kopecks)})
            values = {f.name: getattr(change.values, f.name) for f in fields(LiquidationValues)}
            for name in ("current_price_kopecks", "target_price_kopecks"):
                values[name] = Decimal(values[name])
            physical = None
            if change.values.resulting_approval_id is not None:
                physical = self._execute("SELECT approval_row_id FROM wb_repricer_price_approvals WHERE "
                    + self._predicate + " AND approval_id=:approval", {**params, "approval": change.values.resulting_approval_id}).scalar_one_or_none()
                if physical is None:
                    raise RepricingStateError("state_invalid")
            values["resulting_approval_row_id"] = physical
            state = change.values.state
            event = ("created" if previous is None else "resumed" if previous.change.values.state == "paused" and state == "active"
                     else "replaced" if state == "active" else "paused" if state == "paused" else state)
        params.update(values)
        owner_columns = "organization_id,marketplace_account_id,catalog_sku_id,marketplace"
        owner_values = ":org,:account,:sku,'wb'"
        if family == "liquidation":
            owner_columns += ",campaign_id"
            owner_values += ",:campaign"
        names = tuple(values)
        self._execute(f"INSERT INTO wb_repricing_{family}_versions(" + owner_columns
            + ",revision,parent_revision,command_id,audit_id,actor_membership_id,created_at,request_payload,request_checksum,"
            + ",".join(names) + ") VALUES(" + owner_values
            + ",:revision,:parent,:command,:audit,:actor,:now,:payload,:checksum,"
            + ",".join(":" + name for name in names) + ")", params)
        head_names = names if family == "liquidation" else ()
        if previous is None:
            self._execute(f"INSERT INTO wb_repricing_{family}_heads(" + owner_columns
                + ",current_revision,version,updated_at" + ("," + ",".join(head_names) if head_names else "")
                + ") VALUES(" + owner_values + ",:revision,:revision,:now"
                + ("," + ",".join(":" + name for name in head_names) if head_names else "") + ")", params)
        else:
            updated = self._execute(f"UPDATE wb_repricing_{family}_heads SET current_revision=:revision,version=:revision,updated_at=:now"
                + ("," + ",".join(name + "=:" + name for name in head_names) if head_names else "")
                + " WHERE " + predicate + " AND version=:expected RETURNING version", params).scalar_one_or_none()
            if updated != Decimal(revision):
                raise RepricingStateError("state_conflict")
        self._execute("INSERT INTO wb_repricing_state_audit(audit_id,organization_id,domain,marketplace_account_id,"
            "catalog_sku_id,marketplace,campaign_id,command_id,actor_membership_id,occurred_at,event_kind,"
            + family + "_before," + family + "_after) VALUES(:audit,:org,:domain,:account,:sku,'wb',:campaign,"
            ":command,:actor,:now,:event,:parent,:revision)", {**params, "campaign": campaign_id, "domain": family, "event": event})
        return StoredStateRevision(change, revision, audit, now, raw, params["checksum"])
