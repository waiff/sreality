"""Unit tests for the shared keyset-cursor helper (api/cursor.py)."""

from __future__ import annotations

import pytest

from api.cursor import decode_cursor, encode_cursor


def test_roundtrip_timestamp_and_int_id():
    token = encode_cursor(["2026-06-19T10:00:00.123456+00:00", 9931])
    assert decode_cursor(token) == ["2026-06-19T10:00:00.123456+00:00", 9931]


def test_roundtrip_timestamp_and_uuid_id():
    uid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    token = encode_cursor(["2026-06-19T10:00:00+00:00", uid])
    assert decode_cursor(token) == ["2026-06-19T10:00:00+00:00", uid]


def test_token_is_url_safe_base64_no_padding_issues():
    token = encode_cursor(["2026-06-19T10:00:00+00:00", 1])
    # url-safe alphabet only (no +/), decodes cleanly
    assert all(c.isalnum() or c in "-_=" for c in token)
    assert decode_cursor(token)[1] == 1


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!!",
        "",
        encode_cursor([1, 2, 3]),  # wrong arity
        encode_cursor("a string not a list"),  # not a 2-list
    ],
)
def test_malformed_cursor_raises_valueerror(bad):
    with pytest.raises(ValueError):
        decode_cursor(bad)
