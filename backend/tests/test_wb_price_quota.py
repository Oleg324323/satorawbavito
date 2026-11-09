"""Source-level quota boundary tests. SQL doubles do not prove PG concurrency/RLS."""
import importlib
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.platform.integrations.worker_identity import (
    ExecutorIdentityDenied,
    ExecutorRoleIdentity,
)

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
IDENTITY = ExecutorRoleIdentity("wb_live_worker", "wb_live_api")


def api():
    assert importlib.util.find_spec("app.platform.integrations.wb_price_quota"), "missing shared price quota service"
    return importlib.import_module("app.platform.integrations.wb_price_quota")


class SessionDouble:
    def __init__(self):
        self.bind = create_engine("postgresql+psycopg://")
        self.root = None
        self.physical = SimpleNamespace(is_active=True)
        self.new, self.dirty, self.deleted = (), (), ()
        self.is_active = True
        self.events, self.statements = [], []
        self.dispatch = SimpleNamespace(before_commit=[])
        self.deadline = NOW
        self.fail = None
        self.owner_exists = True
        self.identity_calls = 0

    def get_bind(self):
        return self.bind

    def in_transaction(self):
        return self.root is not None

    def in_nested_transaction(self):
        return False

    def get_transaction(self):
        return self.root

    def begin(self):
        self.root = SimpleNamespace(is_active=True)
        self.events.append("begin")

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if "FOR UPDATE" in sql:
            self.events.append("quota-lock")
            return SimpleNamespace(scalar_one=lambda: self.deadline)
        if "current_setting" in sql:
            return SimpleNamespace(one=lambda: ("7", "23"))
        return SimpleNamespace(rowcount=1)

    def scalar(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if "clock_timestamp" in sql:
            self.events.append("db-clock")
            return NOW
        return 23 if self.owner_exists else None

    def commit(self):
        if self.fail == "physical-root":
            self.physical = SimpleNamespace(is_active=True)
        for callback in self.dispatch.before_commit:
            callback(self)
        self.events.append("commit")
        if self.fail == "commit":
            raise RuntimeError("private-commit-diagnostic")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")
        if self.fail == "close":
            raise RuntimeError("private-close-diagnostic")


def service(monkeypatch):
    module, db = api(), SessionDouble()
    monkeypatch.setattr(module, "Session", SessionDouble)
    monkeypatch.setattr(module, "_require_clean_publication_root", lambda session: None)
    monkeypatch.setattr(module, "_physical_connection", lambda session: SimpleNamespace(get_transaction=lambda: db.physical))
    monkeypatch.setattr(module, "set_tenant_context", lambda session, org: None)
    monkeypatch.setattr(module, "set_marketplace_account_context", lambda session, **kwargs: None)
    monkeypatch.setattr(module, "event", SimpleNamespace(listen=lambda session, name, fn: session.dispatch.before_commit.append(fn)))
    def verify(session, *, identity):
        assert identity is IDENTITY
        db.identity_calls += 1
        if db.fail == "identity" or (db.fail == "closing-identity" and db.identity_calls > 1):
            raise ExecutorIdentityDenied()
    monkeypatch.setattr(module, "verify_executor_login", verify)
    return module.WbPriceQuotaService(session_factory=lambda: db, identity=IDENTITY), db


@pytest.mark.parametrize("method,path", [("GET", "/api/v2/list/goods/filter"), ("POST", "/api/v2/upload/task"),
    ("GET", "/api/v2/history/tasks"), ("GET", "/api/v2/history/goods/task")])
def test_reviewed_operations_reserve_one_shared_category_after_database_lock(monkeypatch, method, path):
    quota, db = service(monkeypatch)
    assert quota.admit_request(7, 23, method, path, 900) is True
    assert db.events.index("quota-lock") < db.events.index("db-clock") < db.events.index("commit") < db.events.index("close")
    updates = [(sql, params) for sql, params in db.statements if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert updates[0][1]["deadline"] == NOW + timedelta(seconds=900)
    assert all("provider='wb'" in sql and "category='prices_discounts'" in sql
               for sql, _ in db.statements if sql.startswith(("UPDATE", "SELECT next_allowed_at")))
    assert db.identity_calls >= 2
    assert not any(word in " ".join(sql for sql, _ in db.statements)
                   for word in ("credential_ref", "ciphertext", "marketplace_account_credentials", "wb_token"))


def test_not_due_returns_false_only_after_commit_and_close(monkeypatch):
    quota, db = service(monkeypatch)
    db.deadline = NOW + timedelta(seconds=1)
    assert quota.admit_request(7, 23, "GET", "/api/v2/list/goods/filter", 900) is False
    assert not any(sql.startswith("UPDATE") for sql, _ in db.statements)
    assert db.events[-3:] == ["commit", "rollback", "close"]


@pytest.mark.parametrize("change", [{"org": True}, {"account": 0}, {"account": 2**31}, {"method": "get"},
    {"path": "/api/v2/arbitrary"}, {"path": "/api/v2/upload/task?query=1"}, {"method": "GET", "path": "/api/v2/upload/task"},
    {"minimum": True}, {"minimum": 899}, {"minimum": 901}])
def test_unreviewed_operation_or_policy_rejected_before_session(monkeypatch, change):
    quota, db = service(monkeypatch)
    values = {"org": 7, "account": 23, "method": "POST", "path": "/api/v2/upload/task", "minimum": 900, **change}
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_INVALID$"):
        quota.admit_request(values["org"], values["account"], values["method"], values["path"], values["minimum"])
    assert db.events == []


@pytest.mark.parametrize("failure", ["commit", "close", "identity", "closing-identity", "physical-root"])
def test_unknown_commit_close_or_changed_login_never_admits(monkeypatch, failure):
    quota, db = service(monkeypatch)
    db.fail = failure
    with pytest.raises(api().WbPriceQuotaError) as error:
        quota.admit_request(7, 23, "POST", "/api/v2/upload/task", 900)
    assert error.value.code == ("WB_PRICE_QUOTA_DENIED" if "identity" in failure or failure == "physical-root"
                               else "WB_PRICE_QUOTA_UNAVAILABLE")
    assert error.value.__context__ is None
    assert "private" not in str(error.value)


def test_cooldown_uses_utc_and_greatest_without_a_ttl_cap(monkeypatch):
    quota, db = service(monkeypatch)
    incoming = datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=3)))
    assert quota.record_cooldown(7, 23, incoming) is True
    sql, params = next((sql, params) for sql, params in db.statements if sql.startswith("UPDATE"))
    assert "GREATEST(" in sql and "next_allowed_at" in sql
    assert params["deadline"] == datetime(2029, 12, 31, 21, tzinfo=UTC)
    assert db.events.index("quota-lock") < db.events.index("db-clock") < db.events.index("commit")


