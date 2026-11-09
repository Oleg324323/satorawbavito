"""Decoded POST response identity projection; no WB client/provider calls."""

from decimal import Decimal

import pytest

from app.modules.wb_repricing_upload_receipt import (
    UploadReceiptValidationError,
    extract_post_upload_id,
)


def project(payload, **changes):
    metadata = {
        "method": "POST",
        "path": "/api/v2/upload/task",
        "status_code": 200,
        "ok": True,
        "apply_mode": "wb_api",
    }
    return extract_post_upload_id(payload, **(metadata | changes))


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"id": 146567}, "error": False, "errorText": ""},
        {"data": {"uploadID": "146567"}},
        {"id": 146567},
        {"data": {"id": 146567, "uploadID": "146567"}},
    ],
)
def test_existing_response_shapes_return_only_exact_decimal_identity(payload):
    assert project(payload) == "146567"


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        12.5,
        12.0,
        float("nan"),
        Decimal(12),
        None,
        "",
        " 12",
        "12 ",
        "01",
        "+12",
        "1e2",
        "12.0",
        "１２",
        [],
        {},
    ],
)
def test_no_lossy_coercion_or_fallback_from_present_invalid_id(value):
    with pytest.raises(UploadReceiptValidationError):
        project({"data": {"id": value, "uploadID": 146567}})


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": None},
        {"data": []},
        {"data": {"id": 1, "uploadID": 2}},
        {"data": {"id": 1}, "error": True},
        {"data": {"id": 1}, "error": "false"},
        {"data": {"id": 1}, "errorText": "sensitive-canary"},
    ],
)
def test_missing_conflicting_or_error_response_cannot_make_receipt(payload):
    with pytest.raises(UploadReceiptValidationError) as caught:
        project(payload)
    assert "sensitive-canary" not in str(caught.value)
    assert "sensitive-canary" not in repr(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "GET"},
        {"path": "/api/v2/history/tasks"},
        {"path": "/api/v2/history/goods/task"},
        {"status_code": 429},
        {"status_code": 500},
        {"status_code": True},
        {"status_code": 200.0},
        {"ok": False},
        {"ok": 1},
        {"apply_mode": "local_mock"},
        {"apply_mode": "dry_run"},
    ],
)
def test_non_post_or_unusable_transport_cannot_claim_initial_receipt(changes):
    with pytest.raises(UploadReceiptValidationError):
        project({"data": {"id": 146567}}, **changes)


def test_large_id_is_lossless_without_bigint_or_python_int_string_limit():
    assert project({"id": 2**80}) == "1208925819614629174706176"
    assert project({"id": 10**5000}) == "1" + "0" * 5000


def test_projection_does_not_retain_raw_payload_or_mutate_it():
    payload = {"data": {"id": 146567}, "notes": "sensitive-canary"}
    result = project(payload)
    assert payload == {"data": {"id": 146567}, "notes": "sensitive-canary"}
    payload["data"]["id"] = 99
    assert type(result) is str and result == "146567"


@pytest.mark.parametrize(
    "data",
    [
        {"id": 1, "error": True},
        {"id": 1, "errorText": "sensitive-canary"},
    ],
)
def test_nested_explicit_error_cannot_be_ignored(data):
    with pytest.raises(UploadReceiptValidationError):
        project({"data": data, "error": False})
