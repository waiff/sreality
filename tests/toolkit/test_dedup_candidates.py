"""Path C as ruled on 2026-09-10, held to in Python: the registry shape, the fingerprint
that identifies a parameter set, every "not available" definition, the fallback-on-absence
rule, the merge chokepoint's category guards, the byt floor rule, and the generation-run
lifecycle over the migration-492 store (fake connection — control flow and SQL params,
not the schema; CI's schema-replay job PREPAREs the statements themselves)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from toolkit import dedup_candidates as dc
from toolkit import dedup_sim_settings as dss

INPUTS: dict[str, Any] = {
    "path": "C",
    "generator_version": "c3",
    "l0_path_c_town_key": "obec_kod",
    "l0_path_c_district_key": "cast_obce_kod",
    "l0_path_c_district_split_towns": "554782,582786,554821",
    "l0_candidate_scope": "all",
    "l0_floor_tolerance": 2,
    "l0_c1_area_tolerance_pct": 20,
    "l0_area_tolerance_pct_general": 5,
    "l0_area_tolerance_pct_pozemek": 2,
}


def _l(
    listing_id: int,
    *,
    town: str | None = "554782",
    ctype: str | None = "prodej",
    cmain: str | None = "byt",
    dispo: str | None = "2+kk",
    floor: int | None = 3,
    usable: float | None = 60.0,
    estate: float | None = None,
    district: str | None = None,
) -> dc.ListingAttrs:
    return dc.ListingAttrs(listing_id, town, ctype, cmain, dispo, floor, usable, estate, district)


# ------------------------------------------------------------------ registry + inputs


def test_path_c_is_the_only_defined_path_with_rungs_c1_and_c3() -> None:
    assert set(dc.PATHS) == {"C"}
    pd = dc.path_def("C")
    assert pd.block_key == "obec_kod"
    assert pd.floor_feature == "dedup_path_c"
    assert [r.code for r in pd.rungs] == ["C1", "C3"]
    assert pd.rungs[0].needs == ("disposition",)
    assert pd.rungs[1].needs == ("area",)
    for r in pd.rungs:
        assert r.explanation
    assert pd.explanation


def test_path_c_floor_is_declared_in_the_serving_contract() -> None:
    from location_data import serving_contracts as sc

    floor = sc.floor_for(dc.path_def("C").floor_feature)
    assert (floor.grain, floor.min_granularity, floor.min_confidence) == ("listing", "obec", "any")
    assert sc.meets_floor("dedup_path_c", granularity="obec", match_confidence="low")
    assert not sc.meets_floor("dedup_path_c", granularity="okres", match_confidence="exact")


def test_undefined_path_raises() -> None:
    with pytest.raises(KeyError, match="no candidate path 'A'"):
        dc.path_def("A")


def test_every_path_setting_key_is_registered_and_the_inputs_carry_them_all() -> None:
    pd = dc.path_def("C")
    for key in pd.settings_keys:
        assert key in dss.REGISTRY, key
    inputs = dc.path_inputs("C", dss.effective_settings(None))
    assert inputs["path"] == "C"
    assert inputs["generator_version"] == dc.GENERATOR_VERSION["C"]
    assert set(inputs) == {"path", "generator_version", *pd.settings_keys}
    assert inputs == INPUTS  # the registry defaults ARE the ruled parameters


def test_inputs_missing_a_setting_raise() -> None:
    with pytest.raises(KeyError, match="l0_floor_tolerance"):
        dc.path_inputs("C", {"l0_path_c_town_key": "obec_kod"})


def test_fingerprint_is_stable_order_independent_and_sensitive_to_every_input() -> None:
    fp = dc.fingerprint(INPUTS)
    assert len(fp) == 16 and int(fp, 16) >= 0
    shuffled = dict(reversed(list(INPUTS.items())))
    assert dc.fingerprint(shuffled) == fp
    for key in INPUTS:
        changed = {**INPUTS, key: "x" if isinstance(INPUTS[key], str) else INPUTS[key] + 1}
        assert dc.fingerprint(changed) != fp, key


def test_fingerprint_pins_the_ruled_defaults() -> None:
    # The literal hash of the ruled parameter set. A changed default or generator version
    # MUST move it — pairs generated under different inputs live in different key spaces —
    # and whoever changes it must say so in the ledger. Moved 2026-09-10 by the district
    # split + the C1 area check (generator version c1 -> c2), and 2026-09-12 by W2-b moving
    # the block key onto `listing_location` (c2 -> c3).
    assert dc.fingerprint(INPUTS) == "992cc57488111635"


def test_fingerprint_is_canonical_over_how_a_number_was_typed() -> None:
    # JSON has no int/float distinction: an override typed as 5.0 IS the default 5.
    assert dc.fingerprint({**INPUTS, "l0_area_tolerance_pct_general": 5.0}) == dc.fingerprint(INPUTS)
    assert dc.fingerprint({**INPUTS, "l0_area_tolerance_pct_general": 5.5}) != dc.fingerprint(INPUTS)
    assert dc._canonical({"a": [1.0, 2.5, True], "b": {"c": 3.0}}) == {"a": [1, 2.5, True], "b": {"c": 3}}


def test_the_town_key_setting_must_match_the_column_the_sql_implements() -> None:
    with pytest.raises(ValueError, match="l0_path_c_town_key='momc_kod' is not implemented"):
        dc.path_inputs("C", {**dss.effective_settings(None), "l0_path_c_town_key": "momc_kod"})
    assert dss.REGISTRY["l0_path_c_town_key"].enum_choices == (dc.path_def("C").block_key,)


def test_the_district_key_setting_must_match_the_column_the_sql_implements() -> None:
    with pytest.raises(ValueError, match="l0_path_c_district_key='momc_kod' is not implemented"):
        dc.path_inputs("C", {**dss.effective_settings(None), "l0_path_c_district_key": "momc_kod"})
    assert dss.REGISTRY["l0_path_c_district_key"].enum_choices == (dc.path_def("C").district_key,)


# ------------------------------------------------------------------ the city-district split


def test_split_towns_parses_the_setting() -> None:
    assert dc.split_towns(INPUTS) == ("554782", "582786", "554821")
    assert dc.split_towns({**INPUTS, "l0_path_c_district_split_towns": " 1, 2 ,"}) == ("1", "2")
    assert dc.split_towns({**INPUTS, "l0_path_c_district_split_towns": ""}) == ()


def test_district_applies_only_inside_a_split_town_and_only_when_known() -> None:
    assert dc.district_of("554782", "490067", INPUTS) == "490067"   # Praha, quarter known
    assert dc.district_of("554782", None, INPUTS) is None            # Praha, quarter unknown
    assert dc.district_of("554499", "490067", INPUTS) is None        # a small town is never split
    assert dc.district_of(None, "490067", INPUTS) is None
    assert dc.district_of("554782", "490067", {**INPUTS, "l0_path_c_district_split_towns": ""}) is None


def test_two_known_districts_must_match_but_an_unknown_one_never_vetoes() -> None:
    praha = {"town": "554782", "dispo": "2+kk"}
    same = dc.evaluate_pair(_l(1, district="A", **praha), _l(2, district="A", **praha), INPUTS)
    assert same is not None and same.rung == "C1"
    assert dc.evaluate_pair(_l(1, district="A", **praha), _l(2, district="B", **praha), INPUTS) is None
    # the whole point of the fallback: a listing with no quarter still reaches the town
    assert dc.evaluate_pair(_l(1, district=None, **praha), _l(2, district="B", **praha), INPUTS) is not None
    assert dc.evaluate_pair(_l(1, district="A", **praha), _l(2, district=None, **praha), INPUTS) is not None
    assert dc.districts_compatible(_l(1, district=None), _l(2, district="B")) is True


# ------------------------------------------------------------------ "not available"


@pytest.mark.parametrize("value,available", [
    ("2+kk", True), (" 3+1 ", True), (None, False), ("", False), ("   ", False),
])
def test_disposition_availability(value: str | None, available: bool) -> None:
    assert dc.disposition_available(value) is available
    assert (dc.normalized_disposition(value) is not None) is available


def test_area_of_picks_plot_area_for_land_and_treats_zero_as_missing() -> None:
    assert dc.area_of("byt", 60, 900) == 60.0
    assert dc.area_of("pozemek", 60, 900) == 900.0
    assert dc.area_of("pozemek", 60, None) is None
    assert dc.area_of("byt", 0, None) is None
    assert dc.area_of("byt", None, 900) is None
    assert dc.area_of("byt", "72.5", None) == 72.5


def test_area_diff_is_a_percent_of_the_larger_side() -> None:
    assert dc.area_diff_pct(95, 100) == pytest.approx(5.0)
    assert dc.area_diff_pct(100, 95) == pytest.approx(5.0)


def test_area_tolerance_is_the_pozemek_one_when_either_side_is_land() -> None:
    assert dc.area_tolerance_pct("byt", "byt", INPUTS) == 5.0
    assert dc.area_tolerance_pct("pozemek", "pozemek", INPUTS) == 2.0
    assert dc.area_tolerance_pct(None, "pozemek", INPUTS) == 2.0


# ------------------------------------------------------------------ the rule


def test_same_listing_is_never_a_pair() -> None:
    a = _l(1)
    assert dc.evaluate_pair(a, a, INPUTS) is None


def test_different_or_missing_town_is_never_a_pair() -> None:
    assert dc.evaluate_pair(_l(1, town="554782"), _l(2, town="582786"), INPUTS) is None
    assert dc.evaluate_pair(_l(1, town=None), _l(2), INPUTS) is None
    assert dc.evaluate_pair(_l(1), _l(2, town=None), INPUTS) is None


def test_sale_and_rent_never_pair_but_unknown_type_is_not_a_conflict() -> None:
    assert dc.evaluate_pair(_l(1, ctype="prodej"), _l(2, ctype="pronajem"), INPUTS) is None
    assert dc.evaluate_pair(_l(1, ctype=None), _l(2, ctype="pronajem"), INPUTS) is not None


def test_category_main_guard_is_the_chokepoints() -> None:
    assert dc.evaluate_pair(_l(1, cmain="byt"), _l(2, cmain="dum", dispo="2+kk"), INPUTS) is None
    # the one sanctioned cross-type
    v = dc.evaluate_pair(_l(1, cmain="dum", dispo="5+1"), _l(2, cmain="komercni", dispo="5+1"), INPUTS)
    assert v is not None and v.rung == "C1"
    assert dc.evaluate_pair(_l(1, cmain=None), _l(2, cmain="dum", dispo="2+kk"), INPUTS) is not None


def test_c1_needs_both_dispositions_and_they_must_match() -> None:
    v = dc.evaluate_pair(_l(1, dispo="2+kk"), _l(2, dispo="2+kk"), INPUTS)
    assert v == dc.PairVerdict("C1", "2+kk", 60.0, 60.0, 0.0, True)
    # a mismatch does NOT fall back to the area rung — only absence does
    assert dc.evaluate_pair(_l(1, dispo="2+kk"), _l(2, dispo="3+kk", usable=60.0), INPUTS) is None
    # whitespace is not a different disposition
    assert dc.evaluate_pair(_l(1, dispo="2+kk"), _l(2, dispo=" 2+kk "), INPUTS) is not None


def test_c1_also_checks_the_area_when_both_sides_state_one() -> None:
    # ruled 2026-09-10: town + disposition + area, +/- 20% by default
    ok = dc.evaluate_pair(_l(1, usable=100), _l(2, usable=81), INPUTS)
    assert ok is not None and ok.rung == "C1"
    assert (ok.area_lo, ok.area_hi) == (100.0, 81.0) and ok.area_diff_pct == pytest.approx(19.0)
    assert dc.evaluate_pair(_l(1, usable=100), _l(2, usable=79), INPUTS) is None
    tight = {**INPUTS, "l0_c1_area_tolerance_pct": 5}
    assert dc.evaluate_pair(_l(1, usable=100), _l(2, usable=81), tight) is None


def test_c1_keeps_the_pair_when_an_area_is_missing_as_with_an_unknown_floor() -> None:
    for a, b in ((_l(1, usable=None), _l(2)), (_l(1), _l(2, usable=0))):
        v = dc.evaluate_pair(a, b, INPUTS)
        assert v is not None and v.rung == "C1"
        assert (v.area_lo, v.area_hi, v.area_diff_pct) == (None, None, None)


def test_c1_area_lo_hi_follow_listing_id_order() -> None:
    v = dc.evaluate_pair(_l(9, usable=100), _l(4, usable=90), INPUTS)
    assert v is not None and (v.area_lo, v.area_hi) == (90.0, 100.0)


def test_missing_disposition_on_either_side_falls_back_to_the_area_rung() -> None:
    for a, b in ((_l(1, dispo=None), _l(2)), (_l(1), _l(2, dispo="")), (_l(1, dispo=None), _l(2, dispo=None))):
        v = dc.evaluate_pair(a, b, INPUTS)
        assert v is not None and v.rung == "C3", (a, b)
        assert v.disposition is None
        assert (v.area_lo, v.area_hi) == (60.0, 60.0) and v.area_diff_pct == 0.0


def test_c3_needs_area_on_both_sides() -> None:
    assert dc.evaluate_pair(_l(1, dispo=None, usable=None), _l(2), INPUTS) is None
    assert dc.evaluate_pair(_l(1, dispo=None), _l(2, usable=0), INPUTS) is None


def test_c3_area_tolerance_general_and_pozemek() -> None:
    assert dc.evaluate_pair(_l(1, dispo=None, usable=100), _l(2, usable=95), INPUTS) is not None
    assert dc.evaluate_pair(_l(1, dispo=None, usable=100), _l(2, usable=94), INPUTS) is None
    land = dict(cmain="pozemek", dispo=None, usable=None)
    assert dc.evaluate_pair(_l(1, estate=1000, **land), _l(2, estate=980, **land), INPUTS) is not None
    assert dc.evaluate_pair(_l(1, estate=1000, **land), _l(2, estate=970, **land), INPUTS) is None


def test_c3_area_lo_hi_follow_listing_id_order_not_argument_order() -> None:
    v = dc.evaluate_pair(_l(9, dispo=None, usable=100), _l(4, usable=96), INPUTS)
    assert v is not None and (v.area_lo, v.area_hi) == (96.0, 100.0)
    assert v.area_diff_pct == pytest.approx(4.0)


def test_floor_rule_applies_to_byt_pairs_with_both_floors_and_is_skipped_otherwise() -> None:
    assert dc.evaluate_pair(_l(1, floor=1), _l(2, floor=3), INPUTS) is not None
    assert dc.evaluate_pair(_l(1, floor=1), _l(2, floor=4), INPUTS) is None
    unchecked = dc.evaluate_pair(_l(1, floor=None), _l(2, floor=9), INPUTS)
    assert unchecked is not None and unchecked.floor_checked is False
    houses = dc.evaluate_pair(_l(1, cmain="dum", floor=1), _l(2, cmain="dum", floor=9), INPUTS)
    assert houses is not None and houses.floor_checked is False
    # the floor rule vetoes on C3 as well as C1
    assert dc.evaluate_pair(_l(1, dispo=None, floor=1), _l(2, floor=4), INPUTS) is None


def test_unknown_category_never_lets_the_floor_rule_veto() -> None:
    # the SQL mirrors this with COALESCE(category_main, '') = 'byt'
    v = dc.evaluate_pair(_l(1, cmain=None, floor=1), _l(2, floor=9), INPUTS)
    assert v is not None and v.floor_checked is False
    v = dc.evaluate_pair(_l(1, cmain=None, floor=1), _l(2, cmain=None, floor=9), INPUTS)
    assert v is not None and v.floor_checked is False


def test_floor_tolerance_comes_from_the_inputs() -> None:
    tight = {**INPUTS, "l0_floor_tolerance": 0}
    assert dc.evaluate_pair(_l(1, floor=2), _l(2, floor=3), tight) is None
    assert dc.evaluate_pair(_l(1, floor=3), _l(2, floor=3), tight) is not None


def test_rule_is_symmetric() -> None:
    cases = [
        (_l(1, dispo="2+kk"), _l(2, dispo="2+kk")),
        (_l(1, dispo=None, usable=100), _l(2, usable=97)),
        (_l(1, floor=1), _l(2, floor=4)),
        (_l(1, ctype="prodej"), _l(2, ctype="pronajem")),
    ]
    for a, b in cases:
        assert dc.evaluate_pair(a, b, INPUTS) == dc.evaluate_pair(b, a, INPUTS), (a, b)


def test_evaluate_pair_refuses_other_paths() -> None:
    with pytest.raises(ValueError):
        dc.evaluate_pair(_l(1), _l(2), {**INPUTS, "path": "A"})


# ------------------------------------------------------------------ lifecycle


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._last: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.calls.append((sql, params))
        self._last = self._conn.answers.pop(0) if self._conn.answers else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._last

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [self._last] if self._last else []


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, answers: list[tuple[Any, ...] | None]) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.answers = list(answers)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx()


def test_begin_generation_writes_run_inputs_and_generation_in_order() -> None:
    # answers: settings overrides (none), run id, inputs id, generation id
    conn = _Conn([None, (11,), (5,), (77,)])
    gen = dc.begin_generation(conn, "C")
    sqls = [c[0] for c in conn.calls]
    assert "FROM dedup_sim.settings" in sqls[0]
    assert sqls[1].startswith("INSERT INTO dedup_sim.simulation_runs")
    assert sqls[2].startswith("INSERT INTO dedup_sim.candidate_inputs")
    assert sqls[3].startswith("INSERT INTO dedup_sim.candidate_generations")
    run_params = conn.calls[1][1]
    assert run_params["triggered_by"] == "lane"
    assert set(json.loads(run_params["snapshot"])) == set(dss.REGISTRY)
    inputs_params = conn.calls[2][1]
    assert inputs_params["path"] == "C"
    assert inputs_params["fingerprint"] == dc.fingerprint(INPUTS)
    assert json.loads(inputs_params["inputs"]) == INPUTS
    assert conn.calls[3][1] == {"run_id": 11, "inputs_id": 5, "progress": None}
    assert (gen.id, gen.simulation_run_id, gen.inputs_id) == (77, 11, 5)
    assert gen.status == "running" and gen.fingerprint == dc.fingerprint(INPUTS)


def test_finish_generation_closes_both_rows_with_a_terminal_status() -> None:
    conn = _Conn([None, (1,), (2,), (3,)])
    gen = dc.begin_generation(conn, "C")
    conn.calls.clear()
    dc.finish_generation(conn, gen, status="success", stats={"pairs": 3})
    assert [c[0].split(" SET")[0] for c in conn.calls] == [
        "UPDATE dedup_sim.candidate_generations",
        "UPDATE dedup_sim.simulation_runs",
    ]
    for _, params in conn.calls:
        assert params["status"] == "success"
        assert json.loads(params["stats"]) == {"pairs": 3}
        assert params["error"] is None
    assert conn.calls[0][1]["id"] == 3 and conn.calls[1][1]["id"] == 1
    with pytest.raises(ValueError):
        dc.finish_generation(conn, gen, status="running")


def test_failed_generation_keeps_its_row_and_records_the_error() -> None:
    conn = _Conn([None, (1,), (2,), (3,)])
    gen = dc.begin_generation(conn, "C")
    conn.calls.clear()
    dc.finish_generation(conn, gen, status="failed", error="boom", progress={"block_index": 4})
    assert conn.calls[0][1]["error"] == "boom"
    assert json.loads(conn.calls[0][1]["progress"]) == {"block_index": 4}
    assert conn.calls[1][1]["error"] == "boom"


def test_an_operator_override_reaches_the_inputs_the_fingerprint_and_the_snapshot() -> None:
    # settings overrides answered as rows of (key, value), then run id, inputs id, gen id
    conn = _Conn([("l0_floor_tolerance", 3), (11,), (5,), (77,)])
    gen = dc.begin_generation(conn, "C")
    assert gen.inputs["l0_floor_tolerance"] == 3
    assert gen.fingerprint == dc.fingerprint({**INPUTS, "l0_floor_tolerance": 3}) != dc.fingerprint(INPUTS)
    assert json.loads(conn.calls[1][1]["snapshot"])["l0_floor_tolerance"] == 3


def test_get_generation_reads_jsonb_as_dicts_or_strings() -> None:
    # psycopg hands jsonb back as dicts; the str branch covers a driver that does not
    as_dicts = (9, 4, 2, "C", "abcd", dict(INPUTS), "success", {"blocks_done": 3}, {"pairs": {"total": 7}})
    conn = _Conn([as_dicts])
    gen = dc.get_generation(conn, 9)
    assert gen is not None and gen.inputs == INPUTS and gen.stats == {"pairs": {"total": 7}}
    assert gen.progress == {"blocks_done": 3} and gen.status == "success"
    assert conn.calls[-1][1] == {"id": 9}
    as_strings = (9, 4, 2, "C", "abcd", json.dumps(INPUTS), "success", '{"blocks_done": 3}', '{"pairs": 1}')
    gen2 = dc.get_generation(_Conn([as_strings]), 9)
    assert gen2 is not None and gen2.inputs == INPUTS and gen2.stats == {"pairs": 1}
    assert dc.get_generation(_Conn([None]), 9) is None


def test_reopen_generation_puts_both_rows_back_to_running() -> None:
    conn = _Conn([None, (1,), (2,), (3,)])
    gen = dc.begin_generation(conn, "C")
    conn.calls.clear()
    dc.reopen_generation(conn, gen)
    assert [c[0].split(" SET")[0] for c in conn.calls] == [
        "UPDATE dedup_sim.candidate_generations", "UPDATE dedup_sim.simulation_runs"]
    assert "status = 'running', completed_at = NULL, error_message = NULL" in conn.calls[0][0]
    assert conn.calls[0][1] == {"id": 3} and conn.calls[1][1] == {"id": 1}


def test_record_progress_and_latest_generation_roundtrip() -> None:
    conn = _Conn([None])
    dc.record_progress(conn, 9, {"block_index": 2})
    assert conn.calls[-1][1] == {"id": 9, "progress": json.dumps({"block_index": 2})}
    row = (9, 4, 2, "C", "abcd", json.dumps(INPUTS), "running", '{"block_index": 2}', None)
    conn = _Conn([row])
    gen = dc.latest_generation(conn, "C", fingerprint_="abcd", status="running")
    assert gen is not None and gen.id == 9 and gen.inputs == INPUTS
    assert gen.progress == {"block_index": 2} and gen.stats is None
    assert conn.calls[-1][1] == {"path": "C", "fingerprint": "abcd", "status": "running"}
    assert dc.latest_generation(_Conn([None]), "C") is None


# --------------------------------------------------------------------------- audit reads


def test_store_ready_probes_the_catalog_and_never_raises() -> None:
    """`to_regclass` answers NULL for a relation that is not there, so the audit page can
    ask "is migration 492 applied?" without a failing query to recover from."""
    conn = _Conn([(True,)])
    assert dc.store_ready(conn) is True
    assert "to_regclass" in conn.calls[0][0]
    assert conn.calls[0][1] is None  # a catalog probe takes no parameters
    assert dc.store_ready(_Conn([(None,)])) is False
    assert dc.store_ready(_Conn([None])) is False


def test_list_recent_generations_zips_the_column_contract() -> None:
    row = (77, "success", "2026-09-10T08:00:00", "2026-09-10T09:30:12", "abcd", "all", False, 421_100)
    conn = _Conn([row])
    assert dc.list_recent_generations(conn, "C", 20) == [
        {
            "id": 77,
            "status": "success",
            "created_at": "2026-09-10T08:00:00",
            "completed_at": "2026-09-10T09:30:12",
            "fingerprint": "abcd",
            "scope": "all",
            "partial": False,
            "pairs_total": 421_100,
        }
    ]
    sql, params = conn.calls[-1]
    assert params == {"path": "C", "limit": 20}
    # The three facts read straight off `stats`, and a tiebreaker so the picker's order
    # never reshuffles between two runs created in the same instant.
    assert "g.stats->>'scope'" in sql and "g.stats->'pairs'->>'total'" in sql
    assert "ORDER BY g.created_at DESC, g.id DESC" in sql
    assert dc.list_recent_generations(_Conn([]), "C") == []


def test_list_recent_generations_defaults_to_twenty() -> None:
    conn = _Conn([])
    dc.list_recent_generations(conn, "C")
    assert conn.calls[-1][1] == {"path": "C", "limit": 20}


def test_generation_timestamps_returns_the_four_audit_columns() -> None:
    conn = _Conn([("2026-09-10T08:00:00", "2026-09-10T08:00:03", None, "statement timeout")])
    assert dc.generation_timestamps(conn, 77) == {
        "created_at": "2026-09-10T08:00:00",
        "started_at": "2026-09-10T08:00:03",
        "completed_at": None,
        "error_message": "statement timeout",
    }
    assert conn.calls[-1][1] == {"id": 77}
    assert dc.generation_timestamps(_Conn([None]), 77) is None
