"""The C4 switch's in-process oracle: the compiler-backed adapters vs the legacy bodies.

Lives for ONE commit. The legacy `_shared_filter_where_legacy` / `_build_match_clauses_legacy`
are the hand-coded bodies renamed in place; this runs seeded random inputs through both
and requires the same clause multiset, the same first clause, the same params (Jsonb by
`.obj`) and the same raise class. The next commit deletes the legacy bodies and this file;
the golden (`test_filter_compiler_golden.py`) and the CI rows rail outlive both.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import toolkit.filter_registry as fr
from api.notifications import (
    WatchdogFilterSpec,
    _build_match_clauses,
    _build_match_clauses_legacy,
)
from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _shared_filter_where,
    _shared_filter_where_legacy,
)

_ROOT = Path(__file__).resolve().parents[2]
_N = 2000
_CITY = {f.id for f in fr.all_filters() if f.category == fr.CATEGORY_CITY_QUALITY}
_CHIP_SETS = [
    c["chips"] for c in
    json.loads((_ROOT / "tests" / "fixtures" / "district_chip_plan.json").read_text())["cases"]
]


def _norm(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("__jsonb__", v.obj) if isinstance(v, Jsonb) else v for k, v in params.items()}


def _outcome(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
    try:
        where, params = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the raise class is part of the contract
        return ("raises", type(exc).__name__)
    return (Counter(where), where[0] if where else None, _norm(params))


def _value(rng: random.Random, f: fr.FilterDef) -> Any:
    enum = [o.value for o in f.enum_values or ()]
    if f.id == "city_index_rules":
        return rng.choice([None, [], [{"index_name": "celkove_hodnoceni", "op": ">=",
                                       "value": rng.choice([3, 6.5])}]])
    if f.id == "price_change_window_days":
        return rng.choice([None, 30, 90, 365])
    if f.id == "total_price_change_pct":
        return rng.choice([None, -5, 0, 0.0, 7.5])
    if f.type is fr.FilterType.STRING_LIST:
        return rng.choice([None, []]) if rng.random() < 0.2 else rng.sample(
            enum, rng.randint(1, min(3, len(enum))))
    if f.type is fr.FilterType.BOOL:
        return rng.choice([None, True, False])
    if f.type is fr.FilterType.STRING and enum:
        return rng.choice([None, *enum])
    if f.type is fr.FilterType.INT:
        return rng.choice([None, 0, 1, 3, 250])
    if f.type is fr.FilterType.FLOAT:
        return rng.choice([None, 0, 0.5, 12.25, 90000.0])
    return None


def _listings_case(rng: random.Random) -> tuple[TargetSpec, ComparableFilters]:
    target = TargetSpec(
        lat=50.08, lng=14.42,
        area_m2=rng.choice([None, 65.0, 120.5]),
        disposition=rng.choice([None, "2+kk", "3+1", "6+kk"]),
        floor=rng.choice([None, 0, 3]),
        exclude_listing_ids=rng.choice([[], [7, 8]]),
    )
    kwargs: dict[str, Any] = {}
    for name in ComparableFilters.__dataclass_fields__:
        if name in _CITY or name == "near_city_proximity" or rng.random() > 0.35:
            continue
        if name == "radius_m":
            kwargs[name] = rng.choice([None, 500, 1000])
        elif name == "area_band_pct":
            kwargs[name] = rng.choice([0.1, 0.2])
        elif name == "disposition_match":
            kwargs[name] = rng.choice(["exact", "loose", "any"])
        elif name == "lifecycle":
            kwargs[name] = rng.choice([None, "active", "delisted", "all"])
        elif name == "include_unreliable":
            kwargs[name] = rng.choice([True, False])
        else:
            kwargs[name] = _value(rng, fr.REGISTRY[name])
    return target, ComparableFilters(**kwargs)


def _watchdog_case(rng: random.Random) -> WatchdogFilterSpec | None:
    data: dict[str, Any] = {}
    if rng.random() < 0.3:
        data.update(lat=50.08, lng=14.42, radius_m=rng.choice([None, 800]))
    for name in WatchdogFilterSpec.model_fields:
        if name in ("lat", "lng", "radius_m") or rng.random() > 0.35:
            continue
        if name == "districts":
            data[name] = rng.choice(_CHIP_SETS)
        elif name == "near_city_proximity":
            data[name] = rng.choice([None, {}, {"radius_km": 10, "index_rules": []}])
        elif name == "include_no_price":
            data[name] = rng.choice([True, False])
        else:
            data[name] = _value(rng, fr.REGISTRY[name])
    try:
        return WatchdogFilterSpec(**data)
    except ValueError:
        return None


def test_the_listings_adapter_matches_the_legacy_body() -> None:
    rng = random.Random(16)
    diffs: list[str] = []
    for i in range(_N):
        target, filters = _listings_case(rng)
        old = _outcome(_shared_filter_where_legacy, target, filters)
        new = _outcome(_shared_filter_where, target, filters)
        if old != new:
            diffs.append(f"#{i} {filters}: {old} != {new}")
    assert not diffs, f"{len(diffs)} diffs, first: {diffs[:3]}"


def test_the_corridor_drops_exactly_the_anchor_circle() -> None:
    rng = random.Random(16)
    for _ in range(_N):
        target, filters = _listings_case(rng)
        try:
            where, params = _shared_filter_where_legacy(target, filters)
        except ValueError:
            continue
        legacy = [w for w in where if "ST_DWithin(ll.geom" not in w]
        params.pop("radius_m", None)
        new_where, new_params = _shared_filter_where(target, filters, radius=False)
        assert Counter(new_where) == Counter(legacy)
        assert new_where[0] == legacy[0]
        assert _norm(new_params) == _norm(params)


def test_the_watchdog_adapter_matches_the_legacy_body() -> None:
    rng = random.Random(16)
    diffs: list[str] = []
    checked = 0
    for i in range(_N):
        spec = _watchdog_case(rng)
        if spec is None:
            continue
        checked += 1
        old = _outcome(_build_match_clauses_legacy, spec)
        new = _outcome(_build_match_clauses, spec)
        if old != new:
            diffs.append(f"#{i} {spec.model_dump(exclude_defaults=True)}: {old} != {new}")
    assert checked > _N // 2
    assert not diffs, f"{len(diffs)} diffs, first: {diffs[:3]}"


@pytest.mark.parametrize("chips", _CHIP_SETS)
def test_every_district_chip_set_matches(chips: list[dict[str, Any]]) -> None:
    spec = WatchdogFilterSpec(districts=chips, min_price_czk=1)
    assert _outcome(_build_match_clauses_legacy, spec) == _outcome(_build_match_clauses, spec)
