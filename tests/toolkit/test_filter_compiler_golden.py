"""The per-FilterDef golden for rule 16's compile (C4).

Every case is one filter input and what the adapter renders for it: the WHERE fragments
(sorted, because AND commutes), the FIRST fragment (the served predicate on the Watchdog,
`ll.geom IS NOT NULL` on the cohort, both of which must stay first), and the params. The
file was written from the hand-coded `_shared_filter_where` / `_build_match_clauses`
BEFORE `toolkit/filter_compiler.py` existed, and must stay unchanged through the switch:
a diff here is a change to what a saved watchdog or an estimation cohort matches.

Regenerate ONLY to add cases for a new filter (`test_golden_covers_every_compiled_filter`
names what is missing), and review that diff as a behaviour change:

    python -m tests.toolkit.test_filter_compiler_golden --write

`tests/test_filter_compiler_rows_live.py` executes every case on the CI Postgres.
"""

from __future__ import annotations

import dataclasses
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import toolkit.filter_registry as fr

_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = _ROOT / "tests" / "fixtures" / "filter_compile_golden.json"
ROWS_PATH = _ROOT / "tests" / "fixtures" / "filter_compile_rows.json"

GRAINS = ("listings", "watchdog")

# TargetSpec variants for the listings grain, applied over the fixture's target point.
TARGETS: dict[str, dict[str, Any]] = {
    "bare": {},
    "2+kk": {"disposition": "2+kk"},
    "6+kk": {"disposition": "6+kk"},   # not in the loose table: falls back to itself
    "area65": {"area_m2": 65.0},
    "floor3": {"floor": 3},
    "exclude": {"exclude_listing_ids": [7, 8]},
    "full": {"area_m2": 65.0, "disposition": "2+kk", "floor": 3,
             "exclude_listing_ids": [7, 8]},
}

_CITY_RULE = {"index_name": "celkove_hodnoceni", "op": ">=", "value": 6}
# The Watchdog's base is "invariants only": the spec's `pronajem` default is itself a
# filter, so every single-filter case unsets it.
_WATCHDOG_BASE: dict[str, Any] = {"category_type": None}


# --- encoding ----------------------------------------------------------------


