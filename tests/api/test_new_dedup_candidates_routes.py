"""Tests for /new-dedup/candidates/* — what the Candidate audit page reads.

The routes hold no SQL of their own: they call five `toolkit.dedup_candidates` helpers and
shape the answer. So the helpers are monkeypatched wholesale here and the assertions are
about the CONTRACT — the `{"data": ...}` envelope, the path registry (a column for the
unbuilt paths A and B from day one), the stats passed through untouched, the 404 on a run
that does not exist, and above all that an un-migrated database renders instead of 500ing.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from toolkit import dedup_candidates as dc


class _FakeConn:
    """The routes only pass the connection to the (patched) toolkit helpers."""


def _generation(**over: Any) -> dc.Generation:
    base: dict[str, Any] = {
        "id": 77,
        "simulation_run_id": 11,
        "inputs_id": 5,
        "path": "C",
        "fingerprint": "abc123",
        "inputs": {"path": "C", "l0_candidate_scope": "all", "l0_floor_tolerance": 2},
        "status": "success",
        "progress": {"blocks_total": 6254, "blocks_done": 6254},
        "stats": None,
    }
    base.update(over)
    return dc.Generation(**base)


# A generation's `stats` exactly as scripts/dedup_candidates_generate.py:generation_stats
# writes it (plus the keys `generate()` adds afterwards). The route passes this through
# untouched — that pass-through is what the shape assertion below pins.
FULL_STATS: dict[str, Any] = {
    "matrix": [
        {
            "rung": "C1",
            "category_main_lo": "byt",
            "category_main_hi": "byt",
            "category_type": "prodej",
            "pairs": 412_000,
            "floor_checked": 301_500,
        },
        {
            "rung": "C3",
            "category_main_lo": "pozemek",
            "category_main_hi": "pozemek",
            "category_type": "prodej",
            "pairs": 9_100,
            "floor_checked": 0,
        },
    ],
    "pairs": {"C1": 412_000, "C3": 9_100, "total": 421_100},
    "listings_with_candidates": [
        {"category_main": "byt", "listings": 31_402},
        {"category_main": "pozemek", "listings": 2_180},
    ],
    "towns_with_pairs": 4_912,
    "top_towns": [
        {"block_key": "554782", "C1": 190_000, "C3": 4_000, "obec_name": "Praha"},
        {"block_key": "582786", "C1": 41_000, "C3": 900, "obec_name": "Brno"},
    ],
    "distribution": [
        {"pairs_from": 1, "pairs_to": 10, "towns": 3_100},
        {"pairs_from": 11, "pairs_to": None, "towns": 1_812},
    ],
    "funnel": [
        {
            "source": "sreality",
            "category_main": "byt",
            "category_type": "prodej",
            "listings": 120_000,
            "active": 30_000,
            "with_projection": 119_400,
            "with_town": 118_900,
            "with_disposition": 117_000,
            "with_area": 119_000,
            "byt": 120_000,
            "byt_with_floor": 88_000,
            "c1_eligible": 116_800,
            "c3_eligible": 1_900,
            "town_no_attribute": 200,
        }
    ],
    "top_buckets": [
        {
            "obec_kod": "554782",
            "obec_name": "Praha",
            "disposition": "2+kk",
            "listings": 16_000,
            "active": 4_100,
        }
    ],
    "town_assignment": [{"method": "projection", "listings": 512_000}],
    "partial": False,
    "only": [],
    "stale_deleted": 1_204,
    "chunks_done": 8_310,
    "pairs_upserted": 421_100,
    "seconds": 5_412.7,
    "scope": "all",
}

RECENT_ROWS: list[dict[str, Any]] = [
    {
        "id": 77,
        "status": "success",
        "created_at": "2026-09-10T08:00:00+00:00",
        "completed_at": "2026-09-10T09:30:12+00:00",
        "fingerprint": "abc123",
        "scope": "all",
        "partial": False,
        "pairs_total": 421_100,
    },
    {
        "id": 76,
        "status": "failed",
        "created_at": "2026-09-09T08:00:00+00:00",
        "completed_at": "2026-09-09T08:04:00+00:00",
        "fingerprint": "def456",
        "scope": "active",
        "partial": True,
        "pairs_total": None,
    },
]

TIMES: dict[str, Any] = {
    "created_at": "2026-09-10T08:00:00+00:00",
    "started_at": "2026-09-10T08:00:03+00:00",
    "completed_at": "2026-09-10T09:30:12+00:00",
    "error_message": None,
}


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: _FakeConn()
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def store(monkeypatch):
    """Patch the five toolkit reads and record what the route asked for."""

    calls: dict[str, Any] = {"ready": 0, "get": [], "latest": [], "times": [], "recent": []}
    state: dict[str, Any] = {"ready": True, "generation": None, "recent": []}

    def store_ready(conn: Any) -> bool:
        calls["ready"] += 1
        return bool(state["ready"])

    def get_generation(conn: Any, generation_id: int) -> dc.Generation | None:
        calls["get"].append(generation_id)
        gen = state["generation"]
        return gen if gen is not None and gen.id == generation_id else None

    def latest_generation(conn: Any, code: str, **kw: Any) -> dc.Generation | None:
        calls["latest"].append((code, kw))
        return state["generation"]

    def generation_timestamps(conn: Any, generation_id: int) -> dict[str, Any]:
        calls["times"].append(generation_id)
        return dict(TIMES)

    def list_recent_generations(conn: Any, code: str, limit: int = 20) -> list[dict[str, Any]]:
        calls["recent"].append((code, limit))
        return list(state["recent"])

    monkeypatch.setattr(dc, "store_ready", store_ready)
    monkeypatch.setattr(dc, "get_generation", get_generation)
    monkeypatch.setattr(dc, "latest_generation", latest_generation)
    monkeypatch.setattr(dc, "generation_timestamps", generation_timestamps)
    monkeypatch.setattr(dc, "list_recent_generations", list_recent_generations)
    return {"calls": calls, "state": state}


# --------------------------------------------------------------- the un-migrated database


def test_overview_renders_when_the_store_does_not_exist(client, store):
    """Migration 492 not applied: 200 with `store_ready: false`, never a 500."""
    store["state"]["ready"] = False
    resp = client.get("/new-dedup/candidates/overview")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["store_ready"] is False
    assert (data["generation"], data["stats"], data["recent"]) == (None, None, [])
    # The registry is code, not data — it renders with no store at all.
    assert [p["code"] for p in data["paths"]] == list(dc.PATH_CODES)


def test_store_ready_is_probed_before_any_other_query(client, store, monkeypatch):
    """The whole no-500 guarantee rests on the catalog probe going FIRST."""
    store["state"]["ready"] = False

    def _boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("queried the store before the readiness probe")

    for name in (
        "get_generation",
        "latest_generation",
        "generation_timestamps",
        "list_recent_generations",
    ):
        monkeypatch.setattr(dc, name, _boom)

    assert client.get("/new-dedup/candidates/overview").status_code == 200
    assert client.get("/new-dedup/candidates/generations").json() == {"data": []}
    assert store["calls"]["ready"] == 2


# --------------------------------------------------------------------- the path registry


def test_paths_carry_a_column_for_the_unbuilt_paths(client, store):
    """A and B have no `PathDef` yet; the audit shows them empty rather than omitting them."""
    data = client.get("/new-dedup/candidates/overview").json()["data"]
    paths = {p["code"]: p for p in data["paths"]}
    assert set(paths) == {"A", "B", "C"}
    for code in ("A", "B"):
        assert paths[code]["built"] is False
        assert paths[code]["rungs"] == []
        assert paths[code]["block_key"] is None
        assert paths[code]["explanation"].strip()
    c = paths["C"]
    assert c["built"] is True
    assert c["block_key"] == dc.PATHS["C"].block_key
    assert [r["code"] for r in c["rungs"]] == ["C1", "C3"]
    assert c["rungs"][0]["needs"] == ["disposition"]


# ------------------------------------------------------------------- store ready, no run


def test_overview_with_the_store_ready_but_no_generation_yet(client, store):
    body = client.get("/new-dedup/candidates/overview").json()
    assert body["data"]["store_ready"] is True
    assert body["data"]["generation"] is None
    assert body["data"]["stats"] is None
    assert body["data"]["recent"] == []
    # No run to describe means no timestamp read.
    assert store["calls"]["times"] == []
    assert store["calls"]["latest"] == [("C", {"status": "success"})]


# ------------------------------------------------------------------- a full stats payload


def test_overview_returns_the_full_generation_and_stats_payload(client, store):
    store["state"]["generation"] = _generation(stats=FULL_STATS)
    store["state"]["recent"] = RECENT_ROWS

    body = client.get("/new-dedup/candidates/overview").json()
    assert set(body) == {"data"}
    data = body["data"]
    assert set(data) == {"store_ready", "paths", "generation", "stats", "recent"}
    assert data["store_ready"] is True
    assert data["generation"] == {
        "id": 77,
        "simulation_run_id": 11,
        "inputs_id": 5,
        "path": "C",
        "fingerprint": "abc123",
        "status": "success",
        "created_at": TIMES["created_at"],
        "started_at": TIMES["started_at"],
        "completed_at": TIMES["completed_at"],
        "inputs": {"path": "C", "l0_candidate_scope": "all", "l0_floor_tolerance": 2},
        "progress": {"blocks_total": 6254, "blocks_done": 6254},
        "error_message": None,
    }
    # The audit numbers reach the page byte-for-byte as the lane computed them.
    assert data["stats"] == FULL_STATS
    assert data["recent"] == RECENT_ROWS
    assert store["calls"]["recent"] == [("C", 20)]


def test_a_failed_run_carries_its_error_message(client, store, monkeypatch):
    """A failed run is reachable from the picker and explains itself: no stats, no
    completion time, the database's message."""
    store["state"]["generation"] = _generation(status="failed", stats=None)
    times = dict(TIMES, completed_at=None, error_message="statement timeout")
    monkeypatch.setattr(dc, "generation_timestamps", lambda conn, gid: times)

    data = client.get("/new-dedup/candidates/overview?generation_id=77").json()["data"]
    assert data["generation"]["status"] == "failed"
    assert data["generation"]["error_message"] == "statement timeout"
    assert data["generation"]["completed_at"] is None
    assert data["stats"] is None


