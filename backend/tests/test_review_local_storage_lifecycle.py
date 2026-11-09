"""Trusted SQL transaction participant tests; never live-session/auth proof."""
# Separate contexts deliberately expose the physical commit failure boundary.
# ruff: noqa: SIM117

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as facts
from tests import test_review_local_storage_schema as storage
from tests import test_review_run_binding_migration as binding
from tests.test_review_local_storage_codec import (
    composite,
    json_row,
    literal_generation,
    literal_policy,
)
from tests.test_review_local_storage_schema import HEADS, TABLES, scope
from tests.test_review_lossless_migration import unit

cluster = candidate.cluster
db = storage.db
AT = "2026-09-09T12:00:02.987654Z"
OWNER = dict(facts.OWNER)


class Conflict(Exception):
    """Synthetic participant conflict, not an HTTP or authorization error."""


class InjectedFailure(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def intent(operation, data, *, actor=77, key=None):
    return {"schemaVersion": "review-local-command-v1", "organizationId": 91001,
            "marketplaceAccountId": 91101, "marketplace": "avito",
            "localCommandId": str(key or uuid4()), "actorMembershipId": actor,
            "operationKind": "review." + operation + ".v1", "input": data}


def lookup(c, table, *, owner=OWNER, **keys):
    values = dict(owner, **keys)
    return c.execute(text(f"SELECT * FROM {table} WHERE " + " AND ".join(f"{k}=:{k}" for k in values)),
                     values).mappings().one_or_none()


def execute(c, request, *, fail_after=None, transform=None):
    """Caller owns root/commit; exact account fence, fresh receipt first, then writes.

    Synthetic authority is explicit. This participant exercises physical storage;
    it is not a service implementation or UserSessionPrincipal substitute.
    """
    org, account, provider = (request[k] for k in ("organizationId", "marketplaceAccountId", "marketplace"))
    scope(c, org, account)
    own = {"organization_id": org, "marketplace_account_id": account, "marketplace": provider}
    account_row = c.execute(text("""SELECT external_account_id,credential_ref FROM marketplace_accounts
        WHERE organization_id=:organization_id AND marketplace_account_id=:marketplace_account_id
          AND marketplace=:marketplace FOR UPDATE"""), own).mappings().one_or_none()
    if account_row is None:
        raise Conflict("account")
    stamp = binding.stamp(org, account, provider, account_row["external_account_id"], account_row["credential_ref"])
    raw = canonical(request)
    key, actor, op = request["localCommandId"], request["actorMembershipId"], request["operationKind"]
    old = lookup(c, "review_local_command_receipts", owner=own, local_command_id=key)
    if old is not None:
        if (old["actor_membership_id"], old["operation_kind"], bytes(old["request_payload"]),
            bytes(old["account_binding_payload"])) != (actor, op, raw, stamp["account_binding_payload"]):
            raise Conflict("replay")
        return bytes(old["result_payload"])

    writes = 0

    def written():
        nonlocal writes
        writes += 1
        if writes == fail_after:
            raise InjectedFailure("after_write")

    def insert(table, row):
        if transform is not None:
            row = transform(table, dict(row))
            if row is None:
                return
        facts.insert(c, table, row)
        written()

    data, event = request["input"], str(uuid4())
    audit = dict(own, event_id=event, actor_membership_id=actor, local_command_id=key,
                 occurred_at=AT, review_id=None, policy_id=None, policy_version=None,
                 draft_id=None, draft_revision=None, decision_id=None,
                 before_state=None, after_state=None)
    result = {"schemaVersion": "review-local-command-result-v1", "localCommandId": key,
              "operationKind": op, "auditEventId": event, "completedAt": AT,
              "policyId": None, "policyVersion": None, "draftId": None, "draftRevision": None,
              "decisionId": None, "headId": None, "headVersion": None}
    receipt = dict(own, local_command_id=key, actor_membership_id=actor, operation_kind=op,
                   request_payload=raw, request_checksum=digest(raw), **stamp,
                   completed_at=AT, audit_event_id=event, review_id=data.get("reviewId"),
                   expected_head_version=data.get("expectedHeadVersion"),
                   expected_draft_revision=data.get("expectedDraftRevision"),
                   expected_policy_head_id=data.get("expectedPolicyHeadId"),
                   expected_policy_head_version=data.get("expectedPolicyHeadVersion"))

    if op == "review.policy.create.v1":
        p = literal_policy(data["policy"])
        payload = canonical(data["policy"])
        p.update(policy_payload=payload, policy_checksum=digest(payload), actor_membership_id=actor,
                 created_at=AT, audit_event_id=event)
        insert("review_policy_versions", p)
        result.update(policyId=p["policy_id"], policyVersion=p["version"])
        audit.update(aggregate_kind="policy_version", aggregate_id=p["policy_id"],
                     aggregate_version=p["version"], event_kind="policy.created",
                     policy_id=p["policy_id"], policy_version=p["version"])
    elif op == "review.policy.select.v1":
        p = lookup(c, "review_policy_versions", owner=own, policy_id=data["policyId"], version=data["policyVersion"])
        if p is None or p["policy_checksum"] != data["policyChecksum"]:
            raise Conflict("policy")
        h = lookup(c, "review_policy_heads", owner=own)
        expected = data["expectedHeadVersion"]
        if (0 if h is None else h["version"]) != expected:
            raise Conflict("cas")
        hid = str(uuid4()) if h is None else str(h["head_id"])
        head = dict(own, head_id=hid, version=expected + 1,
                    current_policy_id=data["policyId"], current_policy_version=data["policyVersion"],
                    current_policy_checksum=data["policyChecksum"], updated_at=AT)
        if h is None:
            insert("review_policy_heads", head)
        else:
            if transform is not None:
                head = transform("review_policy_heads", head)
            changed = c.execute(text("""UPDATE review_policy_heads SET version=:version,
                current_policy_id=:current_policy_id,current_policy_version=:current_policy_version,
                current_policy_checksum=:current_policy_checksum,updated_at=:updated_at
                WHERE organization_id=:organization_id AND marketplace_account_id=:marketplace_account_id
                  AND marketplace=:marketplace AND version=:expected RETURNING version"""),
                dict(head, expected=expected)).all()
            if len(changed) != 1:
                raise Conflict("cas")
            written()
        result.update(policyId=data["policyId"], policyVersion=data["policyVersion"], headId=hid, headVersion=expected + 1)
        audit.update(aggregate_kind="policy_head", aggregate_id=hid, aggregate_version=expected + 1,
                     event_kind="policy.selected", policy_id=data["policyId"], policy_version=data["policyVersion"],
                     before_state=None if h is None else "policy_selected", after_state="policy_selected")
    else:
        rid = data["reviewId"]
        fact = lookup(c, "review_facts", owner=own, review_id=rid)
        if fact is None:
            raise Conflict("fact")
        external = bytes(fact["external_review_id_utf8"]) if fact["external_review_id"] is None else fact["external_review_id"].encode()
        if external != data["externalReviewId"].encode():
            raise Conflict("identity")
        h = lookup(c, "review_workflow_heads", owner=own, review_id=rid)
        expected = data["expectedHeadVersion"]
        if (0 if h is None else h["version"]) != expected:
            raise Conflict("cas")
        hid = str(uuid4()) if h is None else str(h["head_id"])
        before = None if h is None else "draft_current" if h["current_decision_id"] is None else "decision_current"
        if op == "review.draft.publish.v1":
            g = data["generation"]
            epoch = lookup(c, "review_local_audit", owner=own, aggregate_kind="policy_head",
                           aggregate_id=data["expectedPolicyHeadId"], aggregate_version=data["expectedPolicyHeadVersion"])
            if epoch is None:
                raise Conflict("epoch")
            revision = data["expectedDraftRevision"] + 1
            d = dict(own, **literal_generation(g), review_id=rid, draft_id=data["draftId"], revision=revision,
                     text_utf8=data["text"].encode(), text_checksum=digest(data["text"].encode()),
                     generation_payload=canonical(g), generation_checksum=digest(canonical(g)),
                     policy_head_id=data["expectedPolicyHeadId"], policy_head_version=data["expectedPolicyHeadVersion"],
                     policy_selection_event_id=epoch["event_id"], created_at=AT, audit_event_id=event)
            binding_value = {"contract": "review-decision-v1", "owner": [org, account, provider, data["externalReviewId"]],
                "draft": [data["draftId"], revision, d["text_checksum"]],
                "source": [g["sourceObservationId"], g["sourceChecksum"]],
                "policy": [g["policyId"], g["policyVersion"], g["policyChecksum"], g["templateVersion"], g["modelVersion"]]}
            d.update(binding_payload=canonical(binding_value), binding_checksum=digest(canonical(binding_value)))
            insert("review_draft_revisions", d)
            result.update(policyId=g["policyId"], policyVersion=g["policyVersion"], draftId=data["draftId"], draftRevision=revision)
            audit.update(event_kind="draft.published", policy_id=g["policyId"], policy_version=g["policyVersion"],
                         draft_id=data["draftId"], draft_revision=revision, after_state="draft_current")
        else:
            did = str(uuid4())
            decision = dict(own, review_id=rid, decision_id=did, draft_id=data["draftId"],
                            draft_revision=data["draftRevision"], binding_checksum=data["bindingChecksum"],
                            decision_kind=data["decisionKind"], actor_membership_id=actor,
                            decided_at=AT, audit_event_id=event)
            insert("review_decisions", decision)
            result.update(draftId=data["draftId"], draftRevision=data["draftRevision"], decisionId=did)
            audit.update(event_kind="decision." + data["decisionKind"], draft_id=data["draftId"],
                         draft_revision=data["draftRevision"], decision_id=did, after_state="decision_current")
        head = dict(own, review_id=rid, head_id=hid, version=expected + 1,
                    current_draft_id=result["draftId"], current_draft_revision=result["draftRevision"],
                    current_decision_id=result["decisionId"], updated_at=AT)
        if h is None:
            insert("review_workflow_heads", head)
        else:
            changed = c.execute(text("""UPDATE review_workflow_heads SET version=:version,
                current_draft_id=:current_draft_id,current_draft_revision=:current_draft_revision,
                current_decision_id=:current_decision_id,updated_at=:updated_at
                WHERE organization_id=:organization_id AND marketplace_account_id=:marketplace_account_id
                  AND marketplace=:marketplace AND review_id=:review_id AND version=:expected RETURNING version"""),
                dict(head, expected=expected)).all()
            if len(changed) != 1:
                raise Conflict("cas")
            written()
        result.update(headId=hid, headVersion=expected + 1)
        audit.update(aggregate_kind="workflow_head", aggregate_id=hid, aggregate_version=expected + 1,
                     review_id=rid, before_state=before)

    audit_value = {"schemaVersion": "review-audit-v1", "organizationId": org,
        "marketplaceAccountId": account, "marketplace": provider, "eventId": event,
        "aggregateId": str(audit["aggregate_id"]), "aggregateVersion": int(audit["aggregate_version"]),
        "eventKind": audit["event_kind"], "occurredAt": AT, "actorKind": "membership",
        "actorMembershipId": actor, "commandId": None, "attemptId": None, "reasonCode": None,
        "policyId": audit["policy_id"], "draftId": audit["draft_id"], "decisionId": audit["decision_id"],
        "beforeState": audit["before_state"], "afterState": audit["after_state"]}
    audit.update(audit_payload=canonical(audit_value), audit_checksum=digest(canonical(audit_value)))
    insert("review_local_audit", audit)
    for source, target in (("policyId", "policy_id"), ("policyVersion", "policy_version"),
                           ("draftId", "draft_id"), ("draftRevision", "draft_revision"),
                           ("decisionId", "decision_id"), ("headId", "head_id"), ("headVersion", "head_version")):
        receipt["result_" + target] = result[source]
    payload = canonical(result)
    receipt.update(result_payload=payload, result_checksum=digest(payload))
    insert("review_local_command_receipts", receipt)
    return payload


def policy_command(version=1):
    return intent("policy.create", {"policy": {"schemaVersion": "review-policy-v1", "organizationId": 91001,
        "marketplaceAccountId": 91101, "marketplace": "avito", "policyId": str(uuid4()), "version": version,
        "approvalMode": "manual", "templateVersion": "template-v1", "modelVersion": "fake-v1"}})


def select_command(c, policy):
    p = policy["input"]["policy"]
    h = lookup(c, "review_policy_heads")
    return intent("policy.select", {"policyId": p["policyId"], "policyVersion": p["version"],
        "policyChecksum": digest(canonical(p)), "expectedHeadVersion": 0 if h is None else int(h["version"])})


def preparation(c, *, source=None, previous=None, mode="fake", text_value="Synthetic café é 😀\x00"):
    if source is None:
        source = unit(c, {"review_sync_runs_v2": binding.stamp(), "review_observations": {"can_answer": True}})
    fact, observation = source["review_facts"], source["review_observations"]
    h = lookup(c, "review_policy_heads")
    p = lookup(c, "review_policy_versions", policy_id=h["current_policy_id"], version=h["current_policy_version"])
    wh = lookup(c, "review_workflow_heads", review_id=fact["review_id"])
    g = {"schemaVersion": "review-generation-v1", "generationId": str(uuid4()),
         "sourceObservationId": str(observation["observation_id"]), "sourceChecksum": observation["content_checksum"],
         "policyId": str(p["policy_id"]), "policyVersion": int(p["version"]), "policyChecksum": p["policy_checksum"],
         "templateVersion": p["template_version"], "modelVersion": p["model_version"], "mode": mode,
         "actorMembershipId": 77, "startedAt": "2026-09-09T12:00:00.123456Z",
         "completedAt": "2026-09-09T12:00:01.654321Z"}
    if mode == "manual_edit":
        g["previousDraftId"] = previous if previous is not None else str(wh["current_draft_id"])
    return intent("draft.publish", {"reviewId": str(fact["review_id"]),
        "externalReviewId": fact["external_review_id"] if fact["external_review_id"] is not None else bytes(fact["external_review_id_utf8"]).decode(),
        "draftId": str(uuid4()), "expectedHeadVersion": 0 if wh is None else int(wh["version"]),
        "expectedDraftRevision": 0 if wh is None else int(wh["current_draft_revision"]),
        "expectedPolicyHeadId": str(h["head_id"]), "expectedPolicyHeadVersion": int(h["version"]),
        "generation": g, "text": text_value}), source


def decision_command(c, published, kind="approved"):
    data = published["input"]
    d = lookup(c, "review_draft_revisions", draft_id=data["draftId"])
    h = lookup(c, "review_workflow_heads", review_id=data["reviewId"])
    return intent("decision.record", {"reviewId": data["reviewId"], "externalReviewId": data["externalReviewId"],
        "draftId": data["draftId"], "draftRevision": int(d["revision"]), "bindingChecksum": d["binding_checksum"],
        "sourceObservationId": str(d["source_observation_id"]), "expectedHeadVersion": int(h["version"]),
        "expectedPolicyHeadId": str(d["policy_head_id"]), "expectedPolicyHeadVersion": int(d["policy_head_version"]),
        "decisionKind": kind})


def setup_policy(c, version=1):
    scope(c)
    p = policy_command(version)
    execute(c, p)
    execute(c, select_command(c, p))
    return p


def snapshot(c):
    scope(c)
    return {table: [dict(row) for row in c.exec_driver_sql(f"SELECT * FROM {table} ORDER BY 1,2,3,4").mappings()]
            for table in TABLES}


def test_four_operations_commit_and_historical_exact_replay(db):
    requests, outputs = [], []
    with db[1].begin() as c:
        scope(c)
        p = policy_command(9223372036854775808)
        requests.append(p)
        outputs.append(execute(c, p))
        select = select_command(c, p)
        requests.append(select)
        outputs.append(execute(c, select))
        publish, source = preparation(c)
        requests.append(publish)
        outputs.append(execute(c, publish))
        approve = decision_command(c, publish)
        requests.append(approve)
        outputs.append(execute(c, approve))
    with db[1].begin() as c:
        scope(c)
        execute(c, decision_command(c, publish, "rejected"))
        manual, _ = preparation(c, source=source, mode="manual_edit")
        execute(c, manual)
        setup_policy(c)
        before = snapshot(c)
    # Fresh physical connection models committed write + lost response retry.
    with db[1].begin() as c:
        for request, expected in zip(requests, outputs, strict=True):
            assert execute(c, request) == expected
        assert snapshot(c) == before


@pytest.mark.parametrize("change", ["actor", "kind", "text", "expected"])
def test_same_key_changed_intent_conflicts_without_mutation(db, change):
    with db[1].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
        execute(c, request)
    changed = deepcopy(request)
    if change == "actor":
        changed["actorMembershipId"] = 78
    elif change == "kind":
        changed["operationKind"] = "review.policy.select.v1"
    elif change == "text":
        changed["input"]["text"] += "changed"
    else:
        changed["input"]["expectedHeadVersion"] += 1
    with db[1].begin() as c:
        before = snapshot(c)
        with pytest.raises(Conflict):
            execute(c, changed)
        assert snapshot(c) == before


@pytest.mark.parametrize("write", [1, 2, 3, 4])
def test_failure_after_each_publication_write_rolls_back_root(db, write):
    with db[1].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
    with db[1].begin() as c:
        before = snapshot(c)
    with pytest.raises(InjectedFailure):
        with db[1].begin() as c:
            execute(c, request, fail_after=write)
    with db[1].begin() as c:
        assert snapshot(c) == before
        # Rollback never reserves the prepared generation or command UUID.
        execute(c, request)


def test_policy_aba_rejects_new_decision_but_allows_original_receipt(db):
    with db[1].begin() as c:
        original = setup_policy(c)
        publish, _ = preparation(c)
        result = execute(c, publish)
        setup_policy(c)
        execute(c, select_command(c, original))
        decision = decision_command(c, publish)
    with db[1].begin() as c:
        before = snapshot(c)
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, decision)
    assert caught.value.orig.diag.message_primary == "review_local_policy_changed"
    with db[1].begin() as c:
        assert execute(c, publish) == result
        assert snapshot(c) == before


