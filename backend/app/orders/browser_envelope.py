"""Strict browser evidence decoding, not authorization or complete synchronization."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    OrderContractValidationError,
    make_avito_source_line_key,
    map_avito_status,
)
from app.orders.ingestion import (
    ObservedOrderItem,
    OrderManifest,
    OrderObservation,
    OrderPage,
    _instant,
)

BROWSER_ENVELOPE_VERSION = "avito-browser-evidence-v1"
BROWSER_ADAPTER_VERSION = "avito-browser-envelope-v1"
MAX_BROWSER_ENVELOPE_BYTES = 1_048_576
_INT4_MAX = 2_147_483_647
_CAPTURE_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class BrowserEnvelopeValidationError(OrderContractValidationError):
    """A fixed public error code, without untrusted payload text."""

    def __init__(self):
        super().__init__("orders_browser_envelope_invalid")


@dataclass(frozen=True, slots=True)
class DecodedBrowserEnvelope:
    idempotency_key: UUID
    captured_at: datetime
    captured_at_text: str
    payload_checksum: str
    manifest: OrderManifest

    @property
    def source_run_key(self) -> str:
        return f"avito-browser-envelope-v1:{self.idempotency_key}"


def _invalid():
    raise BrowserEnvelopeValidationError()


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _invalid()
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _text(value, *, nullable=False):
    if value is None and nullable:
        return None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    ):
        _invalid()
    return value


def _integer(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= _INT4_MAX:
        _invalid()
    return value


def _array(value):
    if type(value) is not list or not value:
        _invalid()
    return value


def _capture(value):
    value = _text(value)
    if not _CAPTURE_INSTANT.fullmatch(value) or value.endswith("-00:00"):
        _invalid()
    instant = datetime.fromisoformat(value)
    _instant(instant)
    return instant


def decode_avito_browser_envelope(
    payload: bytes,
    *,
    organization_id: int,
    marketplace_account_id: int,
    observed_at: datetime,
) -> DecodedBrowserEnvelope:
    """Owner and receipt time come from trusted context, never the JSON body.

    Capture time is browser evidence only; it is not provider effective time or
    ordering authority. A future capture is retained, not used to advance current
    state. Only a partial, nonterminal manifest is produced, even for one order.
    """
    try:
        _integer(organization_id)
        _integer(marketplace_account_id)
        _instant(observed_at)
        if (
            type(payload) is not bytes
            or not 0 < len(payload) <= MAX_BROWSER_ENVELOPE_BYTES
        ):
            _invalid()
        body = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: _invalid(),
        )
        _object(body, ("schema_version", "idempotency_key", "captured_at", "orders"))
        if body["schema_version"] != BROWSER_ENVELOPE_VERSION:
            _invalid()
        key_text = _text(body["idempotency_key"])
        key = UUID(key_text)
        if str(key) != key_text or key.int == 0:
            _invalid()
        captured_text = _text(body["captured_at"])
        captured_at = _capture(captured_text)
        orders = _array(body["orders"])
        canonical_bytes = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        checksum = hashlib.sha256(canonical_bytes).hexdigest()
        snapshot = f"{BROWSER_ENVELOPE_VERSION}:{checksum}"
        # Existing opaque source_revision stores explicit capture provenance,
        # not a fabricated provider revision. effective_at remains unknown.
        capture_provenance = json.dumps(
            {"schema_version": BROWSER_ENVELOPE_VERSION, "captured_at": captured_text},
            sort_keys=True,
            separators=(",", ":"),
        )
        observations = []
        for order in orders:
            _object(order, ("external_order_id", "raw_status", "items"))
            identity = ExternalOrderIdentity(
                organization_id,
                marketplace_account_id,
                "avito",
                _text(order["external_order_id"]),
            )
            status = map_avito_status(_text(order["raw_status"]))
            items = []
            for item in _array(order["items"]):
                _object(
                    item,
                    (
                        "external_item_id",
                        "stable_order_line_id",
                        "occurrence_index",
                        "quantity",
                    ),
                )
                external_item_id = _text(item["external_item_id"], nullable=True)
                stable_line_id = _text(item["stable_order_line_id"], nullable=True)
                if external_item_id is None and stable_line_id is None:
                    _invalid()
                occurrence = _integer(item["occurrence_index"], minimum=0)
                quantity = _integer(item["quantity"])
                source_line_key = make_avito_source_line_key(
                    identity.external_order_id,
                    external_item_id,
                    occurrence,
                    stable_line_id,
                )
                items.append(
                    ObservedOrderItem(
                        ExternalOrderItemIdentity(
                            identity, source_line_key, external_item_id, occurrence
                        ),
                        quantity,
                        stable_order_line_id=stable_line_id,
                    )
                )
            observations.append(
                OrderObservation(
                    identity,
                    "avito-browser",
                    BROWSER_ADAPTER_VERSION,
                    capture_provenance,
                    None,
                    observed_at,
                    status,
                    tuple(items),
                )
            )
        manifest = OrderManifest(
            organization_id,
            marketplace_account_id,
            "avito",
            "avito-browser",
            BROWSER_ADAPTER_VERSION,
            BROWSER_ENVELOPE_VERSION,
            snapshot,
            "partial",
            None,
            (OrderPage(1, snapshot, False, tuple(observations)),),
        )
        return DecodedBrowserEnvelope(
            key, captured_at, captured_text, checksum, manifest
        )
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise BrowserEnvelopeValidationError() from None
