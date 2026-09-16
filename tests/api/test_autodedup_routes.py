"""Tests for /autodedup/* — what the AUTODEDUP Progress page reads (PROGRAM.md §12, W1).

The routes hold no SQL of their own; `autodedup/progress_sql.py` does. So the connection is
faked and dispatches on the statement, and the assertions are about the CONTRACT: the
`{"data": …, "store_ready": …}` envelope, every `autodedup.iterations` column reaching the
page, keyset paging that reports `has_more` from a real extra row, Decimal/datetime coming
back as JSON, the header rollup summing the per-wave statement, and above all that an
un-migrated database renders instead of 500ing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import autodedup as routes
from autodedup import progress_sql as psql
from autodedup import ui_sql as usql


# Every W5 statement the routes may execute, mapped to the canned-row bucket it reads.
_LABELS: dict[str, str] = {
    usql.GROUPS_WEAKEST_SQL: "groups",
    usql.GROUPS_NEWEST_SQL: "groups_newest",
    usql.GROUPS_LARGEST_SQL: "groups_largest",
    usql.GROUP_ONE_SQL: "group_one",
    usql.GROUP_MEMBERS_SQL: "members",
    usql.EDGE_SUMMARY_SQL: "edges",
    usql.LISTING_IMAGES_SQL: "images",
    usql.LISTING_PHASHES_SQL: "phashes",
    usql.CLUSTER_PAIRS_SQL: "cluster_pairs",
    usql.PAIR_ONE_SQL: "pair_one",
    usql.JUDGEMENTS_LATEST_SQL: "judgements",
    usql.PAIR_VERDICTS_SQL: "pair_verdicts",
    usql.CLUSTER_VERDICTS_SQL: "cluster_verdicts",
    usql.CLUSTER_CONFLICTS_SQL: "conflicts",
    usql.RESIDUAL_SQL: "residual",
    usql.GROUPS_COUNT_SQL: "groups_count",
    usql.RESIDUAL_COUNT_SQL: "residual_count",
    usql.BLOCKS_SQL: "blocks",
    usql.LISTING_DETAIL_SQL: "listing_detail",
    usql.PAIR_ZONES_SQL: "zones",
    usql.CERTIFICATE_COUNTS_SQL: "certificates",
    usql.GENERATION_COUNTS_SQL: "generations",
    usql.VERDICT_COUNTS_SQL: "verdict_counts",
    usql.JUDGEMENT_COUNTS_SQL: "judgement_counts",
    usql.LAST_SCORE_RUN_SQL: "last_run",
    usql.VERDICT_PAIR_UPSERT_SQL: "verdict_write",
    usql.VERDICT_CLUSTER_UPSERT_SQL: "verdict_write",
    usql.MUST_NOT_LINK_UPSERT_SQL: "must_not_link",
    usql.MUST_NOT_LINK_RETRACT_SQL: "must_not_link_retract",
    usql.CLUSTER_EXISTS_SQL: "cluster_exists",
    usql.PAIR_EXISTS_SQL: "pair_exists",
}
# The statements that fetch one row over the asked-for page size.
_PAGED: frozenset[str] = frozenset({"groups", "groups_newest", "groups_largest", "residual"})


def _tuple(columns: tuple[str, ...], **values: Any) -> tuple[Any, ...]:
    """A canned row in the select list's order — named, so a column added to the tuple in the
    wrong slot is impossible rather than merely unlikely."""
    unknown = sorted(set(values) - set(columns))
    assert not unknown, f"not columns of this statement: {unknown}"
    return tuple(values.get(name) for name in columns)


def _iteration_row(iteration_id: int, wave: str = "W1") -> tuple[Any, ...]:
    """One row in `ITERATION_COLUMNS` order — the select list's contract."""
    return (
        iteration_id,
        wave,
        "blocking probe bake-off",
        "done",
        "ran K1 and K4 over the trial region and compared recall",
        ["psql", "autodedup.census"],
        {"cohort": "praha", "blocks": 412},
        {"recall": 0.91, "gate": 0.85},
        Decimal("12.3456"),
        {"report": "https://example.invalid/a.json"},
        998877,
        "nothing surprising",
        datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 16, 7, 59, tzinfo=timezone.utc),
    )


