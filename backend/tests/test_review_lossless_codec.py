"""Lossless storage representation is not part of semantic Review hashing."""

import importlib

import pytest

from app.reviews.ingestion_contract import ReviewRepositoryError


def codec():
    return importlib.import_module("app.reviews.lossless_storage")


@pytest.mark.parametrize(
    "value,encoded",
    [
        (None, None),
        ("", b""),
        ("\0", b"\0"),
        ("a\0b", b"a\0b"),
        ("е\u0301🚀", b"\xd0\xb5\xcc\x81\xf0\x9f\x9a\x80"),
        (" x ", b" x "),
    ],
)
def test_scalar_bytes_and_legacy_preserve_exact_value(value, encoded):
    c = codec()
    assert c.encode_scalar_pair(value, "text") == {"text": None, "text_utf8": encoded}
    assert c.decode_scalar_pair({"text": None, "text_utf8": encoded}, "text") == value
    assert c.decode_scalar_pair({"text": value, "text_utf8": None}, "text") == value


@pytest.mark.parametrize(
    "legacy,encoded,required",
    [
        ("x", b"x", False),
        (None, None, True),
        (None, b"\xff", False),
        (None, "bytes?", False),
        (42, None, False),
        ("\ud800", None, False),
    ],
)
def test_invalid_scalar_pair_has_safe_error(legacy, encoded, required):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_scalar_pair(
            {"text": legacy, "text_utf8": encoded}, "text", required=required
        )


@pytest.mark.parametrize("value", [None, "", "\ud800", 42])
def test_required_nonempty_scalar_write_rejects_invalid_value(value):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().encode_scalar_pair(
            value, "source_run_id", required=True, allow_empty=False
        )


def test_coverage_exact_json_bytes_preserve_nested_nul():
    value = {
        "from": None,
        "to": None,
        "streams": [{"name": "a\0🚀", "terminalReached": True}],
        "pagesObserved": 1,
        "providerEndReached": True,
    }
    expected = b'{"from":null,"pagesObserved":1,"providerEndReached":true,"streams":[{"name":"a\\u0000\xf0\x9f\x9a\x80","terminalReached":true}],"to":null}'
    c = codec()
    assert c.encode_coverage_pair(value) == {
        "coverage": None,
        "coverage_utf8": expected,
    }
    assert (
        c.decode_coverage_pair({"coverage": None, "coverage_utf8": expected}) == value
    )
    assert c.decode_coverage_pair({"coverage": value, "coverage_utf8": None}) == value


@pytest.mark.parametrize(
    "encoded",
    [
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b'{"x":1,"x":2}',
        b'{"from":null,"to":null,"streams":[{"name":"a","name":"b","terminalReached":true}],"pagesObserved":1,"providerEndReached":true}',
        b'{"from":null,"to":null,"streams":[{"name":"a","terminalReached":true},{"name":"a","terminalReached":true}],"pagesObserved":1,"providerEndReached":true}',
    ],
)
def test_invalid_coverage_cannot_pass_sql_object_admission_alone(encoded):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_coverage_pair({"coverage": None, "coverage_utf8": encoded})


@pytest.mark.parametrize("row", [{}, {"text": "x"}, {"text_utf8": b"x"}])
def test_missing_scalar_representation_column_is_not_silently_legacy(row):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_scalar_pair(row, "text")


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"coverage": {}},
        {"coverage_utf8": b"{}"},
        {"coverage": {}, "coverage_utf8": b"{}"},
        {"coverage": None, "coverage_utf8": None},
        {"coverage": None, "coverage_utf8": "{}"},
    ],
)
def test_coverage_pair_requires_exactly_one_valid_representation(row):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_coverage_pair(row)


@pytest.mark.parametrize(
    "row", [{"key": "", "key_utf8": None}, {"key": None, "key_utf8": b""}]
)
def test_empty_required_key_is_not_confused_with_optional_empty_text(row):
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_scalar_pair(row, "key", required=True, allow_empty=False)


def test_deep_invalid_coverage_returns_safe_error_instead_of_parser_exception():
    encoded = b'{"unknown":' + b"[" * 20_000 + b"0" + b"]" * 20_000 + b"}"
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
        codec().decode_coverage_pair({"coverage": None, "coverage_utf8": encoded})
