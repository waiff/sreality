"""Tests for the street + disposition dedup engine.

The pure rule logic (toolkit.dedup_engine) is tested directly — no DB. The
orchestration (scripts.dedup_engine.run_engine) is tested against a scripted
fake connection with injected classify/compare callables, so the stage flow,
caps, and merge/queue dispatch are verified without a real DB or LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.dedup_engine import (
    ListingKey,
    classify_pair,
    decide_phash_fastpath,
    disposition_compatible,
    normalize_street,
    rooms_in_priority,
    verdict_is_merge,
)


def _key(
    sid: int, *, pid: int | None = None, source: str = "sreality",
    street: str = "id:42", disp: str = "2+kk", hn: str | None = "10",
    floor: int | None = 3, area: float | None = 60.0,
    description: str | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid if pid is not None else sid,
        source=source, street_key=street, disposition=disp,
        house_number=hn, floor=floor, area_m2=area, description=description,
    )


# --- normalize_street -------------------------------------------------------

def test_normalize_street_prefers_street_id() -> None:
    assert normalize_street("Nádražní", 42) == "id:42"


def test_normalize_street_falls_back_to_name_diacritics_stripped() -> None:
    assert normalize_street("Nádražní 12", None) == "name:nadrazni 12"


def test_normalize_street_none_when_empty_or_negative_id() -> None:
    assert normalize_street(None, None) is None
    assert normalize_street("", None) is None
    assert normalize_street("Hlavní", -1) == "name:hlavni"  # -1 sentinel -> name


# --- disposition compatibility ----------------------------------------------

def test_disposition_loose_equivalence() -> None:
    assert disposition_compatible("2+kk", "2+1")
    assert disposition_compatible("2+kk", "2+kk")
    assert not disposition_compatible("2+kk", "3+kk")
    assert not disposition_compatible(None, "2+kk")


# --- classify_pair: rule B (exact address auto-merge) -----------------------

def test_exact_address_auto_merges() -> None:
    d = classify_pair(_key(1, source="sreality"), _key(2, source="bazos"))
    assert d.action == "auto_merge"
    assert d.reason == "address_exact"


def test_exact_address_works_same_source() -> None:
    # operator decision: merge regardless of source
    d = classify_pair(_key(1, source="sreality"), _key(2, source="sreality"))
    assert d.action == "auto_merge"


def test_exact_address_area_guard_demotes_to_candidate() -> None:
    # same street+no+disp+floor but areas 60 vs 70 (>5%) -> visual, not blind merge
    d = classify_pair(_key(1, area=60.0), _key(2, area=70.0))
    assert d.action == "candidate"
    assert d.reason == "area_guard"


def test_exact_address_within_area_guard_still_merges() -> None:
    d = classify_pair(_key(1, area=60.0), _key(2, area=62.0))  # ~3% < 5%
    assert d.action == "auto_merge"


# --- classify_pair: rule C (candidate + disqualifiers) ----------------------

def test_same_street_disposition_no_house_number_is_candidate() -> None:
    d = classify_pair(_key(1, hn=None), _key(2, hn=None))
    assert d.action == "candidate"
    assert d.reason is None


def test_floor_contradiction_rejects() -> None:
    d = classify_pair(_key(1, floor=2), _key(2, floor=5))
    assert d.action == "reject"
    assert d.detail == "floor_contradiction"


def test_area_contradiction_rejects_beyond_20pct() -> None:
    d = classify_pair(_key(1, hn=None, area=50.0), _key(2, hn=None, area=80.0))
    assert d.action == "reject"
    assert d.detail == "area_contradiction"


def test_house_number_contradiction_rejects() -> None:
    d = classify_pair(_key(1, hn="10", floor=None), _key(2, hn="12", floor=None))
    assert d.action == "reject"
    assert d.detail == "house_number_contradiction"


def test_unit_marker_contradiction_rejects_different_plots() -> None:
    # the real Kostelec development: pozemek č.4 vs č.3, identical otherwise
    a = _key(1, hn=None, description="dům 4+kk, pozemek č.4 o velikosti 479 m²")
    b = _key(2, hn=None, description="dům 4+kk, pozemek č.3 o velikosti 484 m²")
    d = classify_pair(a, b)
    assert d.action == "reject"
    assert d.detail == "unit_marker_contradiction"


def test_unit_marker_contradiction_house_and_flat_variants() -> None:
    assert classify_pair(
        _key(1, hn=None, description="dům 3A na klíč"),
        _key(2, hn=None, description="dům 5C na klíč"),
    ).detail == "unit_marker_contradiction"
    assert classify_pair(
        _key(1, hn=None, description="prodej byt 42 v centru"),
        _key(2, hn=None, description="prodej byt 45 v centru"),
    ).detail == "unit_marker_contradiction"


def test_same_unit_marker_does_not_block() -> None:
    # identical descriptions (same unit, or no unit token) → not a contradiction
    a = _key(1, hn=None, description="komerční nemovitost v centru, jednotka č. 5")
    b = _key(2, hn=None, description="komerční nemovitost v centru, jednotka č. 5")
    assert classify_pair(a, b).action == "candidate"
    # unit keyword present on only one side → no contradiction
    c = _key(3, hn=None, description="pozemek č.4")
    e = _key(4, hn=None, description="hezký dům u lesa")
    assert classify_pair(c, e).action == "candidate"


def test_street_mismatch_rejects() -> None:
    d = classify_pair(_key(1, street="id:1"), _key(2, street="id:2"))
    assert d.action == "reject"
    assert d.detail == "street_mismatch"


def test_disposition_mismatch_rejects() -> None:
    d = classify_pair(_key(1, disp="2+kk", hn=None), _key(2, disp="3+kk", hn=None))
    assert d.action == "reject"
    assert d.detail == "disposition_mismatch"


def test_already_same_property_rejects() -> None:
    d = classify_pair(_key(1, pid=99), _key(2, pid=99))
    assert d.action == "reject"
    assert d.detail == "already_merged"


def test_missing_floor_on_one_side_is_candidate_not_merge() -> None:
    # no exact-address merge without floor on both; not a contradiction either
    d = classify_pair(_key(1, floor=None), _key(2, floor=3))
    assert d.action == "candidate"


# --- rule D helpers ---------------------------------------------------------

def test_phash_fastpath_needs_two_identical_pairs() -> None:
    assert not decide_phash_fastpath(0)
    assert not decide_phash_fastpath(1)
    assert decide_phash_fastpath(2)
    assert decide_phash_fastpath(5)


def test_rooms_in_priority_orders_and_filters() -> None:
    common = {"bedroom", "kitchen", "bathroom", "floor_plan"}
    # floor_plan is not in ROOM_PRIORITY -> excluded; kitchen before bathroom before bedroom
    assert rooms_in_priority(common) == ["kitchen", "bathroom", "bedroom"]


def test_verdict_gate_high_only() -> None:
    assert verdict_is_merge("High")
    assert not verdict_is_merge("Medium")
    assert not verdict_is_merge("Low")
    assert not verdict_is_merge(None)


# --- orchestration: run_engine against a fake conn --------------------------

class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append(s)
        if "count(*) FILTER" in s and "FROM listings WHERE is_active" in s:
            self._rows = [(4, 100, 5)]  # eligible, flagged_location, flagged_disposition
        elif "FROM listings l" in s and "l.street IS NOT NULL" in s:
            self._rows = list(self._conn.eligible_rows)
        elif "INSERT INTO property_identity_candidates" in s:
            self._conn.enqueued.append(params)
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, eligible_rows: list[tuple[Any, ...]]) -> None:
        self.eligible_rows = eligible_rows
        self.executed: list[str] = []
        self.enqueued: list[Any] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def _row(sid: int, pid: int, *, street_id: int = 42, disp: str = "2+kk",
         hn: str | None = "10", floor: int | None = 3, area: float | None = 60.0,
         source: str = "sreality", description: str | None = None) -> tuple[Any, ...]:
    # matches _ELIGIBLE_SQL column order:
    # sreality_id, property_id, source, street, street_id, disposition,
    # house_number, floor, area_m2, description
    return (sid, pid, source, "Nádražní", street_id, disp, hn, floor, area, description)


def test_run_engine_exact_address_merges(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    merges: list[tuple[int, int, str]] = []

    def fake_merge(conn: Any, *, survivor_id: int, retired_id: int, reason: str, **kw: Any) -> dict:
        merges.append((survivor_id, retired_id, reason))
        return {"data": {}}

    monkeypatch.setattr(eng, "merge_properties", fake_merge)

    conn = _FakeConn([_row(1, 101), _row(2, 102)])  # same street/no/disp/floor/area
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert stats["auto_address"] == 1
    assert merges == [(101, 102, "address_exact")]
    assert stats["auto_phash"] == 0 and stats["auto_visual"] == 0


def test_run_engine_phash_fastpath_merges(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {}},
    )
    # >=2 identical interior pairs
    monkeypatch.setattr(eng, "_phash_interior_identical_pairs", lambda *a, **k: 3)

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "kitchen"},
            {"image_id": sid * 10 + 2, "room_type": "bathroom"},
        ]}}

    # No house number -> candidate (not exact-address), so it goes to visual.
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, classify_fn=classify, compare_fn=lambda *a, **k: None,
                           max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert merges == ["image_phash"]


def test_run_engine_visual_high_merges_low_queues(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {}},
    )
    monkeypatch.setattr(eng, "_phash_interior_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    # First pair: kitchen -> High. (single candidate pair)
    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list) -> dict:
        return {"verdict": "High", "rationale": "matching tiles"}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, classify_fn=classify, compare_fn=compare, max_vision_calls=10)
    assert stats["auto_visual"] == 1
    assert merges == ["visual_match"]
    assert stats["vision_calls"] == 1

    # Low verdict -> queue, no merge.
    merges.clear()
    conn2 = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats2 = eng.run_engine(
        conn2, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": "different windows"},
        max_vision_calls=10,
    )
    assert stats2["auto_visual"] == 0
    assert stats2["queued"] == 1
    assert merges == []


def test_run_engine_rejects_floor_contradiction(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {}})

    conn = _FakeConn([_row(1, 101, floor=2), _row(2, 102, floor=8)])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)
    assert stats["rejected"] == 1
    assert stats["auto_address"] == 0 and stats["queued"] == 0
