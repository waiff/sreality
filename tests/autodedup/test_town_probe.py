"""The town probe: the population rail, the rule floor, E12, and one paged scan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import town_probe as T
from autodedup.text_facts import MAX_CODE_POPULATION


def side(listing_id: int, **kw: Any) -> T.Side:
    base = {
        "id": listing_id, "source": "sreality", "category_main": "byt",
        "category_type": "prodej", "area_m2": 60.0, "disposition": "2+kk", "floor": 3,
        "obec_kod": 1, "obec_name": "A", "cast_obce_kod": None, "okres_kod": 10,
        "okres_name": "OA", "kraj_kod": 100, "granularity": "address_point",
    }
    base.update(kw)
    return T.Side(**base)  # type: ignore[arg-type]


def test_code_pairs_honours_the_population_rail() -> None:
    crowd = {"N1": [side(i) for i in range(MAX_CODE_POPULATION + 1)]}
    assert list(T.code_pairs(crowd)) == []
    pair = {"N2": [side(7), side(3)]}
    got = list(T.code_pairs(pair))
    assert [(c, lo.id, hi.id) for c, lo, hi in got] == [("N2", 3, 7)]


def test_code_pairs_ignores_a_code_only_one_advert_prints() -> None:
    assert list(T.code_pairs({"N3": [side(1)]})) == []


def test_classify_is_e12_missing_is_never_a_mismatch() -> None:
    verdict = T.classify(side(1, obec_kod=None), side(2, obec_kod=5))
    assert verdict["obec"] is None
    assert T.classify(side(1), side(2, obec_kod=5))["obec"] is True
    assert T.classify(side(1), side(2))["obec"] is False


def test_classify_reports_the_rule_floor() -> None:
    assert T.classify(side(1), side(2, category_type="pronajem"))["guard_veto"] == "category_type"
    assert T.classify(side(1), side(2, area_m2=200.0))["guard_veto"] == "area"
    assert T.classify(side(1), side(2))["guard_veto"] is None


def test_parse_args_rejects_the_unknown_and_clamps_the_timeout() -> None:
    with pytest.raises(ValueError, match="unknown town arg"):
        T.parse_args({"nope": "1"})
    with pytest.raises(ValueError, match="timeout_s"):
        T.parse_args({"timeout_s": str(T.TIMEOUT_S_MAX + 1)})
    with pytest.raises(ValueError, match="batch"):
        T.parse_args({"batch": "0"})
    assert T.parse_args({"batch": "10"})["batch"] == 10


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("SET "):
            self.rows = []
        elif sql is T.COVERAGE_SQL:
            self.rows = [{"source": "sreality", "n_listings": 3, "n_with_obec": 3}]
        elif sql is T.SCAN_SQL:
            after = int(params["after"])
            batch = int(params["batch"])
            self.conn.scans.append((after, batch, params["hint"]))
            rows = [r for r in self.conn.listings if int(r["id"]) > after]
            self.rows = rows[:batch]
        else:  # pragma: no cover - the fake must never answer a statement it cannot see
            raise AssertionError(f"unexpected statement: {sql[:60]}")

    @property
    def description(self) -> list[tuple[str]]:
        return [(k,) for k in (self.rows[0] if self.rows else {})]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [tuple(r.values()) for r in self.rows]

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, listings: list[dict[str, Any]]) -> None:
        self.listings = listings
        self.scans: list[tuple[int, int, str]] = []
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx()

    def close(self) -> None:
        self.closed = True


def row(listing_id: int, text: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": listing_id, "source": "sreality", "category_main": "byt",
        "category_type": "prodej", "area_m2": 60.0, "disposition": "2+kk", "floor": 3,
        "description": text, "obec_kod": 1, "obec_name": "A", "cast_obce_kod": None,
        "okres_kod": 10, "okres_name": "OA", "kraj_kod": 100,
        "granularity": "address_point",
    }
    base.update(kw)
    return base


def test_run_town_pages_the_scan_and_counts_the_cross_town_pair(tmp_path: Path) -> None:
    listings = [
        row(1, "Ev. číslo: 657349"),
        row(2, "evidenční číslo zakázky 657349", source="idnes", obec_kod=2, obec_name="B",
            okres_kod=10, okres_name="OA"),
        row(3, "Ev. číslo: 111222", obec_kod=1),
        row(4, "Ev. číslo: 111222", source="bazos", obec_kod=1),
        row(5, "bez kódu, jen text o zakázce"),
    ]
    conn = _Conn(listings)
    result = T.run_town(lambda: conn, {"batch": "2", "timeout_s": "5"}, tmp_path)

    assert [after for after, _, _ in conn.scans] == [0, 2, 4, 5]
    assert result["scanned_rows"] == 5
    assert result["rows_with_code"] == 4
    tally = result["tally"]
    assert tally["pairs"] == 2
    assert tally["pairs_guard_clean"] == 2
    assert tally["obec:differ"] == 1
    assert tally["obec:same"] == 1
    assert tally["okres:same"] == 2
    payload = json.loads((tmp_path / "town.json").read_text())
    assert payload["cross_town_sample"][0]["code"] == "657349"
    assert payload["cross_town_sample"][0]["lo"] == 1
    assert payload["by_source_pair"]["idnes|sreality"]["obec_differ"] == 1
    assert conn.closed


def test_run_town_drops_a_pair_the_rule_floor_refuses(tmp_path: Path) -> None:
    listings = [
        row(1, "Ev. číslo: 657349", area_m2=60.0),
        row(2, "Ev. číslo: 657349", area_m2=200.0, obec_kod=2, obec_name="B"),
    ]
    result = T.run_town(lambda: _Conn(listings), {"timeout_s": "5"}, tmp_path)
    assert result["tally"]["pairs"] == 1
    assert result["tally"]["guard_veto:area"] == 1
    assert result["tally"].get("pairs_guard_clean", 0) == 0
    assert result["tally"].get("obec:differ", 0) == 0


def test_run_town_stops_at_max_rows(tmp_path: Path) -> None:
    listings = [row(i, "Ev. číslo: 65734" + str(i)) for i in range(1, 11)]
    result = T.run_town(
        lambda: _Conn(listings), {"batch": "2", "max_rows": "4", "timeout_s": "5"}, tmp_path
    )
    assert result["scanned_rows"] == 4


def test_lane_registers_the_mode() -> None:
    from autodedup import lane

    assert lane.MODES["town"] is T.run_town
    assert "town" not in lane.ITERATION_META
