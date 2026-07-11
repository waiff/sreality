"""Tests for the dedup-vision batch SUBMIT lane (scripts.submit_dedup_batch).

The submit funnel reuses the engine's pure rules + SQL helpers; these tests
monkeypatch the I/O dependencies (eligible load, pHash, cache reads, request
builders) so the TRAVERSAL + COLLECTION logic is exercised without a DB, R2, or
LLM. The headline is the recall-guard golden test: the compare requests the lane
enqueues are exactly the priority-ordered common rooms the synchronous engine
would walk (stop-at-first-High), so the warm-cache replay is recall-identical.
"""

from __future__ import annotations

from typing import Any

import scripts.submit_dedup_batch as sub
from toolkit.dedup_engine import ListingKey, PairDecision, rooms_in_priority


# --- minimal fakes ----------------------------------------------------------

class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "INSERT INTO dedup_batches" in sql:
            self._row = (1,)
        else:
            self._row = None

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self._conn.inserted_requests.extend(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    def __init__(self) -> None:
        self.inserted_requests: list[tuple[Any, ...]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


class _FakeProvider:
    def build_batch_request_params(self, *, system: Any, messages: Any, tools: Any,
                                   model: str, max_tokens: int = 4096) -> dict[str, Any]:
        return {"model": model, "system": system, "messages": messages, "tools": tools}

    def submit_batch(self, items: list[Any]) -> str:
        return "batch_fake"


class _FakeLLM:
    _MODELS = {
        "llm_room_classify_model": "claude-haiku-4-5",
        "llm_visual_match_model": "claude-sonnet-4-5",
        "llm_site_plan_match_model": "claude-sonnet-4-5",
        "llm_floor_plan_match_model": "claude-sonnet-4-5",
    }

    def resolve_model(self, key: str) -> str:
        return self._MODELS[key]


def _key(sid: int, pid: int, *, source: str, street: str = "name:5001:hlavni",
         disp: str = "2+kk", hn: str | None = None, floor: int | None = 3,
         area: float | None = 60.0) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source, street_key=street,
        disposition=disp, house_number=hn, floor=floor, area_m2=area,
        description=None, category_type="prodej", category_main="byt", street_id=None,
    )


def _run(monkeypatch: Any, *, keys: list[ListingKey], classifications: dict[int, Any],
         site_plan_verdict: Any = None, visual_cached: Any = None,
         phash: int = 0, distinctive: bool = False,
         both_site_plan: bool = False, floor_plan: bool = False,
         floor_plan_cached: Any = None, render_ids: set[int] | None = None,
         in_flight: set[str] | None = None,
         max_requests: int = 100, max_room_attempts: int = 4,
         warm_rooms: int = 1, max_seconds: int = 0,
         lane: str = "street", geo_keys: list[ListingKey] | None = None,
         candidate_pids: set[int] | None = None, geo_classify: Any = None,
         clip_model: str | None = None,
         clip_rooms: dict[int, dict[str, list[int]]] | None = None) -> _FakeConn:
    """Drive collect() over `keys` with all I/O monkeypatched; return the conn so
    the test can inspect the enqueued dedup_batch_requests rows."""
    monkeypatch.setattr(sub, "_load_eligible", lambda conn, **k: list(keys))
    monkeypatch.setattr(sub, "_load_geo_eligible", lambda conn, **k: list(geo_keys or []))
    monkeypatch.setattr(sub, "_proposed_candidate_property_ids",
                        lambda conn, redecide_hours=None: set(candidate_pids or set()))
    monkeypatch.setattr(sub, "clip_room_grouping",
                        lambda conn, *, sreality_id, model: (clip_rooms or {}).get(sreality_id))
    monkeypatch.setattr(
        sub, "_phash_identical_pairs",
        lambda conn, a, b, excluded_tags=(), render_exclude_min=None: phash)
    monkeypatch.setattr(
        sub, "_phash_distinctive_match",
        lambda conn, a, b, rooms=(), render_exclude_min=None: distinctive)
    monkeypatch.setattr(sub, "_both_have_site_plan", lambda conn, a, b: both_site_plan)
    monkeypatch.setattr(
        sub, "_floor_plan_image_ids", lambda conn, sid: [sid] if floor_plan else [])
    monkeypatch.setattr(
        sub, "_high_render_image_ids", lambda conn, a, b, rmin: set(render_ids or set()))
    monkeypatch.setattr(
        sub, "cached_floor_plan_verdict",
        lambda conn, *, sreality_id_a, sreality_id_b, model: floor_plan_cached,
    )
    monkeypatch.setattr(
        sub, "build_floor_plan_request",
        lambda conn, llm, *, sreality_id_a, sreality_id_b, image_ids_a, image_ids_b: {
            "system": "s", "messages": [], "tools": [], "model": "claude-sonnet-4-5",
        },
    )
    monkeypatch.setattr(sub, "_in_flight_custom_ids", lambda conn: set(in_flight or set()))
    monkeypatch.setattr(
        sub, "cached_classification",
        lambda conn, *, sreality_id, model, n_images: classifications[sreality_id],
    )
    monkeypatch.setattr(
        sub, "cached_site_plan_verdict",
        lambda conn, *, sreality_id_a, sreality_id_b, model: site_plan_verdict,
    )
    monkeypatch.setattr(
        sub, "cached_visual_verdict",
        lambda conn, *, sreality_id_a, sreality_id_b, room_type, model: visual_cached,
    )
    monkeypatch.setattr(
        sub, "build_classify_request",
        lambda conn, llm, *, sreality_id, n_images: {
            "system": "s", "messages": [], "tools": [], "model": "claude-haiku-4-5",
            "image_ids": [sreality_id],
        },
    )
    monkeypatch.setattr(
        sub, "build_compare_request",
        lambda conn, llm, *, sreality_id_a, sreality_id_b, room_type, image_ids_a, image_ids_b: {
            "system": "s", "messages": [], "tools": [], "model": "claude-sonnet-4-5",
        },
    )
    monkeypatch.setattr(
        sub, "build_site_plan_request",
        lambda conn, llm, *, sreality_id_a, sreality_id_b, image_ids_a, image_ids_b: {
            "system": "s", "messages": [], "tools": [], "model": "claude-sonnet-4-5",
        },
    )

    conn = _FakeConn()
    submitter = sub._Submitter(conn, _FakeProvider(), max_requests=max_requests, dry_run=False)
    sub.collect(conn, _FakeLLM(), submitter, max_pairs=4000,
                max_room_attempts=max_room_attempts, n_images=12, warm_rooms=warm_rooms,
                max_seconds=max_seconds, lane=lane, geo_classify=geo_classify,
                clip_model=clip_model)
    submitter.flush()
    return conn


def _rows_by_kind(conn: _FakeConn) -> dict[str, list[tuple[Any, ...]]]:
    # row = (batch_id, custom_id, kind, model, a, b, room_type, image_ids)
    out: dict[str, list[tuple[Any, ...]]] = {}
    for r in conn.inserted_requests:
        out.setdefault(r[2], []).append(r)
    return out


# --- recall-guard golden test -----------------------------------------------

def _geo_key(sid: int, pid: int, *, source: str = "sreality",
             cat: str = "dum") -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source,
        street_key="geo:5001:50.1006:14.5374:dum|komercni:prodej",
        disposition="", house_number=None, floor=None, area_m2=800.0,
        description=None, category_type="prodej", category_main=cat, street_id=None,
    )


