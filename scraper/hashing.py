"""Stable content hash for change detection.

Strip volatile fields (topped flags, user-session bits, last-updated
items) so a listing whose only change is being re-promoted by the
seller does not produce a new snapshot row.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

VOLATILE_TOP_KEYS: frozenset[str] = frozenset({
    # legacy v2 session/topped bits (kept so old raw_json still hashes stably)
    "is_topped",
    "is_topped_today",
    "logged_in",
    # sreality-computed nearby-POI enrichment — churns per-request, not
    # listing content
    "labels",
    "labels_extended",
    # per-session / per-user / recommendation state, not listing content
    "note",
    "rus",
    "rusReply",  # legacy camelCase form (kept for old raw_json)
    "rus_reply",
    "stats",  # view counter — increments on every visit (top-level in v1)
})

# Legacy: keys inside a `params` block (old camelCase raw_json) that change
# without the listing changing. The live snake_case API puts `stats` at the
# top level instead (see VOLATILE_TOP_KEYS).
VOLATILE_PARAM_KEYS: frozenset[str] = frozenset({
    "stats",  # view counter — increments on every visit
})

VOLATILE_EMBEDDED_KEYS: frozenset[str] = frozenset({
    "favourite",
    "note",
})

# Keys inside the `user` block (broker contact card) that change without the
# listing changing.
VOLATILE_USER_KEYS: frozenset[str] = frozenset({
    # re-signed avatar CDN URL — rotates per request; verified the sole
    # changing subkey. Broker name/phone stay hashed.
    "image",
})

VOLATILE_ITEM_NAMES: frozenset[str] = frozenset({
    "Aktualizace",
})


def content_hash(raw: dict[str, Any]) -> str:
    """sha256 of canonical JSON with volatile fields stripped."""
    stripped = _strip_volatile(raw)
    canonical = json.dumps(stripped, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_volatile(raw: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(raw)
    for key in VOLATILE_TOP_KEYS:
        out.pop(key, None)
    params = out.get("params")
    if isinstance(params, dict):
        for key in VOLATILE_PARAM_KEYS:
            params.pop(key, None)
    embedded = out.get("_embedded")
    if isinstance(embedded, dict):
        for key in VOLATILE_EMBEDDED_KEYS:
            embedded.pop(key, None)
    user = out.get("user")
    if isinstance(user, dict):
        for key in VOLATILE_USER_KEYS:
            user.pop(key, None)
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = [
            {k: v for k, v in item.items() if k != "topped"}
            for item in items
            if item.get("name") not in VOLATILE_ITEM_NAMES
        ]
    return out
