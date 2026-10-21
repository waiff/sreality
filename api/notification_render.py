"""A push's words (notifications push W4, PROGRAM.md § 3.5): one Czech title and body per `change_kind`.

Read from the event's frozen columns (`trigger_price_czk`, `prev_price_czk`), its property's place label and its ad's
kind and disposition: never a live price, never a per-m² figure (rule 23), and for a share no sender, no URL and no
name (F17). The bell keeps its English labels (frontend/src/pages/Notifications.tsx CHANGE_META); the two maps ARE
the change_kind CHECK's vocabulary by key, pinned by tests/api/test_notification_render.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from api.property_sheet import _czk
from toolkit.filter_registry import label_cs, type_label_cs

# change_kind -> the title (a property event names its place after it, "Zlevněno · Praha 5").
TITLES: dict[str, str] = {
    "new": "Nový inzerát",
    "price_drop": "Zlevněno",
    "price_rise": "Zdraženo",
    "inactive": "Staženo z nabídky",
    "reactivated": "Znovu v nabídce",
    "new_source": "Nový zdroj nabídky",
    "shared_collection": "Sdíleno s vámi",
    "shared_dashboard": "Sdíleno s vámi",
    "shared_pipeline": "Sdíleno s vámi",
}
# What a share opens, and nothing about who shared it.
SHARED_BODIES: dict[str, str] = {
    "shared_collection": "Sbírka", "shared_dashboard": "Dashboard", "shared_pipeline": "Pipeline",
}
# A kind outside TITLES (none since migration 656 narrowed the CHECK to TITLES' keys) still renders, as sw.js's
# fallback does.
FALLBACK_TITLE = "Nové upozornění"
_MONTHLY = "za mesic"


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str


def render(row: Mapping[str, Any]) -> Rendered:
    """The push for one event row: `change_kind`, `display_label`, `category_main`, `subtype`, `disposition`,
    `price_unit`, `trigger_price_czk`, `prev_price_czk` (any may be missing or NULL)."""
    kind = row.get("change_kind")
    if kind in SHARED_BODIES:
        return Rendered(TITLES[kind], SHARED_BODIES[kind])
    if kind not in TITLES:
        return Rendered(FALLBACK_TITLE, "")
    label = row.get("display_label")
    title = f"{TITLES[kind]} · {label}" if label else TITLES[kind]
    category, subtype = row.get("category_main"), row.get("subtype")
    noun = type_label_cs(category, subtype) if category or subtype else None
    subject = " ".join(p for p in (noun, label_cs("disposition", row.get("disposition"))) if p)
    unit = row.get("price_unit")
    trigger, prev = _price(row.get("trigger_price_czk"), unit), _price(row.get("prev_price_czk"), unit)
    if kind in ("price_drop", "price_rise") and trigger and prev:
        return Rendered(title, f"{prev} → {trigger}")
    if kind == "new":
        return Rendered(title, " · ".join(p for p in (subject, trigger) if p))
    return Rendered(title, subject)


def _price(czk: Any, unit: Any) -> str | None:
    """A whole price as the property sheet prints it (api/property_sheet.py: the SPA's fmtCzk), a rent per month."""
    if czk is None:
        return None
    return f"{_czk(czk)}/měs" if unit == _MONTHLY else _czk(czk)
