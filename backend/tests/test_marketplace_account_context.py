"""Admission canaries use actual Sessions without opening external connections."""

import traceback

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infra import db


def api():
    helper = getattr(db, "set_marketplace_account_context", None)
    error = getattr(db, "MarketplaceAccountContextError", None)
    assert callable(helper), "missing marketplace account context helper"
    assert isinstance(error, type) and issubclass(error, RuntimeError)
    return helper, error


def test_required_interface():
    api()


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 2**31, "1", 1.0, None])
def test_noncanonical_ids_deny_before_binding(field, value):
    helper, error = api()
    with Session() as session, session.begin():
        args = {"organization_id": 1, "marketplace_account_id": 11, field: value}
        with pytest.raises(error, match="^account_context_invalid$"):
            helper(session, **args)


@pytest.mark.parametrize("state", ["absent", "ended", "nested", "dialect"])
def test_invalid_transaction_or_dialect(state):
    helper, error = api()
    engine = create_engine("sqlite://")
    try:
        with Session(engine) as session:
            if state != "absent":
                session.begin()
            if state == "ended":
                session.commit()
            if state == "nested":
                session.begin_nested()
            with pytest.raises(error, match="^account_context_invalid$"):
                helper(session, organization_id=1, marketplace_account_id=11)
    finally:
        engine.dispose()


def test_actual_session_required():
    helper, error = api()
    with pytest.raises(error, match="^account_context_invalid$"):
        helper(object(), organization_id=1, marketplace_account_id=11)


@pytest.mark.parametrize("value", ["synthetic-secret-canary", None, [], 123])
def test_error_constructor_does_not_reflect_untrusted_values(value):
    _, error = api()
    caught = error(value)
    assert caught.code == "account_context_failed"
    assert str(caught) == "account_context_failed"
    assert "synthetic-secret-canary" not in repr(caught)
    assert "synthetic-secret-canary" not in "".join(traceback.format_exception(caught))
