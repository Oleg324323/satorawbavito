"""Strict0065 representation codec; never part of semantic Review hashes."""

import json

from app.reviews.ingestion_contract import ReviewRepositoryError, snapshot_manifest


def encode_scalar_pair(value, key, *, required=False, allow_empty=True):
    if value is None:
        if required:
            raise ReviewRepositoryError()
        encoded = None
    else:
        if type(value) is not str or (not allow_empty and value == ""):
            raise ReviewRepositoryError()
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ReviewRepositoryError() from None
    return {key: None, key + "_utf8": encoded}


def decode_scalar_pair(row, key, *, required=False, allow_empty=True):
    if key not in row or key + "_utf8" not in row:
        raise ReviewRepositoryError()
    legacy, encoded = row[key], row[key + "_utf8"]
    if encoded is not None:
        if legacy is not None or type(encoded) is not bytes:
            raise ReviewRepositoryError()
        try:
            value = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ReviewRepositoryError() from None
    else:
        value = legacy
    encode_scalar_pair(value, key, required=required, allow_empty=allow_empty)
    return value


def _coverage(value):
    return snapshot_manifest((), value, "partial", {})[1]


def encode_coverage_pair(value):
    try:
        encoded = json.dumps(
            _coverage(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeError, ValueError, TypeError):
        raise ReviewRepositoryError() from None
    return {"coverage": None, "coverage_utf8": encoded}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewRepositoryError()
        result[key] = value
    return result


def decode_coverage_pair(row):
    if "coverage" not in row or "coverage_utf8" not in row:
        raise ReviewRepositoryError()
    legacy, encoded = row["coverage"], row["coverage_utf8"]
    if encoded is not None:
        if legacy is not None or type(encoded) is not bytes:
            raise ReviewRepositoryError()
        try:
            value = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise ReviewRepositoryError() from None
    else:
        value = legacy
    return _coverage(value)
