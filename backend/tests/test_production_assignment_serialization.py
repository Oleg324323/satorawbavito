import hashlib

import pytest

from app.modules.production import (
    AssignmentCommand,
    assignment_command_checksum,
    deserialize_assignment_command,
    serialize_assignment_command,
)

COMMAND = AssignmentCommand(1, 2, "synthetic-key", 3, "synthetic-reason")
GOLDEN = b'{"command":{"catalog_sku_id":3,"expected_version":2,"idempotency_key":"synthetic-key","reason":"synthetic-reason","work_item_id":1},"schema_version":1}'


def test_command_codec_matches_schema_request_canonical_bytes():
    assert serialize_assignment_command(COMMAND) == GOLDEN
    assert assignment_command_checksum(COMMAND) == hashlib.sha256(GOLDEN).hexdigest()
    assert deserialize_assignment_command(GOLDEN) == COMMAND


@pytest.mark.parametrize(
    "payload",
    [
        GOLDEN + b" ",
        GOLDEN.replace(b'"schema_version":1', b'"schema_version":true'),
        GOLDEN.replace(b'"work_item_id":1', b'"work_item_id":true'),
        GOLDEN.replace(b'"work_item_id":1', b'"actor_id":99,"work_item_id":1'),
        GOLDEN.replace(b'"work_item_id":1', b'"work_item_id":1,"work_item_id":1'),
        GOLDEN.decode("ascii"),
        b"[]",
    ],
)
def test_noncanonical_or_unexpected_command_payload_is_rejected(payload):
    with pytest.raises(ValueError):
        deserialize_assignment_command(payload)


def test_unicode_exact_command_remains_ascii_on_wire():
    command = AssignmentCommand(
        1, 2, "synthetic-\u043a\u043b\u044e\u0447", 3, "synthetic-\U0001f600-e\u0301"
    )
    encoded = serialize_assignment_command(command)
    assert encoded.isascii()
    assert b"\\ud83d\\ude00" in encoded and b"e\\u0301" in encoded
    assert deserialize_assignment_command(encoded) == command


def test_deep_invalid_command_payload_has_typed_validation_error():
    depth = 10000
    with pytest.raises(ValueError, match="Invalid assignment receipt payload"):
        deserialize_assignment_command(b"[" * depth + b"]" * depth)
