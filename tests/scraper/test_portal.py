"""Hermetic tests for scraper.portal: PortalConfig + PortalLimits + the loader."""

from __future__ import annotations

from typing import Any

import pytest

from scraper import portal
from scraper.portal import (
    PortalConfig,
    PortalLimits,
    _read_global_limits,
    default_config,
    load_portal_config,
    price_changed,
)


class _Cur:
    """Returns the portal row for a `portals` query and the global row for an
    `app_settings` query, so it can stand in for both reads the loader makes."""

    def __init__(self, portal_row: Any, global_row: Any) -> None:
        self._portal_row = portal_row
        self._global_row = global_row
        self._last: Any = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = self._global_row if "app_settings" in sql else self._portal_row

    def fetchone(self) -> Any:
        return self._last


class _Conn:
    def __init__(self, portal_row: Any, global_row: Any = None) -> None:
        self._portal_row = portal_row
        self._global_row = global_row

    def cursor(self) -> _Cur:
        return _Cur(self._portal_row, self._global_row)


class _RaisingConn:
    def cursor(self) -> Any:
        raise RuntimeError("db down")


# --- identity config (migration 107) ---

def test_default_config_sreality():
    cfg = default_config("sreality")
    assert cfg.supports_complete_walk is True
    assert cfg.split_threshold == 10000
    assert cfg.splits is True
    assert len(cfg.categories) == 6
    assert {"category_main_cb": 1, "category_type_cb": 2} in cfg.categories


def test_default_config_bazos():
    cfg = default_config("bazos")
    assert cfg.supports_complete_walk is True
    assert cfg.split_threshold is None
    assert cfg.splits is False
    # Every bazos property section × sale + rent. The fine sections carry the
    # subtype; the nomination is subtype-scoped so same-category_main sections
    # don't nominate each other.
    assert len(cfg.categories) == 22
    assert {"sale_type": "prodam", "category": "byt"} in cfg.categories
    assert {"sale_type": "prodam", "category": "chata"} in cfg.categories
    assert {"sale_type": "pronajmu", "category": "kancelar"} in cfg.categories
    # migration 488: the four sections migration 160 deferred and never revisited
    for cat in ("pozemek", "zahrada", "garaz", "ostatni"):
        assert {"sale_type": "prodam", "category": cat} in cfg.categories
        assert {"sale_type": "pronajmu", "category": cat} in cfg.categories


def test_default_config_idnes():
    cfg = default_config("idnes")
    assert cfg.supports_complete_walk is True   # complete-walk (total + no page cap)
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert {"sale_type": "prodej", "category": "byty"} in cfg.categories
    assert {"sale_type": "prodej", "category": "komercni-nemovitosti"} in cfg.categories
    assert {"sale_type": "prodej", "category": "male-objekty-garaze"} in cfg.categories
    assert len(cfg.categories) == 10             # 5 slugs × prodej + pronajem


def test_default_config_mmreality():
    cfg = default_config("mmreality")
    # The flag lives on the live registry row; the coverage gate flips it from
    # slice-ledger evidence (migration 455), so the baked-in default stays down.
    assert cfg.supports_complete_walk is False
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert {"sale_type": "prodej", "category": "byty"} in cfg.categories
    assert {"sale_type": "pronajem", "category": "komercni-objekty"} in cfg.categories
    assert len(cfg.categories) == 10             # 5 property types × prodej + pronajem
    assert {"index": "nemovitosti"} not in cfg.categories   # the prodej-only feed is gone


def test_default_config_remax():
    cfg = default_config("remax")
    assert cfg.supports_complete_walk is True   # complete-walk via agenda-grain delisting
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert len(cfg.categories) == 10            # 5 categories × prodej + pronajem
    assert {c["sale"] for c in cfg.categories} == {1, 2}
    assert all("category_main" in c and "category_type" in c for c in cfg.categories)


