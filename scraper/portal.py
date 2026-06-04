"""Portal operational config (Phase 4 portal framework).

A portal is "operational config + a fetcher + a parser", with no per-portal
branches in the shared `portal_runner`. This module holds the config half:
`PortalConfig` mirrors the operational columns on the `portals` registry
(migration 107) and now carries `PortalLimits` — the per-portal tuning knobs
(rate / workers / per-run caps / image limits) made operator-editable in
migration 114. `load_portal_config` reads a row, merges the
global default layer (`app_settings.scraper_limits_global`) under the per-portal
overrides, and falls back to baked-in defaults so a registry hiccup never breaks
a scrape. The behavioral half — the `Portal` protocol the runner consumes and
the concrete portals — builds on this in the per-portal `*_main` modules.

Resolution precedence (highest wins): CLI override (in each *_main) >
per-portal `portals.operational_limits` > global `scraper_limits_global` >
baked-in code default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

LOG = logging.getLogger(__name__)


def _as_opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


# Operational-limit field name -> coercer. The coercer is applied to any value
# coming from JSONB (operator-edited), so a bad-typed leaf is caught per-field.
_LIMIT_COERCERS: dict[str, Any] = {
    "index_rate": float,
    "detail_workers": int,
    "detail_rate": float,
    "max_detail_per_run": _as_opt_int,
    "max_detail_per_category": _as_opt_int,
    "image_workers": int,
    "max_image_downloads": _as_opt_int,
    "suspicious_stop_window": int,
    "suspicious_stop_threshold": float,
}


@dataclass(frozen=True)
class PortalLimits:
    """Per-portal operational tuning (migration 114). Every field has a baked
    default; the DB layers (global, then per-portal) override by key. A key
    absent from a JSONB layer is inherited; a key present (incl. null) is
    applied (null = "unlimited" for the optional caps)."""

    index_rate: float = 2.0
    detail_workers: int = 4
    detail_rate: float = 2.0
    max_detail_per_run: int | None = None
    max_detail_per_category: int | None = None
    image_workers: int = 32
    max_image_downloads: int | None = 1000
    suspicious_stop_window: int = 100
    suspicious_stop_threshold: float = 0.30

    def merged(self, overrides: Any) -> "PortalLimits":
        """Return a copy with each present key from `overrides` (a dict, or None)
        applied. Bad-typed leaves are skipped with a warning so an operator's
        malformed dashboard edit can never crash a scrape."""
        if not overrides or not isinstance(overrides, dict):
            return self
        changes: dict[str, Any] = {}
        for key, coerce in _LIMIT_COERCERS.items():
            if key not in overrides:
                continue
            try:
                changes[key] = coerce(overrides[key])
            except (TypeError, ValueError):
                LOG.warning(
                    "ignoring bad scraper-limit %s=%r; keeping %r",
                    key, overrides[key], getattr(self, key),
                )
        return replace(self, **changes) if changes else self


# The generic baseline (a portal with no specific tuning). Per-portal baked
# defaults below override only what differs, mirroring today's code defaults so
# a DB-down fallback reproduces current behavior exactly.
_GENERIC_LIMITS = PortalLimits()


@dataclass(frozen=True)
class PortalConfig:
    """The portal-defining operational knobs (migration 107) + per-portal limits
    (migration 114).

    - supports_complete_walk: can the portal prove a near-complete index walk?
      Gates mark_inactive (architectural rule #3). Partial-walk crawlers stay
      false and never flip listings inactive.
    - categories: the per-portal list of category descriptors the runner walks.
      Shape is portal-specific (the Portal object interprets it).
    - split_threshold: deep-pagination cap above which a category is walked
      per-district and unioned (None = no cap, never split).
    - limits: per-portal operational tuning (rate / workers / caps / images).
    """

    source: str
    supports_complete_walk: bool
    categories: list[dict[str, Any]]
    split_threshold: int | None = None
    limits: PortalLimits = _GENERIC_LIMITS

    @property
    def splits(self) -> bool:
        return self.split_threshold is not None


# Baked-in defaults — the source of truth the runner falls back to when the DB
# row or a column is missing, so a registry glitch can never break a scrape. The
# `portals` row (migrations 107 + 114) is the operator-tunable override + Health
# surface. Each portal's `limits` mirror its *current code defaults* (argparse
# defaults + the portal's index_rate), NOT the production workflow values, so a
# DB-down run behaves exactly as it does today.
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
        limits=PortalLimits(
            index_rate=2.0, detail_workers=4, detail_rate=2.0,
            image_workers=32, max_image_downloads=1000,
        ),
    ),
    "bazos": PortalConfig(
        source="bazos",
        # The index reports a total, so a full walk of the configured scope is
        # provable-complete; the per-walk completeness guard + the 12h sweep
        # throttle (migration 113) keep delisting inference safe.
        supports_complete_walk=True,
        # byt + houses (dum/chata) + commercial (restaurace/kancelar/prostory/
        # sklad) × sale + rent. The fine sections collapse onto one category_main,
        # so the sweep is subtype-scoped (BazosPortal.mark_inactive). Mirrors the
        # DB registry (migration 158). pozemek / garaz / ostatni are one-line adds.
        categories=[
            {"sale_type": st, "category": cat}
            for st in ("prodam", "pronajmu")
            for cat in ("byt", "dum", "chata", "restaurace",
                        "kancelar", "prostory", "sklad")
        ],
        split_threshold=None,
        # detail_rate 0.6 is the politeness ceiling (req/s); 4 workers share that
        # one limiter, so they only overlap per-listing geocode/DB latency, not
        # raise the request rate. max_detail_per_run high so the drain's
        # --max-seconds budget governs, not a tight claim cap (migration 168).
        limits=PortalLimits(
            index_rate=0.5, detail_workers=4, detail_rate=0.6,
            max_detail_per_run=1500,
        ),
    ),
    "idnes": PortalConfig(
        source="idnes",
        # Search pages carry a result total and have no deep-pagination cap, so a
        # per-category walk is provable-complete → mark_inactive runs under the
        # completeness guard (source-scoped). No split needed.
        supports_complete_walk=True,
        categories=[
            {"sale_type": "prodej",   "category": "byty"},
            {"sale_type": "pronajem", "category": "byty"},
            {"sale_type": "prodej",   "category": "domy"},
            {"sale_type": "pronajem", "category": "domy"},
            {"sale_type": "prodej",   "category": "pozemky"},
            {"sale_type": "pronajem", "category": "pozemky"},
            {"sale_type": "prodej",   "category": "komercni-nemovitosti"},
            {"sale_type": "pronajem", "category": "komercni-nemovitosti"},
            {"sale_type": "prodej",   "category": "male-objekty-garaze"},
            {"sale_type": "pronajem", "category": "male-objekty-garaze"},
        ],
        split_threshold=None,
        limits=PortalLimits(
            index_rate=3.0, detail_workers=4, detail_rate=3.0,
        ),
    ),
    "mmreality": PortalConfig(
        source="mmreality",
        # A single mixed-category index (no per-category slice) that can't be
        # gated per-(category_main, category_type) the way source-scoped
        # mark_inactive needs, so it stays partial-walk: the runner never flips
        # its listings inactive from index absence (bazos posture, rule #3).
        supports_complete_walk=False,
        categories=[{"index": "nemovitosti"}],
        split_threshold=None,
        limits=PortalLimits(
            index_rate=1.0, detail_workers=4, detail_rate=2.0,
        ),
    ),
    "maxima": PortalConfig(
        source="maxima",
        # A small agency catalogue on TWO mixed indexes — sale (af=1) and rent
        # (af=2, the buy/rent toggle). No per-category URL, so each descriptor
        # pairs a category with its agenda; walk_category walks that agenda once
        # (cached) and keeps the id-prefix slice for its category. Pilot:
        # supports_complete_walk=false (maxima reports a per-AGENDA total, not a
        # per-category one, so a per-(cm,ct) completeness check isn't available).
        supports_complete_walk=False,
        categories=[
            {"category_main": "byt",      "category_type": "prodej",   "af": 1},
            {"category_main": "dum",      "category_type": "prodej",   "af": 1},
            {"category_main": "pozemek",  "category_type": "prodej",   "af": 1},
            {"category_main": "komercni", "category_type": "prodej",   "af": 1},
            {"category_main": "ostatni",  "category_type": "prodej",   "af": 1},
            {"category_main": "byt",      "category_type": "pronajem", "af": 2},
            {"category_main": "dum",      "category_type": "pronajem", "af": 2},
            {"category_main": "pozemek",  "category_type": "pronajem", "af": 2},
            {"category_main": "komercni", "category_type": "pronajem", "af": 2},
            {"category_main": "ostatni",  "category_type": "pronajem", "af": 2},
        ],
        split_threshold=None,
        limits=PortalLimits(
            index_rate=1.0, detail_workers=2, detail_rate=1.0,
        ),
    ),
    "remax": PortalConfig(
        source="remax",
        # TWO mixed indexes — sale (sale=1) and rent (sale=2) — no per-category
        # URL. Each descriptor pairs a category with its offer-type flag;
        # walk_category walks that agenda once (cached) and keeps the
        # title-derived slice for its category. Pilot: supports_complete_walk=
        # false (remax reports a per-AGENDA total, and the per-category slice is
        # title-derived — not a portal-reported per-(cm,ct) total — so a safe
        # per-category completeness check isn't available).
        supports_complete_walk=False,
        categories=[
            {"category_main": "byt",      "category_type": "prodej",   "sale": 1},
            {"category_main": "dum",      "category_type": "prodej",   "sale": 1},
            {"category_main": "pozemek",  "category_type": "prodej",   "sale": 1},
            {"category_main": "komercni", "category_type": "prodej",   "sale": 1},
            {"category_main": "ostatni",  "category_type": "prodej",   "sale": 1},
            {"category_main": "byt",      "category_type": "pronajem", "sale": 2},
            {"category_main": "dum",      "category_type": "pronajem", "sale": 2},
            {"category_main": "pozemek",  "category_type": "pronajem", "sale": 2},
            {"category_main": "komercni", "category_type": "pronajem", "sale": 2},
            {"category_main": "ostatni",  "category_type": "pronajem", "sale": 2},
        ],
        split_threshold=None,
        limits=PortalLimits(
            index_rate=1.0, detail_workers=4, detail_rate=2.0,
        ),
    ),
    "bezrealitky": PortalConfig(
        source="bezrealitky",
        # GraphQL listAdverts gives a totalCount and has no deep-pagination cap,
        # so a per-category walk is provable-complete (mark_inactive runs under
        # the completeness guard, source-scoped). No split needed.
        supports_complete_walk=True,
        categories=[
            {"offer_type": "PRODEJ",   "estate_type": "BYT"},
            {"offer_type": "PRONAJEM", "estate_type": "BYT"},
            {"offer_type": "PRODEJ",   "estate_type": "DUM"},
            {"offer_type": "PRONAJEM", "estate_type": "DUM"},
            {"offer_type": "PRODEJ",   "estate_type": "POZEMEK"},
            {"offer_type": "PRONAJEM", "estate_type": "POZEMEK"},
            # KANCELAR + NEBYTOVY_PROSTOR both canonicalise to 'komercni' —
            # grouped into one walk so the source-scoped mark_inactive (which
            # keys on canonical cm/ct) sees the union, not two disjoint subsets
            # that would mutually delist each other.
            {"offer_type": "PRODEJ",   "estate_type": ["KANCELAR", "NEBYTOVY_PROSTOR"], "category_main": "komercni"},
            {"offer_type": "PRONAJEM", "estate_type": ["KANCELAR", "NEBYTOVY_PROSTOR"], "category_main": "komercni"},
            # GARAZ + REKREACNI_OBJEKT both canonicalise to 'ostatni' — same.
            # The PRONAJEM half excludes imports because REKREACNI/PRONAJEM
            # imports are ~7000 vacation-rental aggregator listings (not real-
            # estate market data); we lose ~13 garaz/pronájem imports too, but
            # that's the right trade. PRODEJ keeps imports (rec. cabins for sale
            # are legit market inventory).
            {"offer_type": "PRODEJ",   "estate_type": ["GARAZ", "REKREACNI_OBJEKT"], "category_main": "ostatni"},
            {"offer_type": "PRONAJEM", "estate_type": ["GARAZ", "REKREACNI_OBJEKT"], "category_main": "ostatni", "include_imports": False},
        ],
        split_threshold=None,
        limits=PortalLimits(
            index_rate=1.0, detail_workers=8, detail_rate=1.0,
        ),
    ),
}


def default_config(source: str) -> PortalConfig:
    """The baked-in config for a known portal. Raises for an unknown source."""
    try:
        return _DEFAULTS[source]
    except KeyError:
        raise ValueError(f"no portal config for source={source!r}") from None


def _read_global_limits(conn: Any) -> dict[str, Any] | None:
    """The global default-limits layer (app_settings.scraper_limits_global), or
    None if absent/malformed — a missing global layer is not an error."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = 'scraper_limits_global'"
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - global layer is best-effort
        LOG.warning("read scraper_limits_global failed: %s; skipping global layer", exc)
        return None
    if row is None or not isinstance(row[0], dict):
        return None
    return row[0]


def load_portal_config(conn: Any, source: str) -> PortalConfig:
    """Read operational config from the `portals` registry, merging the global
    default-limits layer under the per-portal limit overrides, and falling back
    to the baked-in default for any missing row or NULL column.

    The DB is the operator-tunable surface (edit limits in the dashboard / a SQL
    update, no deploy); the code default is the robustness floor. Resolution:
    baked default < global (scraper_limits_global) < per-portal
    (portals.operational_limits).
    """
    default = _DEFAULTS.get(source)
    base_limits = default.limits if default is not None else _GENERIC_LIMITS
    global_limits = _read_global_limits(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT supports_complete_walk, categories, split_threshold, "
            "operational_limits FROM portals WHERE source = %s",
            (source,),
        )
        row = cur.fetchone()

    if row is None:
        if default is None:
            raise ValueError(f"no portal config for source={source!r}")
        return replace(default, limits=base_limits.merged(global_limits))

    scw, categories, split_threshold, op_limits = row
    if categories is None:
        categories = default.categories if default is not None else []
    merged_limits = base_limits.merged(global_limits).merged(op_limits)
    return PortalConfig(
        source=source,
        supports_complete_walk=bool(scw),
        categories=list(categories),
        split_threshold=split_threshold,
        limits=merged_limits,
    )
