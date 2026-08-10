"""Canonical serialization and the two content hashes.

`claim_set_hash` and the resolution's content hash are what make "same inputs ⇒
byte-identical output" (03 §3.0) a testable statement rather than an intention, so the
encoding is pinned here and nowhere else:

* keys sorted, no insignificant whitespace, UTF-8, `ensure_ascii=False`;
* **every float is rendered as a fixed 9-decimal STRING**. `json.dumps` uses `repr()` for
  floats, which is shortest-round-trip and therefore stable in CPython — but a fixed
  rendering additionally survives a value arriving as `Decimal` from psycopg on one path
  and as `float` from a fixture on another, which is exactly the shape of a replay
  mismatch that would look like a resolver bug;
* `datetime` is rendered as an ISO-8601 UTC instant, so a `tzinfo` difference between the
  DB round-trip and a fixture cannot change the hash;
* NaN / ±Inf raise. A hash over a non-finite coordinate is a silent data bug.

`claim_set_hash` deliberately hashes the CONSUMED claim set — its ids, types, values and
observation instants — because `as_of` (03 §3.0 rule 2) must be derivable from what the key
already covers, without adding a sixth key component.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

FLOAT_FORMAT = "{:.9f}"


def canonical(value: Any) -> str:
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float in a canonical payload: {value!r}")
        return FLOAT_FORMAT.format(value)
    if isinstance(value, Decimal):
        return FLOAT_FORMAT.format(float(value))
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_plain(v) for v in sorted(value, key=repr)]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _plain(getattr(value, name))
            for name in sorted(value.__dataclass_fields__)  # type: ignore[attr-defined]
        }
    raise TypeError(f"no canonical form for {type(value).__name__}")


def claim_set_hash(claims: Sequence[Any]) -> str:
    """A stable hash over the consumed claims. Ids are included: the resolution names them
    in `input_claim_ids`, so two different claim rows carrying the same value are two
    different inputs and must not collide."""
    payload = [
        {
            "id": claim.id,
            "type": claim.claim_type,
            "method": claim.extraction_method,
            "source": claim.source,
            "value": _claim_value(claim),
            "observed_at": claim.observed_at,
        }
        for claim in sorted(claims, key=lambda c: c.id)
    ]
    return digest(payload)


def _claim_value(claim: Any) -> Any:
    return {
        "text": claim.value_text,
        "num": claim.value_num,
        "lat": claim.lat,
        "lon": claim.lon,
        "jsonb": claim.value_jsonb or {},
    }


def as_of(claims: Iterable[Any]) -> datetime | None:
    """`as_of = max(observed_at)` over the consumed claims — the resolver's ONLY notion of
    "now" (03 §3.0 rule 2)."""
    moments = [c.observed_at for c in claims if c.observed_at is not None]
    return max(moments) if moments else None