def test_default_config_ceskereality():
    cfg = default_config("ceskereality")
    assert cfg.supports_complete_walk is True   # per-category total, no pagination cap
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert len(cfg.categories) == 12            # 6 categories × prodej + pronajem
    assert {c["sale_type"] for c in cfg.categories} == {"prodej", "pronajem"}
    assert all("sale_type" in c and "category" in c for c in cfg.categories)
    # houses + land (the categories the original branch config omitted) are present
    assert {"rodinne-domy", "pozemky"} <= {c["category"] for c in cfg.categories}
    assert cfg.limits.detail_workers == 4       # proxy removes the throttle -> normal speed


def test_default_config_unknown_raises():
    with pytest.raises(ValueError):
        default_config("nope")


def test_load_reads_db_row():
    row = (True, [{"category_main_cb": 9, "category_type_cb": 9}], 5000, None)
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.supports_complete_walk is True
    assert cfg.split_threshold == 5000
    assert cfg.categories == [{"category_main_cb": 9, "category_type_cb": 9}]
    # no operational_limits column + no global row → baked sreality limits
    assert cfg.limits == default_config("sreality").limits


def test_load_missing_row_falls_back_to_default():
    cfg = load_portal_config(_Conn(None), "bazos")
    assert cfg == default_config("bazos")


def test_load_null_categories_falls_back_to_default_categories():
    row = (False, None, None, None)
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.categories == default_config("sreality").categories
    assert cfg.supports_complete_walk is False  # the row's value still wins


def test_portalconfig_splits_property():
    assert PortalConfig("x", True, [], split_threshold=1).splits is True
    assert PortalConfig("x", True, [], split_threshold=None).splits is False


# --- operational limits (migration 114) ---

def test_per_portal_limits_override_baked_default():
    row = (True, [{"x": 1}], None, {"detail_workers": 16, "detail_rate": 9.5})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.detail_workers == 16
    assert cfg.limits.detail_rate == 9.5
    # a key the override omits keeps the baked idnes default
    assert cfg.limits.index_rate == default_config("idnes").limits.index_rate


def test_global_underlays_per_portal():
    portal_row = (True, [{"x": 1}], None, {"detail_workers": 7})
    global_row = ({"detail_workers": 5, "max_detail_per_run": 999},)
    cfg = load_portal_config(_Conn(portal_row, global_row), "idnes")
    assert cfg.limits.detail_workers == 7        # per-portal wins over global
    assert cfg.limits.max_detail_per_run == 999  # global applies (per-portal omits)


def test_missing_row_still_applies_global():
    cfg = load_portal_config(_Conn(None, ({"detail_rate": 11.0},)), "bazos")
    assert cfg.limits.detail_rate == 11.0
    assert cfg.categories == default_config("bazos").categories  # identity intact


def test_bad_typed_limit_leaf_is_ignored():
    row = (True, [{"x": 1}], None, {"detail_workers": "lots", "detail_rate": 4.0})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.detail_workers == default_config("idnes").limits.detail_workers
    assert cfg.limits.detail_rate == 4.0  # the good leaf still applies


def test_limits_merged_present_keys_only():
    base = PortalLimits()
    merged = base.merged({"detail_workers": 12})
    assert merged.detail_workers == 12
    assert merged.detail_rate == base.detail_rate


def test_limits_merged_none_and_nondict_are_noops():
    base = PortalLimits()
    assert base.merged(None) is base
    assert base.merged("nope") is base
    assert base.merged({}) is base


def test_limits_merged_present_null_means_unlimited():
    base = PortalLimits(max_detail_per_run=500)
    assert base.merged({"max_detail_per_run": None}).max_detail_per_run is None


def test_global_read_swallows_db_error():
    assert _read_global_limits(_RaisingConn()) is None


# --- shared_rate_limiter (migration 268 politeness ledger gate) ---

def test_shared_rate_limiter_defaults_off_everywhere():
    assert PortalLimits().shared_rate_limiter is False
    for source in ("sreality", "bazos", "idnes", "bezrealitky", "maxima",
                   "mmreality", "remax", "ceskereality", "realitymix"):
        assert default_config(source).limits.shared_rate_limiter is False


def test_shared_rate_limiter_per_portal_override():
    row = (True, [{"x": 1}], None, {"shared_rate_limiter": True})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.shared_rate_limiter is True


