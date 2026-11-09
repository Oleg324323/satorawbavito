"""Actual0071 service/HTTP, fresh live membership and owned Unix PostgreSQL.

No live providers, application DB, credentials, generation API or real dispatch.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.control_plane.auth import ActorContext
from app.reviews.canonical_contract import normalize_avito_review
from app.reviews.local_command_payloads import encode_review_local_request
from app.reviews.local_http import _versions
from app.reviews.local_preparation import prepare_local_review_draft
from app.reviews.local_repository import ReviewLocalError
from app.reviews.local_service import execute_local_review, read_local_review_context
from app.reviews.storage_payloads import encode_review_policy
from tests import test_review_facts_repository as facts
from tests import test_review_local_storage_schema as storage
from tests import test_review_publication_repository as publication
from tests.test_orders_schema_candidate import scope

cluster = storage.cluster
db = storage.db
principal = publication.principal
SETTINGS = Settings(review_shadow_enabled=True, review_shadow_account_pairs=((91001, 91101), (91001, 91102)))


@pytest.fixture
def actor(db, principal):
    with db[0].begin() as c:
        c.execute(text("UPDATE iam_memberships SET permissions='[\"reviews:read\",\"reviews:write\",\"reviews:approve\"]' "
                       "WHERE membership_id=:id"), {"id": principal.membership_id})
    return ActorContext(principal.user_id, principal.user_id, 91001, "custom", frozenset(), session_id=principal.session_id)


def context(db, actor, review=None):
    return read_local_review_context(db[1], actor=actor, settings=SETTINGS,
        marketplace_account_id=91101, marketplace="avito", **({} if review is None else review))


def command(actor, member, kind, data):
    return {"schemaVersion": "review-local-command-v1", "organizationId": actor.organization_id,
        "marketplaceAccountId": 91101, "marketplace": "avito", "localCommandId": str(uuid4()),
        "actorMembershipId": member, "operationKind": "review." + kind + ".v1", "input": data}


def execute(db, actor, request):
    return json.loads(execute_local_review(db[1], actor=actor, settings=SETTINGS, request=request).canonical_bytes)


def policy(db, actor, *, version=1):
    ctx = context(db, actor)
    p = {"schemaVersion": "review-policy-v1", "organizationId": 91001, "marketplaceAccountId": 91101,
        "marketplace": "avito", "policyId": str(uuid4()), "version": version, "approvalMode": "manual",
        "templateVersion": "local-v1", "modelVersion": "fake-v1"}
    create = command(actor, ctx["actorMembershipId"], "policy.create", {"policy": p})
    execute(db, actor, create)
    select = command(actor, ctx["actorMembershipId"], "policy.select", {"policyId": p["policyId"],
        "policyVersion": p["version"], "policyChecksum": encode_review_policy(p).checksum,
        "expectedHeadVersion": 0 if ctx["policyHead"] is None else ctx["policyHead"]["version"]})
    execute(db, actor, select)
    return create, select


def source(db, *, key=None, body="Exact\0 café é 😀", can_answer=True, expected=0):
    key, run = key or uuid4().hex, uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = facts.repository(c)
        reserved = repo.reserve_run(source_run_id=run, request_checksum="a" * 64, started_at=facts.NOW)
        fact = normalize_avito_review({"id": key, "createdAt": facts.NOW, "updatedAt": datetime.now(UTC),
            "text": body, "answered": False, "canAnswer": can_answer}, 91001, 91101, run, facts.NOW)
        facts.ingest(repo, reserved, [fact], {key: expected})
        current = repo.get_fact(key)
    return {"review_id": str(current.review_id), "external_review_id": key}


def prepared(db, actor, review, *, mode="fake", value=None):
    encoded = prepare_local_review_draft(context(db, actor, review), local_command_id=uuid4(),
        draft_id=uuid4(), generation_id=uuid4(), mode=mode, text=value)
    return json.loads(encoded.canonical_bytes)


def decision(db, actor, review, *, kind="approved"):
    ctx = context(db, actor, review)
    return command(actor, ctx["actorMembershipId"], "decision.record", {
        "reviewId": ctx["review"]["reviewId"], "externalReviewId": ctx["review"]["externalReviewId"],
        "draftId": ctx["draft"]["draftId"], "draftRevision": ctx["draft"]["revision"],
        "bindingChecksum": ctx["draft"]["bindingChecksum"], "sourceObservationId": ctx["review"]["sourceObservationId"],
        "expectedHeadVersion": ctx["workflowHead"]["version"], "expectedPolicyHeadId": ctx["policyHead"]["headId"],
        "expectedPolicyHeadVersion": ctx["policyHead"]["version"], "decisionKind": kind})


def test_policy_fake_manual_decision_and_original_replay(db, actor):
    try:
        create, _ = policy(db, actor, version=2**80 + 1)
    except ReviewLocalError as error:
        # Synthetic-only diagnostics; never dump SQL, parameters or Review bodies.
        original = getattr(error.__context__, "orig", None)
        diagnostic = getattr(original, "diag", None)
        raise AssertionError((type(original).__name__, getattr(original, "sqlstate", None),
                              getattr(diagnostic, "message_primary", None))) from None
    review = source(db)
    fake = prepared(db, actor, review)
    first = execute(db, actor, fake)
    approved = decision(db, actor, review)
    approval = execute(db, actor, approved)
    edit = prepared(db, actor, review, mode="manual_edit", value="  unchanged?\0é é 😀\n")
    edited = execute(db, actor, edit)
    current = context(db, actor, review)
    assert current["decision"] is None and current["draft"]["text"] == edit["input"]["text"]
    assert edited["draftRevision"] == 2 and edited["headVersion"] == 3
    assert execute(db, actor, fake) == first
    assert execute(db, actor, approved) == approval
    assert execute(db, actor, create)["policyVersion"] == 2**80 + 1
    assert context(db, actor, review)["workflowHead"]["version"] == 3
    with db[0].connect() as c:
        assert c.execute(text("SELECT count(*) FROM review_local_command_receipts WHERE local_command_id=:id"),
                         {"id": fake["localCommandId"]}).scalar_one() == 1


@pytest.mark.parametrize("changed", ["text", "actor", "key"])
def test_published_generation_cannot_be_rebound(db, actor, changed):
    policy(db, actor)
    request = prepared(db, actor, source(db))
    execute(db, actor, request)
    forged = deepcopy(request)
    if changed == "text":
        forged["input"]["text"] += "!"
    elif changed == "actor":
        forged["actorMembershipId"] += 1
        forged["input"]["generation"]["actorMembershipId"] += 1
    else:
        forged["localCommandId"] = str(uuid4())
        forged["input"]["draftId"] = str(uuid4())
        forged["input"]["expectedHeadVersion"] = 1
        forged["input"]["expectedDraftRevision"] = 1
    with pytest.raises(ReviewLocalError):
        execute(db, actor, forged)


@pytest.mark.parametrize("change", ["session", "permission", "scope", "binding"])
def test_historical_receipt_does_not_bypass_live_authority(db, actor, change):
    create, _ = policy(db, actor)
    with db[0].begin() as c:
        if change == "session":
            c.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"), {"id": actor.session_id})
        elif change == "permission":
            c.execute(text("UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"), {"id": actor.user_id})
        elif change == "scope":
            c.execute(text("UPDATE iam_memberships SET allowed_account_ids='[91102]' WHERE user_id=:id"), {"id": actor.user_id})
        else:
            c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='rebound' WHERE marketplace_account_id=91101")
    try:
        with pytest.raises(ReviewLocalError):
            execute(db, actor, create)
    finally:
        if change == "binding":
            with db[0].begin() as c:
                c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101")


def test_policy_aba_needs_new_draft_but_original_receipt_replays(db, actor):
    _, selected = policy(db, actor)
    review = source(db)
    publish = prepared(db, actor, review)
    receipt = execute(db, actor, publish)
    approve = decision(db, actor, review)
    policy(db, actor)
    back = deepcopy(selected)
    back["localCommandId"] = str(uuid4())
    back["input"]["expectedHeadVersion"] = context(db, actor)["policyHead"]["version"]
    execute(db, actor, back)
    assert execute(db, actor, publish) == receipt
    approve["input"]["expectedPolicyHeadVersion"] = context(db, actor)["policyHead"]["version"]
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_POLICY_CHANGED"):
        execute(db, actor, approve)


def test_source_change_rejects_old_prepared_command(db, actor):
    policy(db, actor)
    review = source(db)
    publish = prepared(db, actor, review)
    source(db, key=review["external_review_id"], body="changed", expected=1)
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_SOURCE_CHANGED"):
        execute(db, actor, publish)


def test_unknown_answerability_allows_draft_and_reject_but_not_approve(db, actor):
    policy(db, actor)
    review = source(db, can_answer=None)
    execute(db, actor, prepared(db, actor, review))
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_NOT_ANSWERABLE"):
        execute(db, actor, decision(db, actor, review))
    execute(db, actor, decision(db, actor, review, kind="rejected"))
    assert context(db, actor, review)["decision"]["decisionKind"] == "rejected"


def test_old_manual_predecessor_cannot_be_used_with_fresh_cas(db, actor):
    policy(db, actor)
    review = source(db)
    execute(db, actor, prepared(db, actor, review))
    old = prepared(db, actor, review, mode="manual_edit", value="old edit")
    execute(db, actor, prepared(db, actor, review, mode="manual_edit", value="new edit"))
    old["input"].update(expectedHeadVersion=2, expectedDraftRevision=2)
    with pytest.raises(ReviewLocalError):
        execute(db, actor, old)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_publish_replay_or_one_cas_winner(db, actor, same_key):
    policy(db, actor)
    review = source(db)
    first = prepared(db, actor, review)
    other = deepcopy(first) if same_key else prepared(db, actor, review)
    def run(request):
        try:
            return execute(db, actor, request)
        except ReviewLocalError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, other)))
    if same_key:
        assert results[0] == results[1] and isinstance(results[0], dict)
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "REVIEW_LOCAL_CONFLICT" in results
    assert context(db, actor, review)["workflowHead"]["version"] == 1


def test_receipt_insert_failure_rolls_back_draft_head_and_audit(db, actor):
    policy(db, actor)
    review = source(db)
    request = prepared(db, actor, review)
    def fail(c, cursor, statement, parameters, ctx, many):
        if statement.startswith("INSERT INTO review_local_command_receipts"):
            raise OperationalError("private synthetic SQL", {}, Exception("private synthetic diagnostic"))
    event.listen(db[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(ReviewLocalError, match="^REVIEW_LOCAL_STORAGE_UNAVAILABLE$"):
            execute(db, actor, request)
    finally:
        event.remove(db[1], "before_cursor_execute", fail)
    assert context(db, actor, review)["draft"] is None
    assert execute(db, actor, request)["draftRevision"] == 1


def client(db, actor):
    from app.reviews import local_http
    app = FastAPI()
    app.include_router(local_http.router)
    app.dependency_overrides[local_http._actor] = lambda: actor
    app.dependency_overrides[local_http._engine] = lambda: db[1]
    app.dependency_overrides[local_http._settings] = lambda: SETTINGS
    return TestClient(app)


def test_http_lossless_versions_and_safe_request_errors(db, actor):
    ctx = context(db, actor)
    p = {"schemaVersion": "review-policy-v1", "organizationId": 91001, "marketplaceAccountId": 91101,
        "marketplace": "avito", "policyId": str(uuid4()), "version": 2**90 + 1, "approvalMode": "manual",
        "templateVersion": "v1", "modelVersion": "fake-v1"}
    request = command(actor, ctx["actorMembershipId"], "policy.create", {"policy": p})
    with client(db, actor) as http:
        response = http.post("/api/v2/reviews/local/commands", json=_versions(request))
        assert response.status_code == 200, response.text
        assert response.json()["policyVersion"] == str(p["version"])
        assert response.headers["cache-control"] == "no-store"
        assert "credential" not in response.text
        # Numeric JSON versions are rejected instead of silently rounded by JS.
        assert http.post("/api/v2/reviews/local/commands", json=request).status_code == 400
        for raw in ('{"private":"SYNTHETIC","private":1}', '{"value":NaN}', '{"private":'):
            error = http.post("/api/v2/reviews/local/commands", content=raw)
            assert error.status_code == 400
            assert error.json() == {"detail": {"code": "REVIEW_LOCAL_INVALID"}}
        error = http.get("/api/v2/reviews/local/context?marketplace_account_id=91101&marketplace=avito&review_id=private&external_review_id=SYNTHETIC")
        assert error.status_code == 400 and "SYNTHETIC" not in error.text


def test_disabled_and_sessionless_fail_without_engine():
    actor = ActorContext("synthetic", "synthetic", 91001, "custom", frozenset(), session_id="synthetic")
    p = {"schemaVersion": "review-policy-v1", "organizationId": 91001, "marketplaceAccountId": 91101,
        "marketplace": "avito", "policyId": str(uuid4()), "version": 1, "approvalMode": "manual", "templateVersion": "v1", "modelVersion": "fake-v1"}
    request = command(actor, 1, "policy.create", {"policy": p})
    encode_review_local_request(request)
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_DISABLED"):
        execute_local_review(None, actor=actor, settings=Settings(), request=request)
    with pytest.raises(ReviewLocalError, match="REVIEW_LOCAL_DENIED"):
        execute_local_review(None, actor=replace(actor, session_id=None), settings=SETTINGS, request=request)
