"""Stable content hash for change detection.

Strip volatile fields (topped flags, user-session bits, last-updated
items, re-signed CDN URLs, portal-side review counters) so a listing
whose only change is being re-promoted by the seller or re-signed by
the CDN does not produce a new snapshot row.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

VOLATILE_TOP_KEYS: frozenset[str] = frozenset({
    # lister re-save / re-promotion date — the v1 form of the legacy
    # 'Aktualizace' item; alone it means a no-op edit (8.7% of churned pairs)
    "edited",
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

# Keys inside the `premise` block (agency office card) that churn portal-side
# without the listing changing: firmy.cz review counters, the re-signed logo
# CDN URL, paid-tier flags. 46% of churned snapshot pairs differed only here.
VOLATILE_PREMISE_KEYS: frozenset[str] = frozenset({
    "logo",
    "premise_paid_firmy",
    "review_count",
    "review_score",
})

VOLATILE_PREMISE_COMPANY_KEYS: frozenset[str] = frozenset({
    "sos_custom_advert_card",  # portal-side paid-card flag, flips false<->true
})

# The stable identity of one advert_images entry. `kind` flaps 2<->4
# portal-side and the sdn.cz `url` token re-signs wholesale (verified: same
# image id, different path) with width/height following the re-encode — so
# only id/alt/order plus list position carry photo-set content.
_IMAGE_IDENTITY_KEYS: tuple[str, ...] = ("id", "alt", "order")

# sdn_*_attachment_url values (energy certificate PDFs etc.) re-sign the same
# way the image URLs do; only presence/absence is stable content.
_ATTACHMENT_URL_PREFIX = "sdn_"
_ATTACHMENT_URL_SUFFIX = "_attachment_url"


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
    images = out.get("advert_images")
    if isinstance(images, list):
        out["advert_images"] = [_image_identity(img) for img in images]
    premise = out.get("premise")
    if isinstance(premise, dict):
        for key in VOLATILE_PREMISE_KEYS:
            premise.pop(key, None)
        company = premise.get("company")
        if isinstance(company, dict):
            for key in VOLATILE_PREMISE_COMPANY_KEYS:
                company.pop(key, None)
    for key in out:
        if key.startswith(_ATTACHMENT_URL_PREFIX) and key.endswith(_ATTACHMENT_URL_SUFFIX):
            out[key] = bool(out[key])
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = [
            {k: v for k, v in item.items() if k != "topped"}
            for item in items
            if item.get("name") not in VOLATILE_ITEM_NAMES
        ]
    return out


def _image_identity(img: Any) -> Any:
    if not isinstance(img, dict):
        return img
    identity = {k: img[k] for k in _IMAGE_IDENTITY_KEYS if k in img}
    if "id" not in identity and isinstance(img.get("url"), str):
        identity["url"] = img["url"].split("?", 1)[0]
    return identity