class _Cursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.calls.append((sql, params))
        if "to_regclass" in sql:
            self._rows = [(self._conn.ready,)]
        elif "per_wave" in sql:
            self._rows = list(self._conn.wave_rows)
        elif sql == psql.AUTODEDUP_ITERATIONS_SQL:
            if not self._conn.ready:
                raise AssertionError("queried the ledger before the readiness probe")
            after = (params or {}).get("after_id")
            limit = int((params or {}).get("limit") or 0)
            rows = [r for r in self._conn.iteration_rows if after is None or r[0] < after]
            self._rows = rows[:limit]
        else:
            if not self._conn.ready:
                raise AssertionError("queried the store before the readiness probe")
            # Dispatch on the CONSTANT, not on a substring of it: a statement the route
            # invented (or renamed) must fail the test rather than silently read another
            # endpoint's canned rows.
            label = _LABELS.get(sql)
            if label is None:
                raise AssertionError(f"unexpected statement: {' '.join(sql.split())[:90]}")
            rows = list(self._conn.canned.get(label, []))
            limit = (params or {}).get("limit")
            if label in _PAGED and limit is not None:
                rows = rows[: int(limit)]
            self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self) -> None:
        self.ready: bool = True
        self.iteration_rows: list[tuple[Any, ...]] = []
        self.wave_rows: list[tuple[Any, ...]] = []
        self.canned: dict[str, list[tuple[Any, ...]]] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def client(conn: _FakeConn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


# --------------------------------------------------------------- the un-migrated database


def test_iterations_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/iterations")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    # The catalog probe went first and nothing else was asked.
    assert len(conn.calls) == 1
    assert "to_regclass('autodedup.iterations')" in conn.calls[0][0]


def test_stats_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/stats")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    assert len(conn.calls) == 1


# ------------------------------------------------------------------------ the ledger page


def test_the_select_list_is_the_column_tuple(client, conn):
    """`ITERATION_COLUMNS` is what rows are zipped onto, and the SELECT list is what fills
    them: a column added to one and not the other would silently mislabel every column after
    it, and `zip` never raises. So the two are compared literally."""
    body = psql.AUTODEDUP_ITERATIONS_SQL
    head = body[body.index("SELECT ") + len("SELECT "): body.index(" FROM ")]
    assert tuple(name.strip() for name in head.split(",")) == psql.ITERATION_COLUMNS


def test_the_keyset_predicate_is_strictly_less_than(client, conn):
    """The fake cursor re-implements paging, so only the statement text proves the operator:
    `<=` here would hand back the previous page's last row on every boundary."""
    assert "id < %(after_id)s::bigint" in psql.AUTODEDUP_ITERATIONS_SQL
    assert "ORDER BY id DESC" in psql.AUTODEDUP_ITERATIONS_SQL


def test_every_iteration_column_reaches_the_page(client, conn):
    conn.iteration_rows = [_iteration_row(7)]
    body = client.get("/autodedup/iterations").json()
    assert set(body) == {"data", "store_ready"}
    assert body["store_ready"] is True
    item = body["data"]["items"][0]
    assert set(item) == set(psql.ITERATION_COLUMNS)
    assert item["id"] == 7
    assert item["wave"] == "W1"
    assert item["status"] == "done"
    assert item["tools"] == ["psql", "autodedup.census"]
    assert item["sample_stats"] == {"cohort": "praha", "blocks": 412}
    assert item["metrics"] == {"recall": 0.91, "gate": 0.85}
    assert item["run_id"] == 998877
    # numeric(10,4) and timestamptz both have to survive the wire
    assert item["cost_usd"] == pytest.approx(12.3456)
    assert item["started_at"] == "2026-09-16T08:00:00+00:00"
    assert item["finished_at"] == "2026-09-16T09:30:00+00:00"
    assert item["created_at"] == "2026-09-16T07:59:00+00:00"


def test_an_empty_ledger_is_an_empty_page_not_a_missing_store(client, conn):
    body = client.get("/autodedup/iterations").json()
    assert body["store_ready"] is True
    assert body["data"] == {"items": [], "has_more": False, "next_after_id": None}


def test_the_default_page_size_is_fifty_and_the_order_is_newest_first(client, conn):
    conn.iteration_rows = [_iteration_row(n) for n in range(200, 0, -1)]
    body = client.get("/autodedup/iterations").json()["data"]
    assert len(body["items"]) == 50
    assert [i["id"] for i in body["items"][:3]] == [200, 199, 198]
    # the extra probe row is asked for, never shown
    assert conn.calls[-1][1] == {"after_id": None, "limit": 51}


def test_keyset_paging_walks_the_ledger_without_repeating_a_row(client, conn):
    conn.iteration_rows = [_iteration_row(n) for n in range(5, 0, -1)]

    first = client.get("/autodedup/iterations", params={"limit": 2}).json()["data"]
    assert [i["id"] for i in first["items"]] == [5, 4]
    assert first["has_more"] is True
    assert first["next_after_id"] == 4

    second = client.get(
        "/autodedup/iterations", params={"limit": 2, "after": first["next_after_id"]}
    ).json()["data"]
    assert [i["id"] for i in second["items"]] == [3, 2]
    assert second["has_more"] is True

    last = client.get(
        "/autodedup/iterations", params={"limit": 2, "after": second["next_after_id"]}
    ).json()["data"]
    assert [i["id"] for i in last["items"]] == [1]
    assert last["has_more"] is False
    assert last["next_after_id"] is None


def test_a_page_that_exactly_empties_the_ledger_is_the_end(client, conn):
    """The honest `has_more`: a full page is not by itself another page."""
    conn.iteration_rows = [_iteration_row(n) for n in (2, 1)]
    body = client.get("/autodedup/iterations", params={"limit": 2}).json()["data"]
    assert len(body["items"]) == 2
    assert body["has_more"] is False
    assert body["next_after_id"] is None


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_the_page_size_is_bounded(client, conn, limit):
    assert client.get("/autodedup/iterations", params={"limit": limit}).status_code == 422


# ------------------------------------------------------------------------ the header strip


def test_stats_sums_the_per_wave_statement(client, conn):
    conn.wave_rows = [
        ("W1", 3, Decimal("12.5000"), datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), "running"),
        ("W0", 2, Decimal("7.2500"), datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc), "done"),
    ]
    body = client.get("/autodedup/stats").json()
    assert body["store_ready"] is True
    data = body["data"]
    assert set(data) == {
        "n_iterations",
        "total_cost_usd",
        "run_cap_usd",
        "program_cap_usd",
        "last_iteration_at",
        "waves",
        "engine",
    }
    assert data["n_iterations"] == 5
    assert data["total_cost_usd"] == pytest.approx(19.75)
    # D2's spend gate travels with the spend, so the page never retypes the caps.
    assert (data["run_cap_usd"], data["program_cap_usd"]) == (25.0, 200.0)
    assert data["last_iteration_at"] == "2026-09-16T09:00:00+00:00"
    assert data["waves"] == [
        {"wave": "W1", "n": 3, "last_status": "running", "cost_usd": 12.5},
        {"wave": "W0", "n": 2, "last_status": "done", "cost_usd": 7.25},
    ]


def test_the_headline_total_is_the_wave_column_added_up(client, conn):
    """The header must equal the wave rows added by hand — the route accumulates the
    numeric(10,4) as Decimal, though at 4-dp rounding no fixture can tell that from float
    addition; what this catches is a wave dropped from one of the two numbers."""
    conn.wave_rows = [
        ("W2", 1, Decimal("0.1000"), datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), "done"),
        ("W1", 1, Decimal("0.2000"), datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc), "done"),
        ("W0", 1, Decimal("0.4000"), datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc), "done"),
    ]
    data = client.get("/autodedup/stats").json()["data"]
    assert data["total_cost_usd"] == 0.7


EMPTY_ENGINE: dict[str, Any] = {
    "pairs_by_zone": {},
    "n_pairs": 0,
    "certificates": {},
    "generations": [],
    "latest_generation": None,
    "verdicts": [],
    "n_verdicts": 0,
    "judgements": [],
    "n_judgements": 0,
    "last_score_run": None,
}


def test_stats_on_an_empty_ledger(client, conn):
    data = client.get("/autodedup/stats").json()["data"]
    assert data == {
        "n_iterations": 0,
        "total_cost_usd": 0.0,
        "run_cap_usd": 25.0,
        "program_cap_usd": 200.0,
        "last_iteration_at": None,
        "waves": [],
        "engine": EMPTY_ENGINE,
    }


# ------------------------------------------------------------------------- the deployment


def test_the_image_ships_the_package_the_router_imports():
    """`api/routes/autodedup.py` imports `autodedup.progress_sql` at module scope, so the
    Dockerfile must COPY that tree: setuptools' `packages.find` matches an absent directory
    silently, so a missing COPY builds green and crash-loops the WHOLE API at boot."""
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    assert "COPY autodedup/ ./autodedup/" in dockerfile.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- the gate


def test_autodedup_routes_require_admin(client, conn):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/autodedup/iterations").status_code == 401
    assert client.get("/autodedup/stats").status_code == 401


def test_a_non_admin_jwt_is_refused(client, conn):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"app_metadata": {}}
    assert client.get("/autodedup/iterations").status_code == 403
    assert client.get("/autodedup/stats").status_code == 403


# ------------------------------------------------------------------- the missing relation


def test_a_relation_that_vanishes_between_probe_and_read_still_renders(client, conn, monkeypatch):
    """The probe can be beaten by a `drop schema autodedup cascade` — teardown is one
    statement in this program — so the read catches the SQLSTATE too."""
    pytest.importorskip("psycopg")
    from psycopg import errors as pg_errors

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise pg_errors.UndefinedTable("relation does not exist")

    monkeypatch.setattr(routes, "_fetch", _boom)
    assert client.get("/autodedup/iterations").json() == {"data": None, "store_ready": False}
    assert client.get("/autodedup/stats").json() == {"data": None, "store_ready": False}


# ==================================================== the validation UI (W5, PROGRAM.md §12)


