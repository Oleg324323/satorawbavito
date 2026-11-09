"""Raw receipt JSON boundary; synthetic bytes only, no transport or storage."""

import pytest

from app.modules.wb_repricing_upload_receipt import (
    UploadReceiptValidationError,
    extract_post_upload_id_from_bytes,
)


def project(raw, **changes):
    arguments = {
        "method": "POST",
        "path": "/api/v2/upload/task",
        "status_code": 200,
        "ok": True,
        "apply_mode": "wb_api",
        "max_response_bytes": 10000,
    }
    return extract_post_upload_id_from_bytes(raw, **(arguments | changes))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"data":{"id":146567},"error":false,"errorText":""}',
        b'{"id":"146567"}',
        b'{"data":{"uploadID":146567}}',
        b'{"data":{"id":146567,"uploadID":"146567"}}',
    ],
)
def test_raw_response_preserves_exact_upload_identity(raw):
    assert project(raw) == "146567"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":1,"id":2}',
        b'{"id":1,"id":1}',
        b'{"data":{"id":1},"data":{"id":2}}',
        b'{"data":{"id":1,"i\\u0064":2}}',
        b'{"data":{"id":1},"ignored":{"x":1,"x":2}}',
        b'{"id":1,"error":true,"error":false}',
    ],
)
def test_duplicate_keys_are_rejected_before_last_value_can_win(raw):
    with pytest.raises(UploadReceiptValidationError):
        project(raw)


@pytest.mark.parametrize(
    "token",
    [
        b"true",
        b"false",
        b"null",
        b"0",
        b"-1",
        b"-0",
        b"1.0",
        b"12.5",
        b"1e2",
        b"1e99999999999999999999999999999999999",
        b"NaN",
        b"Infinity",
        b"-Infinity",
    ],
)
def test_json_number_syntax_cannot_turn_into_a_different_upload_id(token):
    with pytest.raises(UploadReceiptValidationError):
        project(b'{"id":' + token + b"}")


def test_raw_large_integer_avoids_float_and_python_digit_limit():
    digits = b"1" + b"0" * 5000
    assert project(b'{"id":' + digits + b"}") == digits.decode("ascii")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b'{"id":1} trailing',
        b'{"id":1,}',
        b'{"id":1,"note":"\xff"}',
        b'{"id":"sensitive-canary"}',
        b'{"id":1,"note":NaN}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_invalid_json_and_parser_errors_expose_only_safe_error(raw):
    with pytest.raises(UploadReceiptValidationError) as caught:
        project(raw)
    assert str(caught.value) == "invalid_upload_receipt"
    assert "sensitive-canary" not in repr(caught.value)


@pytest.mark.parametrize("raw", ['{"id":1}', bytearray(b'{"id":1}'), {}, None])
def test_only_original_immutable_response_bytes_are_accepted(raw):
    with pytest.raises(UploadReceiptValidationError):
        project(raw)


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "10"])
def test_response_budget_is_explicit_validated_policy(limit):
    with pytest.raises(UploadReceiptValidationError):
        project(b'{"id":1}', max_response_bytes=limit)


def test_exact_byte_budget_boundary_is_not_silently_truncated():
    raw = b'{"id":1}'
    assert project(raw, max_response_bytes=len(raw)) == "1"
    with pytest.raises(UploadReceiptValidationError):
        project(raw, max_response_bytes=len(raw) - 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "GET"},
        {"path": "/api/v2/history/tasks"},
        {"status_code": 429},
        {"ok": False},
        {"apply_mode": "local_mock"},
        {"apply_mode": "dry_run"},
    ],
)
def test_raw_valid_json_is_not_provider_authority(changes):
    with pytest.raises(UploadReceiptValidationError):
        project(b'{"id":1}', **changes)
