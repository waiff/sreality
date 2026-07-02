"""Hermetic tests for scraper.portal: PortalConfig + PortalLimits + the loader."""

from __future__ import annotations

from typing import Any

import pytest

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
    # byt + houses (dum/chata) + commercial (restaurace/kancelar/prostory/sklad)
    # × sale + rent. The fine sections carry the subtype; the sweep is
    # subtype-scoped so same-category_main sections don't flip each other.
    assert len(cfg.categories) == 14
    assert {"sale_type": "prodam", "category": "byt"} in cfg.categories
    assert {"sale_type": "prodam", "category": "chata"} in cfg.categories
    assert {"sale_type": "pronajmu", "category": "kancelar"} in cfg.categories


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
    assert cfg.supports_complete_walk is False  # mixed single index → partial walk
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert cfg.categories == [{"index": "nemovitosti"}]


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