def _cluster(cluster_key: int = 101, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "cluster_key": cluster_key,
        "generation": "g1",
        "size": 2,
        "block_key": 500123,
        "cat_group": "flat",
        "category_main": "byt",
        "category_type": "prodej",
        "area_min": Decimal("68.0"),
        "area_max": Decimal("68.5"),
        "sources": ["sreality", "bazos"],
        "medoid_listing_id": 11,
        "min_edge_score": 0.71,
        "mean_edge_score": 0.82,
        "n_judged_edges": 1,
        "n_certificate_edges": 1,
        "evidence_families": 33,  # IMG | ATTR
        "max_gap_days": 214,
        "shared_photo_warning": False,
        "status": "proposed",
        "model_version": "hand_v1",
        "feature_version": 1,
        "first_built_at": datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc),
        "last_changed_at": datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
        "verdict": None,
        "verdict_note": None,
        "verdict_decided_by": None,
        "verdict_decided_at": None,
    }
    values.update(over)
    return _tuple(usql.CLUSTER_COLUMNS, **values)


def _member(cluster_key: int, listing_id: int, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "cluster_key": cluster_key,
        "listing_id": listing_id,
        "source": "sreality",
        "source_url": f"https://www.sreality.cz/detail/{listing_id}",
        "category_main": "byt",
        "category_type": "prodej",
        "disposition": "3+kk",
        "area_m2": Decimal("68.0"),
        "floor": 3,
        "price_czk": 8_900_000,
        "first_seen_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "is_active": True,
        "cover_storage_path": f"listings/{listing_id}/1.jpg",
        "cover_sreality_url": "https://img.sreality.cz/1.jpg",
        "n_images": 12,
    }
    values.update(over)
    return _tuple(usql.MEMBER_COLUMNS, **values)


def _pair_row(lo: int = 11, hi: int = 12, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "listing_lo": lo,
        "listing_hi": hi,
        "probes": ["K1", "K5"],
        "families": 33,
        "features": {"area_rel_diff": [0.004, True], "dispo_equal": [1.0, True],
                     "phash_tight_matches": [6.0, True], "overlap_days": [0.0, False]},
        "score": 0.71,
        "zone": "band",
        "decision": "certificate:K-A:evidence_gate",
        "guard_veto": None,
        "cluster_key": 101,
        "feature_version": 1,
        "model_version": "hand_v1",
        "decided_at": datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc),
    }
    values.update(over)
    return _tuple(usql.PAIR_COLUMNS, **values)


def _residual_row(lo: int = 21, hi: int = 22, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "listing_lo": lo,
        "listing_hi": hi,
        "score": 0.44,
        "zone": "band",
        "decision": "model",
        "guard_veto": None,
        "families": 5,  # ATTR | TXT
        "probes": ["K1"],
        "features": {"area_rel_diff": [0.01, True], "dispo_equal": [1.0, True],
                     "rare_token_overlap": [3.0, True]},
        "feature_version": 1,
        "model_version": "hand_v1",
        "decided_at": datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc),
        "block_key": 500123,
        "judge_verdict": "same_property",
        "judge_confidence": 0.81,
        "judge_tier": "text",
        "verdict": None,
        "verdict_note": None,
        "verdict_decided_by": None,
        "verdict_decided_at": None,
    }
    for side, source in (("a_", "sreality"), ("b_", "bazos")):
        values.update(
            {
                f"{side}source": source,
                f"{side}source_url": f"https://{source}.cz/x",
                f"{side}category_main": "byt",
                f"{side}category_type": "prodej",
                f"{side}disposition": "2+kk",
                f"{side}area_m2": Decimal("52.0"),
                f"{side}floor": 2,
                f"{side}total_floors": 5,
                f"{side}price_czk": 5_500_000,
                f"{side}first_seen_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                f"{side}last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                f"{side}is_active": True,
                f"{side}cover_storage_path": f"listings/{side}cover.jpg",
                f"{side}cover_sreality_url": f"https://img.example.invalid/{side}cover.jpg",
                f"{side}n_images": 8,
            }
        )
    values.update(over)
    return _tuple(usql.RESIDUAL_COLUMNS, **values)


def _image(listing_id: int, image_id: int, sequence: int, phash: int | None) -> tuple[Any, ...]:
    """A gallery frame. `phash` arrives as TEXT — the statement casts it, because a 64-bit
    dHash is not a safe JSON number. `sreality_url` is NOT NULL in `images` (migration 001),
    so a frame always has a CDN url to fall back on when R2 has no copy yet; the SPA's
    `imageSrc` relies on exactly that, which is why no fixture here fakes it away."""
    return _tuple(
        usql.IMAGE_COLUMNS,
        listing_id=listing_id,
        image_id=image_id,
        sequence=sequence,
        storage_path=f"listings/{listing_id}/{sequence}.jpg",
        sreality_url=f"https://img.example.invalid/{listing_id}/{sequence}.jpg",
        phash=None if phash is None else str(phash),
    )


def _phash(listing_id: int, image_id: int, phash: int) -> tuple[Any, ...]:
    """A row of the UNCAPPED distance basis — every hashed frame, gallery cap or not."""
    return _tuple(
        usql.LISTING_PHASH_COLUMNS,
        listing_id=listing_id,
        image_id=image_id,
        phash=phash,
    )


def _listing_detail(listing_id: int, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "id": listing_id,
        "source": "sreality",
        "source_id_native": "4242",
        "source_url": f"https://www.sreality.cz/detail/{listing_id}",
        "category_main": "byt",
        "category_type": "prodej",
        "subtype": "3+kk",
        "disposition": "3+kk",
        "area_m2": Decimal("68.0"),
        "floor": 3,
        "total_floors": 6,
        "price_czk": 8_900_000,
        "price_unit": "celkem",
        "condition": "velmi dobry",
        "published_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "description": "Kontaktujte Jana Novakova na 777 123 456 nebo jan.novak@example.cz",
        "first_seen_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "inactive_at": None,
        "is_active": True,
    }
    values.update(over)
    return _tuple(usql.LISTING_DETAIL_COLUMNS, **values)


def _last_call(conn: _FakeConn, sql: str) -> dict[str, Any]:
    """The params the route sent with one statement — the filter contract, asserted directly."""
    for text, params in reversed(conn.calls):
        if text == sql:
            return params or {}
    raise AssertionError("statement was never executed")


# ------------------------------------------------------------------------- proposed groups


def test_groups_renders_a_cluster_with_members_and_edge_evidence(client, conn):
    conn.canned = {
        "groups": [_cluster()],
        "members": [_member(101, 11), _member(101, 12, source="bazos", price_czk=8_700_000)],
        "edges": [
            _tuple(
                usql.EDGE_SUMMARY_COLUMNS,
                cluster_key=101,
                n_edges=1,
                min_score=0.71,
                mean_score=0.71,
                n_certificates=1,
                n_judged=1,
                families=33,
            )
        ],
    }
    body = client.get("/autodedup/groups").json()
    assert set(body) == {"data", "store_ready"}
    assert body["store_ready"] is True
    data = body["data"]
    assert (data["has_more"], data["next_after"], data["generation"]) == (False, None, "g1")
    item = data["items"][0]
    assert item["cluster"]["cluster_key"] == 101
    assert item["cluster"]["sources"] == ["sreality", "bazos"]
    # The bitmask is decoded for the page — the chips never re-implement the bit values.
    assert item["cluster"]["evidence_family_names"] == ["ATTR", "IMG"]
    assert item["cluster"]["area_min"] == pytest.approx(68.0)
    assert item["verdict"] is None
    assert [m["listing_id"] for m in item["members"]] == [11, 12]
    assert item["members"][0]["cover"] == {
        "storage_path": "listings/11/1.jpg",
        "sreality_url": "https://img.sreality.cz/1.jpg",
    }
    assert item["members"][0]["n_images"] == 12
    assert item["edges"]["n_certificates"] == 1
    assert item["edges"]["family_names"] == ["ATTR", "IMG"]