def _accept_all_geo(a: ListingKey, b: ListingKey) -> PairDecision:
    return PairDecision("candidate", None, "geo_stub")


def test_candidates_lane_warms_both_tiers(monkeypatch: Any) -> None:
    """lane='candidates' loads the proposed queue population from BOTH loaders
    (street + geo, restricted to the candidate property ids) and warms each pair:
    street pairs via classify_pair, geo pairs via the provided geo classifier."""
    rooms = {"kitchen": [11], "bathroom": [12]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        geo_keys=[_geo_key(3, 103), _geo_key(4, 104, source="idnes")],
        candidate_pids={101, 102, 103, 104},
        classifications={1: ("classified", rooms), 2: ("classified", rooms),
                         3: ("classified", rooms), 4: ("classified", rooms)},
        lane="candidates", geo_classify=_accept_all_geo,
    )
    by_kind = _rows_by_kind(conn)
    compared = {(r[4], r[5]) for r in by_kind.get("compare", [])}
    assert (1, 2) in compared    # street pair warmed
    assert (3, 4) in compared    # geo pair warmed via the geo classifier


def test_geo_pairs_skipped_without_geo_classifier(monkeypatch: Any) -> None:
    """A geo-keyed pair in a run with no geo classifier is out of scope — never judged by
    classify_pair (which reads street/disposition fields geo keys don't carry)."""
    rooms = {"kitchen": [11]}
    conn = _run(
        monkeypatch,
        keys=[],
        geo_keys=[_geo_key(3, 103), _geo_key(4, 104, source="idnes")],
        candidate_pids={103, 104},
        classifications={3: ("classified", rooms), 4: ("classified", rooms)},
        lane="candidates", geo_classify=None,
    )
    assert conn.inserted_requests == []


