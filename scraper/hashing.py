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
    "is_topped",
    "is_topped_today",
    "logged_in",
})

VOLATILE_EMBEDDED_KEYS: frozenset[str] = frozenset({
    "favourite",
    "note",
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
    embedded = out.get("_embedded")
    if isinstance(embedded, dict):
        for key in VOLATILE_EMBEDDED_KEYS:
            embedded.pop(key, None)
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = [
            {k: v for k, v in item.items() if k != "topped"}
            for item in items
            if item.get("name") not in VOLATILE_ITEM_NAMES
        ]
    return out