def test_a_group_carries_the_operators_latest_verdict(client, conn):
    conn.canned = {
        "groups": [
            _cluster(
                verdict="same",
                verdict_note="obviously one flat",
                verdict_decided_by="operator@example.com",
                verdict_decided_at=datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc),
            )
        ]
    }
    item = client.get("/autodedup/groups").json()["data"]["items"][0]
    assert item["verdict"] == {
        "verdict": "same",
        "note": "obviously one flat",
        "decided_by": "operator@example.com",
        "decided_at": "2026-09-16T07:00:00+00:00",
    }
    # the verdict is lifted OUT of the cluster object, never duplicated inside it
    assert "verdict" not in item["cluster"]


def test_group_filters_reach_the_statement_as_parameters(client, conn):
    client.get(
        "/autodedup/groups",
        params={
            "generation": "g2",
            "block": 500123,
            "source": "bazos",
            "category_main": "byt",
            "category_type": "prodej",
            "min_size": 2,
            "max_size": 5,
            "min_score": 0.3,
            "max_score": 0.9,
            "verdict": "unreviewed",
            "shared_photo": 1,
            "has_judgement": 0,
            "limit": 10,
        },
    )
    params = _last_call(conn, usql.GROUPS_WEAKEST_SQL)
    assert params["generation"] == "g2"
    assert params["block"] == 500123
    assert params["source"] == "bazos"
    assert (params["min_size"], params["max_size"]) == (2, 5)
    assert (params["min_score"], params["max_score"]) == (0.3, 0.9)
    assert params["verdict"] == "unreviewed"
    # 0|1 off the wire becomes the boolean the `::boolean` arm compares
    assert params["shared_photo"] is True
    assert params["has_judgement"] is False
    assert params["limit"] == 11


def test_the_block_grain_travels_with_the_block_code(client, conn):
    """A block is a (code, grain) pair. `c490245` is a quarter and `o490245` a town, and
    migration 529 added `block_grain` because the code alone silently conflated the two —
    so the picker's two options must reach the statement as two different filters."""
    client.get("/autodedup/groups", params={"block": 490245, "block_grain": "c"})
    assert _last_call(conn, usql.GROUPS_WEAKEST_SQL)["block_grain"] == "c"
    # The headline count is the same cohort as the queue below it, grain included.
    conn.canned = {"groups": [_cluster()], "groups_count": [(88,)]}
    client.get("/autodedup/groups", params={"block": 490245, "block_grain": "c"})
    assert _last_call(conn, usql.GROUPS_COUNT_SQL)["block_grain"] == "c"
    assert "block_grain" in usql._CLUSTER_WHERE


def test_a_block_grain_outside_the_vocabulary_is_refused(client, conn):
    for surface in ("/autodedup/groups", "/autodedup/residual"):
        assert client.get(surface, params={"block_grain": "x"}).status_code == 400


def test_the_residual_block_filter_reads_the_store_the_block_comes_from(client, conn):
    """`autodedup.listing_fp` is the lane's scratch copy of a location fact and no shipped
    lane writes a row into it, so a block filter reading it emptied the queue for every
    block on offer. The block of a pair is read where the engine reads it from."""
    assert "listing_fp" not in usql.RESIDUAL_SQL
    assert "listing_location" in usql.RESIDUAL_SQL
    # `cast_obce_kod` when the town is split, else `obec_kod` — `fingerprint.block_key_of`.
    assert "coalesce(ll.cast_obce_kod, ll.obec_kod)" in usql.RESIDUAL_SQL
    # and the filter the count runs is the filter the page ran, joins included
    assert usql._RESIDUAL_BLOCK in usql.RESIDUAL_COUNT_SQL
    client.get("/autodedup/residual", params={"block": 490245, "block_grain": "c"})
    assert _last_call(conn, usql.RESIDUAL_SQL)["block_grain"] == "c"


def test_an_unset_group_filter_is_a_null_parameter_not_a_predicate(client, conn):
    client.get("/autodedup/groups")
    params = _last_call(conn, usql.GROUPS_WEAKEST_SQL)
    for key in ("block", "block_grain", "source", "category_main", "category_type",
                "min_size", "max_size",
                "min_score", "max_score", "verdict", "shared_photo", "has_judgement",
                "after_score", "after_key", "after_ts", "after_size"):
        assert params[key] is None, key


def test_the_first_page_carries_the_total_for_the_current_filter(client, conn):
    """"20 of N". A keyset page cannot count itself, so the count is its own statement — and
    it is the SAME filter: the route hands it the params dict the list read with."""
    conn.canned = {"groups": [_cluster()], "groups_count": [(412,)]}
    data = client.get("/autodedup/groups", params={"block": 500123}).json()["data"]
    assert data["total"] == 412
    assert _last_call(conn, usql.GROUPS_COUNT_SQL)["block"] == 500123


def test_the_count_is_the_same_where_clause_as_the_page(client, conn):
    """Two statements, one filter. Spelled as a text assertion because the fake connection
    re-implements neither: a count that drifted from the list's predicate would hand the
    operator a headline about a cohort the queue below it is not showing."""
    assert usql._CLUSTER_WHERE in usql.GROUPS_COUNT_SQL
    assert usql._CLUSTER_WHERE in usql.GROUPS_WEAKEST_SQL
    assert usql._RESIDUAL_WHERE in usql.RESIDUAL_COUNT_SQL
    assert usql._RESIDUAL_WHERE in usql.RESIDUAL_SQL
    # The cursor is where the page is, not what the filter selects — the count carries none.
    assert "after_score" not in usql.GROUPS_COUNT_SQL
    assert "after_score" not in usql.RESIDUAL_COUNT_SQL
    # Nor does it run the display-only cover LATERALs.
    assert "images" not in usql.RESIDUAL_COUNT_SQL


def test_a_continuation_page_does_not_recount(client, conn):
    """Paging cannot change N, and counting again per page is pure cost on a statement that
    already reads every matching row. `None` says "not counted here", never "zero"."""
    conn.canned = {"groups": [_cluster()], "groups_count": [(412,)]}
    data = client.get("/autodedup/groups", params={"after": "0.5|99"}).json()["data"]
    assert data["total"] is None
    assert all(sql != usql.GROUPS_COUNT_SQL for sql, _ in conn.calls)


def test_the_residual_page_carries_its_own_total(client, conn):
    conn.canned = {"residual": [_residual_row()], "residual_count": [(58,)]}
    data = client.get("/autodedup/residual", params={"zone": "band"}).json()["data"]
    assert data["total"] == 58
    assert _last_call(conn, usql.RESIDUAL_COUNT_SQL)["zone"] == "band"


def test_a_store_with_no_count_row_reports_no_total_not_zero(client, conn):
    conn.canned = {"groups": [_cluster()]}
    assert client.get("/autodedup/groups").json()["data"]["total"] is None


# --------------------------------------------------------- the blocks of a generation


def _block(key: int, grain: str = "o", **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "block_key": key,
        "block_grain": grain,
        "name": "Jablonec nad Nisou",
        "n_clusters": 412,
        "n_listings": 907,
    }
    values.update(over)
    return _tuple(usql.BLOCK_COLUMNS, **values)


