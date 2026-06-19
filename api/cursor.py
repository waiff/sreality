"""Opaque keyset-pagination cursor shared by the API list feeds.

A cursor is the ORDER BY tuple of the last row of a page — a sort value plus
a stable id tiebreaker — serialised as base64-url JSON. Both /estimations and
/notifications/dispatches are newest-first feeds that grow with live inserts;
under OFFSET, an insert between page fetches shifts every later offset by one
and the page seam dups/skips a row. Anchoring each page to (sort value, id)
removes that. The id only has to break ties deterministically, so a uuid
tiebreaker (dispatches) is as valid as a serial one (estimations).
"""

from __future__ import annotations

import base64
import json
from typing import Any


def encode_cursor(values: list[Any]) -> str:
    raw = json.dumps(values, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> list[Any]:
    """Decode a cursor token to its [sort_value, id] list. Raises ValueError
    on a malformed token (callers treat that as 'no cursor' / 400)."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        out = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"malformed cursor: {exc}") from exc
    if not isinstance(out, list) or len(out) != 2:
        raise ValueError("malformed cursor: expected [sort_value, id]")
    return out