def test_old_manual_predecessor_with_fresh_versions_is_denied(db):
    with db[1].begin() as c:
        setup_policy(c)
        first, source = preparation(c)
        execute(c, first)
        second, _ = preparation(c, source=source, mode="manual_edit")
        execute(c, second)
        stale, _ = preparation(c, source=source, mode="manual_edit", previous=first["input"]["draftId"])
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, stale)
    assert caught.value.orig.diag.message_primary == "review_local_predecessor_stale"


def test_new_command_cannot_reuse_published_generation(db):
    with db[1].begin() as c:
        setup_policy(c)
        first, source = preparation(c)
        execute(c, first)
        second, _ = preparation(c, source=source)
        second["input"]["generation"]["generationId"] = first["input"]["generation"]["generationId"]
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, second)
    assert caught.value.orig.sqlstate == "23505"


@pytest.mark.parametrize("kind,answered,can_answer,permitted", [
    ("publish", True, False, True), ("rejected", True, False, True),
    ("approved", True, True, False), ("approved", False, None, False),
    ("approved", False, False, False), ("approved", False, True, True),
])
def test_answerability_only_gates_approval(db, kind, answered, can_answer, permitted):
    with db[1].begin() as c:
        setup_policy(c)
        source = unit(c, {"review_sync_runs_v2": binding.stamp(),
                         "review_observations": {"answered": answered, "can_answer": can_answer}})
        publish, _ = preparation(c, source=source)
        execute(c, publish)
        command = None if kind == "publish" else decision_command(c, publish, kind)
    if command is None:
        return
    if permitted:
        with db[1].begin() as c:
            execute(c, command)
    else:
        with pytest.raises(DBAPIError) as caught:
            with db[1].begin() as c:
                execute(c, command)
        assert caught.value.orig.diag.message_primary == "review_local_not_answerable"