def test_blocks_are_offered_with_their_names_and_counts(client, conn):
    """The BLOCK filter was a free-text field for a bigint RÚIAN code: typing a town name
    silently dropped the parameter. This is the vocabulary that replaces it."""
    conn.canned = {"blocks": [_block(563510), _block(490245, "c", name="Žižkov", n_clusters=88)]}
    body = client.get("/autodedup/blocks", params={"generation": "g1"}).json()
    assert body["store_ready"] is True
    assert body["data"]["generation"] == "g1"
    assert body["data"]["items"] == [
        {
            "block_key": 563510,
            "block_grain": "o",
            "name": "Jablonec nad Nisou",
            "n_clusters": 412,
            "n_listings": 907,
        },
        {
            "block_key": 490245,
            "block_grain": "c",
            "name": "Žižkov",
            "n_clusters": 88,
            "n_listings": 907,
        },
    ]
    assert _last_call(conn, usql.BLOCKS_SQL) == {"generation": "g1", "limit": 200}


def test_a_block_with_no_resolved_name_is_still_offered(client, conn):
    """A grain that predates migration 529 joins no name — the code is still a real filter
    value, and a guessed name would be a town's number read as a quarter's."""
    conn.canned = {"blocks": [_block(563510, grain=None, name=None)]}
    item = client.get("/autodedup/blocks").json()["data"]["items"][0]
    assert item["name"] is None
    assert item["block_key"] == 563510


def test_the_block_name_comes_from_the_resolved_location_store(client, conn):
    """Names are never a portal's spelling and never a column of the autodedup schema: they
    are read off `listing_location`, at the grain the block was keyed at."""
    assert "listing_location" in usql.BLOCKS_SQL
    assert "obec_name" in usql.BLOCKS_SQL and "cast_obce_name" in usql.BLOCKS_SQL
    # The most frequent spelling wins, so a picker's label cannot flicker mid-resolve.
    assert "ORDER BY grain, kod, n DESC" in usql.BLOCKS_SQL


def test_the_blocks_vocabulary_is_bounded(client, conn):
    """The busiest blocks first, capped: at corpus scale this is thousands of obce, which is
    neither a payload worth sending nor a select anyone can use. A block outside the cap is
    still a filter — the picker keeps whatever key a URL arrived with."""
    assert "LIMIT %(limit)s::int" in usql.BLOCKS_SQL
    assert "ORDER BY b.n_clusters DESC" in usql.BLOCKS_SQL
    client.get("/autodedup/blocks")
    assert _last_call(conn, usql.BLOCKS_SQL)["limit"] == 200


def test_blocks_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    assert client.get("/autodedup/blocks").json() == {"data": None, "store_ready": False}


def test_blocks_refuses_a_filter_outside_the_registry(client, conn):
    assert client.get("/autodedup/blocks", params={"block": 1}).status_code == 400


@pytest.mark.parametrize(
    "params",
    [
        {"nonsense": "1"},
        {"decided_by": "someone@example.com"},  # a §12 filter that is NOT served yet
    ],
)
def test_an_unknown_group_filter_is_refused(client, conn, params):
    resp = client.get("/autodedup/groups", params=params)
    assert resp.status_code == 400
    assert "unknown filter" in resp.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "cheapest"},
        {"verdict": "maybe"},
        {"shared_photo": 2},
    ],
)
def test_a_filter_value_outside_the_registry_is_refused(client, conn, params):
    assert client.get("/autodedup/groups", params=params).status_code == 400


def test_groups_pages_by_the_weakest_edge(client, conn):
    conn.canned = {"groups": [_cluster(101, min_edge_score=0.40),
                              _cluster(102, min_edge_score=0.55),
                              _cluster(103, min_edge_score=0.71)]}
    first = client.get("/autodedup/groups", params={"limit": 2}).json()["data"]
    assert [i["cluster"]["cluster_key"] for i in first["items"]] == [101, 102]
    assert first["has_more"] is True
    assert first["next_after"] == "0.55|102"

    client.get("/autodedup/groups", params={"limit": 2, "after": first["next_after"]})
    params = _last_call(conn, usql.GROUPS_WEAKEST_SQL)
    assert (params["after_score"], params["after_key"]) == (0.55, 102)


def test_a_cluster_without_an_edge_still_pages(client, conn):
    """A singleton has no `min_edge_score`; the cursor uses the same -1 coalesce the
    statement sorts by, or the next page would never satisfy the row comparison."""
    conn.canned = {"groups": [_cluster(101, min_edge_score=None), _cluster(102)]}
    page = client.get("/autodedup/groups", params={"limit": 1}).json()["data"]
    assert page["next_after"] == "-1.0|101"


def test_sorting_by_newest_uses_its_own_statement_and_cursor(client, conn):
    conn.canned = {"groups_newest": [_cluster(101), _cluster(102)]}
    page = client.get("/autodedup/groups", params={"sort": "newest", "limit": 1}).json()["data"]
    assert page["sort"] == "newest"
    assert page["next_after"] == "2026-09-16T06:00:00+00:00|101"
    client.get("/autodedup/groups", params={"sort": "newest", "after": page["next_after"]})
    params = _last_call(conn, usql.GROUPS_NEWEST_SQL)
    assert params["after_ts"] == "2026-09-16T06:00:00+00:00"
    assert params["after_key"] == 101


def test_sorting_by_largest_uses_its_own_statement_and_cursor(client, conn):
    conn.canned = {"groups_largest": [_cluster(101, size=7), _cluster(102, size=3)]}
    page = client.get("/autodedup/groups", params={"sort": "largest", "limit": 1}).json()["data"]
    assert page["next_after"] == "7|101"
    client.get("/autodedup/groups", params={"sort": "largest", "after": "7|101"})
    params = _last_call(conn, usql.GROUPS_LARGEST_SQL)
    assert (params["after_size"], params["after_key"]) == (7, 101)


@pytest.mark.parametrize(
    "params",
    [
        {"after": "nonsense"},
        {"after": "a|b"},
        {"sort": "newest", "after": "abc|5"},
        {"sort": "largest", "after": "x|5"},
    ],
)
def test_a_malformed_cursor_is_refused(client, conn, params):
    """Every sort's cursor is validated HERE. An unparsable timestamp reaching
    `%(after_ts)s::timestamptz` would be a 500 where the numeric sorts answer 400."""
    assert client.get("/autodedup/groups", params=params).status_code == 400
    assert all(sql != usql.GROUPS_WEAKEST_SQL for sql, _ in conn.calls)


# ----------------------------------------------------------------------- one group, whole


