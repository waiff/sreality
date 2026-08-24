"""The one per-area price-marker test shared by every portal's price parser.

`listings.price_czk` is a TOTAL (or a monthly rent) across all nine portals —
production carries only `za nemovitost` / `za mesic` / `celkem` / `měsíc`, none
per-area. A per-m2 figure written there reads as a total in every downstream
consumer (Kč/m2 stats, estimation comparables, Browse sort, price-drop
watchdogs), which is strictly worse than the missing value it replaces. So a
price cell that quotes a unit price must yield NULL, not the unit price.

The test is ANCHORED to the text immediately after the amount, never a
substring search: `4 990 000 Kč (4 008 Kč/m²)` is a total with a per-m2 NOTE,
and a substring search would throw the real price away. `Kč/měsíc` is likewise
not a per-area marker — the `m` must be followed by `2`/`²`.

Callers pass the slice of the price text that FOLLOWS the amount they parsed.
"""

from __future__ import annotations

import re
import unicodedata

# Matched against NFKD-folded text, so `Kč`->`Kc`, `m²`->`m2`, `měsíc`->`mesic`
# and the alternatives stay small. `_G` is the thin / no-break / zero-width gap
# the Czech portals sprinkle between amount, currency and unit. Currency and
# separator are optional because the portals spell the same cell five ways:
# `Kč/m²`, `Kč za m²`, `CZK/ za m2`, a bare `/m²`, and realitymix's bracketed
# `45 Kč / (za m²)` — the bracket is why the marker must be allowed to open one.
# It stays anchored regardless: `4 990 000 Kč (4 008 Kč/m²)` is a total with a
# per-m² NOTE, and the `m2` right after the optional bracket is what refuses it.
_G = r"[\s\u200b-\u200d\u2060]"
_PER_AREA_RE = re.compile(
    rf"^{_G}*(?:kc|czk)?{_G}*(?:/{_G}*)?(?:[(\[]{_G}*)?(?:za{_G}+)?(?:m{_G}*2(?!\w)|metr)",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def is_per_area_price(after_amount: str | None) -> bool:
    """True when the text right after a parsed amount marks it as a per-m2 price."""
    if not after_amount:
        return False
    return _PER_AREA_RE.match(_fold(after_amount)) is not None
