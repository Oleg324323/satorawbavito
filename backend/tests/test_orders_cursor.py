from dataclasses import replace

import pytest

from app.orders.cursor import OrdersCursorCodec
from app.platform.integrations.publication_guard import UserSessionPrincipal

PRINCIPAL = UserSessionPrincipal(91001, "synthetic-user", 1, "synthetic-session")
KEY = b"synthetic-cursor-signing-key-only!"


def issue(codec):
    return codec.issue(
        principal=PRINCIPAL,
        accounts=(91101, 91102),
        snapshot_id=17,
        after_position=23,
        query_checksum="a" * 64,
    )


def parse(codec, token, **changes):
    values = {
        "principal": PRINCIPAL,
        "accounts": (91101, 91102),
        "query_checksum": "a" * 64,
    }
    return codec.parse(token, **(values | changes))


def test_signed_cursor_roundtrip_binds_snapshot_position_and_context():
    codec = OrdersCursorCodec(KEY)
    token = issue(codec)
    assert parse(codec, token) == (17, 23)
    assert issue(codec) == token
    assert KEY.decode() not in repr(codec)


@pytest.mark.parametrize(
    "change",
    [
        {"principal": replace(PRINCIPAL, organization_id=91002)},
        {"principal": replace(PRINCIPAL, membership_id=2)},
        {"principal": replace(PRINCIPAL, user_id="synthetic-other")},
        {"principal": replace(PRINCIPAL, session_id="synthetic-other")},
        {"accounts": (91101,)},
        {"query_checksum": "b" * 64},
    ],
)
def test_signed_cursor_rejects_context_substitution(change):
    codec = OrdersCursorCodec(KEY)
    with pytest.raises(ValueError, match="orders_cursor_invalid"):
        parse(codec, issue(codec), **change)


def test_signed_cursor_rejects_tampering_and_different_key():
    codec = OrdersCursorCodec(KEY)
    token = issue(codec)
    with pytest.raises(ValueError, match="orders_cursor_invalid"):
        parse(codec, "X" + token[1:])
    with pytest.raises(ValueError, match="orders_cursor_invalid"):
        parse(OrdersCursorCodec(b"x" * 32), token)


@pytest.mark.parametrize("token", ["", "a.b.c", "=.a", "x" * 20000, 1, None])
def test_malformed_cursor_has_safe_error(token):
    with pytest.raises(ValueError, match="orders_cursor_invalid"):
        parse(OrdersCursorCodec(KEY), token)


@pytest.mark.parametrize("key", [b"", b"short", "not-bytes", None])
def test_cursor_requires_nontrivial_explicit_key(key):
    with pytest.raises(ValueError, match="orders_cursor_key_invalid"):
        OrdersCursorCodec(key)