# ----------------------------------------------------------------- picking a generation


def test_overview_shows_the_generation_asked_for_by_id(client, store):
    store["state"]["generation"] = _generation(id=77, stats=FULL_STATS)

    data = client.get("/new-dedup/candidates/overview?generation_id=77").json()["data"]
    assert data["generation"]["id"] == 77
    # Asked for by id => the "newest successful" query is not run at all.
    assert store["calls"]["get"] == [77]
    assert store["calls"]["latest"] == []


def test_overview_404s_on_a_generation_that_does_not_exist(client, store):
    store["state"]["generation"] = _generation(id=77)
    resp = client.get("/new-dedup/candidates/overview?generation_id=999")
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


# ------------------------------------------------------------------------- the picker


def test_generations_lists_the_recent_runs(client, store):
    store["state"]["recent"] = RECENT_ROWS
    assert client.get("/new-dedup/candidates/generations").json() == {"data": RECENT_ROWS}
    assert store["calls"]["recent"] == [("C", 20)]


def test_generations_is_empty_when_the_store_does_not_exist(client, store):
    store["state"]["ready"] = False
    assert client.get("/new-dedup/candidates/generations").json() == {"data": []}


# ---------------------------------------------------------------------------- the gate


def test_new_dedup_candidates_require_admin(client, store):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/new-dedup/candidates/overview").status_code == 401
    assert client.get("/new-dedup/candidates/generations").status_code == 401