def test_shared_rate_limiter_global_underlay():
    portal_row = (True, [{"x": 1}], None, None)
    global_row = ({"shared_rate_limiter": True},)
    cfg = load_portal_config(_Conn(portal_row, global_row), "idnes")
    assert cfg.limits.shared_rate_limiter is True
    # ... and the per-portal layer can still force it off.
    portal_row = (True, [{"x": 1}], None, {"shared_rate_limiter": False})
    cfg = load_portal_config(_Conn(portal_row, global_row), "idnes")
    assert cfg.limits.shared_rate_limiter is False


def test_shared_rate_limiter_rejects_non_bool_leaf():
    # bool("false") would be True — a politeness knob must not flip on a
    # mistyped dashboard edit, so only a real JSON boolean is applied.
    row = (True, [{"x": 1}], None, {"shared_rate_limiter": "true"})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.shared_rate_limiter is False


# --- payload_dual_write (location-data W2a-2 payload archive gate) ---

def test_payload_dual_write_defaults_off_everywhere():
    # Enabling it is gated on the churn sign-off (02 section 2.3.2's storage
    # question), so no portal may ship with the archive already writing.
    assert PortalLimits().payload_dual_write is False
    for source in ("sreality", "bazos", "idnes", "bezrealitky", "maxima",
                   "mmreality", "remax", "ceskereality", "realitymix"):
        assert default_config(source).limits.payload_dual_write is False


def test_payload_dual_write_per_portal_override():
    # Per portal on purpose: the storage cost of archiving mmreality's 245 KB
    # pages is not bazos's 41 KB one, so the decision is taken per portal.
    row = (True, [{"x": 1}], None, {"payload_dual_write": True})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.payload_dual_write is True


def test_payload_dual_write_global_underlay():
    portal_row = (True, [{"x": 1}], None, None)
    global_row = ({"payload_dual_write": True},)
    cfg = load_portal_config(_Conn(portal_row, global_row), "idnes")
    assert cfg.limits.payload_dual_write is True
    # ... and one portal can still be held back from a global enable.
    portal_row = (True, [{"x": 1}], None, {"payload_dual_write": False})
    cfg = load_portal_config(_Conn(portal_row, global_row), "idnes")
    assert cfg.limits.payload_dual_write is False


def test_payload_dual_write_rejects_non_bool_leaf():
    row = (True, [{"x": 1}], None, {"payload_dual_write": "true"})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.payload_dual_write is False


# --- payload_index_archive (location-data W2a-6 index-only second gate) ---

def test_payload_index_archive_defaults_off_everywhere():
    # Split from payload_dual_write because index pages are their own storage
    # decision (02 section 2.3.2 P2: they re-order on every walk, and sreality
    # walks them 24x/day), so it ships off on every portal for the same reason.
    assert PortalLimits().payload_index_archive is False
    for source in ("sreality", "bazos", "idnes", "bezrealitky", "maxima",
                   "mmreality", "remax", "ceskereality", "realitymix"):
        assert default_config(source).limits.payload_index_archive is False


def test_payload_index_archive_is_independent_of_dual_write():
    # Enabling the archive for a portal must not enable its index surface, and
    # the two must be separately settable in one operator edit.
    row = (True, [{"x": 1}], None, {"payload_dual_write": True})
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.limits.payload_dual_write is True
    assert cfg.limits.payload_index_archive is False

    row = (True, [{"x": 1}], None,
           {"payload_dual_write": True, "payload_index_archive": True})
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.limits.payload_index_archive is True


def test_payload_index_archive_global_underlay():
    portal_row = (True, [{"x": 1}], None, None)
    global_row = ({"payload_index_archive": True},)
    cfg = load_portal_config(_Conn(portal_row, global_row), "sreality")
    assert cfg.limits.payload_index_archive is True
    # ... and one portal can still be held back from a global enable.
    portal_row = (True, [{"x": 1}], None, {"payload_index_archive": False})
    cfg = load_portal_config(_Conn(portal_row, global_row), "sreality")
    assert cfg.limits.payload_index_archive is False


