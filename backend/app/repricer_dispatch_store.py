"""Durable legacy-adapter receipts in the existing repricer execution ledger.

No schema migration, file fallback or broker. The canonical PostgreSQL approval
domain remains unchanged. Insert/CAS claims follow its reserve-before-dispatch
contract; ambiguous attempts never acquire permission for another POST.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.infra.db import get_session_factory, set_tenant_context
from app.repricer_persistence.orm import WbRepricerExecutionRunRow as Row
from app.repricer_price_safety import checksum


class DispatchPersistenceError(RuntimeError):
    pass


class _ClaimConflict(RuntimeError):
    pass


def _key(kind: str, *parts: Any) -> str:
    return f"px{kind}_" + checksum(parts)[:60]


@contextmanager
def transaction(*, org: int | None = None, write=True):
    try:
        try:
            factory = get_session_factory()
        except RuntimeError:
            raise DispatchPersistenceError("REPRICER_DISPATCH_STORAGE_UNAVAILABLE") from None
        with factory() as session:
            if write and session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if org is not None:
                set_tenant_context(session, org)
            yield session
            session.commit()
    except SQLAlchemyError:
        raise DispatchPersistenceError("REPRICER_DISPATCH_STORAGE_UNAVAILABLE") from None


def _row(session, key: str, org: int):
    return session.scalar(select(Row).where(Row.run_id == key, Row.organization_id == org).with_for_update())


def _put(session, key: str, org: int, payload: dict[str, Any], trigger="price_dispatch"):
    row = _row(session, key, org)
    if row is None:
        session.add(Row(run_id=key, organization_id=org, trigger=trigger, report_payload=deepcopy(payload)))
    else:
        row.report_payload = deepcopy(payload)


def _lock_runtime(session, org: int):
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow
    return session.scalar(select(WbRepricerRuntimeStateRow).where(
        WbRepricerRuntimeStateRow.organization_id == org).with_for_update())


def _lock_pricing_algorithm(session, org: int) -> dict:
    from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
    row = session.scalar(select(WbRepricerAlgorithmSettingsRow).where(
        WbRepricerAlgorithmSettingsRow.organization_id == org).with_for_update())
    return row.settings_payload if row else {}


def save_draft(org: int, draft: dict[str, Any]) -> None:
    with transaction(org=org) as session:
        key = _key("f", org, draft["draftId"])
        previous = _row(session, key, org)
        if previous is not None:
            old = previous.report_payload
            # No silent reapproval of a terminal or in-flight immutable draft.
            transitions = {"draft": {"draft", "approved", "blocked"}, "approved": {"approved", "sent", "accepted", "local_applied", "blocked", "needs_attention"},
                           "sent": {"sent", "accepted", "blocked", "needs_attention", "failed"}, "needs_attention": {"needs_attention", "sent", "accepted", "failed"}}
            if old != draft and draft.get("state") not in transitions.get(old.get("state"), set()):
                raise DispatchPersistenceError("PRICE_DRAFT_ALREADY_DISPATCHED")
            if old.get("normalized") != draft.get("normalized") or old.get("bindingKey") != draft.get("bindingKey"):
                raise DispatchPersistenceError("PRICE_DRAFT_IMMUTABLE_INPUTS_CHANGED")
            for field in ("sourceSnapshot", "economics", "organizationId", "nmId", "articleId", "taxRevision", "settingsRevision", "nightWindowKey", "pendingApprovalId"):
                if old.get(field) != draft.get(field):
                    raise DispatchPersistenceError("PRICE_DRAFT_IMMUTABLE_INPUTS_CHANGED")
            if old.get("approvalRef") and old.get("approvalRef") != draft.get("approvalRef"):
                raise DispatchPersistenceError("PRICE_APPROVAL_REF_CONFLICT")
        _put(session, key, org, draft, "price_draft")


def load_draft(org: int, draft_id: str) -> dict[str, Any] | None:
    with transaction(org=org, write=False) as session:
        row = _row(session, _key("f", org, draft_id), org)
        return deepcopy(row.report_payload) if row else None


def claim(org: int, binding: str, drafts: list[dict[str, Any]], *, now: datetime | None = None) -> list[str] | None:
    """Reserve the entire batch atomically; any duplicate/busy product rejects it."""
    keys = {}
    try:
        with transaction(org=org) as session:
            runtime = _lock_runtime(session, org)
            if runtime is None:
                raise DispatchPersistenceError("REPRICER_RUNTIME_STATE_REQUIRED")
            algorithm = _lock_pricing_algorithm(session, org)
            products = set()
            for draft in sorted(drafts, key=lambda x: x["nmId"]):
                _validate_dispatch_revision(session, org, runtime, draft, algorithm=algorithm, now=now)
                nm = draft["nmId"]
                if nm in products or draft.get("organizationId") != org or draft.get("bindingKey") != binding:
                    raise _ClaimConflict()
                products.add(nm)
                persisted = _row(session, _key("f", org, draft["draftId"]), org)
                if persisted is None or persisted.report_payload != draft or draft.get("state") != "approved":
                    raise _ClaimConflict()
                if draft.get("pendingApprovalId"):
                    pending = (runtime.state_payload.get("pendingPriceApprovals") or {}).get(draft["pendingApprovalId"]) if runtime else None
                    if not isinstance(pending, dict) or pending.get("status") != "applying" or pending.get("claimedDraftId") != draft["draftId"]:
                        raise _ClaimConflict()
                identity = draft.get("approvalRef") or draft["draftId"]
                key = _key("i", org, binding, nm, identity)
                if _row(session, key, org) is not None:
                    raise _ClaimConflict()
                product_key = _key("p", org, binding, nm)
                active = _row(session, product_key, org)
                if active:
                    previous = deepcopy(active.report_payload)
                    if previous.get("state") == "reserved":
                        from app.repricer_price_safety import utc_timestamp
                        at = utc_timestamp(previous.get("reservedAt"))
                        if at and datetime.now(timezone.utc) - at > timedelta(minutes=3):
                            # Only an unsubmitted reservation may expire. Its old
                            # worker loses the product CAS and cannot dispatch.
                            previous.update(state="expired", updatedAt=datetime.now(timezone.utc).isoformat())
                            _put(session, previous["intentKey"], org, previous)
                        else:
                            raise _ClaimConflict()
                    elif previous.get("state") not in {"confirmed", "rejected", "expired"}:
                        raise _ClaimConflict()
                    elif previous.get("state") == "confirmed":
                        from app.repricer_price_safety import utc_timestamp
                        confirmed = utc_timestamp(previous.get("updatedAt"))
                        source_at = utc_timestamp((draft.get("sourceSnapshot", {}).get("priceBaseline") or {}).get("observedAt"))
                        prior_price = previous["draft"]["normalized"]["candidateSellerPriceKopecks"]
                        if prior_price != draft["normalized"].get("currentSellerPriceKopecks") and (
                                source_at is None or confirmed and source_at <= confirmed):
                            raise _ClaimConflict()
                if draft.get("nightWindowKey"):
                    attempts = session.scalars(select(Row).where(Row.organization_id == org,
                        Row.trigger == "price_dispatch", Row.run_id.startswith("pxi_"))).all()
                    if any(item.report_payload.get("bindingKey") == binding
                           and item.report_payload.get("nmId") == nm
                           and item.report_payload.get("draft", {}).get("nightWindowKey") == draft["nightWindowKey"]
                           and item.report_payload.get("state") not in {"rejected", "expired"} for item in attempts):
                        raise _ClaimConflict()
                payload = {"state": "reserved", "bindingKey": binding, "nmId": nm, "articleId": draft["articleId"],
                           "draft": draft, "intentKey": key, "reservedAt": datetime.now(timezone.utc).isoformat()}
                _put(session, key, org, payload)
                _put(session, product_key, org, payload)
                _put(session, _key("f", org, draft["draftId"]), org,
                    {**draft, "state": "sent", "dispatchKey": key}, "price_draft")
                keys[draft["draftId"]] = key
            session.flush()
        return [keys[draft["draftId"]] for draft in drafts]
    except (DispatchPersistenceError, _ClaimConflict):
        # Failed insert/CAS never grants a send permission. No memory fallback.
        return None


def transition(org: int, keys: list[str], state: str, *, result: dict[str, Any] | None = None, now: datetime | None = None) -> None:
    with transaction(org=org) as session:
        runtime = _lock_runtime(session, org)
        algorithm = None
        if state == "dispatched":
            if runtime is None:
                raise DispatchPersistenceError("REPRICER_RUNTIME_STATE_REQUIRED")
            algorithm = _lock_pricing_algorithm(session, org)
        changed = []
        for key in keys:
            row = _row(session, key, org)
            if row is None:
                raise DispatchPersistenceError("PRICE_DISPATCH_NOT_RESERVED")
            payload = deepcopy(row.report_payload)
            if state == "dispatched" and payload.get("state") != "reserved":
                raise DispatchPersistenceError("PRICE_DISPATCH_ALREADY_SENT")
            if state == "dispatched":
                _validate_dispatch_revision(session, org, runtime, payload["draft"], algorithm=algorithm, now=now)
            if payload.get("state") in {"confirmed", "rejected"}:
                if payload.get("state") == state:
                    continue
                raise DispatchPersistenceError("PRICE_DISPATCH_ALREADY_FINISHED")
            if payload.get("state") == "expired":
                raise DispatchPersistenceError("PRICE_RESERVATION_EXPIRED")
            payload.update(state=state, updatedAt=datetime.now(timezone.utc).isoformat())
            if result is not None:
                payload["result"] = result
            _put(session, key, org, payload)
            product_key = _key("p", org, payload["bindingKey"], payload["nmId"])
            active = _row(session, product_key, org)
            if active is None or active.report_payload.get("intentKey") != key:
                raise DispatchPersistenceError("PRICE_PRODUCT_CLAIM_CONFLICT")
            _put(session, product_key, org, payload)
            changed.append(key)
        if not changed:
            return
        keys = changed
        if state == "confirmed":
            if result is None or result.get("priceConfirmed") is not True:
                raise DispatchPersistenceError("WB_PRICE_NOT_CONFIRMED")
            goods = result.get("confirmedGoods") or []
            allowed = {session.get(Row, key).report_payload["nmId"] for key in keys}
            if {item.get("nmID") for item in goods} != allowed:
                raise DispatchPersistenceError("CONFIRMED_PRICE_SCOPE_MISMATCH")
            from app.repricer_cache.store import update_confirmed_goods_in_session
            update_confirmed_goods_in_session(session, org, goods, datetime.now(timezone.utc))
            from app.repricer_cache.orm import WbRepricerSourceCacheRow
            from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerChangelogRow
            runtime = session.scalar(select(WbRepricerRuntimeStateRow).where(
                WbRepricerRuntimeStateRow.organization_id == org).with_for_update())
            runtime_payload = deepcopy(runtime.state_payload) if runtime else {}
            for key in keys:
                intent = session.get(Row, key).report_payload
                draft = intent["draft"]
                job_id = f"job_{draft['draftId'].removeprefix('drf_')}"
                stored = _row(session, _key("f", org, draft["draftId"]), org)
                if stored is not None:
                    stored.report_payload = {**stored.report_payload, "state": "accepted", "applyJobId": job_id,
                        "updatedAt": datetime.now(timezone.utc).isoformat()}
                for pending in (runtime_payload.get("pendingPriceApprovals") or {}).values():
                    if isinstance(pending, dict) and (pending.get("draftId") == draft["draftId"] or
                            pending.get("approvalRef") == draft.get("approvalRef")):
                        pending.update(status="applied", applyState="accepted", jobId=job_id, priceConfirmed=True,
                            wbUploadId=result.get("wbUploadId"), updatedAt=datetime.now(timezone.utc).isoformat())
                old = draft["normalized"].get("currentSellerPriceKopecks")
                new = draft["normalized"]["candidateSellerPriceKopecks"]
                entry_id = "confirmed-" + key
                if old and new != old and not session.scalar(select(WbRepricerChangelogRow).where(
                        WbRepricerChangelogRow.organization_id == org, WbRepricerChangelogRow.entry_id == entry_id)):
                    session.add(WbRepricerChangelogRow(organization_id=org, entry_id=entry_id, article_id=draft["articleId"],
                        entry_payload={"id": entry_id, "articleId": draft["articleId"], "skuName": draft["articleId"],
                            "timestamp": datetime.now(timezone.utc).isoformat(), "oldPriceKopecks": old, "newPriceKopecks": new,
                            "changePct": round((new-old)*100/old, 2), "trigger": "night_median_up" if draft.get("nightWindowKey") else "algorithm",
                            "source": "wb_api", "applyMode": "wb_api", "wbMutationSent": True, "priceConfirmed": True,
                            "scope": "sku", "reason": draft.get("reason") or "Confirmed repricer price",
                            "marginAfterPct": draft["normalized"].get("predictedMarginPct"),
                            "actor": {"id": "repricer-engine", "name": "Repricer Engine", "role": "system"}}))
            if runtime:
                runtime.state_payload = runtime_payload
            night = session.scalar(select(WbRepricerSourceCacheRow).where(
                WbRepricerSourceCacheRow.organization_id == org,
                WbRepricerSourceCacheRow.source_key == "night_median_baskets").with_for_update())
            if night is not None:
                cache = deepcopy(night.payload)
                for key in keys:
                    intent = session.get(Row, key).report_payload
                    draft = intent["draft"]
                    window = (cache.get("windows") or {}).get(draft.get("nightWindowKey"))
                    sample = (window.get("samples") or {}).get(draft["articleId"]) if isinstance(window, dict) else None
                    if isinstance(sample, dict) and (sample.get("nmId"), sample.get("organizationId"), sample.get("accountScope")) == (draft["nmId"], org, intent["bindingKey"]):
                        window.setdefault("applied", {})[draft["articleId"]] = {
                            "at": datetime.now(timezone.utc).isoformat(), "nmId": draft["nmId"],
                            "accountScope": intent["bindingKey"], "uploadId": result.get("wbUploadId"),
                            "priceConfirmed": True}
                night.payload = cache
    if state == "confirmed":
        from app.repricer_cache.store import invalidate_confirmed_price_views, _redis_delete, _source_cache_key
        invalidate_confirmed_price_views(org)
        _redis_delete(_source_cache_key(org, "night_median_baskets", slim=False), _source_cache_key(org, "night_median_baskets", slim=True))


def load_receipt(org: int, key: str) -> dict[str, Any] | None:
    with transaction(org=org, write=False) as session:
        row = _row(session, key, org)
        return deepcopy(row.report_payload) if row else None


def daily_reference(org: int, binding: str, nm: int, fallback: int | None, now: datetime) -> int | None:
    """Rolling 24h, matching the existing guard contract; use the first OLD price."""
    from app.repricer_price_safety import utc_timestamp
    with transaction(org=org, write=False) as session:
        rows = session.scalars(select(Row).where(Row.organization_id == org, Row.trigger == "price_dispatch",
            Row.run_id.startswith("pxi_"), Row.report_payload["nmId"].as_integer() == nm)).all()
        points = []
        for row in rows:
            value = row.report_payload
            at = utc_timestamp(value.get("updatedAt"))
            if value.get("bindingKey") == binding and value.get("state") == "confirmed" and at and now - timedelta(hours=24) <= at <= now:
                old = value["draft"]["normalized"].get("currentSellerPriceKopecks")
                if type(old) is int and old > 0:
                    points.append((at, old))
        return min(points)[1] if points else fallback


def mutate_night_cache(org: int, mutation):
    from app.repricer_cache.orm import WbRepricerSourceCacheRow
    with transaction(org=org) as session:
        row = session.scalar(select(WbRepricerSourceCacheRow).where(
            WbRepricerSourceCacheRow.organization_id == org,
            WbRepricerSourceCacheRow.source_key == "night_median_baskets").with_for_update())
        payload = deepcopy(row.payload) if row else {"windows": {}}
        result = mutation(payload)
        if row is None:
            session.add(WbRepricerSourceCacheRow(organization_id=org, source_key="night_median_baskets", payload=payload,
                fetched_at=datetime.now(timezone.utc)))
        else:
            row.payload = payload
            row.fetched_at = datetime.now(timezone.utc)
    from app.repricer_cache.store import _redis_delete, _source_cache_key
    _redis_delete(_source_cache_key(org, "night_median_baskets", slim=False), _source_cache_key(org, "night_median_baskets", slim=True))
    return result


def planning_tax(org: int, now: datetime) -> tuple[float | None, str]:
    from app.platform.economics.wb_quarterly_tax import planning_tax_pct, revision
    from app.platform.period import MOSCOW
    from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
    with transaction(org=org, write=False) as session:
        value = session.get(WbRepricerAlgorithmSettingsRow, org)
        settings = value.settings_payload if value else {}
        day = now.astimezone(MOSCOW).date()
        rate = planning_tax_pct(settings, day)
        return rate, checksum([revision(settings), day.year, (day.month - 1) // 3 + 1])


def pricing_settings_revision(org: int, article: str) -> str:
    """Product-specific settings/assignment guard, not the whole catalog revision."""
    from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow
    with transaction(org=org, write=False) as session:
        algorithm = session.get(WbRepricerAlgorithmSettingsRow, org)
        runtime = session.get(WbRepricerRuntimeStateRow, org)
        return pricing_revision(algorithm.settings_payload if algorithm else {}, runtime.state_payload if runtime else {}, article)


def pricing_revision(algorithm: dict, state: dict, article: str) -> str:
    from app.repricer_bff import DEFAULT_ALGORITHM_SETTINGS
    product = {field: (state.get(field) or {}).get(article) for field in (
        "skuSettingsOverrides", "skuMetaOverrides", "assignments", "liquidationActive")}
    return checksum([{**DEFAULT_ALGORITHM_SETTINGS, **algorithm}, product])


def assignment_revision(state: dict, article: str) -> str:
    meta = (state.get("skuMetaOverrides") or {}).get(article) or {}
    settings = (state.get("skuSettingsOverrides") or {}).get(article) or {}
    return checksum([(state.get("assignments") or {}).get(article),
        {key: meta.get(key) for key in ("status", "assignmentSource", "assignedAt", "warmupDaysLeft")},
        settings.get("automationEnabled")])


def mutate_assignments(org: int, article_ids: list[str], mutation, *, expected_revisions=None):
    """Product-only mutation under the same durable lock as dispatch start."""
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow
    from fastapi import HTTPException
    with transaction(org=org) as session:
        runtime = _lock_runtime(session, org)
        state = deepcopy(runtime.state_payload) if runtime else {}
        if expected_revisions is not None and any(
            expected_revisions.get(article) != assignment_revision(state, article) for article in article_ids
        ):
            raise HTTPException(status_code=409, detail={"code": "STRATEGY_ASSIGNMENT_CHANGED",
                "message": "Стратегия изменена в другом окне. Данные обновлены; повторите выбор."})
        from app.repricer_settings import organization_algorithm_context
        with organization_algorithm_context(org, _lock_pricing_algorithm(session, org)):
            result = mutation(state)
        for pending in (state.get("pendingPriceApprovals") or {}).values():
            if isinstance(pending, dict) and pending.get("articleId") in article_ids and pending.get("status") == "pending":
                pending.update(status="rejected", applyState="pricing_settings_changed_reapproval_required",
                    updatedAt=datetime.now(timezone.utc).isoformat())
        if runtime is None:
            session.add(WbRepricerRuntimeStateRow(organization_id=org, state_payload=state))
        else:
            runtime.state_payload = state
        result["assignmentRevisions"] = {article: assignment_revision(state, article) for article in article_ids}
    return result


def _validate_dispatch_revision(session, org: int, runtime, draft: dict, *, algorithm: dict, now: datetime | None = None) -> None:
    from app.repricer_bff import _frontend_strategy_definition
    state = runtime.state_payload if runtime else {}
    from app.repricer_price_safety import utc_timestamp
    current_time = now or datetime.now(timezone.utc)
    if any(metric.get("sourceStatus") in {"stale", "blocked"}
           or (expiry := utc_timestamp(metric.get("staleAfter"))) is None or expiry <= current_time
           for metric in (draft.get("sourceSnapshot") or {}).get("metrics", [])):
        raise DispatchPersistenceError("PRICE_DRAFT_SOURCE_EXPIRED")
    current = pricing_revision(algorithm, state, draft["articleId"])
    if draft.get("settingsRevision") != current:
        raise DispatchPersistenceError("pricing_settings_changed_reapproval_required")
    assigned = ((state.get("assignments") or {}).get(draft["articleId"]) or {}).get("strategyId")
    if assigned:
        from fastapi import HTTPException
        try:
            definition = _frontend_strategy_definition(assigned)
        except HTTPException:
            raise DispatchPersistenceError("strategy_unknown_or_discovery_required") from None
        if definition.get("status") == "disabled":
            raise DispatchPersistenceError("strategy_disabled")
    if draft.get("nightWindowKey"):
        settings = ((state.get("skuSettingsOverrides") or {}).get(draft["articleId"]) or {})
        meta = ((state.get("skuMetaOverrides") or {}).get(draft["articleId"]) or {})
        if settings.get("automationEnabled") is False or meta.get("assignmentSource") == "none" or meta.get("status") in {"manual", "warmup"}:
            raise DispatchPersistenceError("night_strategy_no_longer_eligible")


def mutate_pending(org: int, mutation):
    """Atomic runtime JSON mutation; decisions never use file/memory fallback."""
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow
    with transaction(org=org) as session:
        row = session.scalar(select(WbRepricerRuntimeStateRow).where(
            WbRepricerRuntimeStateRow.organization_id == org).with_for_update())
        state = deepcopy(row.state_payload) if row else {}
        pending = state.setdefault("pendingPriceApprovals", {})
        _recover_decision_leases(session, org, pending)
        result = mutation(pending)
        if row is None:
            session.add(WbRepricerRuntimeStateRow(organization_id=org, state_payload=state))
        elif row.state_payload != state:
            row.state_payload = state
    return result


def _recover_decision_leases(session, org: int, pending: dict) -> None:
    """Recover preparation crashes, never a dispatched/uncertain price write."""
    from app.repricer_price_safety import utc_timestamp
    for item in pending.values():
        if not isinstance(item, dict) or item.get("status") != "applying" or not item.get("claimedDraftId"):
            continue
        at = utc_timestamp(item.get("claimedAt"))
        if at is None or datetime.now(timezone.utc) - at <= timedelta(minutes=3):
            continue
        intents = session.scalars(select(Row).where(Row.organization_id == org, Row.trigger == "price_dispatch",
            Row.run_id.startswith("pxi_"))).all()
        matches = [entry for entry in intents if entry.report_payload.get("draft", {}).get("draftId") == item["claimedDraftId"]]
        if any(entry.report_payload.get("state") not in {"reserved", "rejected", "expired"} for entry in matches):
            continue
        for entry in matches:
            if entry.report_payload.get("state") == "reserved":
                value = {**entry.report_payload, "state": "expired", "updatedAt": datetime.now(timezone.utc).isoformat()}
                entry.report_payload = value
                active = _row(session, _key("p", org, value["bindingKey"], value["nmId"]), org)
                if active and active.report_payload.get("intentKey") == entry.run_id:
                    active.report_payload = deepcopy(value)
        item.update(status="pending", claimedDraftId=None, claimedAt=None,
            recoveryReason="unsubmitted_approval_reservation_expired", updatedAt=datetime.now(timezone.utc).isoformat())


def claim_pending(org: int, approval_ids: list[str], *, decision="applying", drafts: dict[str, str] | None = None) -> set[str]:
    if decision not in {"applying", "rejected"}:
        raise ValueError("INVALID_PRICE_APPROVAL_DECISION")

    def mutate(pending):
        claimed = set()
        for approval_id in approval_ids:
            item = pending.get(approval_id)
            if not isinstance(item, dict) or item.get("status", "pending") != "pending":
                continue
            item.update(status=decision, updatedAt=datetime.now(timezone.utc).isoformat())
            if decision == "applying" and drafts and approval_id in drafts:
                item.update(claimedDraftId=drafts[approval_id], claimedAt=datetime.now(timezone.utc).isoformat())
            claimed.add(approval_id)
        return claimed
    return mutate_pending(org, mutate)
