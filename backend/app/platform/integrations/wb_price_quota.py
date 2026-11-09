"""Dormant shared Prices quota; reservation is never price-send authorization.

Trusted bootstrap binds a dedicated reader or distinct price executor factory
and actual login identity. Consumers retain their fresh job/actor guards. No
HTTP, credentials, provider callback, application clock or fallback lives here.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, event, select, text
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context, set_tenant_context
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    PublicationGuardError,
    _physical_connection,
    _require_clean_publication_root,
)
from app.platform.integrations.worker_identity import (
    ExecutorIdentityDenied,
    ExecutorRoleIdentity,
    role_name,
    verify_executor_login,
)

_OPERATIONS = frozenset({("GET", "/api/v2/list/goods/filter"), ("POST", "/api/v2/upload/task"),
                         ("GET", "/api/v2/history/tasks"), ("GET", "/api/v2/history/goods/task")})
_PREDICATE = "organization_id=:org AND marketplace_account_id=:account AND provider='wb' AND category='prices_discounts'"


class WbPriceQuotaError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in {
            "WB_PRICE_QUOTA_INVALID", "WB_PRICE_QUOTA_DENIED", "WB_PRICE_QUOTA_UNAVAILABLE"
        } else "WB_PRICE_QUOTA_UNAVAILABLE"
        super().__init__(self.code)


def _require(condition, code="WB_PRICE_QUOTA_INVALID"):
    if not condition:
        raise WbPriceQuotaError(code)


def _scope(org, account):
    _require(all(type(value) is int and 0 < value < 2**31 for value in (org, account)))
    return {"org": org, "account": account}


def _deadline(value):
    invalid, normalized = False, None
    try:
        if type(value) is not datetime or value.utcoffset() is None:
            invalid = True
        else:
            normalized = value.astimezone(UTC)
    except Exception:  # noqa: BLE001 - unusual tzinfo/overflow must not leak input.
        invalid = True
    _require(not invalid)
    return normalized


class WbPriceQuotaService:
    def __init__(self, *, session_factory, identity):
        _require(callable(session_factory) and type(identity) is ExecutorRoleIdentity)
        invalid = False
        try:
            executor = role_name(identity.marketplace_executor_role)
            api = role_name(identity.marketplace_api_runtime_role)
            invalid = executor == api
        except ExecutorIdentityDenied:
            invalid = True
        _require(not invalid)
        self._factory, self._identity = session_factory, identity

    def admit_request(self, organization_id, marketplace_account_id, method, path, minimum_interval_seconds):
        params = _scope(organization_id, marketplace_account_id)
        _require(type(method) is str and type(path) is str and (method, path) in _OPERATIONS
                 and type(minimum_interval_seconds) is int and minimum_interval_seconds == 900)
        return self._execute(params)

    def record_cooldown(self, organization_id, marketplace_account_id, next_not_before):
        params = _scope(organization_id, marketplace_account_id)
        return self._execute(params, feedback=_deadline(next_not_before))

    def _execute(self, params, *, feedback=None):
        session, owned, result, code = None, False, False, None
        try:
            session = self._factory()
            # Do not rollback, close or join a caller's root/dirty Session.
            _require(isinstance(session, Session) and not session.in_transaction()
                     and not session.in_nested_transaction() and not session.new
                     and not session.dirty and not session.deleted and session.is_active,
                     "WB_PRICE_QUOTA_DENIED")
            bind = session.get_bind()
            _require(isinstance(bind, Engine) and bind.dialect.name == "postgresql", "WB_PRICE_QUOTA_DENIED")
            owned = True
            session.begin()
            _require_clean_publication_root(session)
            root = session.get_transaction()
            physical = _physical_connection(session).get_transaction()
            verify_executor_login(session, identity=self._identity)
            set_tenant_context(session, params["org"])
            set_marketplace_account_context(session, organization_id=params["org"], marketplace_account_id=params["account"])
            account_query = select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.organization_id == params["org"],
                MarketplaceAccountRow.marketplace_account_id == params["account"],
                MarketplaceAccountRow.marketplace == "wb").with_for_update(read=True)
            _require(session.scalar(account_query) == params["account"], "WB_PRICE_QUOTA_DENIED")

            def closing(current):
                _require(current is session and tuple(current.dispatch.before_commit)[-1:] == (closing,),
                         "WB_PRICE_QUOTA_DENIED")
                _require_clean_publication_root(current)
                _require(current.get_transaction() is root and root.is_active
                         and _physical_connection(current).get_transaction() is physical, "WB_PRICE_QUOTA_DENIED")
                verify_executor_login(current, identity=self._identity)
                scope = current.execute(text("SELECT current_setting('app.organization_id',true), "
                    "current_setting('app.marketplace_account_id',true)")).one()
                _require(tuple(scope) == (str(params["org"]), str(params["account"])), "WB_PRICE_QUOTA_DENIED")
                _require(current.scalar(account_query) == params["account"], "WB_PRICE_QUOTA_DENIED")
                # Query hooks can queue ORM work or append callbacks during the
                # validation SQL. Finish with only no-SQL state inspections.
                _require(current.is_active and current.get_transaction() is root and root.is_active
                         and not current.in_nested_transaction() and not current.new
                         and not current.dirty and not current.deleted
                         and _physical_connection(current).get_transaction() is physical
                         and tuple(current.dispatch.before_commit)[-1:] == (closing,), "WB_PRICE_QUOTA_DENIED")

            event.listen(session, "before_commit", closing)
            session.execute(text("INSERT INTO wb_price_quota (organization_id,marketplace_account_id,provider,category,"
                "next_allowed_at,created_at,updated_at) VALUES (:org,:account,'wb','prices_discounts',"
                "clock_timestamp(),clock_timestamp(),clock_timestamp()) ON CONFLICT DO NOTHING"), params)
            previous = session.execute(text("SELECT next_allowed_at FROM wb_price_quota WHERE "
                + _PREDICATE + " FOR UPDATE"), params).scalar_one()
            now = _deadline(session.scalar(text("SELECT clock_timestamp()")))
            if feedback is not None:
                session.execute(text("UPDATE wb_price_quota SET next_allowed_at=GREATEST(next_allowed_at,:deadline,:now),"
                    "updated_at=:now WHERE " + _PREDICATE), {**params, "deadline": feedback, "now": now})
                result = True
            elif _deadline(previous) <= now:
                session.execute(text("UPDATE wb_price_quota SET next_allowed_at=:deadline,updated_at=:now WHERE "
                    + _PREDICATE), {**params, "deadline": now + timedelta(seconds=900), "now": now})
                result = True
            session.commit()
        except WbPriceQuotaError as error:
            code = error.code
        except ExecutorIdentityDenied:
            code = "WB_PRICE_QUOTA_DENIED"
        except PublicationGuardError as error:
            code = "WB_PRICE_QUOTA_UNAVAILABLE" if error.code == "publication_persistence_failed" else "WB_PRICE_QUOTA_DENIED"
        except Exception:  # noqa: BLE001 - uncertain commit grants nobody permission.
            code = "WB_PRICE_QUOTA_UNAVAILABLE"
        finally:
            if owned:
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - still close after rollback failure.
                    code = "WB_PRICE_QUOTA_UNAVAILABLE"
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - never return True on uncertain cleanup.
                    code = "WB_PRICE_QUOTA_UNAVAILABLE"
        if code is not None:
            raise WbPriceQuotaError(code)
        return result
