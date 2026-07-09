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
from toolkit.dedup_engine import ListingKey, rooms_in_priority


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
         warm_rooms: int = 1) -> _FakeConn:
    """Drive collect() over `keys` with all I/O monkeypatched; return the conn so
    the test can inspect the enqueued dedup_batch_requests rows."""
    monkeypatch.setattr(sub, "_load_eligible", lambda conn: list(keys))
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
                max_room_attempts=max_room_attempts, n_images=12, warm_rooms=warm_rooms)
    submitter.flush()
    return conn


def _rows_by_kind(conn: _FakeConn) -> dict[str, list[tuple[Any, ...]]]:
    # row = (batch_id, custom_id, kind, model, a, b, room_type, image_ids)
    out: dict[str, list[tuple[Any, ...]]] = {}
    for r in conn.inserted_requests:
        out.setdefault(r[2], []).append(r)
    return out


# --- recall-guard golden test -----------------------------------------------

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