def test_missing_reciprocal_receipt_fails_at_physical_commit(db):
    with db[0].begin() as c:
        before = snapshot(c)
    request = policy_command()
    p = literal_policy(request["input"]["policy"])
    payload = canonical(request["input"]["policy"])
    p.update(policy_payload=payload, policy_checksum=digest(payload), actor_membership_id=77,
             created_at=AT, audit_event_id=uuid4())
    with pytest.raises(DBAPIError) as caught:
        with db[0].begin() as c:
            scope(c)
            facts.insert(c, "review_policy_versions", p)
            assert c.execute(text("SELECT count(*) FROM review_policy_versions WHERE policy_id=:p"),
                             {"p": p["policy_id"]}).scalar_one() == 1
    assert caught.value.orig.sqlstate in {"23503", "23514"}
    with db[0].begin() as c:
        assert snapshot(c) == before


@pytest.mark.parametrize("change", ["actor", "provider", "checksum", "payload", "zero_version", "fraction"])
def test_immediate_policy_constraint_not_masked_by_orphan_commit(db, change):
    with db[0].begin() as c:
        scope(c)
        control = policy_command()
        execute(c, control)
    request = policy_command()
    p = literal_policy(request["input"]["policy"])
    payload = canonical(request["input"]["policy"])
    p.update(policy_payload=payload, policy_checksum=digest(payload), actor_membership_id=77,
             created_at=AT, audit_event_id=uuid4())
    if change == "actor":
        p["actor_membership_id"] = 79
    elif change == "provider":
        p["marketplace"] = "wb"
        request["input"]["policy"]["marketplace"] = "wb"
        p["policy_payload"] = canonical(request["input"]["policy"])
        p["policy_checksum"] = digest(p["policy_payload"])
    elif change == "checksum":
        p["policy_checksum"] = "f" * 64
    elif change == "payload":
        p["policy_payload"] += b" "
        p["policy_checksum"] = digest(p["policy_payload"])
    else:
        p["version"] = 0 if change == "zero_version" else "1.25"
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            scope(c)
            with pytest.raises(DBAPIError) as caught:
                facts.insert(c, "review_policy_versions", p)
            assert caught.value.orig.sqlstate in {"23503", "23514"}
            if change == "actor":
                assert caught.value.orig.diag.constraint_name == "review_local_policy_actor_fk"
            if change == "provider":
                assert caught.value.orig.diag.constraint_name == "review_local_policy_account_fk"
        finally:
            transaction.rollback()


