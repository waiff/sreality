"""Tests for /new-dedup/tagging-bakeoff/* — the tagging bake-off's read surface.

Admin-gated (require_admin); the happy-path tests override it, and one test does
not, to prove the gate is actually on the router. The toolkit module is
monkeypatched, so what is under test here is the route layer: filters reaching
the reader intact, 404 on an unknown run, 422 on a filter combination that has no
meaning, and the response envelope the frontend codes against.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

PREFIX = "/new-dedup/tagging-bakeoff"


class _FakeConn:
    """Not a SQL fake — the toolkit functions are monkeypatched wholesale."""


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    monkeypatch.setattr(bo, "get_run", lambda conn, *, run_id: (
        {"id": run_id, "label": "set-1", "min_train_positives": 100}
        if run_id == 3 else None))
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


ARM = bo.Arm(
    id=7, run_id=3, arm="dinov3-b16@768/bf16", dim=768, status="ok", note=None,
    encoder=th.EncoderIdentity(
        model="facebook/dinov3-vitb16", revision="5931719e", library="transformers",
        pooling="cls", resolution=768, preprocessing="squash", dtype="bfloat16"),
)


def test_runs_carry_their_arms(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bo, "list_runs", lambda conn, *, limit: [
        {"id": 3, "created_at": "2026-09-08T12:00:00+00:00", "label": "set-1",
         "note": None, "status": "ok", "manifest_key": None,
         "heads": [11, 12], "min_train_positives": 100}])
    monkeypatch.setattr(bo, "list_arms", lambda conn, *, run_id: [ARM])

    body = client.get(f"{PREFIX}/runs").json()["data"]
    assert body[0]["heads"] == [11, 12]
    arm = body[0]["arms"][0]
    # The seven identity facts travel flattened onto the arm — the page renders
    # them beside the short name, so "which encoder was this" needs no join.
    assert arm["arm"] == "dinov3-b16@768/bf16" and arm["dim"] == 768
    for field in th.ENCODER_FIELDS:
        assert field in arm


def test_metrics_returns_the_whole_table_in_one_payload(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"arm_id": 7, "arm": "a", "mode": "pos_neg", "tag_id": 11,
             "tag_label": "kuchyně", "cv_precision": 0.94, "exam_precision": None,
             "exam_graded_n": 0, "status": "ok"}]
    monkeypatch.setattr(bo, "run_metrics", lambda conn, *, run_id: rows)
    body = client.get(f"{PREFIX}/runs/3/metrics").json()["data"]
    assert body == rows
    # A null rate is a null rate all the way to the wire: "nothing was proposed"
    # must not arrive at the page as 0.
    assert body[0]["exam_precision"] is None


def test_unknown_run_is_404_on_every_route(client) -> None:
    assert client.get(f"{PREFIX}/runs/99/metrics").status_code == 404
    assert client.get(f"{PREFIX}/runs/99/images").status_code == 404
    assert client.get(
        f"{PREFIX}/runs/99/buckets?arm_id=7&mode=pos_neg&tag_id=11").status_code == 404


def test_images_passes_every_filter_through(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"images": [], "next_after_image_id": None}

    monkeypatch.setattr(bo, "run_images", fake)
    resp = client.get(f"{PREFIX}/runs/3/images?split=exam&arms=7,8&mode=pos_neg"
                      "&tag_id=11&outcome=fp&after_image_id=41&limit=10")
    assert resp.status_code == 200
    assert seen == {"run_id": 3, "split": "exam", "arm_ids": [7, 8],
                    "mode": "pos_neg", "tag_id": 11, "outcome": "fp",
                    "after_image_id": 41, "limit": 10}


def test_images_rejects_a_bad_arm_list_and_an_unknown_split(client) -> None:
    assert client.get(f"{PREFIX}/runs/3/images?arms=seven").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/images?split=holdout").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/images?outcome=maybe").status_code == 422


def test_images_surfaces_the_readers_own_refusal_as_422(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(conn: Any, **kw: Any) -> dict[str, Any]:
        raise ValueError("outcome needs a tag_id — it is one head's verdict")

    monkeypatch.setattr(bo, "run_images", boom)
    resp = client.get(f"{PREFIX}/runs/3/images?outcome=fp")
    assert resp.status_code == 422 and "tag_id" in resp.json()["detail"]


def test_images_envelope_carries_the_storage_path_and_the_cursor(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bo, "run_images", lambda conn, **kw: {
        "images": [{"image_id": 41, "listing_id": 900, "storage_path": "a.jpg",
                    "scores": [{"arm_id": 7, "arm": "a", "mode": "pos_neg",
                                "tag_id": 11, "tag_label": "k", "split": "cv",
                                "fold": 2, "label": 1, "score": 0.9,
                                "predicted": True, "outcome": "tp"}]}],
        "next_after_image_id": 41})
    data = client.get(f"{PREFIX}/runs/3/images").json()["data"]
    assert data["images"][0]["storage_path"] == "a.jpg"
    assert data["next_after_image_id"] == 41


def test_buckets_passes_its_keys_through_and_needs_them(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"buckets": {}, "histogram": {}}

    monkeypatch.setattr(bo, "run_buckets", fake)
    ok = client.get(f"{PREFIX}/runs/3/buckets?arm_id=7&mode=pos_neg&tag_id=11"
                    "&split=exam&limit=8&offset=16")
    assert ok.status_code == 200
    assert seen == {"arm_id": 7, "mode": "pos_neg", "tag_id": 11, "split": "exam",
                    "limit": 8, "offset": 16}
    # An outcome bucket is one head under one arm and mode; none of the three is
    # optional, so a call missing one is a 422, not a silent aggregate.
    assert client.get(f"{PREFIX}/runs/3/buckets?arm_id=7").status_code == 422


def test_every_bakeoff_route_is_admin_gated() -> None:
    # Recent FastAPI wraps an included router rather than splicing its routes
    # into app.routes, so the walk has to descend — the same shape
    # tests/api/test_admin_route_coverage.py::_collect uses.
    from tests.api.test_admin_route_coverage import _collect

    routes = [r for r in _collect(api_main.app.routes)
              if r.path.startswith(PREFIX)]
    assert len(routes) == 6, [r.path for r in routes]
    for route in routes:
        names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        nested = {getattr(sub.call, "__name__", "")
                  for d in route.dependant.dependencies for sub in d.dependencies}
        assert "require_admin" in (names | nested), route.path


def test_the_gate_actually_rejects_without_an_admin_session() -> None:
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    try:
        resp = TestClient(api_main.app).get(f"{PREFIX}/runs")
        assert resp.status_code in (401, 403)
    finally:
        api_main.app.dependency_overrides.clear()


# --- views C and D ---------------------------------------------------------
#
# Views A and B delegate to a monkeypatched toolkit, so their tests only prove
# the route layer. C and D run their SQL on the connection themselves, so these
# use a SCRIPTED cursor instead: it answers by matching a fragment of the query,
# which keeps the fixtures readable and still pins WHICH statement ran with WHICH
# parameters — the cursor and the keys reaching the database intact are the whole
# contract here.


class _ScriptedCursor:
    def __init__(self, script: list[tuple[str, list[Any]]]) -> None:
        self._script = script
        self._rows: list[Any] = []
        self.seen: list[tuple[str, Any]] = []

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.seen.append((sql, params))
        self._rows = next((rows for marker, rows in self._script if marker in sql), [])

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _ScriptedConn:
    def __init__(self, script: list[tuple[str, list[Any]]]) -> None:
        self.cur = _ScriptedCursor(script)

    def cursor(self) -> _ScriptedCursor:
        return self.cur

    def params_for(self, marker: str) -> Any:
        return next(p for sql, p in self.cur.seen if marker in sql)


def _scripted(script: list[tuple[str, list[Any]]]) -> _ScriptedConn:
    conn = _ScriptedConn(script)
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    return conn


# (image_id, listing_id, storage_path, score, label, predicted, fold, outcome)
SCORE_ROWS = [
    (555, 99213, "a.jpg", 0.9713, 1, True, 2, "tp"),
    (556, None, "b.jpg", 0.8800, 0, True, 1, "fp"),
    (557, 99213, "c.jpg", 0.4412, None, False, None, "abstained"),
]


def test_scores_ranks_the_whole_cell_and_pages_by_offset(client) -> None:
    conn = _scripted([("count(*)", [(9264,)]), ("ORDER BY s.score DESC", SCORE_ROWS)])
    data = client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                      "&split=cv&limit=3").json()["data"]

    assert [r["image_id"] for r in data["rows"]] == [555, 556, 557]
    # The cell's whole size, not this page's — the operator needs to know how
    # deep the ranking goes before deciding to walk it — and the page's own
    # window, echoed back so "x–y of N" is drawn from what was actually served.
    assert data["total"] == 9264
    assert data["limit"] == 3 and data["offset"] == 0
    # An abstention keeps its real score and prediction and is in the list —
    # this view is the union of the buckets PLUS the rows that are in none.
    assert data["rows"][2]["label"] is None
    assert data["rows"][2]["outcome"] == "abstained"
    params = conn.params_for("ORDER BY s.score DESC")
    assert params["arm_id"] == 7 and params["limit"] == 3 and params["offset"] == 0


def test_scores_offset_is_a_position_in_a_total_order(client) -> None:
    # An offset is only a stable position if the ORDER BY is total; scores tie,
    # so the query must break ties on image_id and page with OFFSET, never a
    # cursor the client has to carry.
    conn = _scripted([("count(*)", [(9264,)]), ("ORDER BY s.score DESC", [])])
    data = client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                      "&limit=500&offset=9000").json()["data"]
    sql, params = next(
        (s, p) for s, p in conn.cur.seen if "ORDER BY s.score DESC" in s)
    assert "ORDER BY s.score DESC, s.image_id DESC" in sql
    assert "OFFSET %(offset)s" in sql
    assert params["limit"] == 500 and params["offset"] == 9000
    assert data["offset"] == 9000 and data["rows"] == []


def test_scores_page_size_matches_the_training_set_grid(client) -> None:
    # The training-set page offers 50 … 10,000 a page; the ranking must accept
    # the same widest page, and refuse a negative offset or an empty page.
    _scripted([("count(*)", [(0,)]), ("ORDER BY s.score DESC", [])])
    ok = client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                    "&limit=10000")
    assert ok.status_code == 200
    assert client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                      "&limit=10001").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                      "&offset=-1").status_code == 422


def test_scores_needs_its_cell_keys_and_a_known_run_and_split(client) -> None:
    _scripted([("count(*)", [(0,)])])
    # A ranking is ONE head under ONE arm and mode — none of the three optional.
    assert client.get(f"{PREFIX}/runs/3/scores?arm_id=7").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/scores?arm_id=7&mode=pos_neg&tag_id=11"
                      "&split=holdout").status_code == 422
    assert client.get(f"{PREFIX}/runs/99/scores?arm_id=7&mode=pos_neg"
                      "&tag_id=11").status_code == 404


# (arm_id, arm, resolution, mode, tag_id, tag_label, split, fold, label, score,
#  predicted, outcome)
DETAIL_ROWS = [
    (7, "dinov3-b16@768/bf16", 768, "pos_neg", 11, "kuchyně", "cv", 2, 1, 0.97, True, "tp"),
    (7, "dinov3-b16@768/bf16", 768, "pos_neg", 12, "koupelna", "cv", 2, 0, 0.11, False, "tn"),
    (8, "clip-l14@224/fp16", 224, "pos_neg", 11, "kuchyně", "cv", 1, 1, 0.42, False, "fn"),
]


def test_image_detail_returns_every_head_on_every_arm_with_the_photo(client) -> None:
    conn = _scripted([("a.resolution", DETAIL_ROWS),
                      ("FROM images i", [(555, 99213, "a.jpg")])])
    data = client.get(f"{PREFIX}/runs/3/images/555").json()["data"]

    assert data["image_id"] == 555 and data["listing_id"] == 99213
    assert data["storage_path"] == "a.jpg"
    assert len(data["scores"]) == 3
    # The raw probability per head per arm — what the winner-takes-all reading
    # needs. The arm's resolution rides along so the page can tell a retired
    # low-resolution arm from a live one without a second lookup.
    assert data["scores"][0]["score"] == 0.97
    assert data["scores"][0]["tag_label"] == "kuchyně"
    assert data["scores"][2]["arm_id"] == 8 and data["scores"][2]["resolution"] == 224
    assert conn.params_for("a.resolution") == {"run_id": 3, "image_id": 555}


def test_image_detail_is_404_when_this_run_never_scored_that_photo(client) -> None:
    # Not an empty list: "this run never saw the photo" and "every head said
    # nothing about it" are different answers, and only one of them is true.
    _scripted([("a.resolution", []), ("FROM images i", [(555, None, "a.jpg")])])
    resp = client.get(f"{PREFIX}/runs/3/images/555")
    assert resp.status_code == 404 and "scored no image" in resp.json()["detail"]


def test_image_detail_rejects_an_unknown_run(client) -> None:
    _scripted([("a.resolution", DETAIL_ROWS)])
    assert client.get(f"{PREFIX}/runs/99/images/555").status_code == 404
