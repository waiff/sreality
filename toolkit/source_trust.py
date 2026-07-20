"""Single source of truth for per-portal trust order.

"Which portal's value wins when the same real-world property is advertised on
several portals" was, until this module, an accident of `sreality_id`'s sign in
half the representative-listing selectors (some tie-broke `sreality_id DESC`,
one `ASC`, one used a bare `source='sreality'` boolean, one an inline CASE). The
mapping here — mirrored exactly by the SQL `source_trust_rank(text)` function
(migration 311) — is the *deliberate* policy: lower rank = more trusted. A
`tests/test_source_trust.py` check asserts this dict and the migration's CASE
stay in lockstep.

Rank order rationale: sreality (structured JSON API, richest fields) first, then
the API/structured portals, with the HTML crawlers (esp. bazos, free-text) last.
Unknown / future sources sort last via `UNKNOWN_SOURCE_RANK`.
"""

from __future__ import annotations

SOURCE_TRUST_RANK: dict[str, int] = {
    "sreality": 1,
    "bezrealitky": 2,
    "idnes": 3,
    "mmreality": 4,
    "remax": 5,
    "maxima": 6,
    "ceskereality": 7,
    "realitymix": 8,
    "bazos": 9,
}

# Any source not in the map (incl. NULL) sorts after every known portal.
UNKNOWN_SOURCE_RANK = 10


def source_trust_rank(source: str | None) -> int:
    """Trust rank for a portal source (lower = more trusted)."""
    return SOURCE_TRUST_RANK.get(source or "", UNKNOWN_SOURCE_RANK)