def test_clip_first_grouping_skips_llm_classify(monkeypatch: Any) -> None:
    """With clip_model set and CLIP tags present on both sides, the warm uses CLIP room
    grouping — no classify request is enqueued and the compare rooms are the CLIP rooms
    (the same grouping the prefer-CLIP engine replays with)."""
    clip_rooms = {
        1: {"kitchen": [11], "bathroom": [12]},
        2: {"kitchen": [21], "bathroom": [22]},
    }
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("need_classify", None), 2: ("need_classify", None)},
        clip_model="clip-m", clip_rooms=clip_rooms,
    )
    by_kind = _rows_by_kind(conn)
    assert "classify" not in by_kind          # CLIP grouping made classify unnecessary
    assert len(by_kind.get("compare", [])) >= 1


def test_clip_missing_side_falls_back_to_llm_classify(monkeypatch: Any) -> None:
    """A side with no CLIP tags falls back to the LLM classify cache — and enqueues the
    classify exactly as before (the engine falls back the same way)."""
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("need_classify", None), 2: ("need_classify", None)},
        clip_model="clip-m",
        clip_rooms={1: {"kitchen": [11]}},    # side 2 has no CLIP tags
    )
    by_kind = _rows_by_kind(conn)
    classify_targets = {r[4] for r in by_kind.get("classify", [])}
    assert classify_targets == {2}            # only the CLIP-less side buys a classify


def test_time_budget_stops_enqueuing_cleanly(monkeypatch: Any) -> None:
    """With the wall-clock budget already expired, collect() enqueues NOTHING and
    finalizes cleanly (timed_out stat) instead of being killed mid-submit by the
    workflow's timeout-minutes (which strands the run as 'cancelled' — 3 of 8 runs
    on 2026-07-10). Everything flushed before the deadline is still submitted."""
    rooms = {"kitchen": [11], "bathroom": [12]}
    clock = iter([0.0] + [10_000.0] * 50)  # deadline computed at 0; every check sees expiry
    monkeypatch.setattr(sub.time, "monotonic", lambda: next(clock))
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        max_seconds=60,
    )
    assert conn.inserted_requests == []  # nothing enqueued past the deadline


def test_enqueued_compare_rooms_match_engine_walk(monkeypatch: Any) -> None:
    """The compare requests enqueued for a both-classified cross-source pair are
    exactly rooms_in_priority(common)[:max_room_attempts] — the superset the
    synchronous engine could walk (it stops at the first High), so wherever the
    replay stops, that room's verdict is already warm. THIS is recall-identity."""
    rooms_a = {"kitchen": [11], "bathroom": [12], "living_room": [13], "bedroom": [14], "garden": [15]}
    rooms_b = {"kitchen": [21], "bathroom": [22], "living_room": [23], "bedroom": [24]}
    common = set(rooms_a) & set(rooms_b)

    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms_a), 2: ("classified", rooms_b)},
        warm_rooms=4,  # full-prefix mode: warm every room the replay could stop at
    )
    by_kind = _rows_by_kind(conn)
    enqueued_rooms = [r[6] for r in by_kind.get("compare", [])]

    assert enqueued_rooms == rooms_in_priority(common)[:4]
    assert "classify" not in by_kind  # both already classified
    assert "site_plan" not in by_kind
    # canonical pair on every row
    for r in by_kind["compare"]:
        assert (r[4], r[5]) == (1, 2)


def test_warm_rooms_default_warms_only_first_priority_room(monkeypatch: Any) -> None:
    """The default (warm_rooms=1) warms ONLY the first-priority room — the one the engine
    tries first and stops at on a merge. This drops the tail-room over-buy (rooms 2..N the
    stop-at-first-High replay usually never reaches) that made the all-rooms warmer wasteful.
    A pair needing a later room still resolves via the engine's synchronous fallback."""
    rooms = {"kitchen": [11], "bathroom": [12], "living_room": [13], "bedroom": [14]}
    common = set(rooms)
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        # warm_rooms defaults to 1
    )
    enqueued = [r[6] for r in _rows_by_kind(conn).get("compare", [])]
    assert enqueued == rooms_in_priority(common)[:1]  # exactly the first-priority room
    assert len(enqueued) == 1