@pytest.mark.parametrize("deadline", [None, NOW.replace(tzinfo=None), "2030-01-01", True])
def test_feedback_requires_an_aware_representable_datetime(monkeypatch, deadline):
    quota, db = service(monkeypatch)
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_INVALID$"):
        quota.record_cooldown(7, 23, deadline)
    assert db.events == []


def test_foreign_or_non_wb_account_is_denied(monkeypatch):
    quota, db = service(monkeypatch)
    db.owner_exists = False
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
        quota.admit_request(7, 23, "POST", "/api/v2/upload/task", 900)
    assert not any(sql.startswith("INSERT") for sql, _ in db.statements)


def test_existing_caller_root_is_rejected_without_rollback_or_close():
    module = api()
    session = Session(create_engine("postgresql+psycopg://"))
    root = session.begin()
    try:
        quota = module.WbPriceQuotaService(session_factory=lambda: session, identity=IDENTITY)
        with pytest.raises(module.WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
            quota.admit_request(7, 23, "POST", "/api/v2/upload/task", 900)
        assert session.get_transaction() is root and root.is_active
    finally:
        session.close()


def test_later_commit_listener_cannot_run_after_final_identity_check(monkeypatch):
    quota, db = service(monkeypatch)
    execute = db.execute
    def register_late_listener(statement, params=None):
        result = execute(statement, params)
        if str(statement).startswith("INSERT"):
            db.dispatch.before_commit.append(lambda session: db.events.append("unfenced"))
        return result
    db.execute = register_late_listener
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
        quota.admit_request(7, 23, "POST", "/api/v2/upload/task", 900)
    assert "unfenced" not in db.events and "commit" not in db.events


def test_unrepresentable_utc_feedback_is_sanitized_before_session(monkeypatch):
    quota, db = service(monkeypatch)
    value = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_INVALID$") as error:
        quota.record_cooldown(7, 23, value)
    assert error.value.__context__ is None and db.events == []


@pytest.mark.parametrize("mutation", ["queued-work", "later-listener"])
def test_last_validation_query_hook_cannot_queue_unfenced_changes(monkeypatch, mutation):
    quota, db = service(monkeypatch)
    scalar = db.scalar
    def mutate_after_account_query(statement, params=None):
        result = scalar(statement, params)
        if db.identity_calls > 1 and "marketplace_accounts" in str(statement):
            if mutation == "queued-work":
                db.new = (object(),)
            else:
                db.dispatch.before_commit.append(lambda session: db.events.append("unfenced"))
        return result
    db.scalar = mutate_after_account_query
    with pytest.raises(api().WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
        quota.admit_request(7, 23, "POST", "/api/v2/upload/task", 900)
    assert "unfenced" not in db.events and "commit" not in db.events
