import ast
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.orders import make_avito_source_line_key
from app.orders.browser_envelope import (
    BROWSER_ADAPTER_VERSION,
    BROWSER_ENVELOPE_VERSION,
    MAX_BROWSER_ENVELOPE_BYTES,
    BrowserEnvelopeValidationError,
    decode_avito_browser_envelope,
)

FIXTURE = Path(__file__).parent / "fixtures/orders/avito_browser_envelope_v1.json"
RECEIPT = datetime(2026, 9, 9, 10, tzinfo=UTC)


def body():
    return json.loads(FIXTURE.read_bytes())


def decode(value=None, **kwargs):
    return decode_avito_browser_envelope(
        json.dumps(body() if value is None else value).encode(),
        organization_id=kwargs.pop("organization_id", 91001),
        marketplace_account_id=kwargs.pop("marketplace_account_id", 91101),
        observed_at=kwargs.pop("observed_at", RECEIPT),
        **kwargs,
    )


def test_partial_only_exact_identity_capture_and_no_readiness_inference():
    result = decode()
    manifest = result.manifest
    assert manifest.coverage_state == "partial"
    assert manifest.expected_order_count is None
    assert manifest.source_kind == "avito-browser"
    assert manifest.adapter_version == BROWSER_ADAPTER_VERSION
    assert manifest.source_contract_version == BROWSER_ENVELOPE_VERSION
    assert manifest.pages[0].number == 1 and manifest.pages[0].terminal is False
    assert result.captured_at_text == body()["captured_at"]
    assert result.captured_at == datetime(2026, 9, 9, 9, 34, 56, 123456, tzinfo=UTC)
    row = manifest.observations[0]
    assert row.effective_at is None and row.observed_at == RECEIPT
    assert json.loads(row.source_revision)["captured_at"] == result.captured_at_text
    assert row.identity.external_order_id == "000-synthetic-order"
    assert row.status.raw_status == "synthetic-unknown-status"
    assert (
        row.status.canonical_status is None and row.status.mapping_state == "unmapped"
    )
    assert row.status.mapping_version == "avito-order-status-v1"
    assert [item.quantity for item in row.items] == [3, 1]
    assert len({item.identity.source_line_key for item in row.items}) == 2
    assert (
        result.source_run_key
        == "avito-browser-envelope-v1:11111111-1111-4111-8111-111111111111"
    )
    assert (
        manifest.source_snapshot
        == f"{BROWSER_ENVELOPE_VERSION}:{result.payload_checksum}"
    )
    with pytest.raises(FrozenInstanceError):
        result.payload_checksum = "changed"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("on_confirmation", "pending_confirmation"),
        ("ready_to_ship", "ready_for_fulfillment"),
        ("in_transit", "in_delivery"),
        ("delivered", "delivered"),
        ("closed", "closed"),
        ("canceled", "cancelled"),
        ("on_return", "returning"),
        ("in_dispute", "disputed"),
        ("returned", None),
        ("READY_TO_SHIP", None),
    ],
)
def test_status_mapping_and_returns_are_explicit(raw, expected):
    value = body()
    value["orders"][0]["raw_status"] = raw
    result = decode(value)
    status = result.manifest.observations[0].status
    assert status.raw_status == raw and status.canonical_status == expected
    assert result.manifest.coverage_state == "partial"


def test_stable_line_wins_and_explicit_occurrence_survives_reordering():
    value = body()
    item = value["orders"][0]["items"][0]
    item["stable_order_line_id"] = "000-synthetic-stable-line"
    item["external_item_id"] = None
    item["occurrence_index"] = 7
    first = decode(value)
    first_item = first.manifest.observations[0].items[0]
    assert first_item.identity.source_line_key == make_avito_source_line_key(
        "000-synthetic-order", None, 7, "000-synthetic-stable-line"
    )
    value["orders"][0]["items"].reverse()
    reordered = decode(value)
    assert {item.identity for item in first.manifest.observations[0].items} == {
        item.identity for item in reordered.manifest.observations[0].items
    }
    assert first.payload_checksum != reordered.payload_checksum
    assert first.source_run_key == reordered.source_run_key
    assert first.manifest.checksum != reordered.manifest.checksum


def test_checksum_is_object_order_independent_and_receipt_time_is_not_payload_identity():
    value = body()
    result = decode(value)
    reordered_keys = dict(reversed(list(value.items())))
    again = decode(reordered_keys, observed_at=RECEIPT + timedelta(hours=1))
    assert result.payload_checksum == again.payload_checksum
    assert result.manifest.checksum == again.manifest.checksum
    value["orders"][0]["items"][0]["quantity"] += 1
    changed = decode(value)
    assert changed.source_run_key == result.source_run_key
    assert changed.manifest.checksum != result.manifest.checksum


def test_same_external_ids_coexist_in_trusted_org_and_account_scopes():
    identities = {
        decode(organization_id=org, marketplace_account_id=account)
        .manifest.observations[0]
        .identity
        for org, account in ((91001, 91101), (91001, 91102), (91002, 91103))
    }
    assert len(identities) == 3
    assert {value.external_order_id for value in identities} == {"000-synthetic-order"}