def test_render_images_excluded_from_compare_warmup(monkeypatch: Any) -> None:
    # PARITY with _resolve_visual (migration 239): a high-render image must be dropped
    # from the warm-up compare too — the verdict cache is keyed (a,b,room,model) and
    # ignores the image set, so the warmed verdict must be over the SAME render-excluded
    # set the engine compares, or the engine replays a render-inflated High. Here the
    # kitchen is render-only on both sides -> the kitchen compare is NOT warmed; the
    # bathroom (real) still is.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={
            1: ("classified", {"kitchen": [11], "bathroom": [12]}),
            2: ("classified", {"kitchen": [21], "bathroom": [22]}),
        },
        render_ids={11, 21},  # both kitchens are renders
    )
    rooms = [r[6] for r in _rows_by_kind(conn).get("compare", [])]
    assert "kitchen" not in rooms
    assert "bathroom" in rooms


def test_room_attempts_cap_limits_enqueued_compares(monkeypatch: Any) -> None:
    rooms = {"kitchen": [1], "bathroom": [2], "living_room": [3], "bedroom": [4]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        max_room_attempts=2, warm_rooms=4,  # warm-rooms is clamped down to the engine cap
    )
    enqueued = [r[6] for r in _rows_by_kind(conn).get("compare", [])]
    assert enqueued == rooms_in_priority(set(rooms))[:2]


def test_already_cached_compare_is_not_reenqueued(monkeypatch: Any) -> None:
    rooms = {"kitchen": [1], "bathroom": [2]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        visual_cached="Low",  # every room already has a cached verdict
    )
    assert _rows_by_kind(conn) == {}  # nothing to enqueue


# --- wave 1: classify deferral ----------------------------------------------

def test_unclassified_side_enqueues_classify_and_defers_compare(monkeypatch: Any) -> None:
    rooms = {"kitchen": [1]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("need_classify", None)},
    )
    by_kind = _rows_by_kind(conn)
    assert [r[1] for r in by_kind.get("classify", [])] == ["cls-2"]
    assert "compare" not in by_kind  # deferred until classify ingests


def test_no_images_side_warms_nothing(monkeypatch: Any) -> None:
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("no_images", None)},
    )
    assert conn.inserted_requests == []  # replay queues 'no_images'; nothing to warm


# --- development guard -------------------------------------------------------

def test_both_site_plan_unknown_verdict_enqueues_site_plan_defers_compare(monkeypatch: Any) -> None:
    rooms = {"site_plan": [9], "kitchen": [1]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        both_site_plan=True,
        site_plan_verdict=None,
    )
    by_kind = _rows_by_kind(conn)
    assert [r[1] for r in by_kind.get("site_plan", [])] == ["spl-1-2"]
    assert "compare" not in by_kind  # deferred behind the development-guard verdict


def test_site_plan_different_unit_enqueues_no_compare(monkeypatch: Any) -> None:
    rooms = {"site_plan": [9], "kitchen": [1]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        both_site_plan=True,
        site_plan_verdict="different_unit",
    )
    assert conn.inserted_requests == []  # replay queues it; no compare needed


def test_site_plan_same_unit_still_enqueues_compare(monkeypatch: Any) -> None:
    rooms = {"site_plan": [9], "kitchen": [1]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        both_site_plan=True,
        site_plan_verdict="same_unit",
    )
    by_kind = _rows_by_kind(conn)
    assert [r[6] for r in by_kind.get("compare", [])] == ["kitchen"]
    assert "site_plan" not in by_kind  # verdict already known


# --- the free funnel: pHash / cross-source gate / rule B never warm ----------

def test_phash_fastpath_pair_is_not_warmed(monkeypatch: Any) -> None:
    # >=2 pHash matches (no site plan) -> replay merges for free; submit warms nothing.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
        phash=3,
    )
    assert conn.inserted_requests == []


def test_distinctive_single_phash_match_pair_is_not_warmed(monkeypatch: Any) -> None:
    # #5 parity: only 1 pHash match, but it is a kitchen/bathroom (distinctive) -> the
    # engine merges it, so the batch lane must also treat it as resolved (warm nothing).
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
        phash=1, distinctive=True,
    )
    assert conn.inserted_requests == []


def test_both_floor_plan_pair_warms_floor_plan_request(monkeypatch: Any) -> None:
    # #4: a both-floor-plan pair is warmed BEFORE the pHash skip (the engine's gate
    # needs the verdict even on a pHash merge), so a floor_plan request is enqueued.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
        phash=3, floor_plan=True,
    )
    by_kind = _rows_by_kind(conn)
    assert "floor_plan" in by_kind
    assert by_kind["floor_plan"][0][1] == "fpl-1-2"  # canonical custom_id


def test_cached_floor_plan_pair_is_not_rewarmed(monkeypatch: Any) -> None:
    # Already-cached floor-plan verdict -> no re-warm.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
        phash=3, floor_plan=True, floor_plan_cached="same_layout",
    )
    assert "floor_plan" not in _rows_by_kind(conn)