def test_payload_index_archive_rejects_non_bool_leaf():
    row = (True, [{"x": 1}], None, {"payload_index_archive": "true"})
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.limits.payload_index_archive is False


# --- price_changed (index-walk price-diff jitter tolerance) ---

def test_price_changed_exact_compare_by_default():
    assert price_changed(100, 100) is False
    assert price_changed(100, 101) is True          # any move counts at 0
    assert price_changed(100, 99) is True


def test_price_changed_tolerance_absorbs_jitter_both_directions():
    # idnes FX drift signature: ~0.04-0.08% daily moves on foreign listings
    assert price_changed(23_692_431, 23_710_239, 0.005) is False  # +0.075%
    assert price_changed(23_710_239, 23_692_431, 0.005) is False  # -0.075%
    assert price_changed(10_000_000, 9_900_000, 0.005) is True    # -1% genuine cut
    assert price_changed(10_000_000, 10_100_000, 0.005) is True   # +1% rise


def test_price_changed_exactly_at_threshold_is_changed():
    assert price_changed(10_000, 10_050, 0.005) is True    # == 0.5% -> changed
    assert price_changed(10_000, 10_049, 0.005) is False   # just below
    assert price_changed(10_000, 9_950, 0.005) is True     # == 0.5% down
    assert price_changed(10_000, 9_951, 0.005) is False


def test_price_changed_null_value_transitions_always_change():
    assert price_changed(None, 100, 0.5) is True
    assert price_changed(100, None, 0.5) is True
    assert price_changed(None, None, 0.5) is False  # no difference to report


def test_price_changed_zero_and_huge_prices():
    assert price_changed(0, 5, 0.005) is True                # no ratio on 0
    assert price_changed(2_000_000_000, 2_001_000_000, 0.005) is False  # 0.05%
    assert price_changed(2_000_000_000, 2_001_000_000, 0.0) is True


def test_price_change_min_pct_defaults():
    assert default_config("idnes").limits.price_change_min_pct == 0.005
    assert default_config("sreality").limits.price_change_min_pct == 0.0
    assert default_config("realitymix").limits.price_change_min_pct == 0.0


def test_price_change_min_pct_resolves_through_limit_chain():
    row = (True, [{"x": 1}], None, {"price_change_min_pct": 0.01})
    cfg = load_portal_config(_Conn(row), "idnes")
    assert cfg.limits.price_change_min_pct == 0.01
    # global layer applies when the per-portal row omits it
    global_row = ({"price_change_min_pct": 0.002},)
    cfg = load_portal_config(_Conn((True, [{"x": 1}], None, None), global_row), "realitymix")
    assert cfg.limits.price_change_min_pct == 0.002
    # a bad-typed leaf keeps the baked default
    cfg = load_portal_config(
        _Conn((True, [{"x": 1}], None, {"price_change_min_pct": "lots"})), "idnes"
    )
    assert cfg.limits.price_change_min_pct == 0.005


# --- classify_index_sighting: one verdict rule for all nine portals ---------


def test_absent_index_price_reads_unchanged_not_changed():
    """The 2026-08-17 regression, pinned.

    An index card with no price ("Cena na dotaz") carries no evidence about the
    price. Six portals used to read that as `changed`, re-enqueueing the listing
    on every walk forever — 85% of sreality's refresh queue, 91% of
    ceskereality's — which starved new listings for nine days.
    """
    assert portal.classify_index_sighting({"price_czk": 4_550_000}, None) == "unchanged"
    # ...and it stays unchanged however wide the jitter tolerance is.
    assert portal.classify_index_sighting({"price_czk": 4_550_000}, None, 0.05) == "unchanged"


def test_unseen_listing_is_new():
    assert portal.classify_index_sighting(None, 4_550_000) == "new"
    assert portal.classify_index_sighting(None, None) == "new"


def test_matching_price_is_unchanged_and_a_move_is_changed():
    assert portal.classify_index_sighting({"price_czk": 100}, 100) == "unchanged"
    assert portal.classify_index_sighting({"price_czk": 100}, 90) == "changed"


