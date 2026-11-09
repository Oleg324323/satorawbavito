"""Dormant token-only browser evidence sink; no source acquisition or activation."""

import json
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.orders.browser_envelope import (
    BROWSER_ENVELOPE_VERSION,
    DecodedBrowserEnvelope,
    decode_avito_browser_envelope,
)
from app.orders.publication_service import _persist_orders_manifest
from app.platform.integrations.ingestion_api import (
    CommittedIngestionAcknowledgement,
    IngestionAPIError,
)
from app.platform.integrations.ingestion_publication_guard import (
    acquire_ingestion_publication_guard,
)
from app.platform.integrations.ingestion_tokens import (
    INGESTION_TOKEN_SCOPE,
    VerifiedIngestionToken,
)
from app.platform.integrations.publication_guard import PublicationGuardError

_ACTION = "orders.browser_ingestion_published"


def _validated_envelope(envelope, observed_at):
    # A dataclass constructor is not decoder provenance. Rebuild only the closed
    # wire fields and compare with the real decoder before accepting persistence.
    try:
        if type(envelope) is not DecodedBrowserEnvelope:
            raise ValueError
        manifest = envelope.manifest
        payload = {
            "schema_version": BROWSER_ENVELOPE_VERSION,
            "idempotency_key": str(envelope.idempotency_key),
            "captured_at": envelope.captured_at_text,
            "orders": [
                {
                    "external_order_id": row.identity.external_order_id,
                    "raw_status": row.status.raw_status,
                    "items": [
                        {
                            "external_item_id": item.identity.external_item_id,
                            "stable_order_line_id": item.stable_order_line_id,
                            "occurrence_index": item.identity.occurrence_index,
                            "quantity": item.quantity,
                        }
                        for item in row.items
                    ],
                }
                for row in manifest.observations
            ],
        }
        validated = decode_avito_browser_envelope(
            json.dumps(payload, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")).encode("utf-8"),
            organization_id=manifest.organization_id,
            marketplace_account_id=manifest.marketplace_account_id,
            observed_at=observed_at,
        )
        if validated != envelope:
            raise ValueError
        return validated
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise IngestionAPIError("ingestion_body_invalid") from None


class OrdersIngestionSink:
    """Explicit trusted factory injection; never opens the application's global pool."""

    def __init__(self, *, session_factory: Callable[[], Session]):
        if not callable(session_factory):
            raise IngestionAPIError("ingestion_unavailable")
        self._session_factory = session_factory

    def __call__(
        self, *, raw_bearer: str, admission: VerifiedIngestionToken,
        envelope: object, observed_at: datetime,
    ) -> CommittedIngestionAcknowledgement:
        session = None
        owned = False
        try:
            if type(admission) is not VerifiedIngestionToken:
                raise IngestionAPIError("ingestion_token_invalid")
            envelope = _validated_envelope(envelope, observed_at)
            session = self._session_factory()
            if (
                not isinstance(session, Session)
                or not isinstance(session.bind, Engine)
                or not session.is_active or session.in_transaction()
                or session.new or session.dirty or session.deleted
            ):
                raise IngestionAPIError("ingestion_unavailable")
            owned = True
            with session.begin():
                guard = acquire_ingestion_publication_guard(session, raw_bearer=raw_bearer)
                binding = admission.account_binding
                current = guard.account_binding
                if (
                    admission.owner != guard.owner
                    or admission.token_id != guard.token_id
                    or admission.expires_at != guard.expires_at
                    or admission.scope != INGESTION_TOKEN_SCOPE
                    or binding.binding_schema_version != 1
                    or (binding.marketplace_account_id, binding.provider,
                        binding.external_account_id, binding.credential_ref,
                        binding.binding_version)
                    != (current.marketplace_account_id, current.provider,
                        current.external_account_id, current.credential_ref,
                        guard.binding_version)
                    or (envelope.manifest.organization_id,
                        envelope.manifest.marketplace_account_id,
                        envelope.manifest.marketplace)
                    != (guard.owner.organization_id,
                        guard.owner.marketplace_account_id, guard.owner.provider)
                ):
                    raise IngestionAPIError("ingestion_token_invalid")
                guard.require_participation(session)
                params = {"org": guard.owner.organization_id,
                          "account": guard.owner.marketplace_account_id,
                          "key": envelope.source_run_key}
                prior = session.execute(text(
                    "SELECT sync_run_id,payload_checksum FROM order_sync_runs "
                    "WHERE organization_id=:org AND marketplace_account_id=:account "
                    "AND source_kind COLLATE \"C\"='avito-browser' "
                    "AND source_run_key COLLATE \"C\"=:key FOR UPDATE"
                ), params).one_or_none()
                if prior is not None:
                    receipt = session.execute(text(
                        "SELECT details FROM lk_audit_events WHERE organization_id=:org "
                        "AND action=:action AND object_type='orders_sync_run' AND object_id=:run"
                    ), dict(params, action=_ACTION, run=str(prior.sync_run_id))).scalar_one_or_none()
                    if (
                        prior.payload_checksum != envelope.manifest.checksum
                        or not isinstance(receipt, dict)
                        or receipt.get("payload_checksum") != envelope.payload_checksum
                        or receipt.get("binding_version") != guard.binding_version
                    ):
                        raise IngestionAPIError("ingestion_conflict")
                result = _persist_orders_manifest(
                    session, manifest=envelope.manifest, account=current,
                    source_run_key=envelope.source_run_key, actor_user_id=None,
                )
                if not result.replayed:
                    session.add(LkAuditEventRow(
                        organization_id=guard.owner.organization_id,
                        actor_user_id=None, action=_ACTION,
                        object_type="orders_sync_run", object_id=str(result.run_id),
                        details={"token_id": str(guard.token_id),
                                 "binding_version": guard.binding_version,
                                 "payload_checksum": envelope.payload_checksum},
                    ))
                guard.require_participation(session)
            # Only a naturally completed physical commit can reach this return.
            return CommittedIngestionAcknowledgement(result.run_id, result.state, result.replayed)
        except PublicationGuardError:
            raise IngestionAPIError("ingestion_token_invalid") from None
        except SQLAlchemyError:
            # An uncertain commit is unavailable, never a fabricated success or
            # automatic retry. The same explicit key can later resolve by replay.
            raise IngestionAPIError("ingestion_unavailable") from None
        except IngestionAPIError:
            raise
        except Exception:
            raise IngestionAPIError("ingestion_unavailable") from None
        finally:
            raw_bearer = None
            envelope = None
            if owned:
                try:
                    session.close()
                except Exception:
                    raise IngestionAPIError("ingestion_unavailable") from None