def test_group_detail_carries_members_photos_edges_and_conflicts(client, conn):
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12)],
        "images": [_image(11, 900, 1, 5), _image(12, 910, 1, 7)],
        "cluster_pairs": [_pair_row()],
        "judgements": [
            _tuple(
                usql.JUDGEMENT_COLUMNS,
                listing_lo=11,
                listing_hi=12,
                judge_version="j1",
                tier="text",
                model="gpt-5-mini",
                verdict="same_property",
                confidence=0.93,
                unit_discriminator=None,
                key_evidence=["same street and number"],
                contradicting_evidence=[],
                developer_project_suspected=False,
                cost_usd=Decimal("0.000400"),
                created_at=datetime(2026, 9, 16, 4, tzinfo=timezone.utc),
            )
        ],
        "cluster_verdicts": [],
        "pair_verdicts": [],
        "conflicts": [
            _tuple(
                usql.CONFLICT_COLUMNS,
                id=3,
                kind="invariant",
                cluster_key_a=101,
                cluster_key_b=None,
                listing_lo=11,
                listing_hi=13,
                invariant="floor_conflict",
                detail={"floors": [3, 5]},
                created_at=datetime(2026, 9, 16, 4, tzinfo=timezone.utc),
            )
        ],
    }
    data = client.get("/autodedup/groups/101").json()["data"]
    assert data["cluster"]["cluster_key"] == 101
    assert [m["listing_id"] for m in data["members"]] == [11, 12]
    assert data["members"][0]["images"][0]["storage_path"] == "listings/11/1.jpg"
    pair = data["pairs"][0]
    # the certificate is parsed back out of the reason string, not invented
    assert pair["certificate"] == "K-A"
    assert pair["family_names"] == ["ATTR", "IMG"]
    assert "evidence families" in pair["why_not_merged"]
    assert data["judgements"][0]["verdict"] == "same_property"
    assert data["judgements"][0]["key_evidence"] == ["same street and number"]
    assert data["conflicts"][0]["invariant"] == "floor_conflict"
    assert data["conflicts"][0]["detail"] == {"floors": [3, 5]}
    # the gallery is bounded in the statement
    assert _last_call(conn, usql.LISTING_IMAGES_SQL)["per_listing"] == routes.IMAGES_PER_LISTING


def test_an_unknown_group_is_a_404(client, conn):
    assert client.get("/autodedup/groups/999").status_code == 404


# --------------------------------------------------------------------- residual duplicates


def test_residual_pairs_carry_both_sides_and_why_they_were_not_merged(client, conn):
    conn.canned = {"residual": [_residual_row()]}
    data = client.get("/autodedup/residual").json()["data"]
    assert data["min_score"] == pytest.approx(0.20)
    item = data["items"][0]
    assert (item["listing_lo"], item["listing_hi"]) == (21, 22)
    assert item["families"] == ["ATTR", "TXT"]
    assert item["a"]["source"] == "sreality" and item["b"]["source"] == "bazos"
    assert item["a"]["cover"] == {
        "storage_path": "listings/a_cover.jpg",
        "sreality_url": "https://img.example.invalid/a_cover.jpg",
    }
    assert item["a"]["listing_id"] == 21 and item["b"]["listing_id"] == 22
    assert item["judge"] == {"verdict": "same_property", "confidence": 0.81, "tier": "text"}
    assert item["verdict"] is None
    assert "review band" in item["why_not_merged"]
    # the pair was scored by the hand prior, which ships with the repo, so the breakdown is
    # the model's own log-odds contributions rather than a stand-in
    assert len(item["top_features"]) <= routes.TOP_FEATURES
    assert all(
        {"name", "value", "present", "contribution"} == set(f) for f in item["top_features"]
    )
    # 1.05 of value term + 0.10 of presence term: ONE feature, its whole contribution
    assert item["top_features"][0] == {
        "name": "rare_token_overlap", "value": 3.0, "present": True, "contribution": 1.15
    }


def test_the_breakdown_is_one_row_per_feature_not_one_per_model_term(client, conn):
    """E21 wants a legible breakdown. `LogisticModel.contributions` reports a feature's value
    term and its presence term apart; ranked apart under the hand prior the presence halves
    (0.1-0.3) outrank most value terms and fill the list with rows that carry no value at all.
    Summed, and with the terms that contributed nothing dropped, every row says something."""
    conn.canned = {"residual": [_residual_row()]}
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert [f["name"] for f in item["top_features"]] == [
        "rare_token_overlap", "dispo_equal", "area_rel_diff"
    ]
    assert not any(":present" in f["name"] for f in item["top_features"])
    assert all(f["value"] is not None and f["contribution"] for f in item["top_features"])


def test_a_pair_scored_by_a_model_this_deployment_has_not_got_falls_back_to_features(
    client, conn
):
    """A contribution attributed to the WRONG model would be worse than none, so an unknown
    `model_version` degrades to the highest PRESENT features with no contribution at all."""
    conn.canned = {"residual": [_residual_row(model_version="trained_v9")]}
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert [f["name"] for f in item["top_features"]] == [
        "rare_token_overlap", "dispo_equal", "area_rel_diff"
    ]
    assert all(f["contribution"] is None for f in item["top_features"])


@pytest.mark.parametrize(
    "decision,veto,zone,expected",
    [
        ("model", "sale_rent", "reject", "hard guard"),
        ("auto_reject:numeral_conflict", None, "reject", "auto-rejected"),
        ("certificate:K-C:evidence_gate", None, "band", "evidence families"),
        ("model", None, "reject", "below the review band"),
        ("model", None, "merge", "no cluster of this generation"),
    ],
)
def test_why_not_merged_names_the_precondition_that_failed(
    client, conn, decision, veto, zone, expected
):
    conn.canned = {"residual": [_residual_row(decision=decision, guard_veto=veto, zone=zone)]}
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert expected in item["why_not_merged"]


def test_residual_filters_and_cursor_reach_the_statement(client, conn):
    conn.canned = {"residual": [_residual_row(21, 22), _residual_row(23, 24, score=0.31)]}
    page = client.get(
        "/autodedup/residual",
        params={"limit": 1, "zone": "band", "min_score": 0.3, "source_pair": "bazos+sreality",
                "block": 500123, "has_judgement": 1, "verdict": "unreviewed"},
    ).json()["data"]
    assert page["has_more"] is True
    assert page["next_after"] == "0.44|21|22"
    params = _last_call(conn, usql.RESIDUAL_SQL)
    assert params["zone"] == "band"
    assert params["min_score"] == 0.3
    assert params["source_pair"] == "bazos+sreality"
    assert params["block"] == 500123
    assert params["has_judgement"] is True
    assert params["limit"] == 2

    client.get("/autodedup/residual", params={"after": "0.44|21|22"})
    cursor = _last_call(conn, usql.RESIDUAL_SQL)
    assert (cursor["after_score"], cursor["after_lo"], cursor["after_hi"]) == (0.44, 21, 22)


@pytest.mark.parametrize("params", [{"zone": "nowhere"}, {"sort": "score_asc"}, {"nope": 1}])
def test_residual_refuses_a_filter_outside_the_registry(client, conn, params):
    assert client.get("/autodedup/residual", params=params).status_code == 400


# --------------------------------------------------------------------------- one pair, whole


def _pair_evidence(conn: _FakeConn) -> None:
    conn.canned = {
        "pair_one": [_pair_row(11, 12)],
        "listing_detail": [_listing_detail(11), _listing_detail(12, source="bazos")],
        "images": [
            _image(11, 900, 1, 0b1011),
            _image(11, 901, 2, None),
            _image(12, 910, 1, 0b1001),
            _image(12, 911, 2, 0b1011),
        ],
        "phashes": [
            _phash(11, 900, 0b1011),
            _phash(12, 910, 0b1001),
            _phash(12, 911, 0b1011),
        ],
        "judgements": [],
        "pair_verdicts": [],
    }


