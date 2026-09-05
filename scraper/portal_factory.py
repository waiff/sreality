"""One place that turns a source name into a Portal (or client) instance.

Two callers need the same table and the same two constructor exceptions: the
realtime worker, to run a lane, and the coverage gate, to ask a portal what its
declared categories canonicalise to. A second copy of that table is a second
thing to get wrong, and the thing it would get wrong is not cosmetic — the gate
mis-counting a portal's categories is what kept ceskereality permanently parked.

Imports are lazy and inside the functions on purpose: every portal module imports
`scraper.portal_runner`, so a module-level import here would close a cycle.
"""

from __future__ import annotations

import importlib
from typing import Any

from scraper.portal import PortalConfig

# sreality and bazos predate the config-taking constructor (see build_portal), so
# their entries are unused for construction; they are kept so the two tables stay
# symmetric and a future refactor toward the uniform constructor has one place to
# update.
PORTAL_CLASSES: dict[str, tuple[str, str]] = {
    "bazos": ("scraper.bazos_main", "BazosPortal"),
    "bezrealitky": ("scraper.bezrealitky_main", "BezrealitkyPortal"),
    "ceskereality": ("scraper.ceskereality_main", "CeskerealityPortal"),
    "idnes": ("scraper.idnes_main", "IdnesPortal"),
    "maxima": ("scraper.maxima_main", "MaximaPortal"),
    "mmreality": ("scraper.mmreality_main", "MmRealityPortal"),
    "realitymix": ("scraper.realitymix_main", "RealitymixPortal"),
    "remax": ("scraper.remax_main", "RemaxPortal"),
    "sreality": ("scraper.main", "SrealityPortal"),
}

CLIENT_CLASSES: dict[str, tuple[str, str]] = {
    "bazos": ("scraper.bazos_client", "BazosClient"),
    "bezrealitky": ("scraper.bezrealitky_client", "BezrealitkyClient"),
    "ceskereality": ("scraper.ceskereality_client", "CeskerealityClient"),
    "idnes": ("scraper.idnes_client", "IdnesClient"),
    "maxima": ("scraper.maxima_client", "MaximaClient"),
    "mmreality": ("scraper.mmreality_client", "MmRealityClient"),
    "realitymix": ("scraper.realitymix_client", "RealitymixClient"),
    "remax": ("scraper.remax_client", "RemaxClient"),
    "sreality": ("scraper.sreality_client", "SrealityClient"),
}


def build_portal(source: str, config: PortalConfig) -> Any:
    """A Portal instance for `source`, configured from `config`."""
    if source == "bazos":
        # Bazos predates the config-taking constructor: it takes scopes +
        # geocoder and reads its limits off attributes (the bazos_main.main
        # wiring, reproduced here).
        from scraper import bazos_main, location

        scopes = [
            c for c in config.categories
            if bazos_main.SALE_TYPE.get(c.get("sale_type"))
            and bazos_main.CATEGORY_MAIN.get(c.get("category"))
        ]
        portal = bazos_main.BazosPortal(
            categories=scopes, geocoder=location.build_geocoder(),
        )
        portal.index_rate = config.limits.index_rate
        portal.shared_rate_limiter = config.limits.shared_rate_limiter
        portal.supports_complete_walk = config.supports_complete_walk
        return portal
    if source == "sreality":
        # Also predates the config-taking constructor (main.SrealityPortal takes
        # index_rate, not a PortalConfig, and builds its own category list).
        from scraper import main as sreality_main

        return sreality_main.SrealityPortal(index_rate=config.limits.index_rate)
    mod_name, cls_name = PORTAL_CLASSES[source]
    cls = getattr(importlib.import_module(mod_name), cls_name)
    return cls(config)


def build_client_class(source: str) -> Any:
    """The client CLASS (not an instance) for `source` — callers read class
    attributes off it (`USE_PROXY`, `PROXY_REQUIRED`) rather than making one."""
    mod_name, cls_name = CLIENT_CLASSES[source]
    return getattr(importlib.import_module(mod_name), cls_name)


def canonical_category_count(source: str, categories: list[dict[str, Any]]) -> int | None:
    """How many DISTINCT canonical (category_main, category_type) pairs this
    portal's declared categories map to — or None when it cannot be resolved.

    NOT `len(categories)`, and that distinction is load-bearing. ceskereality
    declares both `rodinne-domy` and `chaty-chalupy`, and BOTH canonicalise to
    `dum` (sreality lumps chata/chalupa under "dům"), so its 12 config entries
    can only ever produce 10 slice-ledger rows. A gate comparing the ledger's 10
    against the config's 12 can never be satisfied — which is exactly what kept
    ceskereality parked with no way out, and would have caught every other portal
    whose config collapses the same way.

    `category_labels` is the framework seam every portal already implements for
    precisely this translation, so this reads the mapping rather than restating
    it. Returns None if the portal cannot be built or maps nothing usable, so the
    caller can fall back to a stricter number rather than a wrong one.
    """
    try:
        portal = build_portal(source, _config_for(source, categories))
        pairs = {portal.category_labels(c) for c in categories}
        usable = {p for p in pairs if p and p[0] and p[1]}
        return len(usable) or None
    except Exception:  # noqa: BLE001 - an unresolvable mapping is not an error here
        return None


def _config_for(source: str, categories: list[dict[str, Any]]) -> PortalConfig:
    from scraper.portal import default_config

    base = default_config(source)
    return PortalConfig(
        source=base.source,
        supports_complete_walk=base.supports_complete_walk,
        categories=categories,
        split_threshold=base.split_threshold,
        limits=base.limits,
    )