def test_scope_switch_cannot_hide_pending_witness_validation(db):
    with pytest.raises(DBAPIError) as caught:
        with db[0].begin() as c:
            execute(c, policy_command())
            scope(c, 91002, 91201)
    assert caught.value.orig.diag.message_primary == "review_local_context_invalid"


def wait_for_block(observer, blocked, blocker):
    until = monotonic() + 5
    while monotonic() < until:
        if observer.execute(text("SELECT :blocker=ANY(pg_blocking_pids(:blocked))"),
                            {"blocker": blocker, "blocked": blocked}).scalar_one():
            return
        sleep(0.01)
    raise AssertionError("exact PostgreSQL blocking relationship not observed")


@pytest.mark.parametrize("mode", ["same_key", "first_head", "generation", "cas"])
def test_account_serialized_races_have_one_atomic_winner(db, mode):
    with db[1].begin() as c:
        setup_policy(c)
        first, source = preparation(c)
        if mode == "cas":
            execute(c, first)
            first, _ = preparation(c, source=source, mode="manual_edit")
        second = deepcopy(first)
        if mode == "generation":
            second, _ = preparation(c)
            second["input"]["generation"]["generationId"] = first["input"]["generation"]["generationId"]
        if mode != "same_key":
            second["localCommandId"] = str(uuid4())
            second["input"]["draftId"] = str(uuid4())
            if mode != "generation":
                second["input"]["generation"]["generationId"] = str(uuid4())
        before_counts = {table: len(rows) for table, rows in snapshot(c).items()}
        new_head = lookup(c, "review_workflow_heads", review_id=first["input"]["reviewId"]) is None
    ready, release = Event(), Event()
    winner_result, pids = {}, {}

    def winner():
        with db[1].begin() as c:
            pids["winner"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            winner_result["bytes"] = execute(c, first)
            ready.set()
            assert release.wait(5)

    def loser():
        assert ready.wait(5)
        try:
            with db[1].begin() as c:
                pids["loser"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                return "ok", execute(c, second)
        except (Conflict, DBAPIError):
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(winner), pool.submit(loser)
        try:
            assert ready.wait(5)
            until = monotonic() + 5
            while "loser" not in pids and monotonic() < until:
                sleep(0.01)
            assert "loser" in pids
            with db[0].connect() as observer:
                wait_for_block(observer, pids["loser"], pids["winner"])
        finally:
            release.set()
        one.result(timeout=5)
        outcome, payload = two.result(timeout=5)
    assert outcome == ("ok" if mode == "same_key" else "conflict")
    if mode == "same_key":
        assert payload == winner_result["bytes"]
    with db[1].begin() as c:
        scope(c)
        after_counts = {table: len(rows) for table, rows in snapshot(c).items()}
        expected_counts = dict(before_counts)
        for table in ("review_draft_revisions", "review_local_audit", "review_local_command_receipts"):
            expected_counts[table] += 1
        expected_counts["review_workflow_heads"] += int(new_head)
        assert after_counts == expected_counts
        for table, column in (("review_draft_revisions", "draft_id"),
                              ("review_local_audit", "local_command_id"),
                              ("review_local_command_receipts", "local_command_id")):
            ids = [first["input"]["draftId"], second["input"]["draftId"]] if column == "draft_id" else [first["localCommandId"], second["localCommandId"]]
            assert c.execute(text(f"SELECT count(*) FROM {table} WHERE {column}=ANY(CAST(:ids AS uuid[]))"),
                             {"ids": ids}).scalar_one() == 1


def test_rebind_denies_old_receipt_without_changing_it(db):
    with db[1].begin() as c:
        request = policy_command()
        execute(c, request)
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            scope(c)
            before = snapshot(c)
            c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101")
            with pytest.raises(Conflict):
                execute(c, request)
            assert snapshot(c) == before
        finally:
            transaction.rollback()


@pytest.mark.parametrize("missing", ["review_local_audit", "review_local_command_receipts"])
def test_complete_target_cannot_commit_without_each_reciprocal_witness(db, missing):
    reached_commit = False
    with pytest.raises(DBAPIError) as caught:
        with db[0].begin() as c:
            execute(c, policy_command(), transform=lambda table, row: None if table == missing else row)
            reached_commit = True
    assert reached_commit
    assert caught.value.orig.sqlstate in {"23503", "23514"}


def test_captured_intermediate_head_cannot_be_hidden_by_later_valid_head(db):
    with db[0].begin() as c:
        p1 = setup_policy(c)
        p2 = policy_command()
        execute(c, p2)
    reached_commit = False

    def interchange(table, row):
        if table == "review_policy_heads":
            p = p2["input"]["policy"]
            row.update(current_policy_id=p["policyId"], current_policy_version=p["version"],
                       current_policy_checksum=digest(canonical(p)))
        return row

    with pytest.raises(DBAPIError) as caught:
        with db[0].begin() as c:
            scope(c)
            execute(c, select_command(c, p1), transform=interchange)
            # Final head is correct. Only validation of captured earlier NEW sees the forgery.
            execute(c, select_command(c, p1))
            reached_commit = True
    assert reached_commit
    assert caught.value.orig.diag.message_primary == "review_local_invalid"


def test_valid_select_publish_select_root_retains_earlier_epoch_witness(db):
    with db[1].begin() as c:
        p = setup_policy(c)
        publish, _ = preparation(c)
        result = execute(c, publish)
        execute(c, select_command(c, p))
    with db[1].begin() as c:
        before = snapshot(c)
        assert execute(c, publish) == result
        assert snapshot(c) == before


@pytest.mark.parametrize("case", ["ambiguous", "replaced_source", "unbound", "rebound_source"])
def test_publication_requires_current_bound_source(db, case):
    with db[0].begin() as c:
        setup_policy(c)
        source = unit(c, {"review_sync_runs_v2": {} if case == "unbound" else binding.stamp()})
        request, _ = preparation(c, source=source)
        if case in {"ambiguous", "replaced_source"}:
            rid, oid = source["review_facts"]["review_id"], source["review_observations"]["observation_id"]
            if case == "ambiguous":
                facts.advance(c, rid, oid, expected_version=1, source_order_state="ambiguous", ambiguous_observation_id=oid)
            else:
                new_run, seq = facts.run(c, **binding.stamp())
                new_oid = facts.observation(c, rid, new_run, revision=2)
                facts.item(c, rid, new_oid, new_run)
                assert facts.advance(c, rid, new_oid, expected_version=1,
                                     last_source_run_id=new_run, last_source_run_sequence=seq) == 1
        if case == "rebound_source":
            c.exec_driver_sql("UPDATE marketplace_accounts SET credential_ref='synthetic-new-ref' WHERE marketplace_account_id=91101")
    try:
        with pytest.raises(DBAPIError) as caught:
            with db[0].begin() as c:
                execute(c, request)
        assert caught.value.orig.diag.message_primary == (
            "review_local_source_changed" if case in {"ambiguous", "replaced_source"} else "review_local_source_unbound")
    finally:
        if case == "rebound_source":
            with db[0].begin() as c:
                c.exec_driver_sql("UPDATE marketplace_accounts SET credential_ref=NULL WHERE marketplace_account_id=91101")


def test_account_lock_precedes_business_tuple_wait_and_other_account_progresses(db):
    with db[0].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
        execute(c, request)
    release, started = Event(), Event()
    pids, errors = {}, []

    def holder():
        with db[0].begin() as c:
            scope(c)
            pids["holder"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            c.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE")
            started.set()
            assert release.wait(5)

    def updater():
        assert started.wait(5)
        try:
            with db[0].begin() as c:
                scope(c)
                pids["updater"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                c.execute(text("UPDATE review_workflow_heads SET version=version+1 WHERE review_id=:rid"),
                          {"rid": request["input"]["reviewId"]})
        except DBAPIError as caught:
            errors.append(caught.orig.diag.message_primary)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(holder), pool.submit(updater)
        try:
            assert started.wait(5)
            until = monotonic() + 5
            while "updater" not in pids and monotonic() < until:
                sleep(0.01)
            with db[0].begin() as c:
                wait_for_block(c, pids["updater"], pids["holder"])
                scope(c)
                # NOWAIT succeeds only if waiting statement has not taken the business tuple first.
                c.execute(text("SELECT 1 FROM review_workflow_heads WHERE review_id=:rid FOR UPDATE NOWAIT"),
                          {"rid": request["input"]["reviewId"]})
                c.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91102 FOR UPDATE NOWAIT")
        finally:
            release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert errors == ["review_local_invalid"]


@pytest.mark.parametrize("case", ["first_manual", "foreign_predecessor", "same_text", "stale_expectation"])
def test_manual_edit_admission_matrix(db, case):
    with db[1].begin() as c:
        setup_policy(c)
        first, source = preparation(c)
        if case == "first_manual":
            request = deepcopy(first)
            request["input"]["generation"].update(mode="manual_edit", previousDraftId=str(uuid4()))
        else:
            execute(c, first)
            execute(c, decision_command(c, first))
            request, _ = preparation(c, source=source, mode="manual_edit")
            if case == "foreign_predecessor":
                foreign, _ = preparation(c)
                execute(c, foreign)
                request["input"]["generation"]["previousDraftId"] = foreign["input"]["draftId"]
            elif case == "stale_expectation":
                request["input"]["expectedHeadVersion"] -= 1
    if case == "same_text":
        with db[1].begin() as c:
            result = json.loads(execute(c, request))
            h = lookup(c, "review_workflow_heads", review_id=request["input"]["reviewId"])
            assert h["current_decision_id"] is None
            assert h["current_draft_revision"] == result["draftRevision"] == 2
            assert h["version"] == result["headVersion"] == 3
    else:
        with pytest.raises(Conflict if case == "stale_expectation" else DBAPIError):
            with db[1].begin() as c:
                execute(c, request)


@pytest.mark.parametrize("field", ["actor", "time", "request_bytes", "result_bytes", "command"])
def test_complete_bundles_reject_interchanged_private_witnesses(db, field):
    def corrupt(table, row):
        if table == "review_local_audit" and field in {"actor", "time", "command"}:
            value = json.loads(bytes(row["audit_payload"]))
            if field == "actor":
                row["actor_membership_id"] = value["actorMembershipId"] = 78
            elif field == "time":
                row["occurred_at"] = value["occurredAt"] = "2026-09-09T12:00:03.000000Z"
            else:
                row["local_command_id"] = str(uuid4())
            row["audit_payload"] = canonical(value)
            row["audit_checksum"] = digest(row["audit_payload"])
        if table == "review_local_command_receipts" and field in {"request_bytes", "result_bytes"}:
            prefix = field.removesuffix("_bytes")
            value = json.loads(bytes(row[prefix + "_payload"]))
            if prefix == "request":
                value["actorMembershipId"] = 78
            else:
                value["completedAt"] = "2026-09-09T12:00:03.000000Z"
            row[prefix + "_payload"] = canonical(value)
            row[prefix + "_checksum"] = digest(row[prefix + "_payload"])
        return row

    with db[0].begin() as c:
        before = snapshot(c)
    with pytest.raises(DBAPIError) as caught:
        with db[0].begin() as c:
            execute(c, policy_command(), transform=corrupt)
    assert caught.value.orig.sqlstate in {"23503", "23514"}
    with db[0].begin() as c:
        assert snapshot(c) == before


@pytest.mark.parametrize("action", ["publish", "approved", "rejected"])
def test_new_action_after_same_target_selection_requires_new_epoch(db, action):
    with db[1].begin() as c:
        p = setup_policy(c)
        publish, source = preparation(c)
        if action != "publish":
            execute(c, publish)
            request = decision_command(c, publish, action)
        else:
            request = publish
        execute(c, select_command(c, p))
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, request)
    assert caught.value.orig.diag.message_primary == "review_local_policy_changed"
    with db[1].begin() as c:
        scope(c)
        fresh, _ = preparation(c, source=source, mode="fake" if action == "publish" else "manual_edit")
        execute(c, fresh)
        execute(c, decision_command(c, fresh))


@pytest.mark.parametrize("field", ["templateVersion", "modelVersion"])
def test_generation_labels_must_match_immutable_policy(db, field):
    with db[1].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
    request["input"]["generation"][field] = "different-valid-label"
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, request)
    assert caught.value.orig.diag.message_primary == "review_local_invalid"


@pytest.mark.parametrize("table", sorted(HEADS))
@pytest.mark.parametrize("change", ["identity", "skipped", "reused", "backwards_time"])
def test_head_identity_and_monotone_transition_checks_are_immediate(db, table, change):
    with db[0].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
        execute(c, request)
        head = lookup(c, table, **({"review_id": request["input"]["reviewId"]} if table == "review_workflow_heads" else {}))
    assignment = {"identity": "head_id=:replacement,version=version+1", "skipped": "version=version+2",
                  "reused": "version=version", "backwards_time": "version=version+1,updated_at=updated_at-interval '1 second'"}[change]
    with db[0].connect() as c:
        tx = c.begin()
        try:
            scope(c)
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(f"UPDATE {table} SET {assignment} WHERE head_id=:head"),
                          {"replacement": uuid4(), "head": head["head_id"]})
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_invalid"
        finally:
            tx.rollback()


@pytest.mark.parametrize("kind", ["policy", "workflow"])
def test_generated_audit_head_fk_is_actual_scoped_reference(db, kind):
    with db[0].begin() as c:
        policy = setup_policy(c)
        request = select_command(c, policy) if kind == "policy" else preparation(c)[0]

    def wrong_head(table, row):
        if table == "review_local_audit":
            value = json.loads(row["audit_payload"])
            row["aggregate_id"] = value["aggregateId"] = str(uuid4())
            row["audit_payload"] = canonical(value)
            row["audit_checksum"] = digest(row["audit_payload"])
        return row

    constraint = "review_local_audit_" + ("phead" if kind == "policy" else "whead") + "_fk"
    with db[0].connect() as c:
        tx = c.begin()
        try:
            # Otherwise complete entity/head/audit/receipt, then force only the target FK.
            execute(c, request, transform=wrong_head)
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql("SET CONSTRAINTS " + constraint + " IMMEDIATE")
            assert caught.value.orig.sqlstate == "23503"
            assert caught.value.orig.diag.constraint_name == constraint
        finally:
            tx.rollback()


@pytest.mark.parametrize("column", ["policy_head_id", "workflow_head_id"])
def test_audit_head_generated_columns_cannot_be_supplied(db, column):
    with db[0].connect() as c:
        tx = c.begin()
        try:
            scope(c)
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(f"INSERT INTO review_local_audit ({column}) VALUES (:value)"), {"value": uuid4()})
            assert caught.value.orig.sqlstate == "428C9"
        finally:
            tx.rollback()