def test_the_pair_view_is_the_whole_evidence(client, conn):
    _pair_evidence(conn)
    data = client.get("/autodedup/pair/11/12").json()["data"]
    assert data["pair"]["certificate"] == "K-A"
    names = [f["name"] for f in data["features"]]
    assert names == sorted(names)
    # presence travels with the value: E12's "unknown is not zero"
    absent = [f for f in data["features"] if f["name"] == "overlap_days"][0]
    assert absent["present"] is False
    assert data["digests"]["a"]["listing_id"] == 11
    assert data["digests"]["b"]["portal"] == "bazos"
    # per frame, the nearest dHash on the other side — and none at all for a NULL phash
    first = data["images"]["a"][0]
    assert first["best_match"] == {"image_id": 911, "hamming": 0}
    assert data["images"]["a"][1]["best_match"] is None
    assert data["images"]["b"][0]["best_match"] == {"image_id": 900, "hamming": 1}


def test_the_best_match_searches_past_the_gallery_cap(client, conn):
    """The gallery renders 30 frames a side; the engine scored the whole album. A frame whose
    only twin sits beyond the cap must still report the match, or the page contradicts the IMG
    family bit printed next to it."""
    _pair_evidence(conn)
    conn.canned["phashes"] = [
        _phash(11, 900, 0b1011),
        _phash(12, 910, 0b1001),
        _phash(12, 999, 0b1011),  # past the rendered 30
    ]
    data = client.get("/autodedup/pair/11/12").json()["data"]
    first = data["images"]["a"][0]
    assert first["best_match"] == {"image_id": 999, "hamming": 0}
    assert (first["best_match_image_id"], first["best_hamming"]) == (999, 0)
    assert data["images"]["n_frames_shown"]["b"] == 2
    assert data["images"]["n_hashed_frames"]["b"] == 2
    assert "per_listing" not in _last_call(conn, usql.LISTING_PHASHES_SQL)


def test_a_dhash_travels_as_text_not_as_a_rounded_json_number(client, conn):
    """2^53 is where JSON numbers stop being exact and most 64-bit dHashes start."""
    assert "i.phash::text AS phash" in usql.LISTING_IMAGES_SQL
    _pair_evidence(conn)
    data = client.get("/autodedup/pair/11/12").json()["data"]
    assert data["images"]["a"][0]["phash"] == "11"


def test_the_pair_view_carries_no_pii(client, conn):
    """E28. The digest is the judge's own PII-free record, and the statement selects no
    broker column at all — so the contact details in a description are the only way a person
    could reach the wire, and they are scrubbed."""
    _pair_evidence(conn)
    raw = client.get("/autodedup/pair/11/12").text
    assert "broker" not in raw.lower()
    assert "777 123 456" not in raw
    assert "example.cz" not in raw
    assert "Jana Novakova" not in raw
    assert "[telefon]" in raw and "[email]" in raw
    for name in ("broker_name", "broker_phone", "broker_email"):
        assert name not in usql.LISTING_DETAIL_SQL
        assert name not in usql.LISTING_DETAIL_COLUMNS


def test_the_pair_view_keeps_the_price_unit_the_judge_digest_dropped(client, conn):
    """Two behaviours meeting in one payload: W4 removed `price_unit` from `ListingDigest`
    (the `celkem` / `za nemovitost` suffix is one fact in two portal vocabularies), while the
    pair page still declares the key (`AutodedupDigest.price_unit`). The route therefore
    sources it from the raw row, not from the digest."""
    from autodedup.judge import ListingDigest

    assert not hasattr(ListingDigest(listing_id=1), "price_unit")
    _pair_evidence(conn)
    digests = client.get("/autodedup/pair/11/12").json()["data"]["digests"]
    assert digests["a"]["price_unit"] == "celkem"


def test_an_unknown_pair_is_a_404(client, conn):
    assert client.get("/autodedup/pair/11/12").status_code == 404


def test_a_pair_must_be_asked_for_in_canonical_order(client, conn):
    """`listing_lo < listing_hi` is a table constraint, so the reversed URL can only ever
    404 — it is refused as the client error it is, before any statement runs."""
    assert client.get("/autodedup/pair/12/11").status_code == 400
    assert conn.calls == []


# ------------------------------------------------------------------- the operator's verdict


def _verdict_row(**over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "id": 1,
        "kind": "pair",
        "cluster_key": None,
        "listing_lo": 11,
        "listing_hi": 12,
        "verdict": "different",
        "weight": 1.0,
        "note": "two units in one building",
        "decided_by": "operator@example.com",
        "decided_at": datetime(2026, 9, 16, 10, tzinfo=timezone.utc),
    }
    values.update(over)
    return _tuple(usql.VERDICT_COLUMNS, **values)


@pytest.fixture()
def admin_client(conn: _FakeConn):
    """The admin dependency returns the JWT claims, and `decided_by` is the email in them."""
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True,
        "email": "operator@example.com",
        "sub": "uuid-1",
    }
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_a_negative_pair_verdict_also_writes_the_permanent_must_not_link(admin_client, conn):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "different",
              "note": "two units in one building"},
    ).json()
    assert body["store_ready"] is True
    assert body["data"]["must_not_link"] is True
    assert body["data"]["verdict"]["decided_by"] == "operator@example.com"
    assert body["data"]["verdict"]["decided_at"] == "2026-09-16T10:00:00+00:00"
    written = _last_call(conn, usql.VERDICT_PAIR_UPSERT_SQL)
    assert written["decided_by"] == "operator@example.com"
    assert written["verdict"] == "different"
    mnl = _last_call(conn, usql.MUST_NOT_LINK_UPSERT_SQL)
    assert (mnl["listing_lo"], mnl["listing_hi"]) == (11, 12)
    assert mnl["reason"] == "two units in one building"


def test_same_building_different_unit_is_also_a_must_not_link(admin_client, conn):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12,
              "verdict": "same_building_different_unit"},
    ).json()
    assert body["data"]["must_not_link"] is True
    assert _last_call(conn, usql.MUST_NOT_LINK_UPSERT_SQL)["reason"] == (
        "operator: same_building_different_unit"
    )


@pytest.mark.parametrize("verdict", ["same", "unsure"])
def test_a_non_negative_verdict_writes_no_must_not_link(admin_client, conn, verdict):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row(verdict=verdict)]}
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": verdict},
    ).json()
    assert body["data"]["must_not_link"] is False
    assert all(sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in conn.calls)


@pytest.mark.parametrize("verdict", ["same", "unsure"])
def test_reversing_a_negative_pair_verdict_retracts_the_must_not_link(
    admin_client, conn, verdict
):
    """The mirror of the must-not-link write. A veto the operator has taken back has to stop
    vetoing: `guards.py` refuses every pair in the table on every future run, so a row left
    behind kills a pair the page now shows as confirmed."""
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row(verdict=verdict)]}
    admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "different"},
    )
    admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": verdict},
    )
    retracted = _last_call(conn, usql.MUST_NOT_LINK_RETRACT_SQL)
    assert (retracted["listing_lo"], retracted["listing_hi"]) == (11, 12)
    # only the operator's own veto is dropped — a guard/model/llm row is not theirs to retract
    assert "source = 'operator'" in usql.MUST_NOT_LINK_RETRACT_SQL


def test_a_negative_verdict_does_not_retract_what_it_just_wrote(admin_client, conn):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "different"},
    )
    assert all(sql != usql.MUST_NOT_LINK_RETRACT_SQL for sql, _ in conn.calls)