def encode(value: Any) -> Any:
    """Params -> JSON. Jsonb survives as {"__jsonb__": obj}."""
    if isinstance(value, Jsonb):
        return {"__jsonb__": value.obj}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    """JSON -> params, re-wrapping Jsonb."""
    if isinstance(value, dict):
        if set(value) == {"__jsonb__"}:
            return Jsonb(value["__jsonb__"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def canonical_statement(sql: str) -> str:
    """Sort each maximal WHERE/AND line run, so only the textual AND order is free."""
    out: list[str] = []
    run: tuple[str, list[str]] | None = None

    def flush() -> None:
        nonlocal run
        if run is not None:
            out.append(run[0] + "WHERE " + " AND ".join(sorted(run[1])))
            run = None

    for line in sql.split("\n"):
        where = re.match(r"^(\s*)WHERE (.*)$", line)
        conj = re.match(r"^\s*AND (.*)$", line)
        if where:
            flush()
            run = (where.group(1), [where.group(2)])
        elif conj and run is not None:
            run[1].append(conj.group(1))
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


# --- the case generator, driven by the registry ------------------------------


def rows() -> dict[str, Any]:
    return json.loads(ROWS_PATH.read_text(encoding="utf-8"))


def target(variant: str) -> Any:
    from toolkit.comparables import TargetSpec

    point = rows()["target"]
    return TargetSpec(lat=point["lat"], lng=point["lng"], **TARGETS[variant])


def model_fields(grain: str) -> set[str]:
    if grain == "listings":
        from toolkit.comparables import ComparableFilters

        return {f.name for f in dataclasses.fields(ComparableFilters)}
    from api.notifications import WatchdogFilterSpec

    return set(WatchdogFilterSpec.model_fields)


def _agenda(grain: str) -> fr.Agenda:
    return fr.Agenda.COMPARABLES if grain == "listings" else fr.Agenda.WATCHDOG


def _relation_rows(grain: str) -> list[dict[str, Any]]:
    data = rows()
    return data["listings"] if grain == "listings" else data["properties_public"]


def _median(grain: str, column: str) -> Any:
    values = sorted(r[column] for r in _relation_rows(grain) if r.get(column) is not None)
    return statistics.median_low(values)


def _is_bound(f: fr.FilterDef) -> bool:
    return f.id.startswith(("min_", "max_")) or f.id.endswith(("_min", "_max"))


def _values_for(f: fr.FilterDef, grain: str) -> list[Any]:
    """The inputs one filter is pinned with. Shape-driven, so a new regular filter
    needs no line here."""
    enum = [o.value for o in f.enum_values or ()]
    if f.type is fr.FilterType.STRING_LIST:
        if fr.UNKNOWN_FILTER_VALUE in enum:
            return [[enum[0]], [fr.UNKNOWN_FILTER_VALUE], [enum[0], fr.UNKNOWN_FILTER_VALUE]]
        return [[enum[0]], [enum[0], enum[1]], []]
    if f.type is fr.FilterType.BOOL:
        return [True, False]
    if f.type is fr.FilterType.STRING and enum:
        return [enum[0]]
    if f.type in (fr.FilterType.INT, fr.FilterType.FLOAT) and f.pg_column:
        median = _median(grain, f.pg_column)
        return [0, median] if _is_bound(f) else [median]
    raise LookupError(f"{f.id}: no golden value shape — add it to _SPECIAL")


# Irregular filters: hooks and the adapters' own target-relative clauses. Each entry is
# (input, target variant) pairs; the generator adds the grain's base.
_SPECIAL: dict[str, list[tuple[dict[str, Any], str | None]]] = {
    "radius_m": [({"radius_m": 500}, "bare"), ({"radius_m": None}, "bare")],
    "area_band_pct": [({"area_band_pct": 0.1}, "area65"), ({"area_band_pct": 0.1}, "bare")],
    "disposition_match": [
        ({"disposition_match": m}, t)
        for m in ("exact", "loose", "any") for t in ("2+kk", "6+kk", "bare")
    ],
    "floor_band": [({"floor_band": 1}, "floor3"), ({"floor_band": 1}, "bare")],
    "lifecycle": [({"lifecycle": v}, "bare") for v in ("active", "delisted", "all")],
    "max_age_days": [({"max_age_days": 30}, "bare")],
    "include_unreliable": [({"include_unreliable": v}, "bare") for v in (False, True)],
    "tom_days_min": [({"tom_days_min": 5}, "bare")],
    "tom_days_max": [({"tom_days_max": 20}, "bare")],
    "last_seen_max_days": [({"last_seen_max_days": 3}, "bare")],
    "last_seen_min_days": [({"last_seen_min_days": 3}, "bare")],
    "first_seen_max_days": [({"first_seen_max_days": 20}, "bare")],
    "first_seen_min_days": [({"first_seen_min_days": 20}, "bare")],
    "include_no_price": [({"include_no_price": True}, None)],
    "price_change_window_days": [({"price_change_window_days": 30}, None)],
    "price_change_count_min": [
        ({"price_change_count_min": 1, "price_change_window_days": w}, None)
        for w in (None, 30, 90, 365)
    ],
    "total_price_change_pct": [({"total_price_change_pct": v}, None) for v in (-5, 0, 5)],
    "city_index_rules": [({"city_index_rules": [_CITY_RULE]}, None),
                         ({"city_index_rules": []}, None)],
}


def _price_cases(f: fr.FilterDef, grain: str) -> list[tuple[dict[str, Any], str | None]]:
    tgt = "bare" if grain == "listings" else None
    out: list[tuple[dict[str, Any], str | None]] = []
    for v in (0, _median(grain, "price_czk")):
        if grain == "watchdog":
            out.extend(({f.id: v, "include_no_price": inp}, tgt) for inp in (False, True))
        else:
            out.append(({f.id: v}, tgt))
    return out


def _district_cases() -> list[tuple[str, list[dict[str, Any]]]]:
    plan = json.loads((_ROOT / "tests" / "fixtures" / "district_chip_plan.json").read_text())
    return [(c["name"], c["chips"]) for c in plan["cases"]]


def _case(grain: str, name: str, inp: dict[str, Any], tgt: str | None,
          filter_id: str | None = None) -> dict[str, Any]:
    base = dict(_WATCHDOG_BASE) if grain == "watchdog" else {}
    case: dict[str, Any] = {"name": f"{grain}:{name}", "grain": grain,
                            "input": {**base, **inp}, "target_variant": tgt}
    if filter_id is not None:
        case["filter"] = filter_id
    return case


def _label(inp: dict[str, Any], tgt: str | None) -> str:
    label = ",".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in inp.items())
    return label if tgt in (None, "bare") else f"{label}@{tgt}"


def _pinned(f: fr.FilterDef, grain: str) -> list[tuple[str, dict[str, Any], str | None]]:
    """(label, input, target variant) for every case that pins one filter."""
    tgt = "bare" if grain == "listings" else None
    if f.id == "districts":
        return [(f"districts={name}", {"districts": chips}, None)
                for name, chips in _district_cases()]
    if f.id in ("min_price_czk", "max_price_czk"):
        pairs = _price_cases(f, grain)
    elif f.id in _SPECIAL:
        pairs = [(inp, tgt if grain == "watchdog" else t) for inp, t in _SPECIAL[f.id]]
    else:
        pairs = [({f.id: v}, tgt) for v in _values_for(f, grain)]
    return [(_label(inp, t), inp, t) for inp, t in pairs]


def _last_set(values: list[Any]) -> Any:
    """The widest pinned value (two elements, a real+unknown pair, the median bound)."""
    return [v for v in values if v is not None and v != []][-1]


def generate_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    everything: dict[str, dict[str, Any]] = {g: {} for g in GRAINS}
    point = rows()["target"]
    for grain in GRAINS:
        fields = model_fields(grain)
        cases.append(_case(grain, "empty", {}, "bare" if grain == "listings" else None))
        for f in fr.filters_for_agenda(_agenda(grain)):
            if f.id not in fields:
                continue
            pinned = _pinned(f, grain)
            for label, inp, tgt in pinned:
                cases.append(_case(grain, label, inp, tgt, f.id))
            if f.id != "districts":
                everything[grain][f.id] = _last_set([inp[f.id] for _, inp, _ in pinned])
        if grain == "watchdog":
            circle = {"lat": point["lat"], "lng": point["lng"], "radius_m": 800}
            cases.append(_case(grain, "location=circle", circle, None, "location"))
            everything[grain].update(circle)
            everything[grain].update({
                "districts": _district_cases()[2][1],
                "include_no_price": True,
                "price_change_window_days": 30,
                "category_type": "prodej",
            })
        else:
            everything[grain].update({
                "disposition_match": "loose",
                "include_unreliable": False,
                "lifecycle": "active",
            })
        cases.append(_case(grain, "everything", everything[grain],
                           "full" if grain == "listings" else None))

    # Rule 17 on the cohort: every city-quality id raises, derived from the category.
    for f in fr.all_filters():
        if f.category != fr.CATEGORY_CITY_QUALITY:
            continue
        value: Any = [_CITY_RULE] if f.id == "city_index_rules" else 1
        cases.append(_case("listings", f"rule17:{f.id}", {f.id: value}, "bare"))
    # Retired, loudly, on the Watchdog — `{}` included (is-not-None, not truthiness).
    for value in ({}, {"radius_km": 10, "index_rules": []}):
        cases.append(_case("watchdog", f"retired:near_city_proximity={json.dumps(value)}",
                           {"near_city_proximity": value}, None))

    # Whole statements, so the interleaving with each caller's own SQL is pinned too.
    for builder in ("build_query", "velocity", "corridor"):
        for label, inp, tgt in (("empty", {}, "bare"),
                                ("everything", everything["listings"], "full"),
                                ("radius_m=null", {"radius_m": None}, "bare")):
            cases.append({"name": f"statement:{builder}:{label}", "grain": "statement",
                          "builder": builder, "input": inp, "target_variant": tgt})
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "golden case names must be unique"
    return cases


# --- rendering ---------------------------------------------------------------


def render(case: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The CURRENT adapter's (where, params) for a listings / watchdog case."""
    if case["grain"] == "listings":
        from toolkit.comparables import ComparableFilters, _shared_filter_where

        return _shared_filter_where(target(case["target_variant"]),
                                    ComparableFilters(**case["input"]))
    from api.notifications import WatchdogFilterSpec, _build_match_clauses

    return _build_match_clauses(WatchdogFilterSpec(**case["input"]))


def _statement(case: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from toolkit.comparables import ComparableFilters, build_query
    from toolkit.transit_axis import build_corridor_query
    from toolkit.velocity import build_market_velocity_query

    tgt, filters = target(case["target_variant"]), ComparableFilters(**case["input"])
    if case["builder"] == "build_query":
        return build_query(tgt, filters)
    if case["builder"] == "velocity":
        return build_market_velocity_query(tgt, filters, "all")
    return build_corridor_query(tgt, filters, ["tram"], 800, 300)


def output(case: dict[str, Any]) -> dict[str, Any]:
    if case["grain"] == "statement":
        sql, params = _statement(case)
        return {"sql": canonical_statement(sql), "params": encode(params)}
    where, params = render(case)
    return {"where_sorted": sorted(where), "first": where[0], "params": encode(params)}


def _match_for(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("rule 17 violation"):
        return "^rule 17 violation"
    if "near_city_proximity is retired" in text:
        return "near_city_proximity is retired"
    return "^" + re.escape(text[:60])


def write() -> None:
    stored = {c["name"]: c for c in golden()} if GOLDEN_PATH.exists() else {}
    lines: list[str] = []
    for case in generate_cases():
        refused = case["grain"] == "listings" and set(case["input"]) - model_fields("listings")
        if refused and case["name"] in stored:
            # The model now refuses the field structurally (test_golden_case accepts that);
            # keep the pre-switch pin rather than rewriting it as a TypeError.
            lines.append(json.dumps(stored[case["name"]], ensure_ascii=False, sort_keys=True))
            continue
        try:
            result = output(case)
        except (ValueError, TypeError, LookupError) as exc:
            result = {"raises": type(exc).__name__, "match": _match_for(exc)}
        lines.append(json.dumps({**case, **result}, ensure_ascii=False, sort_keys=True))
    why = ("Written from the hand-coded adapters before toolkit/filter_compiler.py existed; "
           "see tests/toolkit/test_filter_compiler_golden.py. A diff here is a change to "
           "what a saved watchdog or an estimation cohort matches.")
    GOLDEN_PATH.write_text(
        '{"_why": ' + json.dumps(why) + ',\n"cases": [\n' + ",\n".join(lines) + "\n]}\n",
        encoding="utf-8",
    )
    print(f"wrote {len(lines)} cases to {GOLDEN_PATH}")


def golden() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]


# --- the assertions ----------------------------------------------------------


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("case", golden() if GOLDEN_PATH.exists() else [],
                         ids=lambda c: c["name"])
def test_golden_case(case: dict[str, Any]) -> None:
    try:
        actual = output(case)
    except Exception as exc:  # noqa: BLE001 — the raise IS the assertion
        if "raises" not in case:
            raise
        absent = set(case["input"]) - model_fields(case["grain"])
        if isinstance(exc, TypeError) and case["grain"] == "listings" and absent:
            # The field is gone from ComparableFilters: the model refuses it structurally,
            # which is stronger than the raise it used to reach (rule 17).
            return
        assert type(exc).__name__ == case["raises"], f"{case['name']}: {exc!r}"
        assert re.search(case["match"], str(exc)), f"{case['name']}: {exc}"
        return
    assert "raises" not in case, f"{case['name']} no longer raises {case['raises']}"
    if case["grain"] == "statement":
        assert actual["sql"] == case["sql"], case["name"]
    else:
        missing = sorted(set(case["where_sorted"]) - set(actual["where_sorted"]))
        added = sorted(set(actual["where_sorted"]) - set(case["where_sorted"]))
        assert actual["where_sorted"] == case["where_sorted"], (
            f"{case['name']}: dropped {missing}, added {added}"
        )
        assert actual["first"] == case["first"], case["name"]
    assert _dump(actual["params"]) == _dump(case["params"]), case["name"]


def test_golden_covers_every_compiled_filter() -> None:
    """A new filter on either grain forces a reviewable golden diff."""
    covered = {(c["grain"], c.get("filter")) for c in golden()}
    missing: list[str] = []
    for grain in GRAINS:
        fields = model_fields(grain) | ({"location"} if grain == "watchdog" else set())
        for f in fr.filters_for_agenda(_agenda(grain)):
            if f.id in fields and (grain, f.id) not in covered:
                missing.append(f"{grain}:{f.id}")
    assert not missing, (
        f"no golden case for {missing}; run "
        "`python -m tests.toolkit.test_filter_compiler_golden --write` and review the diff"
    )


def test_the_golden_is_what_the_generator_writes_for_today_s_models() -> None:
    """Every generated case for a filter that still exists is in the file, unchanged in
    input — so `--write` cannot have been run against a hand-edited generator."""
    stored = {c["name"]: c for c in golden()}
    for case in generate_cases():
        if case["grain"] == "listings" and set(case["input"]) - model_fields("listings"):
            continue
        assert case["name"] in stored, f"generated but not stored: {case['name']}"
        assert _dump(stored[case["name"]]["input"]) == _dump(case["input"]), case["name"]


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        write()
    else:
        sys.exit("usage: python -m tests.toolkit.test_filter_compiler_golden --write")