@pytest.mark.parametrize("action", ["publication", "decision"])
def test_waited_policy_selection_invalidates_prepared_new_action(db, action):
    with db[1].begin() as c:
        policy = setup_policy(c)
        publish, _ = preparation(c)
        if action == "decision":
            execute(c, publish)
            request = decision_command(c, publish)
        else:
            request = publish
        select = select_command(c, policy)
        before_counts = {table: len(rows) for table, rows in snapshot(c).items()}
    ready, release = Event(), Event()
    pids = {}

    def selector():
        with db[1].begin() as c:
            pids["selector"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            execute(c, select)
            ready.set()
            assert release.wait(5)

    def action_writer():
        assert ready.wait(5)
        try:
            with db[1].begin() as c:
                pids["action"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                execute(c, request)
        except DBAPIError as caught:
            return caught.orig.diag.message_primary
        raise AssertionError("stale prepared action unexpectedly committed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(selector), pool.submit(action_writer)
        try:
            assert ready.wait(5)
            until = monotonic() + 5
            while "action" not in pids and monotonic() < until:
                sleep(0.01)
            assert "action" in pids
            with db[0].connect() as observer:
                wait_for_block(observer, pids["action"], pids["selector"])
        finally:
            release.set()
        one.result(timeout=5)
        assert two.result(timeout=5) == "review_local_policy_changed"
    with db[1].begin() as c:
        scope(c)
        expected_counts = dict(before_counts)
        expected_counts["review_local_audit"] += 1
        expected_counts["review_local_command_receipts"] += 1
        assert {table: len(rows) for table, rows in snapshot(c).items()} == expected_counts
        assert lookup(c, "review_local_command_receipts", local_command_id=request["localCommandId"]) is None
        if action == "publication":
            assert lookup(c, "review_draft_revisions", draft_id=request["input"]["draftId"]) is None
        else:
            assert lookup(c, "review_workflow_heads", review_id=request["input"]["reviewId"])["current_decision_id"] is None


@pytest.mark.parametrize("change", ["rebind", "provider", "delete"])
def test_waited_account_change_is_rechecked_before_receipt_or_mutation(db, change):
    request = policy_command()
    account = 91101 if change == "rebind" else 91102
    request["marketplaceAccountId"] = request["input"]["policy"]["marketplaceAccountId"] = account
    if change == "rebind":
        with db[1].begin() as c:
            original = execute(c, request)
    ready, release = Event(), Event()
    pids = {}

    def changer():
        with db[0].begin() as c:
            pids["changer"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            if change == "delete":
                c.exec_driver_sql("DELETE FROM marketplace_accounts WHERE marketplace_account_id=91102")
            else:
                assignment = "credential_ref='synthetic-waited-ref'" if change == "rebind" else "marketplace='wb'"
                c.exec_driver_sql(f"UPDATE marketplace_accounts SET {assignment} WHERE marketplace_account_id={account}")
            ready.set()
            assert release.wait(5)

    def writer():
        assert ready.wait(5)
        try:
            with db[1].begin() as c:
                pids["writer"] = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                execute(c, request)
        except Conflict as caught:
            return str(caught)
        raise AssertionError("waited incompatible account accepted")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(changer), pool.submit(writer)
            try:
                assert ready.wait(5)
                until = monotonic() + 5
                while "writer" not in pids and monotonic() < until:
                    sleep(0.01)
                assert "writer" in pids
                with db[0].connect() as observer:
                    wait_for_block(observer, pids["writer"], pids["changer"])
            finally:
                release.set()
            one.result(timeout=5)
            assert two.result(timeout=5) == ("replay" if change == "rebind" else "account")
        with db[0].begin() as c:
            scope(c, 91001, account)
            row = lookup(c, "review_local_command_receipts", owner=dict(OWNER, marketplace_account_id=account),
                         local_command_id=request["localCommandId"])
            if change == "rebind":
                assert bytes(row["result_payload"]) == original
            else:
                assert row is None
                assert lookup(c, "review_policy_versions", owner=dict(OWNER, marketplace_account_id=account),
                              policy_id=request["input"]["policy"]["policyId"]) is None
    finally:
        with db[0].begin() as c:
            if change == "delete":
                c.exec_driver_sql("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES (91102,91001,'avito','synthetic-b','connected')")
            elif change == "provider":
                c.exec_driver_sql("UPDATE marketplace_accounts SET marketplace='avito' WHERE marketplace_account_id=91102")
            else:
                c.exec_driver_sql("UPDATE marketplace_accounts SET credential_ref=NULL WHERE marketplace_account_id=91101")


@pytest.mark.parametrize("table,operation", [
    (table, operation) for table in TABLES for operation in ("DELETE", "TRUNCATE", "UPDATE")
    if operation != "UPDATE" or table not in HEADS
])
def test_immutable_operations_reject_even_empty_statements(db, table, operation):
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            scope(c)
            before = snapshot(c)
            if operation == "TRUNCATE":
                # The runtime fixture uses actual head:0074 adds referencing
                # children outside the original0071 table list. Explicitly name
                # its FK closure in this owned database to reach the TARGET's
                # immutable trigger, not merely PostgreSQL's earlier FK denial.
                # Keep23514/message assertions below; never use CASCADE or alter
                # any constraint/trigger to make the test reach the guard.
                relations = c.execute(text("""
                    WITH RECURSIVE closure(oid) AS (
                        SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='public' AND c.relname=ANY(:tables)
                        UNION
                        SELECT fk.conrelid FROM pg_constraint fk JOIN closure parent
                            ON fk.confrelid=parent.oid WHERE fk.contype='f'
                    )
                    SELECT format('%I.%I',n.nspname,c.relname)
                    FROM closure JOIN pg_class c ON c.oid=closure.oid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    ORDER BY (n.nspname='public' AND c.relname=:target) DESC,n.nspname,c.relname
                """), {"tables": list(TABLES), "target": table}).scalars().all()
                assert relations and relations[0] == f"public.{table}"
                sql = "TRUNCATE " + ",".join(relations)
            else:
                sql = (f"DELETE FROM {table} WHERE false" if operation == "DELETE"
                       else f"UPDATE {table} SET organization_id=organization_id WHERE false")
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql(sql)
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_immutable"
        finally:
            transaction.rollback()
        assert snapshot(c) == before


@pytest.mark.parametrize("table", TABLES)
def test_single_table_truncate_native_fk_refusal_preserves_rows(db, table):
    with db[0].connect() as c:
        tx = c.begin()
        try:
            before = snapshot(c)
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql(f"TRUNCATE {table}")
            assert caught.value.orig.sqlstate == "0A000"
            assert "foreign key constraint" in caught.value.orig.diag.message_primary
        finally:
            tx.rollback()
        assert snapshot(c) == before


def test_policy_created_audit_aggregate_version_matches_immutable_target(db):
    with db[1].begin() as c:
        positive = policy_command(7)
        execute(c, positive)
        before = snapshot(c)

    def wrong_aggregate_version(table, row):
        if table == "review_local_audit":
            value = json.loads(row["audit_payload"])
            row["aggregate_version"] = value["aggregateVersion"] = 8
            row["audit_payload"] = canonical(value)
            row["audit_checksum"] = digest(row["audit_payload"])
        return row

    reached_commit = False
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, policy_command(7), transform=wrong_aggregate_version)
            reached_commit = True
    assert reached_commit, "test must exercise complete-bundle physical commit"
    assert caught.value.orig.sqlstate == "23514"
    assert caught.value.orig.diag.message_primary == "review_local_invalid"
    with db[1].begin() as c:
        assert snapshot(c) == before


def test_first_publication_requires_a_physical_policy_head(db):
    with db[0].begin() as c:
        setup_policy(c)
        valid, _ = preparation(c)
        execute(c, valid)
        draft = dict(lookup(c, "review_draft_revisions", draft_id=valid["input"]["draftId"]))
    policy = policy_command()
    policy["marketplaceAccountId"] = policy["input"]["policy"]["marketplaceAccountId"] = 91102
    with db[0].begin() as c:
        execute(c, policy)
        source = unit(c, {"review_sync_runs_v2": binding.stamp(91001, 91102, "avito", "synthetic-b", None)}, account=91102)
        assert lookup(c, "review_policy_heads", owner=dict(OWNER, marketplace_account_id=91102)) is None
        value = policy["input"]["policy"]
        draft.update(marketplace_account_id=91102, draft_id=uuid4(), generation_id=uuid4(),
                     review_id=source["review_facts"]["review_id"], revision=1,
                     source_observation_id=source["review_observations"]["observation_id"],
                     source_checksum=source["review_observations"]["content_checksum"],
                     policy_id=value["policyId"], policy_version=value["version"], policy_checksum=digest(canonical(value)),
                     policy_head_id=uuid4(), policy_head_version=1, policy_selection_event_id=uuid4(), audit_event_id=uuid4())
        projection = composite("review_draft_revisions", "d")
        draft["generation_payload"] = bytes(c.execute(text("SELECT review_local_generation_bytes(" + projection + ")"), {"d": json_row(draft)}).scalar_one())
        draft["generation_checksum"] = digest(draft["generation_payload"])
        draft["binding_payload"] = bytes(c.execute(text("SELECT review_local_decision_binding_bytes(" + projection + ",:external)"),
            {"d": json_row(draft), "external": source["review_facts"]["external_review_id"].encode()}).scalar_one())
        draft["binding_checksum"] = digest(draft["binding_payload"])
    with db[0].connect() as c:
        tx = c.begin()
        try:
            scope(c, 91001, 91102)
            with pytest.raises(DBAPIError) as caught:
                facts.insert(c, "review_draft_revisions", draft)
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_policy_changed"
        finally:
            tx.rollback()


@pytest.mark.parametrize("case", ["missing", "foreign_owner"])
def test_selection_witness_requires_exact_scoped_persisted_event(db, case):
    foreign = policy_command()
    foreign.update(organizationId=91002, marketplaceAccountId=91201, marketplace="wb", actorMembershipId=79)
    foreign["input"]["policy"].update(organizationId=91002, marketplaceAccountId=91201, marketplace="wb")
    with db[0].begin() as c:
        execute(c, foreign)
        owner = {"organization_id": 91002, "marketplace_account_id": 91201, "marketplace": "wb"}
        head = lookup(c, "review_policy_heads", owner=owner)
        p = foreign["input"]["policy"]
        select = intent("policy.select", {"policyId": p["policyId"], "policyVersion": p["version"],
                        "policyChecksum": digest(canonical(p)), "expectedHeadVersion": 0 if head is None else int(head["version"])}, actor=79)
        select.update(organizationId=91002, marketplaceAccountId=91201, marketplace="wb")
        foreign_event = json.loads(execute(c, select))["auditEventId"]
    with db[0].begin() as c:
        setup_policy(c)
        request, _ = preparation(c)
        before = snapshot(c)
    event = str(uuid4()) if case == "missing" else foreign_event

    def substitute(table, row):
        if table == "review_draft_revisions":
            row["policy_selection_event_id"] = event
        return row

    with db[0].connect() as c:
        tx = c.begin()
        try:
            execute(c, request, transform=substitute)
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql("SET CONSTRAINTS review_local_draft_selection_fk IMMEDIATE")
            assert caught.value.orig.sqlstate == "23503"
            assert caught.value.orig.diag.constraint_name == "review_local_draft_selection_fk"
        finally:
            tx.rollback()
        assert snapshot(c) == before


def test_forged_fresh_decision_epoch_does_not_rewrite_old_draft_capture(db):
    with db[1].begin() as c:
        policy = setup_policy(c)
        publish, _ = preparation(c)
        execute(c, publish)
        execute(c, select_command(c, policy))
        request = decision_command(c, publish)
        current = lookup(c, "review_policy_heads")
        request["input"].update(expectedPolicyHeadId=str(current["head_id"]), expectedPolicyHeadVersion=int(current["version"]))
        before = snapshot(c)
    with pytest.raises(DBAPIError) as caught:
        with db[1].begin() as c:
            execute(c, request)
    assert caught.value.orig.sqlstate == "23514"
    assert caught.value.orig.diag.message_primary == "review_local_policy_changed"
    with db[1].begin() as c:
        assert snapshot(c) == before


@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "01", "1.0", "1e1", " 1", "2147483648", "9" * 100])
@pytest.mark.parametrize("setting", ["app.organization_id", "app.marketplace_account_id"])
def test_context_is_validated_before_integer_cast(db, setting, value):
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            scope(c)
            c.execute(text("SELECT set_config(:s,:v,true)"), {"s": setting, "v": value})
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql("UPDATE review_workflow_heads SET version=version+1 WHERE false")
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_context_invalid"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_new_storage_requires_read_committed(db, isolation):
    with db[0].connect().execution_options(isolation_level=isolation) as c:
        transaction = c.begin()
        try:
            scope(c)
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql("UPDATE review_workflow_heads SET version=version+1 WHERE false")
            assert caught.value.orig.sqlstate == "25000"
            assert caught.value.orig.diag.message_primary == "review_local_isolation_invalid"
        finally:
            transaction.rollback()
