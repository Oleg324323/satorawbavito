"""Pure upload-ID projection from the original decoded POST response body.

Call before legacy `_as_int` or DTO coercion. Never pass a history query argument
or reconstructed PriceApplyResponse. This projection is not authentication,
committed-marker proof or receipt persistence. The decoded entrypoint cannot
validate raw JSON keys; the explicit byte entrypoint performs that validation.
Caller-provided transport metadata is trusted adapter context, not a capability.
No current WB client or sending flow imports this module.
"""

import json
import re
from decimal import Decimal, DecimalException


class UploadReceiptValidationError(ValueError):
    def __init__(self):
        super().__init__("invalid_upload_receipt")


def _upload_id(value):
    if type(value) is int and value > 0:
        return format(Decimal(value), "f")
    if type(value) is str and re.fullmatch(r"[1-9][0-9]*", value) is not None:
        return value
    raise UploadReceiptValidationError()


def extract_post_upload_id(
    payload, *, method, path, status_code, ok, apply_mode
) -> str:
    """Return only an exact positive decimal ID, never an applied status.

    Missing/invalid receipt after a marker is uncertainty, not proof of rejection
    and never permission to resend. No raw error text or payload is retained.
    """
    if (
        type(method) is not str
        or method != "POST"
        or type(path) is not str
        or path != "/api/v2/upload/task"
        or type(status_code) is not int
        or not 200 <= status_code < 300
        or ok is not True
        or type(apply_mode) is not str
        or apply_mode != "wb_api"
    ):
        raise UploadReceiptValidationError()
    if type(payload) is not dict:
        raise UploadReceiptValidationError()
    data = payload.get("data", payload)
    if type(data) is not dict:
        raise UploadReceiptValidationError()
    for row in (payload, data):
        if "error" in row and row["error"] is not False:
            raise UploadReceiptValidationError()
        if "errorText" in row and row["errorText"] != "":
            raise UploadReceiptValidationError()
    ids = [_upload_id(data[key]) for key in ("id", "uploadID") if key in data]
    if not ids or any(value != ids[0] for value in ids):
        raise UploadReceiptValidationError()
    return ids[0]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UploadReceiptValidationError()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise UploadReceiptValidationError()


def extract_post_upload_id_from_bytes(
    raw_body, *, method, path, status_code, ok, apply_mode, max_response_bytes
) -> str:
    """Validate bounded original UTF-8 JSON before decoded identity projection.

    Byte budget is explicit trusted adapter policy, not an invented provider limit.
    Duplicate keys (also escape-equivalent keys) and non-JSON numbers are rejected.
    Integer lexemes remain exact text, avoiding float conversion and Python's
    integer-string digit limit; decimal/exponent numbers stay Decimal and cannot
    become an upload ID. No response data/checksum is stored or returned.
    This still does not authenticate transport or prove a dispatch commit.
    """
    if (
        type(raw_body) is not bytes
        or type(max_response_bytes) is not int
        or max_response_bytes <= 0
        or len(raw_body) > max_response_bytes
    ):
        raise UploadReceiptValidationError()
    try:
        payload = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_int=str,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError, DecimalException, RecursionError):
        raise UploadReceiptValidationError() from None
    return extract_post_upload_id(
        payload,
        method=method,
        path=path,
        status_code=status_code,
        ok=ok,
        apply_mode=apply_mode,
    )
