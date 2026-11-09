"""One-use restoration of a SKU's original inherited configuration.

This deliberately supports only an initially absent configuration. It never
restores prices, audit records, daily counters or a client-supplied snapshot.
"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app import repricer_dispatch_store as ledger
from app.repricer_persistence.orm import WbRepricerExecutionRunRow as Run

FIELDS = ("assignments", "skuSettingsOverrides", "skuMetaOverrides", "liquidationActive")


def _idle(session, org, nm, article, state):
    if any(isinstance(item, dict) and (item.get("articleId") == article or item.get("nmId") == nm)
           and item.get("status", "pending") in {"pending", "applying", "sent", "needs_attention"}
           for item in (state.get("pendingPriceApprovals") or {}).values()):
        raise HTTPException(409, "SKU_CONFIGURATION_PENDING_WORK")
    rows = session.scalars(select(Run).where(Run.organization_id == org,
        Run.trigger == "price_dispatch", Run.report_payload["nmId"].as_integer() == nm)).all()
    if any(row.report_payload.get("state") not in {"confirmed", "rejected", "expired"} for row in rows):
        raise HTTPException(409, "SKU_CONFIGURATION_DISPATCH_UNRESOLVED")


def _state(session, org, article):
    runtime = ledger._lock_runtime(session, org)
    if runtime is None:
        raise HTTPException(409, "REPRICER_RUNTIME_STATE_REQUIRED")
    state = deepcopy(runtime.state_payload)
    algorithm = ledger._lock_pricing_algorithm(session, org)
    return runtime, state, ledger.pricing_revision(algorithm, state, article)


def configuration_state(org, article):
    with ledger.transaction(org=org, write=False) as session:
        _, state, revision = _state(session, org, article)
        return {"pricingRevision": revision, "assignmentRevision": ledger.assignment_revision(state, article),
            "present": {field: article in (state.get(field) or {}) for field in FIELDS}}


def create_checkpoint(*, org, nm, article, binding, actor, expected_revision):
    with ledger.transaction(org=org) as session:
        _, state, revision = _state(session, org, article)
        if revision != expected_revision:
            raise HTTPException(409, "SKU_CONFIGURATION_CHANGED")
        _idle(session, org, nm, article, state)
        if any(article in (state.get(field) or {}) for field in FIELDS):
            raise HTTPException(409, "SKU_CONFIGURATION_NOT_INHERITED")
        key = ledger._key("c", org, article, uuid4().hex)
        ledger._put(session, key, org, {"state": "open", "nmId": nm, "articleId": article,
            "binding": binding, "actor": actor, "createdAt": datetime.now(timezone.utc).isoformat(),
            "initialRevision": revision, "originalPresence": {field: False for field in FIELDS}},
            "sku_configuration_checkpoint")
        return {"checkpointId": key, "pricingRevision": revision, "originalPresence": {field: False for field in FIELDS}}


def restore_checkpoint(*, org, nm, article, binding, actor, checkpoint_id, expected_revision):
    with ledger.transaction(org=org) as session:
        runtime, state, revision = _state(session, org, article)
        row = ledger._row(session, checkpoint_id, org)
        if row is None or row.trigger != "sku_configuration_checkpoint":
            raise HTTPException(404, "SKU_CHECKPOINT_NOT_FOUND")
        receipt = deepcopy(row.report_payload)
        if any(receipt.get(key) != value for key, value in {
            "nmId": nm, "articleId": article, "binding": binding, "actor": actor}.items()):
            raise HTTPException(409, "SKU_CHECKPOINT_SCOPE_CHANGED")
        if receipt.get("state") != "open":
            raise HTTPException(409, "SKU_CHECKPOINT_ALREADY_RESTORED")
        if revision != expected_revision:
            raise HTTPException(409, "SKU_CONFIGURATION_CHANGED")
        _idle(session, org, nm, article, state)
        for field in FIELDS:
            if isinstance(state.get(field), dict):
                state[field].pop(article, None)
        runtime.state_payload = state
        receipt.update(state="restored", restoredAt=datetime.now(timezone.utc).isoformat())
        row.report_payload = receipt
        return {"restored": True, "checkpointId": checkpoint_id,
            "assignmentRevision": ledger.assignment_revision(state, article)}