def test_same_source_candidate_is_now_warmed(monkeypatch: Any) -> None:
    # Wave 3 removed the cross-source gate, so the warmer now warms same-source pairs too
    # (the engine visually compares them — the warmer must pre-warm the same verdicts).
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="sreality")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
    )
    assert conn.inserted_requests != []  # no longer gated out


def test_exact_address_pair_is_warmed_as_candidate(monkeypatch: Any) -> None:
    # Rule B retired: an exact-address pair is now an ordinary candidate, so (when pHash doesn't
    # resolve it) the warmer DOES warm its visual compare — it is no longer skipped as a free
    # rule-B merge.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality", hn="10"), _key(2, 102, source="bazos", hn="10")],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
    )
    assert conn.inserted_requests != []


def test_rejected_pair_is_not_warmed(monkeypatch: Any) -> None:
    # floor gap of >=2 -> hard reject, never compared.
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality", floor=2), _key(2, 102, source="bazos", floor=8)],
        classifications={1: ("classified", {"kitchen": [1]}), 2: ("classified", {"kitchen": [2]})},
    )
    assert conn.inserted_requests == []


# --- in-flight guard --------------------------------------------------------

def test_in_flight_request_is_skipped(monkeypatch: Any) -> None:
    rooms = {"kitchen": [1], "bathroom": [2]}
    conn = _run(
        monkeypatch,
        keys=[_key(1, 101, source="sreality"), _key(2, 102, source="bazos")],
        classifications={1: ("classified", rooms), 2: ("classified", rooms)},
        in_flight={"cmp-1-2-kitchen"}, warm_rooms=4,
    )
    enqueued = [r[6] for r in _rows_by_kind(conn).get("compare", [])]
    assert "kitchen" not in enqueued
    assert "bathroom" in enqueued


# --- submit retry (a transient 5xx must cost a backoff, never the window) -----

def _req(cid: str = "cmp-1-2-kitchen") -> "sub._Req":
    return sub._Req(custom_id=cid, kind="compare", model="claude-sonnet-4-5",
                    sreality_id_a=1, sreality_id_b=2, room_type="kitchen",
                    image_ids=None, params={"model": "claude-sonnet-4-5"})


def test_flush_retries_transient_5xx_then_succeeds(monkeypatch: Any) -> None:
    calls = {"n": 0}

    class _Flaky(_FakeProvider):
        def submit_batch(self, items: list[Any]) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Internal server error (500) — overloaded")
            return "batch_ok"

    monkeypatch.setattr(sub.time, "sleep", lambda s: None)
    conn = _FakeConn()
    s = sub._Submitter(conn, _Flaky(), max_requests=10, dry_run=False)
    s._chunk.append(_req())
    s._chunk_bytes = 10
    s.flush()
    assert calls["n"] == 2                       # one retry
    assert s.stats["batches"] == 1               # the chunk was submitted
    assert s.stats.get("submit_failures", 0) == 0
    assert conn.inserted_requests                # bookkeeping row written


def test_flush_drops_chunk_after_exhausted_retries(monkeypatch: Any) -> None:
    class _Dead(_FakeProvider):
        def submit_batch(self, items: list[Any]) -> str:
            raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(sub.time, "sleep", lambda s: None)
    conn = _FakeConn()
    s = sub._Submitter(conn, _Dead(), max_requests=10, dry_run=False)
    s._chunk.append(_req())
    s._chunk_bytes = 10
    s.flush()                                    # must NOT raise
    assert s.stats.get("batches", 0) == 0
    assert s.stats["submit_failures"] == 1
    assert not conn.inserted_requests            # nothing half-recorded
    assert s._chunk == []                        # chunk dropped, run continues


def test_flush_non_transient_error_drops_without_retry(monkeypatch: Any) -> None:
    calls = {"n": 0}

    class _AuthDead(_FakeProvider):
        def submit_batch(self, items: list[Any]) -> str:
            calls["n"] += 1
            raise RuntimeError("401 authentication_error: invalid x-api-key")

    slept = []
    monkeypatch.setattr(sub.time, "sleep", lambda s: slept.append(s))
    conn = _FakeConn()
    s = sub._Submitter(conn, _AuthDead(), max_requests=10, dry_run=False)
    s._chunk.append(_req())
    s._chunk_bytes = 10
    s.flush()
    assert calls["n"] == 1                       # no pointless retries on auth errors
    assert slept == []
    assert s.stats["submit_failures"] == 1