def test_a_price_appearing_is_still_a_change():
    """Absence of an index price is not news; a price ARRIVING is. A listing that
    was price-on-request and now shows a number must be refetched."""
    assert portal.classify_index_sighting({"price_czk": None}, 4_550_000) == "changed"


def test_jitter_below_the_portal_tolerance_is_unchanged():
    # idnes's FX-converted foreign inventory drifts ~0.04-0.08% daily.
    assert portal.classify_index_sighting({"price_czk": 1_000_000}, 1_000_500, 0.01) == "unchanged"
    assert portal.classify_index_sighting({"price_czk": 1_000_000}, 1_020_000, 0.01) == "changed"


def test_no_portal_module_classifies_index_sightings_itself():
    """One definition, not nine (rule #21).

    This bug existed in six portal modules and not in two, because each carried
    its own copy of the comparison. A portal that reaches for `price_changed`
    directly is re-deriving the verdict and can drift again; the shared
    classifier is the only sanctioned caller.
    """
    from pathlib import Path

    scraper_dir = Path(portal.__file__).parent
    offenders = [
        p.name for p in sorted(scraper_dir.glob("*_main.py"))
        if "price_changed(" in p.read_text(encoding="utf-8")
    ]
    if (scraper_dir / "main.py").read_text(encoding="utf-8").count("price_changed(") > 0:
        offenders.append("main.py")
    assert offenders == [], (
        f"{offenders} call price_changed directly; use classify_index_sighting"
    )


# --- the structural walk verdict (rule #3) ----------------------------------


def test_every_stop_reason_is_classified_exactly_once():
    """The two frozensets partition the vocabulary. A reason in neither would
    read as OUR stop forever (a portal that never nominates); a reason in both
    is a contradiction the nine implementations would resolve differently."""
    from typing import get_args

    reasons = set(get_args(portal.StopReason))
    assert portal.PORTAL_ENDS | portal.OUR_STOPS == reasons
    assert not (portal.PORTAL_ENDS & portal.OUR_STOPS)


def test_stop_is_portal_end_splits_the_two_families():
    assert portal.stop_is_portal_end("pager_end")
    assert portal.stop_is_portal_end("declared_total_reached")
    assert portal.stop_is_portal_end("short_page")
    assert portal.stop_is_portal_end("empty_confirmed")
    assert portal.stop_is_portal_end("clamp_repeat")
    for ours in ("deadline", "page_cap", "limit", "slice_subset", "slice_unreached",
                 "error", "pager_stalled", "cap_wall"):
        assert not portal.stop_is_portal_end(ours), ours


def test_barren_is_ours_until_it_is_corroborated():
    """An items-less HTTP 200 is what a soft block looks like AND what the page
    after the last one looks like. Only a portal that re-fetched and corroborated
    the emptiness may report `empty_confirmed`; the raw class stays ours."""
    assert not portal.stop_is_portal_end("barren")
    assert portal.stop_is_portal_end("empty_confirmed")


def test_unknown_stop_reason_reads_as_our_stop(caplog):
    with caplog.at_level("WARNING"):
        assert not portal.stop_is_portal_end("who_knows")  # type: ignore[arg-type]
    assert any("unknown walk stop reason" in m for m in caplog.messages)


def test_walk_reached_end_is_portal_end_and_no_stop_of_ours():
    assert portal.walk_reached_end(portal_end=True, our_stop=False)
    assert not portal.walk_reached_end(portal_end=True, our_stop=True)
    assert not portal.walk_reached_end(portal_end=False, our_stop=False)
    assert not portal.walk_reached_end(portal_end=False, our_stop=True)


def test_walk_reached_end_ignores_the_count():
    """The whole point: a category whose every unit paged to its own last page
    is finished even when the declared totals say it is one row short. That is
    the ceskereality houses-for-sale case (20,964 rows, silent for two days)."""
    assert portal.walk_coverage(86, 87) == "incomplete"
    assert portal.walk_reached_end(portal_end=True, our_stop=False)