def test_a_cluster_verdict_touches_no_must_not_link_either_way(admin_client, conn):
    conn.canned = {"cluster_exists": [(1,)], "verdict_write": [_verdict_row(kind="cluster")]}
    admin_client.post(
        "/autodedup/verdict", json={"kind": "cluster", "cluster_key": 101, "verdict": "same"}
    )
    assert all(
        sql not in (usql.MUST_NOT_LINK_UPSERT_SQL, usql.MUST_NOT_LINK_RETRACT_SQL)
        for sql, _ in conn.calls
    )


def test_a_cluster_verdict_records_only_the_verdict(admin_client, conn):
    """Flagging a GROUP is not the same statement as forbidding each of its pairs: the splits
    happen on the pair view, so no must-not-link is written here."""
    conn.canned = {
        "cluster_exists": [(1,)],
        "verdict_write": [_verdict_row(kind="cluster", cluster_key=101, listing_lo=None,
                                       listing_hi=None)],
    }
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "cluster", "cluster_key": 101, "verdict": "different"},
    ).json()
    assert body["data"]["must_not_link"] is False
    assert body["data"]["verdict"]["cluster_key"] == 101
    assert all(sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in conn.calls)
    assert _last_call(conn, usql.VERDICT_CLUSTER_UPSERT_SQL)["cluster_key"] == 101


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "group", "cluster_key": 1, "verdict": "same"},
        {"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "duplicate"},
        {"kind": "pair", "listing_lo": 11, "verdict": "same"},
        {"kind": "pair", "listing_lo": 12, "listing_hi": 11, "verdict": "same"},
        {"kind": "pair", "listing_lo": 11, "listing_hi": 12, "cluster_key": 3, "verdict": "same"},
        {"kind": "cluster", "verdict": "same"},
        {"kind": "cluster", "cluster_key": 1, "listing_lo": 11, "listing_hi": 12,
         "verdict": "same"},
    ],
)
def test_a_malformed_verdict_is_refused(admin_client, conn, payload):
    assert admin_client.post("/autodedup/verdict", json=payload).status_code == 400


def test_a_verdict_on_something_that_does_not_exist_is_a_404(admin_client, conn):
    assert admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "same"},
    ).status_code == 404
    assert admin_client.post(
        "/autodedup/verdict", json={"kind": "cluster", "cluster_key": 9, "verdict": "same"}
    ).status_code == 404


def test_a_verdict_nothing_can_be_attributed_to_is_refused(conn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    try:
        client = TestClient(api_main.app)
        resp = client.post(
            "/autodedup/verdict",
            json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "same"},
        )
        assert resp.status_code == 403
    finally:
        api_main.app.dependency_overrides.clear()


# ------------------------------------------------------------------------ the header strip


def test_stats_reports_what_the_engine_produced(client, conn):
    conn.canned = {
        "zones": [_tuple(usql.ZONE_COUNT_COLUMNS, zone="band", n=40),
                  _tuple(usql.ZONE_COUNT_COLUMNS, zone="merge", n=12)],
        "certificates": [_tuple(usql.CERTIFICATE_COUNT_COLUMNS, certificate="K-A", n=7)],
        "generations": [
            _tuple(usql.GENERATION_COLUMNS, generation="g1", n_clusters=9, n_members=21,
                   n_conflicted=1, last_changed_at=datetime(2026, 9, 16, tzinfo=timezone.utc))
        ],
        "verdict_counts": [_tuple(usql.VERDICT_COUNT_COLUMNS, kind="pair", verdict="same", n=3)],
        "judgement_counts": [
            _tuple(usql.JUDGEMENT_COUNT_COLUMNS, tier="text", verdict="same_property", n=5)
        ],
        "last_run": [
            _tuple(usql.SCORE_RUN_COLUMNS, id=8, status="success", fingerprint="abc",
                   cohort={"blocks": 4}, params={"generation": "g1"},
                   stats={"n_pairs": 52},
                   started_at=datetime(2026, 9, 16, 5, tzinfo=timezone.utc),
                   finished_at=datetime(2026, 9, 16, 5, 30, tzinfo=timezone.utc))
        ],
    }
    engine = client.get("/autodedup/stats").json()["data"]["engine"]
    assert engine["pairs_by_zone"] == {"band": 40, "merge": 12}
    assert engine["n_pairs"] == 52
    assert engine["certificates"] == {"K-A": 7}
    assert engine["latest_generation"] == "g1"
    assert engine["generations"][0]["n_clusters"] == 9
    assert engine["n_verdicts"] == 3
    assert engine["n_judgements"] == 5
    assert engine["last_score_run"]["status"] == "success"
    assert engine["last_score_run"]["stats"] == {"n_pairs": 52}
    assert engine["last_score_run"]["finished_at"] == "2026-09-16T05:30:00+00:00"
    assert _last_call(conn, usql.LAST_SCORE_RUN_SQL)["mode"] == "score"


# ------------------------------------------------------------------ gate + un-migrated store


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/autodedup/groups", None),
        ("get", "/autodedup/groups/101", None),
        ("get", "/autodedup/residual", None),
        ("get", "/autodedup/pair/11/12", None),
        ("get", "/autodedup/blocks", None),
    ],
)
def test_the_validation_routes_render_when_the_store_does_not_exist(
    admin_client, conn, method, path, payload
):
    conn.ready = False
    resp = getattr(admin_client, method)(path, **({"json": payload} if payload else {}))
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    assert len(conn.calls) == 1


def test_a_relation_that_vanishes_mid_route_still_renders(client, conn, monkeypatch):
    """The teardown race again, but one statement LATER: a guard around only the first read
    leaves every follow-up fetch of the same route bare."""
    pytest.importorskip("psycopg")
    from psycopg import errors as pg_errors

    original = routes._fetch

    def _boom_on_members(c: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
        if sql == usql.GROUP_MEMBERS_SQL:
            raise pg_errors.UndefinedTable("relation does not exist")
        return original(c, sql, params)

    conn.canned = {"groups": [_cluster()], "group_one": [_cluster()]}
    monkeypatch.setattr(routes, "_fetch", _boom_on_members)
    assert client.get("/autodedup/groups").json() == {"data": None, "store_ready": False}
    assert client.get("/autodedup/groups/101").json() == {"data": None, "store_ready": False}


def test_a_verdict_against_a_store_that_does_not_exist_is_refused_not_accepted(
    admin_client, conn
):
    """A read renders "not created yet"; a WRITE may not. `store_ready: false` with HTTP 200
    is a success to the optimistic client, which would then show a decision nothing stored."""
    conn.ready = False
    resp = admin_client.post(
        "/autodedup/verdict", json={"kind": "cluster", "cluster_key": 1, "verdict": "same"}
    )
    assert resp.status_code == 503
    assert len(conn.calls) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/autodedup/groups"),
        ("get", "/autodedup/groups/101"),
        ("get", "/autodedup/residual"),
        ("get", "/autodedup/pair/11/12"),
        ("get", "/autodedup/blocks"),
        ("post", "/autodedup/verdict"),
    ],
)
def test_the_validation_routes_require_admin(client, conn, method, path):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert getattr(client, method)(path).status_code == 401
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: {"app_metadata": {}}
    assert getattr(client, method)(path).status_code == 403