@pytest.mark.parametrize(
    "name,value",
    [
        ("organization_id", 91001),
        ("marketplace_account_id", 91101),
        ("marketplace", "avito"),
        ("complete", True),
        ("coverage_state", "complete"),
        ("expected_order_count", 1),
        ("returns", []),
        ("token", "synthetic-token"),
        ("buyer_name", "synthetic-person"),
        ("capturedAt", "synthetic-legacy"),
    ],
)
def test_extra_body_fields_are_rejected_without_echo(name, value):
    envelope = body()
    envelope[name] = value
    with pytest.raises(BrowserEnvelopeValidationError) as error:
        decode(envelope)
    assert str(error.value) == "orders_browser_envelope_invalid"


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), "avito-browser-evidence-v2"),
        (("idempotency_key",), "11111111111141118111111111111111"),
        (("idempotency_key",), "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        (("idempotency_key",), "00000000-0000-0000-0000-000000000000"),
        (("captured_at",), "2026-09-09T12:00:00"),
        (("captured_at",), "2026-09-09T12:00:00-00:00"),
        (("captured_at",), "2026-09-09T12:00:00+01:99"),
        (("captured_at",), "2026-02-30T12:00:00Z"),
        (("orders",), []),
        (("orders", 0, "external_order_id"), 123),
        (("orders", 0, "external_order_id"), " synthetic-id"),
        (("orders", 0, "raw_status"), None),
        (("orders", 0, "raw_status"), "ready_to_ship "),
        (("orders", 0, "raw_status"), "synthetic\u0000status"),
        (("orders", 0, "raw_status"), "synthetic\ud800status"),
        (("orders", 0, "items"), []),
        (("orders", 0, "items", 0, "quantity"), 0),
        (("orders", 0, "items", 0, "quantity"), True),
        (("orders", 0, "items", 0, "quantity"), 1.0),
        (("orders", 0, "items", 0, "quantity"), 2147483648),
        (("orders", 0, "items", 0, "occurrence_index"), -1),
        (("orders", 0, "items", 0, "occurrence_index"), False),
        (("orders", 0, "items", 0, "external_item_id"), None),
        (("orders", 0, "items", 0, "stable_order_line_id"), ""),
    ],
)
def test_malformed_exact_values(path, value):
    envelope = body()
    node = envelope
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(
        BrowserEnvelopeValidationError, match="^orders_browser_envelope_invalid$"
    ):
        decode(envelope)


@pytest.mark.parametrize("level", ["body", "order", "item"])
def test_missing_mandatory_fields_and_unknown_nested_fields(level):
    envelope = body()
    node = {
        "body": envelope,
        "order": envelope["orders"][0],
        "item": envelope["orders"][0]["items"][0],
    }[level]
    key = next(iter(node))
    saved = node.pop(key)
    with pytest.raises(BrowserEnvelopeValidationError):
        decode(envelope)
    node[key] = saved
    node["synthetic_unrecognized"] = "synthetic-text"
    with pytest.raises(BrowserEnvelopeValidationError):
        decode(envelope)


@pytest.mark.parametrize("level", ["orders", "items"])
def test_duplicate_identities_are_rejected(level):
    envelope = body()
    rows = envelope["orders"] if level == "orders" else envelope["orders"][0]["items"]
    rows.append(rows[0])
    with pytest.raises(BrowserEnvelopeValidationError):
        decode(envelope)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xff",
        b"{}",
        b"null",
        b"[]",
        b'{"x":NaN}',
        b'{"x":1,"x":2}',
        b'{"x":{"y":1,"y":2}}',
        b"[" * 2000 + b"]" * 2000,
        b" " * (MAX_BROWSER_ENVELOPE_BYTES + 1),
    ],
)
def test_raw_encoding_limits_duplicate_keys_and_nesting(payload):
    with pytest.raises(BrowserEnvelopeValidationError):
        decode_avito_browser_envelope(
            payload,
            organization_id=91001,
            marketplace_account_id=91101,
            observed_at=RECEIPT,
        )


@pytest.mark.parametrize(
    "context",
    [
        {"organization_id": True},
        {"organization_id": 0},
        {"marketplace_account_id": 2147483648},
        {"observed_at": RECEIPT.replace(tzinfo=None)},
    ],
)
def test_invalid_trusted_context_is_still_rejected(context):
    with pytest.raises(BrowserEnvelopeValidationError):
        decode(**context)


def test_future_browser_clock_is_not_provider_effective_time():
    envelope = body()
    envelope["captured_at"] = "2099-01-01T00:00:00Z"
    result = decode(envelope)
    assert result.captured_at.year == 2099
    assert result.manifest.observations[0].effective_at is None
    assert result.manifest.coverage_state == "partial"


def test_pure_import_guard():
    source = Path(__file__).parents[1] / "app/orders/browser_envelope.py"
    tree = ast.parse(source.read_text())
    imports = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert not any(
        name
        and any(
            part in name
            for part in ("sqlalchemy", "fastapi", "celery", "httpx", "requests")
        )
        for name in imports
    )
