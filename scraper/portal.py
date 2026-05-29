"""Portal operational config (Phase 4 portal framework).

A portal is "operational config + a fetcher + a parser", with no per-portal
branches in the shared `portal_runner`. This module holds the config half:
`PortalConfig` mirrors the operational columns on the `portals` registry
(migration 107) and `load_portal_config` reads a row, falling back to a baked-in
default so a registry hiccup never breaks a scrape. The behavioral half — the
`Portal` protocol the runner consumes and the concrete sreality / bazos portals —
builds on this in `scraper.portal_runner`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PortalConfig:
    """The portal-defining operational knobs (migration 107).

    - supports_complete_walk: can the portal prove a near-complete index walk?
      Gates mark_inactive (architectural rule #3). Partial-walk crawlers stay
      false and never flip listings inactive.
    - categories: the per-portal list of category descriptors the runner walks.
      Shape is portal-specific (the Portal object interprets it).
    - split_threshold: deep-pagination cap above which a category is walked
      per-district and unioned (None = no cap, never split).
    """

    source: str
    supports_complete_walk: bool
    categories: list[dict[str, Any]]
    split_threshold: int | None = None

    @property
    def splits(self) -> bool:
        return self.split_threshold is not None


# Baked-in defaults — the source of truth the runner falls back to when the DB
# row or a column is missing, so a registry glitch can never break a scrape. The
# `portals` row (migration 107) is the operator-tunable override + Health surface.
_DEFAULTS: dict[str, PortalConfig] = {
    "sreality": PortalConfig(
        source="sreality",
        supports_complete_walk=True,
        categories=[
            {"category_main_cb": 1, "category_type_cb": 2},  # byt / pronajem
            {"category_main_cb": 1, "category_type_cb": 1},  # byt / prodej
            {"category_main_cb": 2, "category_type_cb": 2},  # dum / pronajem
            {"category_main_cb": 2, "category_type_cb": 1},  # dum / prodej
            {"category_main_cb": 4, "category_type_cb": 2},  # komercni / pronajem
            {"category_main_cb": 4, "category_type_cb": 1},  # komercni / prodej
        ],
        split_threshold=10000,
    ),
    "bazos": PortalConfig(
        source="bazos",
        supports_complete_walk=False,
        categories=[{"sale_type": "prodam", "category": "byt"}],
        split_threshold=None,
    ),
    "idnes": PortalConfig(
        source="idnes",
        # Search pages carry a result total and have no deep-pagination cap, so a
        # per-category walk is provable-complete → mark_inactive runs under the
        # completeness guard (source-scoped). No split needed.
        supports_complete_walk=True,
        categories=[
            {"sale_type": "prodej", "category": "byty"},
            {"sale_type": "pronajem", "category": "byty"},
            {"sale_type": "prodej", "category": "domy"},
            {"sale_type": "pronajem", "category": "domy"},
        ],
        split_threshold=None,
    ),
    "bezrealitky": PortalConfig(
        source="bezrealitky",
        # GraphQL listAdverts gives a totalCount and has no deep-pagination cap,
        # so a per-category walk is provable-complete (mark_inactive runs under
        # the completeness guard, source-scoped). No split needed.
        supports_complete_walk=True,
        categories=[
            {"offer_type": "PRODEJ", "estate_type": "BYT"},
            {"offer_type": "PRONAJEM", "estate_type": "BYT"},
            {"offer_type": "PRODEJ", "estate_type": "DUM"},
            {"offer_type": "PRONAJEM", "estate_type": "DUM"},
        ],
        split_threshold=None,
    ),
}


def default_config(source: str) -> PortalConfig:
    """The baked-in config for a known portal. Raises for an unknown source."""
    try:
        return _DEFAULTS[source]
    except KeyError:
        raise ValueError(f"no portal config for source={source!r}") from None


def load_portal_config(conn: Any, source: str) -> PortalConfig:
    """Read operational config from the `portals` registry, falling back to the
    baked-in default for any missing row or NULL column.

    The DB is the operator-tunable surface (edit coverage with a SQL update, no
    deploy); the code default is the robustness floor.
    """
    default = _DEFAULTS.get(source)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT supports_complete_walk, categories, split_threshold "
            "FROM portals WHERE source = %s",
            (source,),
        )
        row = cur.fetchone()
    if row is None:
        return default_config(source)
    scw, categories, split_threshold = row
    if categories is None:
        categories = default.categories if default is not None else []
    return PortalConfig(
        source=source,
        supports_complete_walk=bool(scw),
        categories=list(categories),
        split_threshold=split_threshold,
    )
