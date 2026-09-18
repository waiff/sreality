"""Tests for /autodedup/* — what the AUTODEDUP Progress page reads (PROGRAM.md §12, W1).

The routes hold no SQL of their own; `autodedup/progress_sql.py` does. So the connection is
faked and dispatches on the statement, and the assertions are about the CONTRACT: the
`{"data": …, "store_ready": …}` envelope, every `autodedup.iterations` column reaching the
page, keyset paging that reports `has_more` from a real extra row, Decimal/datetime coming
back as JSON, the header rollup summing the per-wave statement, and above all that an
un-migrated database renders instead of 500ing.
"""

from __future__ import annotations

import json
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
from autodedup import candidates as candidate_groups
from autodedup import progress_sql as psql
from autodedup import ui_sql as usql


# Every W5 statement the routes may execute, mapped to the canned-row bucket it reads.
_LABELS: dict[str, str] = {
    usql.GROUPS_WEAKEST_SQL: "groups",
    usql.GROUPS_NEWEST_SQL: "groups_newest",
    usql.GROUPS_LARGEST_SQL: "groups_largest",
    usql.GROUPS_RANDOM_SQL: "groups_random",
    usql.GROUP_ONE_SQL: "group_one",
    usql.GROUP_MEMBERS_SQL: "members",
    usql.MEMBER_TEXT_SQL: "member_text",
    usql.EDGE_SUMMARY_SQL: "edges",
    usql.LISTING_IMAGES_SQL: "images",
    usql.LISTING_PHASHES_SQL: "phashes",
    usql.CLUSTER_PAIRS_SQL: "cluster_pairs",
    usql.PAIR_ONE_SQL: "pair_one",
    usql.JUDGEMENTS_LATEST_SQL: "judgements",
    usql.PAIR_VERDICTS_SQL: "pair_verdicts",
    usql.MEMBER_PAIR_VERDICTS_SQL: "member_verdicts",
    usql.CLUSTER_MEMBER_IDS_SQL: "cluster_member_ids",
    usql.CLUSTER_VERDICTS_SQL: "cluster_verdicts",
    usql.CLUSTER_CONFLICTS_SQL: "conflicts",
    usql.RESIDUAL_SQL: "residual",
    usql.RESIDUAL_RANDOM_SQL: "residual_random",
    # The candidate queue (E56): three cheap reads feed the packing, one reads the cards.
    usql.CANDIDATE_FINGERPRINT_SQL: "candidate_fingerprint",
    usql.CANDIDATE_PAIRS_SQL: "candidate_pairs",
    usql.CANDIDATE_CLUSTER_MEMBERS_SQL: "candidate_locks",
    usql.OPERATOR_PAIR_VERDICTS_SQL: "pair_verdict_map",
    usql.LISTING_CARDS_SQL: "listing_cards",
    usql.GENERATION_EXISTS_SQL: "generation_exists",
    usql.VALIDATION_GROUPS_SAMPLE_SQL: "validation_sample",
    usql.VALIDATION_GROUPS_TOTAL_SQL: "validation_total",
    usql.VALIDATION_RESIDUAL_SAMPLE_SQL: "validation_sample",
    usql.VALIDATION_RESIDUAL_TOTAL_SQL: "validation_total",
    usql.AGREEMENT_PAIRS_SQL: "agreement_pairs",
    usql.AGREEMENT_OVERSIZE_SQL: "agreement_oversize",
    usql.GROUPS_COUNT_SQL: "groups_count",
    usql.RESIDUAL_COUNT_SQL: "residual_count",
    usql.BLOCKS_SQL: "blocks",
    usql.LISTING_DETAIL_SQL: "listing_detail",
    usql.PAIR_ZONES_SQL: "zones",
    usql.CERTIFICATE_COUNTS_SQL: "certificates",
    usql.GENERATION_COUNTS_SQL: "generations",
    usql.VERDICT_COUNTS_SQL: "verdict_counts",
    usql.REASON_COUNTS_SQL: "reason_counts",
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
_PAGED: frozenset[str] = frozenset(
    {"groups", "groups_newest", "groups_largest", "groups_random", "residual", "residual_random"}
)


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
        if self._conn.open_tx:
            self._conn.tx_calls.append((sql, params))
        if sql in self._conn.raises:
            raise self._conn.raises[sql]
        if "to_regclass" in sql:
            self._rows = [(self._conn.ready,)]
        elif sql == usql.LATEST_GENERATION_SQL:
            # WHICH pass a view opens on is now a read, not a constant — so the fake answers
            # it like the store does, and a store with no clustering yet answers nothing.
            latest = self._conn.latest_generation
            self._rows = [] if latest is None else [(latest,)]
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


class _Tx:
    """`conn.transaction()` — psycopg's own context manager, faked. A route that writes
    several rows as one ruling has to open it, so the fake counts the opens rather than
    quietly tolerating writes that would land half-applied in production."""

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.open_tx += 1
        self._conn.transactions += 1
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self._conn.open_tx -= 1
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.ready: bool = True
        # The newest generation `autodedup.clusters` holds. `g3` and not `g1`: the queue that
        # opened on the first pass forever is the defect these routes were fixed for.
        self.latest_generation: str | None = "g3"
        self.iteration_rows: list[tuple[Any, ...]] = []
        self.wave_rows: list[tuple[Any, ...]] = []
        self.canned: dict[str, list[tuple[Any, ...]]] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # Statement -> the exception executing it raises (the un-migrated-value path).
        self.raises: dict[str, BaseException] = {}
        self.transactions: int = 0
        self.open_tx: int = 0
        self.tx_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)


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
    "verdict_reasons": [],
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
        "verdict_reasons": None,
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


def _member_text(listing_id: int, **over: Any) -> tuple[Any, ...]:
    """A member's own advert text — the DIALOG's row, not the queue's. BOTH strings carry
    contact details on purpose: E28 is asserted on the rendered payload, not on intent, and
    the title is the half that is new here — a bazos advert signs its headline as readily as
    its body, so a title that reached the wire unscrubbed would be the whole leak."""
    values: dict[str, Any] = {
        "listing_id": listing_id,
        "title": f"Prodej bytu 3+kk 68 m2, byt c. {listing_id} - Ing. Jan Novak, tel. 777 123 456",
        "description": (
            "Byt c. 12 ve 4. patre, orientace na jih, 68 m2. "
            "Kontaktujte Jana Novakova na 777 123 456 nebo jan.novak@example.cz"
        ),
    }
    values.update(over)
    return _tuple(usql.MEMBER_TEXT_COLUMNS, **values)


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
        "verdict_reasons": None,
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


def _judgement(lo: int, hi: int, **over: Any) -> tuple[Any, ...]:
    """The judge's latest word on one pair, in `JUDGEMENT_COLUMNS` order."""
    values: dict[str, Any] = {
        "listing_lo": lo,
        "listing_hi": hi,
        "judge_version": "j2",
        "tier": "text",
        "model": "gpt-5.6-luna",
        "verdict": "same_property",
        "confidence": 0.81,
        "unit_discriminator": None,
        "key_evidence": ["same street and number"],
        "contradicting_evidence": [],
        "developer_project_suspected": False,
        "cost_usd": Decimal("0.000400"),
        "created_at": datetime(2026, 9, 16, 4, tzinfo=timezone.utc),
    }
    values.update(over)
    return _tuple(usql.JUDGEMENT_COLUMNS, **values)


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
    # The generation is the store's newest pass, echoed back — never the one the URL omitted.
    assert (data["has_more"], data["next_after"], data["generation"]) == (False, None, "g3")
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
        "reasons": [],
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


# ------------------------------------------------- which generation a validation view opens on


def _generation(name: str, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "generation": name,
        "n_clusters": 9,
        "n_members": 21,
        "n_conflicted": 1,
        "last_changed_at": datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc),
    }
    values.update(over)
    return _tuple(usql.GENERATION_COLUMNS, **values)


def test_a_queue_with_no_generation_named_reads_the_newest_pass(client, conn):
    """THE DEFECT. Every validation view defaulted to `g1` — the first hand-prior pass, which
    over-merged developer units and was superseded twice — so the operator reviewed
    certificate edges the current engine never proposed, against a queue that looked current.
    A generation the caller did not name is now resolved against the store."""
    conn.canned = {"groups": [_cluster(generation="g3")]}
    body = client.get("/autodedup/groups").json()
    assert _last_call(conn, usql.GROUPS_WEAKEST_SQL)["generation"] == "g3"
    assert body["data"]["generation"] == "g3"


def test_a_named_generation_is_read_as_asked_and_costs_no_extra_statement(client, conn):
    """An older pass stays readable — that is how a past review is re-examined — and naming it
    skips the resolving read entirely."""
    client.get("/autodedup/groups", params={"generation": "g1"})
    assert _last_call(conn, usql.GROUPS_WEAKEST_SQL)["generation"] == "g1"
    assert all(sql != usql.LATEST_GENERATION_SQL for sql, _ in conn.calls)


def test_the_residual_scroll_opens_on_the_newest_pass(client, conn):
    """The residual view asks "which scored pair did this clustering NOT join?" — against the
    wrong generation it answers about a clustering nobody is validating."""
    client.get("/autodedup/residual")
    assert _last_call(conn, usql.RESIDUAL_SQL)["generation"] == "g3"


def test_the_block_vocabulary_is_the_newest_passs_blocks(client, conn):
    """The picker lists the blocks a generation clustered: read against `g1` it offers a
    vocabulary that filters the queue down to nothing."""
    body = client.get("/autodedup/blocks").json()
    assert _last_call(conn, usql.BLOCKS_SQL) == {"generation": "g3", "limit": 200}
    assert body["data"]["generation"] == "g3"


def test_the_pair_view_echoes_the_pass_it_was_validated_against(client, conn):
    """`autodedup.pairs` is generation-free, but the echo is what a link written from this page
    carries — so it names a real pass instead of repeating a missing parameter."""
    conn.canned = {"pair_one": [_pair_row()]}
    body = client.get("/autodedup/pair/11/12").json()
    assert body["data"]["generation"] == "g3"


def test_a_store_with_no_clustering_yet_resolves_to_no_generation(client, conn):
    """Nothing persisted is not `g1`: the queue is empty and says so, rather than filtering on
    a generation name the store never wrote."""
    conn.latest_generation = None
    body = client.get("/autodedup/groups").json()
    assert body["store_ready"] is True
    assert body["data"]["generation"] is None
    assert _last_call(conn, usql.GROUPS_WEAKEST_SQL)["generation"] is None


def test_the_generations_route_lists_every_pass_newest_first(client, conn):
    """The picker's vocabulary, and the one place the page learns which pass is current — so a
    view of an older generation can say so instead of looking like the live queue."""
    conn.canned = {"generations": [_generation("g3"), _generation("g1", n_clusters=4)]}
    body = client.get("/autodedup/generations").json()
    assert body["store_ready"] is True
    assert body["data"]["latest"] == "g3"
    assert [row["generation"] for row in body["data"]["items"]] == ["g3", "g1"]
    assert body["data"]["items"][0] == {
        "generation": "g3",
        "n_clusters": 9,
        "n_members": 21,
        "n_conflicted": 1,
        "last_changed_at": "2026-09-16T08:00:00+00:00",
    }


def test_the_generations_route_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    assert client.get("/autodedup/generations").json() == {"data": None, "store_ready": False}


def test_the_generations_route_refuses_a_filter_it_does_not_serve(client, conn):
    assert client.get("/autodedup/generations", params={"generation": "g1"}).status_code == 400


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


# ------------------------------------------ the seeded sample order (D6's unbiased draw)


def _md5(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def test_the_random_sort_has_its_own_statement_and_a_seeded_cursor(client, conn):
    """The cursor is the ORDER KEY, and the order key is `md5(cluster_key || seed)` — so the
    route must spell the hash exactly as Postgres does or the next page starts elsewhere."""
    conn.canned = {"groups_random": [_cluster(101), _cluster(102)]}
    page = client.get(
        "/autodedup/groups", params={"sort": "random", "limit": 1}
    ).json()["data"]
    assert page["sort"] == "random"
    assert page["seed"] == "v1"
    assert page["next_after"] == f"{_md5('101v1')}|101"
    params = _last_call(conn, usql.GROUPS_RANDOM_SQL)
    assert params["seed"] == "v1"
    assert params["after_hash"] is None

    client.get(
        "/autodedup/groups",
        params={"sort": "random", "after": page["next_after"], "seed": "v1"},
    )
    cursor = _last_call(conn, usql.GROUPS_RANDOM_SQL)
    assert cursor["after_hash"] == _md5("101v1")
    assert cursor["after_key"] == 101


def test_a_second_seed_is_a_second_sample(client, conn):
    conn.canned = {"groups_random": [_cluster(101), _cluster(102)]}
    page = client.get(
        "/autodedup/groups", params={"sort": "random", "limit": 1, "seed": "s2"}
    ).json()["data"]
    assert page["seed"] == "s2"
    assert page["next_after"] == f"{_md5('101s2')}|101"
    assert _last_call(conn, usql.GROUPS_RANDOM_SQL)["seed"] == "s2"


def test_the_residual_random_order_keys_on_the_pair(client, conn):
    conn.canned = {"residual_random": [_residual_row(21, 22), _residual_row(23, 24)]}
    page = client.get(
        "/autodedup/residual", params={"sort": "random", "limit": 1}
    ).json()["data"]
    assert page["sort"] == "random"
    # `lo:hi` with a separator, so 1:23 and 12:3 are two strings rather than one.
    assert page["next_after"] == f"{_md5('21:22v1')}|21|22"
    client.get("/autodedup/residual", params={"sort": "random", "after": page["next_after"]})
    cursor = _last_call(conn, usql.RESIDUAL_RANDOM_SQL)
    assert cursor["after_hash"] == _md5("21:22v1")
    assert (cursor["after_lo"], cursor["after_hi"]) == (21, 22)
    # The score cursor is NOT also set: two orders, one cursor each.
    assert cursor["after_score"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "random", "seed": "Nope"},
        {"sort": "random", "seed": "a b"},
        {"sort": "random", "seed": "x" * 17},
        {"sort": "random", "seed": "semínko"},
    ],
)
def test_a_seed_outside_the_charset_is_refused(client, conn, params):
    assert client.get("/autodedup/groups", params=params).status_code == 400
    assert client.get("/autodedup/residual", params=params).status_code == 400
    assert all(sql != usql.GROUPS_RANDOM_SQL for sql, _ in conn.calls)


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "random", "after": "0.44|101"},
        {"sort": "random", "after": "zzzz|101"},
        {"sort": "random", "after": f"{'a' * 31}|101"},
    ],
)
def test_a_cursor_that_is_not_a_seeded_hash_is_refused(client, conn, params):
    """An arbitrary string reaching `%(after_hash)s::text` is not a 500 — it is worse: it
    compares as itself and pages from somewhere nobody asked for."""
    assert client.get("/autodedup/groups", params=params).status_code == 400


# ------------------------------------------------- the validation session counter (D6)


def _counts(n: int, reviewed: int, not_same: int) -> tuple[Any, ...]:
    return _tuple(usql.VALIDATION_COUNT_COLUMNS, n=n, n_reviewed=reviewed, n_not_same=not_same)


def test_the_session_counter_reports_the_sample_and_the_whole_generation(client, conn):
    conn.canned = {
        "validation_sample": [_counts(100, 37, 2)],
        "validation_total": [_counts(870, 103, 5)],
    }
    body = client.get("/autodedup/validation-progress", params={"surface": "groups"}).json()
    assert body["store_ready"] is True
    data = body["data"]
    assert data["generation"] == "g3"
    assert data["seed"] == "v1"
    assert data["sample_size"] == 100
    assert data["grain"] == "cluster"
    assert data["sample"] == {"n": 100, "n_reviewed": 37, "n_not_same": 2}
    assert data["total"] == {"n": 870, "n_reviewed": 103, "n_not_same": 5}
    params = _last_call(conn, usql.VALIDATION_GROUPS_SAMPLE_SQL)
    assert params["seed"] == "v1"
    assert params["sample_size"] == 100
    assert params["generation"] == "g3"


def test_the_residual_counter_counts_pairs_above_the_floor_it_was_given(client, conn):
    conn.canned = {
        "validation_sample": [_counts(100, 12, 9)],
        "validation_total": [_counts(4200, 61, 40)],
    }
    data = client.get(
        "/autodedup/validation-progress",
        params={"surface": "residual", "min_score": 0.35, "seed": "s2"},
    ).json()["data"]
    assert data["grain"] == "pair"
    assert data["seed"] == "s2"
    params = _last_call(conn, usql.VALIDATION_RESIDUAL_SAMPLE_SQL)
    assert params["min_score"] == 0.35
    assert params["seed"] == "s2"
    # The residual statements were used, never the cluster ones.
    assert all(sql != usql.VALIDATION_GROUPS_SAMPLE_SQL for sql, _ in conn.calls)


@pytest.mark.parametrize(
    "params",
    [{"surface": "blocks"}, {"surface": "groups", "seed": "NO"}, {"nope": 1}],
)
def test_the_counter_refuses_anything_outside_its_registry(client, conn, params):
    assert client.get("/autodedup/validation-progress", params=params).status_code == 400


def test_the_counter_renders_against_an_un_migrated_store(client, conn):
    conn.ready = False
    body = client.get("/autodedup/validation-progress").json()
    assert body == {"data": None, "store_ready": False}
    assert len(conn.calls) == 1


# -------------------------------------------------- operator vs judge (the D6 gate)


def _agree_row(
    lo: int,
    hi: int,
    operator: str,
    judge: str,
    tier: str = "gold",
    source: str = "explicit",
) -> tuple[Any, ...]:
    return _tuple(
        usql.AGREEMENT_COLUMNS,
        listing_lo=lo,
        listing_hi=hi,
        operator_verdict=operator,
        operator_source=source,
        judge_verdict=judge,
        judge_tier=tier,
        judge_model="gpt-5.6-luna",
    )


def test_the_agreement_read_reports_the_gate_the_directions_and_the_bound(client, conn):
    """Hand-computed: 4 comparable pairs, 3 agree. 3/4 = 0.75, and the Wilson 95% interval is
    0.30064–0.95442 — worked through on paper (z = 1.959964, z^2 = 3.841459; denominator
    1 + z^2/4 = 1.960365; centre (0.75 + 0.480182)/1.960365 = 0.627530; half-width
    (z/1.960365) * sqrt(0.046875 + 0.060023) = 0.326886) and pinned here, so a rewrite of the
    maths shows up as a failing number rather than as a different gate."""
    conn.canned = {
        "agreement_pairs": [
            _agree_row(1, 2, "same", "same_property"),
            _agree_row(3, 4, "same", "same_property", source="implied"),
            _agree_row(5, 6, "different", "different_property", tier="vision"),
            _agree_row(7, 8, "same", "different_property", source="implied"),
            # Not a judgement: counted, never scored.
            _agree_row(9, 10, "same", "insufficient_evidence", tier="vision"),
        ],
        "agreement_oversize": [(2,)],
    }
    data = client.get("/autodedup/agreement").json()["data"]
    assert data["generation"] == "g3"
    assert data["overall"]["n"] == 4
    assert data["overall"]["n_agree"] == 3
    assert data["overall"]["agreement"] == pytest.approx(0.75)
    assert data["overall"]["ci_low"] == pytest.approx(0.627530 - 0.326886, abs=1e-5)
    assert data["overall"]["ci_high"] == pytest.approx(0.627530 + 0.326886, abs=1e-5)
    # The two directions, apart.
    assert data["overall"]["n_judge_different_operator_same"] == 1
    assert data["overall"]["n_judge_same_operator_different"] == 0
    # Where the labels came from.
    assert (data["overall"]["n_explicit"], data["overall"]["n_implied"]) == (2, 2)
    assert data["n_insufficient_evidence"] == 1
    gold = next(t for t in data["tiers"] if t["tier"] == "gold")
    assert (gold["n"], gold["n_agree"]) == (3, 2)
    assert data["gate"] == {"tier": "gold", "bar": 0.95, "target_n": 200}
    # The implied expansion's bound, reported rather than assumed away.
    assert data["max_cluster_size"] == routes.AGREEMENT_MAX_CLUSTER_SIZE
    assert data["n_clusters_over_cap"] == 2
    assert [
        (d["listing_lo"], d["listing_hi"]) for d in data["disagreements"]
    ] == [(7, 8)]
    params = _last_call(conn, usql.AGREEMENT_PAIRS_SQL)
    assert params["generation"] == "g3"
    assert params["max_cluster_size"] == routes.AGREEMENT_MAX_CLUSTER_SIZE


def test_the_agreement_statement_unions_explicit_and_implied_pairs(client, conn):
    """The implied half is the reason the number exists at all — 103 confirmed groups carry
    far more pair evidence than the pairs ruled on one at a time. Asserted on the statement,
    since the fake connection cannot execute a CTE."""
    sql = " ".join(usql.AGREEMENT_PAIRS_SQL.split())
    # Only a `same` cluster implies anything; a rejected group says nothing per pair.
    assert "WHERE cf.verdict = 'same'" in sql
    # Both members of one cluster, each pair once (mb > ma), and the cluster must be in the
    # generation the view read.
    assert "mb.listing_id > ma.listing_id" in sql
    assert "c.generation = %(generation)s::text" in sql
    # The explicit verdict WINS: an implied row is dropped when the pair carries one.
    assert "WHERE NOT EXISTS ( SELECT 1 FROM explicit e2" in sql
    # `unsure` is not a label; `oss` is not a judge here; the best tier wins.
    assert "WHERE o.verdict <> 'unsure'" in sql
    assert "j.tier IN ('gold', 'vision', 'text')" in sql
    assert "CASE j.tier WHEN 'gold' THEN 0 WHEN 'vision' THEN 1 ELSE 2 END" in sql
    # The implied expansion is bounded.
    assert "c.size <= %(max_cluster_size)s::int" in sql


def test_the_agreement_read_renders_against_an_un_migrated_store(client, conn):
    conn.ready = False
    assert client.get("/autodedup/agreement").json() == {"data": None, "store_ready": False}


def test_the_agreement_read_refuses_an_unknown_filter(client, conn):
    assert client.get("/autodedup/agreement", params={"tier": "gold"}).status_code == 400


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


def test_group_detail_members_carry_the_title_and_the_WHOLE_scrubbed_description(client, conn):
    """The operator's need: developer units share the photos and the attribute row and differ
    only in what the advert SAYS. So the dialog's members carry the title and the full text —
    scrubbed (E28), and explicitly not cut at the judge's token cap."""
    long_tail = " Klidna lokalita, vlastni parkovani." * 80
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12)],
        "member_text": [
            _member_text(11),
            _member_text(12, description="Byt c. 14 v prizemi." + long_tail),
        ],
        "images": [_image(11, 900, 1, 5)],
        "cluster_pairs": [],
        "judgements": [],
        "cluster_verdicts": [],
        "pair_verdicts": [],
        "conflicts": [],
    }
    members = client.get("/autodedup/groups/101").json()["data"]["members"]
    by_id = {m["listing_id"]: m for m in members}
    # The advert's own headline survives the scrub; the broker signed onto it does not.
    assert by_id[11]["title"] == "Prodej bytu 3+kk 68 m2, byt c. 11 - [jmeno], tel. [telefon]"
    # The discriminating sentence survives; the contact details do not.
    assert by_id[11]["description"].startswith("Byt c. 12 ve 4. patre, orientace na jih, 68 m2.")
    assert "777 123 456" not in by_id[11]["description"]
    assert "jan.novak@example.cz" not in by_id[11]["description"]
    # Longer than the judge's cap and NOT truncated — the reader pays no tokens.
    from autodedup.judge import DESCRIPTION_MAX_CHARS

    assert len(by_id[12]["description"]) > DESCRIPTION_MAX_CHARS
    assert by_id[12]["description"].endswith("vlastni parkovani.")
    assert by_id[12]["description_truncated"] is False
    assert by_id[12]["description_chars"] == len(by_id[12]["description"])
    # One statement, once, over the members of THIS cluster only.
    assert _last_call(conn, usql.MEMBER_TEXT_SQL)["ids"] == [11, 12]


def test_a_member_whose_listing_row_is_gone_still_renders_with_no_text(client, conn):
    """The members statement LEFT JOINs `listings`; a pruned row must not make the key vanish
    and turn "absent" into "this surface does not select it" on the client."""
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11)],
        "member_text": [],
        "images": [],
        "cluster_pairs": [],
        "judgements": [],
        "cluster_verdicts": [],
        "pair_verdicts": [],
        "conflicts": [],
    }
    member = client.get("/autodedup/groups/101").json()["data"]["members"][0]
    assert (member["title"], member["description"]) == (None, None)
    assert member["description_truncated"] is False


def test_the_group_dialog_carries_no_pii(client, conn):
    """E28 over the surface that newly carries advert text, asserted the way the pair view is:
    on the RENDERED body, not on the intent of the code that built it.

    The title is the half this wave added, so it gets the same proof as the description — one
    member, one contact block per string, and BOTH tokens have to come back replaced. Counting
    them is what distinguishes "the scrubber ran on both" from "it ran on the body and the
    headline went out whole", which is the failure mode a `assert phone not in description`
    cannot see."""
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11)],
        "member_text": [_member_text(11)],
        "images": [],
        "cluster_pairs": [],
        "judgements": [],
        "cluster_verdicts": [],
        "pair_verdicts": [],
        "conflicts": [],
    }
    raw = client.get("/autodedup/groups/101").text
    for secret in ("777 123 456", "jan.novak@example.cz", "Jan Novak", "Jana Novakova"):
        assert secret not in raw
    # Two strings went in carrying a phone; two came back with it replaced.
    assert raw.count("[telefon]") == 2
    assert raw.count("[jmeno]") == 2 and raw.count("[email]") == 1
    # And the statement itself never asks for a contact field, in a column or out of raw_json.
    assert "broker" not in usql.MEMBER_TEXT_SQL.lower()
    for name in ("broker_name", "broker_phone", "broker_email"):
        assert name not in usql.MEMBER_TEXT_SQL
        assert name not in usql.MEMBER_TEXT_COLUMNS


def test_the_group_QUEUE_carries_no_advert_text_at_all(client, conn):
    """The cost rule, pinned. 20 cards x N members of TOASTed description to render a photo
    strip nobody opened is why the text lives on the detail path and nowhere else."""
    conn.canned = {
        "groups": [_cluster()],
        "members": [_member(101, 11)],
        "member_text": [_member_text(11)],
        "edges": [],
    }
    body = client.get("/autodedup/groups").json()
    member = body["data"]["items"][0]["members"][0]
    assert "description" not in member and "title" not in member
    # Not merely absent from the payload: the statement never ran.
    assert all(sql != usql.MEMBER_TEXT_SQL for sql, _ in conn.calls)
    assert "777 123 456" not in client.get("/autodedup/groups").text


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


def test_the_pair_digest_is_no_longer_cut_at_the_judges_token_cap(client, conn):
    """The pair page is the deep-dive. The JUDGE keeps its capped digest (that cap is a token
    budget); the operator reading the page pays no tokens and gets the whole scrubbed advert."""
    from autodedup.judge import DESCRIPTION_MAX_CHARS

    long_ad = "Byt c. 14 ve 2. patre. " + "Klidna lokalita, jizni orientace. " * 90
    _pair_evidence(conn)
    conn.canned["listing_detail"] = [
        _listing_detail(11, description=long_ad + " tel. 777 123 456"),
        _listing_detail(12, source="bazos"),
    ]
    digest = client.get("/autodedup/pair/11/12").json()["data"]["digests"]["a"]
    assert len(digest["description"]) > DESCRIPTION_MAX_CHARS
    assert digest["description"].endswith("tel. [telefon]")
    assert digest["description_truncated"] is False
    assert "777 123 456" not in digest["description"]


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


# ------------------------------------------------------- the card gallery (12 frames/member)


def test_a_group_member_carries_its_first_frames_not_only_the_cover(client, conn):
    """The queue card pages the photos itself, so the list statement ships a bounded gallery
    per member. `images[0]` IS the cover — the two read the same order — and `n_images` still
    reports the whole album, which is how the card can offer "12 of 30"."""
    frames = [
        {"image_id": 1, "storage_path": "listings/11/1.jpg",
         "sreality_url": "https://img.sreality.cz/1.jpg", "sequence": 1},
        {"image_id": 2, "storage_path": "listings/11/2.jpg",
         "sreality_url": "https://img.sreality.cz/2.jpg", "sequence": 2},
    ]
    conn.canned = {
        "groups": [_cluster()],
        "members": [_member(101, 11, images=frames), _member(101, 12)],
    }
    members = client.get("/autodedup/groups").json()["data"]["items"][0]["members"]
    assert [f["image_id"] for f in members[0]["images"]] == [1, 2]
    assert members[0]["images"][0]["sreality_url"] == members[0]["cover"]["sreality_url"]
    assert members[0]["n_images"] == 12
    # A member whose lateral found nothing is an EMPTY gallery, never a missing key: the page
    # would otherwise have to tell `undefined` from "this advert has no photos".
    assert members[1]["images"] == []
    assert _last_call(conn, usql.GROUP_MEMBERS_SQL)["card_frames"] == routes.GROUP_CARD_IMAGES


def test_the_card_gallery_is_capped_in_the_statement(client, conn):
    """A cap applied after the fetch still drags a 120-frame album across the wire for every
    member of every cluster on the page."""
    assert "LIMIT %(card_frames)s::int" in usql.GROUP_MEMBERS_SQL
    assert usql.MEMBER_COLUMNS[-1] == "images"


def _frames(listing_id: int, *sequences: int) -> list[dict[str, Any]]:
    return [
        {
            "image_id": listing_id * 100 + seq,
            "storage_path": f"listings/{listing_id}/{seq}.jpg",
            "sreality_url": f"https://img.example.invalid/{listing_id}/{seq}.jpg",
            "sequence": seq,
        }
        for seq in sequences
    ]


def test_both_sides_of_a_residual_row_carry_their_first_frames(client, conn):
    """The operator's request, pinned: a residual row pages its photos exactly like a group
    card. Both sides ship a bounded gallery in the cover's own order, so `images[0]` IS
    `cover` — a second frame order would open the carousel on a photo the tile does not name.
    `n_images` still reports the whole album, which is how the row says "+K in the detail"."""
    a_frames, b_frames = _frames(21, 1, 2, 3), _frames(22, 1, 2)
    conn.canned = {
        "residual": [
            _residual_row(
                a_images=a_frames,
                b_images=b_frames,
                # The statement reads the cover off the SAME `sequence NULLS LAST, id` order,
                # so the row Postgres hands back always agrees with itself — the fixture says
                # so too, or the assertion below would only be testing the fixture.
                a_cover_storage_path=a_frames[0]["storage_path"],
                a_cover_sreality_url=a_frames[0]["sreality_url"],
                b_cover_storage_path=b_frames[0]["storage_path"],
                b_cover_sreality_url=b_frames[0]["sreality_url"],
            )
        ]
    }
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert [f["sequence"] for f in item["a"]["images"]] == [1, 2, 3]
    assert [f["sequence"] for f in item["b"]["images"]] == [1, 2]
    for side in ("a", "b"):
        assert item[side]["images"][0]["sreality_url"] == item[side]["cover"]["sreality_url"]
        # The whole album, not the shipped slice — the "+K fotek v detailu" hint.
        assert item[side]["n_images"] == 8
    # ONE cap for both queues: two numbers would make that hint mean two things.
    assert _last_call(conn, usql.RESIDUAL_SQL)["card_frames"] == routes.GROUP_CARD_IMAGES


def test_a_residual_side_with_no_photos_is_an_empty_gallery_not_a_missing_key(client, conn):
    """The page would otherwise have to tell `undefined` from "this advert has no photos" —
    and it falls back to the labelled cover tile on exactly this shape."""
    conn.canned = {"residual": [_residual_row()]}
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert item["a"]["images"] == [] and item["b"]["images"] == []


def test_the_residual_gallery_is_capped_in_the_statement_in_one_spelling(client, conn):
    """Capped where the group card's is (a post-fetch trim still drags both albums of all 20
    rows across the wire), and spelled ONCE: the two residual orders share the fragment, so
    the random sample can never page a different set of frames from the working queue."""
    for statement in (usql.RESIDUAL_SQL, usql.RESIDUAL_RANDOM_SQL):
        assert usql._RESIDUAL_PHOTOS in statement
        assert statement.count("LIMIT %(card_frames)s::int") == 2
    # Display only: the headline count runs no photo LATERAL, gallery or cover.
    assert "card_frames" not in usql.RESIDUAL_COUNT_SQL
    # The names are the select list's ORDER, so a gallery inserted in the wrong slot would
    # hand the page one side's photos under the other side's cover.
    for side in ("a", "b"):
        at = usql.RESIDUAL_COLUMNS.index(f"{side}_n_images")
        assert usql.RESIDUAL_COLUMNS[at + 1] == f"{side}_images"


def test_the_residual_queue_carries_no_advert_text_even_now_it_carries_photos(client, conn):
    """The cost + PII posture is unchanged by the gallery (E28): photos are engine evidence,
    a TOASTed description per side of 20 rows is not, and no broker column was added."""
    conn.canned = {"residual": [_residual_row(a_images=_frames(21, 1, 2))]}
    item = client.get("/autodedup/residual").json()["data"]["items"][0]
    for side in ("a", "b"):
        assert "description" not in item[side] and "title" not in item[side]
    assert all(sql != usql.MEMBER_TEXT_SQL for sql, _ in conn.calls)
    for column in ("description", "broker", "raw_json", "title"):
        assert column not in usql._RESIDUAL_SELECT


# ----------------------------------------------------------------- the unit split (E49)


def _calls(conn: _FakeConn, sql: str) -> list[dict[str, Any]]:
    return [params or {} for text, params in conn.calls if text == sql]


def _split(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "cluster_key": 101,
        "generation": "g1",
        "units": [
            {"listing_id": 11, "unit": "A"},
            {"listing_id": 12, "unit": "A"},
            {"listing_id": 13, "unit": "B"},
        ],
        "relation": "same_project_different_unit",
    }
    body.update(over)
    return body


@pytest.fixture()
def split_conn(conn: _FakeConn) -> _FakeConn:
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12), _member(101, 13)],
        "verdict_write": [_verdict_row(kind="cluster", cluster_key=101, listing_lo=None,
                                       listing_hi=None, verdict="same_project_different_unit")],
    }
    return conn


def test_a_split_rules_on_every_member_pair(admin_client, split_conn):
    body = admin_client.post("/autodedup/verdict/split", json=_split()).json()
    data = body["data"]
    assert body["store_ready"] is True
    # three members -> three pairs: one inside unit A, two across A|B
    assert (data["n_pairs_same"], data["n_pairs_negative"]) == (1, 2)
    assert data["must_not_link_written"] == 2
    assert data["must_not_link_retracted"] == 1
    pairs = {
        (p["listing_lo"], p["listing_hi"]): p["verdict"]
        for p in _calls(split_conn, usql.VERDICT_PAIR_UPSERT_SQL)
    }
    assert pairs == {
        (11, 12): "same",
        (11, 13): "same_project_different_unit",
        (12, 13): "same_project_different_unit",
    }
    vetoes = {(p["listing_lo"], p["listing_hi"]): p["reason"]
              for p in _calls(split_conn, usql.MUST_NOT_LINK_UPSERT_SQL)}
    assert vetoes == {
        (11, 13): "operator split: same_project_different_unit",
        (12, 13): "operator split: same_project_different_unit",
    }
    # the same-unit pair has its earlier veto (if any) dropped, never left vetoing
    assert [(p["listing_lo"], p["listing_hi"])
            for p in _calls(split_conn, usql.MUST_NOT_LINK_RETRACT_SQL)] == [(11, 12)]


def test_a_split_stores_the_cluster_verdict_with_the_assignment_as_its_note(
    admin_client, split_conn
):
    body = admin_client.post("/autodedup/verdict/split", json=_split()).json()
    assert body["data"]["cluster_verdict"]["kind"] == "cluster"
    written = _last_call(split_conn, usql.VERDICT_CLUSTER_UPSERT_SQL)
    assert written["verdict"] == "same_project_different_unit"
    assert written["note"] == "A: 11,12 | B: 13"
    assert written["decided_by"] == "operator@example.com"


def test_an_operator_note_is_kept_beside_the_assignment(admin_client, split_conn):
    admin_client.post("/autodedup/verdict/split", json=_split(note="one developer, two houses"))
    assert _last_call(split_conn, usql.VERDICT_CLUSTER_UPSERT_SQL)["note"] == (
        "one developer, two houses · A: 11,12 | B: 13"
    )


def test_one_unit_means_the_whole_cluster_is_the_same_property(admin_client, split_conn):
    body = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(units=[{"listing_id": i, "unit": "A"} for i in (11, 12, 13)]),
    ).json()
    assert (body["data"]["n_pairs_same"], body["data"]["n_pairs_negative"]) == (3, 0)
    assert _last_call(split_conn, usql.VERDICT_CLUSTER_UPSERT_SQL)["verdict"] == "same"
    assert all(sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in split_conn.calls)


def test_a_split_lands_in_one_transaction(admin_client, split_conn):
    """A half-applied split would leave the pair rows and the cluster row saying different
    things about the same group."""
    admin_client.post("/autodedup/verdict/split", json=_split())
    assert split_conn.transactions == 1
    written = [sql for sql, _ in split_conn.tx_calls]
    assert written.count(usql.VERDICT_PAIR_UPSERT_SQL) == 3
    assert usql.VERDICT_CLUSTER_UPSERT_SQL in written


def test_a_split_never_asks_whether_the_pair_was_scored(admin_client, split_conn):
    """A cluster is the UNION of accepted edges, so two members can share a cluster with no
    scored edge between them. Membership is the validation; `autodedup.pairs` is not."""
    admin_client.post("/autodedup/verdict/split", json=_split())
    assert all(sql != usql.PAIR_EXISTS_SQL for sql, _ in split_conn.calls)


@pytest.mark.parametrize(
    "over",
    [
        # a member left out of the assignment
        {"units": [{"listing_id": 11, "unit": "A"}, {"listing_id": 12, "unit": "A"}]},
        # a listing that is not in the cluster
        {"units": [{"listing_id": i, "unit": "A"} for i in (11, 12, 13)]
         + [{"listing_id": 99, "unit": "B"}]},
        # the same member twice
        {"units": [{"listing_id": 11, "unit": "A"}, {"listing_id": 11, "unit": "B"},
                   {"listing_id": 12, "unit": "A"}, {"listing_id": 13, "unit": "B"}]},
        # an empty label is not a unit
        {"units": [{"listing_id": 11, "unit": " "}, {"listing_id": 12, "unit": "A"},
                   {"listing_id": 13, "unit": "B"}]},
        # `same` is not a relation between two DIFFERENT units
        {"relation": "same"},
        {"relation": "unsure"},
    ],
)
def test_a_malformed_split_is_refused(admin_client, split_conn, over):
    resp = admin_client.post("/autodedup/verdict/split", json=_split(**over))
    assert resp.status_code == 400
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in split_conn.calls)


def test_more_units_than_letters_is_refused(admin_client, conn):
    ids = list(range(1, 30))
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, i) for i in ids],
    }
    resp = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(units=[{"listing_id": i, "unit": f"U{i}"} for i in ids]),
    )
    assert resp.status_code == 400


def test_a_split_of_a_cluster_that_is_not_in_that_generation_is_a_404(admin_client, conn):
    conn.canned = {"group_one": [], "members": [_member(101, 11)]}
    assert admin_client.post(
        "/autodedup/verdict/split", json=_split(generation="g9")
    ).status_code == 404


def test_a_split_against_a_store_that_does_not_exist_is_refused(admin_client, conn):
    conn.ready = False
    resp = admin_client.post("/autodedup/verdict/split", json=_split())
    assert resp.status_code == 503
    assert len(conn.calls) == 1


def test_a_split_nothing_can_be_attributed_to_is_refused(conn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    try:
        assert TestClient(api_main.app).post(
            "/autodedup/verdict/split", json=_split()
        ).status_code == 403
    finally:
        api_main.app.dependency_overrides.clear()


def test_a_vocabulary_the_store_predates_names_the_migration_instead_of_500ing(
    admin_client, split_conn
):
    """Migration 532 widened `autodedup.verdicts.verdict`. Against a store without it the
    write raises a CHECK violation, and a bare 500 would send the operator to the logs."""
    psycopg_errors = pytest.importorskip("psycopg.errors")
    split_conn.raises[usql.VERDICT_PAIR_UPSERT_SQL] = psycopg_errors.CheckViolation(
        'new row violates check constraint "verdicts_verdict_check"'
    )
    resp = admin_client.post("/autodedup/verdict/split", json=_split())
    assert resp.status_code == 503
    assert "532" in resp.json()["detail"]


def test_the_new_verdict_is_negative_everywhere_it_is_read(admin_client, conn):
    """E49: `same_project_different_unit` writes the permanent must-not-link exactly like
    `different` — an operator who says "different unit" must never see the pair merged."""
    assert "same_project_different_unit" in routes.VERDICT_VALUES
    assert "same_project_different_unit" in routes.NEGATIVE_VERDICTS
    assert "same_project_different_unit" in routes.VERDICT_FILTER_VALUES
    conn.canned = {"pair_exists": [(1,)],
                   "verdict_write": [_verdict_row(verdict="same_project_different_unit")]}
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12,
              "verdict": "same_project_different_unit"},
    ).json()
    assert body["data"]["must_not_link"] is True
    assert _last_call(conn, usql.MUST_NOT_LINK_UPSERT_SQL)["reason"] == (
        "operator: same_project_different_unit"
    )


def test_a_cluster_confirmed_as_one_property_retracts_every_veto_in_it(admin_client, conn):
    """E50. "All of these are one property" and a standing must-not-link between two of them
    are a contradiction: `guards.py` would keep refusing the union while the card showed a
    green badge — the very state MUST_NOT_LINK_RETRACT_SQL exists to prevent."""
    conn.canned = {
        "cluster_exists": [(1,)],
        "verdict_write": [_verdict_row(kind="cluster", cluster_key=101, listing_lo=None,
                                       listing_hi=None, verdict="same")],
        "cluster_member_ids": [(11,), (12,), (13,)],
    }
    body = admin_client.post(
        "/autodedup/verdict", json={"kind": "cluster", "cluster_key": 101, "verdict": "same"}
    ).json()
    assert body["data"]["must_not_link_retracted"] == 3
    assert [(p["listing_lo"], p["listing_hi"])
            for p in _calls(conn, usql.MUST_NOT_LINK_RETRACT_SQL)] == [(11, 12), (11, 13), (12, 13)]


@pytest.mark.parametrize("verdict", ["different", "same_project_different_unit", "unsure"])
def test_a_cluster_verdict_that_is_not_same_retracts_nothing(admin_client, conn, verdict):
    """Flagging a group is not the same statement as un-forbidding each of its pairs."""
    conn.canned = {"cluster_exists": [(1,)],
                   "verdict_write": [_verdict_row(kind="cluster", cluster_key=101,
                                                  listing_lo=None, listing_hi=None)]}
    body = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "cluster", "cluster_key": 101, "verdict": verdict},
    ).json()
    assert body["data"]["must_not_link_retracted"] == 0
    assert all(sql != usql.MUST_NOT_LINK_RETRACT_SQL for sql, _ in conn.calls)


@pytest.mark.parametrize(
    ("payload", "statement"),
    [
        ({"kind": "pair", "listing_lo": 11, "listing_hi": 12,
          "verdict": "same_project_different_unit"}, usql.VERDICT_PAIR_UPSERT_SQL),
        ({"kind": "cluster", "cluster_key": 101,
          "verdict": "same_project_different_unit"}, usql.VERDICT_CLUSTER_UPSERT_SQL),
    ],
)
def test_the_plain_verdict_route_names_the_migration_instead_of_500ing(
    admin_client, conn, payload, statement
):
    """The split route already does this. The same click on the same new value, one button
    over, must not answer with a bare 500 that sends the operator to the logs."""
    psycopg_errors = pytest.importorskip("psycopg.errors")
    conn.canned = {"pair_exists": [(1,)], "cluster_exists": [(1,)]}
    conn.raises[statement] = psycopg_errors.CheckViolation(
        'new row violates check constraint "verdicts_verdict_check"'
    )
    resp = admin_client.post("/autodedup/verdict", json=payload)
    assert resp.status_code == 503
    assert "532" in resp.json()["detail"]
    # Nothing permanent was written off a rejected verdict.
    assert all(sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in conn.calls)


def test_a_split_can_name_a_relation_PER_UNIT_PAIR(admin_client, conn):
    """One relation for a whole split stamps a building onto adverts that do not share one.
    A and B are two units of one building; C is a different building of that development."""
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12), _member(101, 13)],
        "verdict_write": [_verdict_row(kind="cluster", cluster_key=101, listing_lo=None,
                                       listing_hi=None, verdict="same_project_different_unit")],
    }
    body = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(
            units=[{"listing_id": 11, "unit": "A"}, {"listing_id": 12, "unit": "B"},
                   {"listing_id": 13, "unit": "C"}],
            relations=[
                {"unit_a": "A", "unit_b": "B", "relation": "same_building_different_unit"},
                {"unit_a": "B", "unit_b": "C", "relation": "same_project_different_unit"},
            ],
        ),
    ).json()
    pairs = {(p["listing_lo"], p["listing_hi"]): p["verdict"]
             for p in _calls(conn, usql.VERDICT_PAIR_UPSERT_SQL)}
    assert pairs == {
        (11, 12): "same_building_different_unit",
        (12, 13): "same_project_different_unit",
        # unnamed: the body's single `relation` is the fill, never a guess at the others
        (11, 13): "same_project_different_unit",
    }
    assert body["data"]["n_pairs_negative"] == 3
    written = _last_call(conn, usql.VERDICT_CLUSTER_UPSERT_SQL)
    # The cluster carries the WEAKEST claim — the only one true of the whole group — and the
    # note says which pair got which, because the single value cannot.
    assert written["verdict"] == "same_project_different_unit"
    assert written["note"] == (
        "A: 11 | B: 12 | C: 13 · A-B: same_building_different_unit · "
        "A-C: same_project_different_unit · B-C: same_project_different_unit"
    )


def test_the_cluster_takes_the_weakest_relation_the_split_used(admin_client, conn):
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12), _member(101, 13)],
        "verdict_write": [_verdict_row(kind="cluster", cluster_key=101, listing_lo=None,
                                       listing_hi=None, verdict="different")],
    }
    admin_client.post(
        "/autodedup/verdict/split",
        json=_split(
            units=[{"listing_id": 11, "unit": "A"}, {"listing_id": 12, "unit": "B"},
                   {"listing_id": 13, "unit": "C"}],
            relations=[{"unit_a": "A", "unit_b": "B", "relation": "different"}],
        ),
    )
    assert _last_call(conn, usql.VERDICT_CLUSTER_UPSERT_SQL)["verdict"] == "different"


@pytest.mark.parametrize(
    "relations",
    [
        [{"unit_a": "A", "unit_b": "A", "relation": "different"}],
        [{"unit_a": "A", "unit_b": "Z", "relation": "different"}],
        [{"unit_a": "A", "unit_b": "B", "relation": "same"}],
        [{"unit_a": "A", "unit_b": "B", "relation": "different"},
         {"unit_a": "B", "unit_b": "A", "relation": "same_project_different_unit"}],
    ],
)
def test_a_malformed_relation_is_refused(admin_client, split_conn, relations):
    resp = admin_client.post("/autodedup/verdict/split", json=_split(relations=relations))
    assert resp.status_code == 400
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in split_conn.calls)


def test_a_split_that_takes_back_an_earlier_veto_asks_first(admin_client, split_conn):
    """The assignment defaults every unnamed member into one unit, so a blank-slate save over
    a group that was already split would reverse the earlier ruling — permanently, and with
    nothing on any surface to show it. It takes a second, deliberate send."""
    split_conn.canned["member_verdicts"] = [
        _verdict_row(listing_lo=11, listing_hi=12, verdict="different")
    ]
    resp = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(units=[{"listing_id": i, "unit": "A"} for i in (11, 12, 13)]),
    )
    assert resp.status_code == 409
    assert "11-12" in resp.json()["detail"]
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in split_conn.calls)
    assert all(sql != usql.MUST_NOT_LINK_RETRACT_SQL for sql, _ in split_conn.calls)


def test_the_confirmed_split_goes_through_and_names_what_it_took_back(
    admin_client, split_conn
):
    split_conn.canned["member_verdicts"] = [
        _verdict_row(listing_lo=11, listing_hi=12, verdict="different")
    ]
    body = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(units=[{"listing_id": i, "unit": "A"} for i in (11, 12, 13)],
                    confirm_retract=True),
    ).json()
    assert body["data"]["reversed_pairs"] == [[11, 12]]
    assert body["data"]["n_pairs_same"] == 3


def test_a_split_that_agrees_with_what_is_stored_asks_nothing(admin_client, split_conn):
    """Only the destructive direction arms: re-asserting a separation retracts no veto."""
    split_conn.canned["member_verdicts"] = [
        _verdict_row(listing_lo=11, listing_hi=13, verdict="same_project_different_unit"),
        _verdict_row(listing_lo=12, listing_hi=13, verdict="same_project_different_unit"),
    ]
    resp = admin_client.post("/autodedup/verdict/split", json=_split())
    assert resp.status_code == 200
    assert resp.json()["data"]["reversed_pairs"] == []


def test_another_operators_veto_is_not_mine_to_take_back(admin_client, split_conn):
    """The upsert conflicts on `decided_by`: a split rewrites MY rulings, never theirs, so
    theirs cannot be the thing this confirmation is about."""
    split_conn.canned["member_verdicts"] = [
        _verdict_row(listing_lo=11, listing_hi=12, verdict="different",
                     decided_by="someone.else@example.com")
    ]
    resp = admin_client.post(
        "/autodedup/verdict/split",
        json=_split(units=[{"listing_id": i, "unit": "A"} for i in (11, 12, 13)]),
    )
    assert resp.status_code == 200


def test_a_group_card_carries_the_operators_rulings_on_its_members(client, conn):
    """A split that is stored only in the server is a write-only record: the card would show
    "A" over every member and the next save would silently retract its must-not-links."""
    conn.canned = {
        "groups": [_cluster()],
        "members": [_member(101, 11), _member(101, 12)],
        "member_verdicts": [
            _verdict_row(listing_lo=11, listing_hi=12,
                         verdict="same_project_different_unit"),
        ],
    }
    item = client.get("/autodedup/groups").json()["data"]["items"][0]
    assert [(v["listing_lo"], v["listing_hi"], v["verdict"]) for v in item["member_verdicts"]] == [
        (11, 12, "same_project_different_unit")
    ]
    assert _last_call(conn, usql.MEMBER_PAIR_VERDICTS_SQL)["ids"] == [11, 12]


def test_a_group_detail_reads_the_verdicts_of_UNSCORED_member_pairs_too(client, conn):
    """A cluster is a union of edges, so a split rules on pairs `autodedup.pairs` never held.
    Keying the read on the scored edges would hide exactly those rulings."""
    conn.canned = {
        "group_one": [_cluster()],
        "members": [_member(101, 11), _member(101, 12)],
        "cluster_pairs": [],
        "member_verdicts": [_verdict_row(listing_lo=11, listing_hi=12, verdict="same")],
    }
    data = client.get("/autodedup/groups/101").json()["data"]
    assert [v["verdict"] for v in data["member_verdicts"]] == ["same"]
    assert all(sql != usql.PAIR_VERDICTS_SQL for sql, _ in conn.calls)


def test_the_new_verdict_filters_the_group_queue(client, conn):
    client.get("/autodedup/groups", params={"verdict": "same_project_different_unit"})
    assert _last_call(conn, usql.GROUPS_WEAKEST_SQL)["verdict"] == "same_project_different_unit"


# ------------------------------------------------- the operator's REASONS (migration 533)


def test_the_reason_registry_is_served_so_the_page_hard_codes_nothing(client):
    """No connection, no `store_ready`: the vocabulary is code, and the chips have to render
    against a database that has not been migrated at all."""
    body = client.get("/autodedup/verdict-reasons").json()
    codes = [r["code"] for r in body["data"]["reasons"]]
    assert "floor_plan_differs" in codes and codes[-1] == "other"
    assert all(r["label"] for r in body["data"]["reasons"])


def test_a_pair_verdict_stores_its_reasons_and_its_note(admin_client, conn):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    resp = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "different",
              "reasons": ["floor_plan_differs", "unit_number"], "note": "different layout"},
    )
    assert resp.status_code == 200
    written = _last_call(conn, usql.VERDICT_PAIR_UPSERT_SQL)
    assert written["reasons"] == ["floor_plan_differs", "unit_number"]
    assert written["note"] == "different layout"


def test_a_cluster_verdict_stores_its_reasons(admin_client, conn):
    conn.canned = {"cluster_exists": [(1,)],
                   "verdict_write": [_verdict_row(kind="cluster", cluster_key=101,
                                                  listing_lo=None, listing_hi=None)]}
    admin_client.post(
        "/autodedup/verdict",
        json={"kind": "cluster", "cluster_key": 101, "verdict": "same",
              "reasons": ["identical_photos"]},
    )
    assert _last_call(conn, usql.VERDICT_CLUSTER_UPSERT_SQL)["reasons"] == ["identical_photos"]


def test_reasons_are_de_duplicated_and_keep_the_click_order(admin_client, conn):
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "same",
              "reasons": ["broker", "identical_photos", "broker"]},
    )
    assert _last_call(conn, usql.VERDICT_PAIR_UPSERT_SQL)["reasons"] == [
        "broker", "identical_photos"
    ]


def test_an_unknown_reason_is_refused_and_nothing_is_written(admin_client, conn):
    """A chip the operator clicked and the store never kept is worse than no chip at all —
    so an unknown code is the client error it is, before any statement runs."""
    conn.canned = {"pair_exists": [(1,)], "verdict_write": [_verdict_row()]}
    resp = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "same",
              "reasons": ["the_curtains"]},
    )
    assert resp.status_code == 400
    assert "the_curtains" in resp.json()["detail"]
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in conn.calls)


def test_an_unknown_reason_on_a_split_is_refused_too(admin_client, split_conn):
    resp = admin_client.post(
        "/autodedup/verdict/split", json=_split(reasons=["nope"])
    )
    assert resp.status_code == 400
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in split_conn.calls)


def test_a_split_stamps_its_reasons_on_the_cluster_row_ONLY(admin_client, split_conn):
    """A split is ONE ruling, and it lands on the cluster row. Per-pair reasons would claim
    the operator said something about each edge that they never said — and worse, one click
    on a 6-member group would post 15 rows into the pair histogram, which would then measure
    cluster size rather than evidence and stop being comparable with the judge."""
    admin_client.post(
        "/autodedup/verdict/split",
        json=_split(reasons=["floor_plan_differs", "same_project"], note="two buildings"),
    )
    pair_writes = _calls(split_conn, usql.VERDICT_PAIR_UPSERT_SQL)
    assert len(pair_writes) == 3
    assert all(p["reasons"] == [] for p in pair_writes)
    # The fan-out rows still say where they came from, in their machine note.
    assert all(p["note"].startswith("operator split:") for p in pair_writes)
    cluster = _last_call(split_conn, usql.VERDICT_CLUSTER_UPSERT_SQL)
    assert cluster["reasons"] == ["floor_plan_differs", "same_project"]
    # The operator's note still leads the assignment string, as it did before 533.
    assert cluster["note"].startswith("two buildings · A: 11,12 | B: 13")


def test_re_deciding_a_verdict_overwrites_the_reasons_it_carried(admin_client, conn):
    """The upsert is the re-decision path: a DO UPDATE that left `reasons` behind would keep
    yesterday's evidence under today's verdict."""
    assert "reasons = excluded.reasons" in usql.VERDICT_PAIR_UPSERT_SQL
    assert "reasons = excluded.reasons" in usql.VERDICT_CLUSTER_UPSERT_SQL


def test_the_reason_histogram_is_counted_per_grain(client, conn):
    conn.canned = {
        "reason_counts": [
            _tuple(usql.REASON_COUNT_COLUMNS, kind="pair", reason="floor_plan_differs", n=7),
            _tuple(usql.REASON_COUNT_COLUMNS, kind="cluster", reason="same_project", n=2),
        ],
    }
    engine = client.get("/autodedup/stats").json()["data"]["engine"]
    assert engine["verdict_reasons"] == [
        {"kind": "pair", "reason": "floor_plan_differs", "n": 7},
        {"kind": "cluster", "reason": "same_project", "n": 2},
    ]


def test_the_reason_histogram_unnests_the_array_rather_than_grouping_on_it(client, conn):
    """Grouping on the whole array would count `{a,b}` as its own bucket — a histogram of
    combinations, not of reasons."""
    assert "unnest(v.reasons)" in usql.REASON_COUNTS_SQL
    assert "GROUP BY 1, 2" in usql.REASON_COUNTS_SQL


def test_a_verdict_against_a_store_without_533_names_that_migration(admin_client, conn):
    """The 532 guard's sibling. A missing COLUMN is a different SQLSTATE from a rejected
    VALUE, so the two are told apart and each names its own migration."""
    psycopg_errors = pytest.importorskip("psycopg.errors")
    conn.canned = {"pair_exists": [(1,)]}
    conn.raises[usql.VERDICT_PAIR_UPSERT_SQL] = psycopg_errors.UndefinedColumn(
        'column "reasons" of relation "verdicts" does not exist'
    )
    resp = admin_client.post(
        "/autodedup/verdict",
        json={"kind": "pair", "listing_lo": 11, "listing_hi": 12, "verdict": "different"},
    )
    assert resp.status_code == 503
    assert "533" in resp.json()["detail"]
    assert all(sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in conn.calls)


def test_a_split_against_a_store_without_533_names_that_migration(admin_client, split_conn):
    psycopg_errors = pytest.importorskip("psycopg.errors")
    split_conn.raises[usql.VERDICT_PAIR_UPSERT_SQL] = psycopg_errors.UndefinedColumn(
        'column "reasons" of relation "verdicts" does not exist'
    )
    resp = admin_client.post("/autodedup/verdict/split", json=_split())
    assert resp.status_code == 503
    assert "533" in resp.json()["detail"]


def test_the_stats_page_still_renders_against_a_store_without_533(client, conn):
    """A READ degrades where a write refuses: the header strip is not the place to learn
    that a migration is missing."""
    psycopg_errors = pytest.importorskip("psycopg.errors")
    conn.raises[usql.REASON_COUNTS_SQL] = psycopg_errors.UndefinedColumn(
        'column v.reasons does not exist'
    )
    body = client.get("/autodedup/stats").json()
    assert body["data"]["engine"]["verdict_reasons"] == []


@pytest.mark.parametrize(
    "path,statement",
    [
        ("/autodedup/groups", usql.GROUPS_WEAKEST_SQL),
        ("/autodedup/groups/7", usql.GROUP_ONE_SQL),
        ("/autodedup/residual", usql.RESIDUAL_SQL),
        ("/autodedup/pair/11/12", usql.PAIR_ONE_SQL),
    ],
)
def test_a_review_page_against_a_store_without_533_renders_instead_of_500ing(
    client, conn, path: str, statement: str
):
    """`store_ready` asks the catalog for the RELATION, so a store with 528 and not 533
    passes it and then raises UndefinedColumn on the `v.reasons` EVERY review statement
    selects. The write refuses with 503; the read degrades, or one un-applied additive
    migration takes the whole UI down rather than the one new column."""
    psycopg_errors = pytest.importorskip("psycopg.errors")
    conn.raises[statement] = psycopg_errors.UndefinedColumn(
        'column v.reasons does not exist'
    )
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}


def test_a_stored_reason_reaches_every_surface_that_shows_a_verdict(client, conn):
    """The chips are rendered beside the badge on the queue rows too, so the list statements
    carry `reasons` — not only the detail reads."""
    conn.canned = {
        "groups": [
            _cluster(verdict="different", verdict_note="two buildings",
                     verdict_reasons=["same_project"],
                     verdict_decided_by="operator@example.com",
                     verdict_decided_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
        ],
    }
    item = client.get("/autodedup/groups").json()["data"]["items"][0]
    assert item["verdict"]["reasons"] == ["same_project"]
    assert "verdict_reasons" not in item["cluster"]


def test_a_residual_row_carries_the_reasons_of_its_stored_verdict(client, conn):
    conn.canned = {
        "residual": [
            _residual_row(verdict="same", verdict_note="same flat",
                          verdict_reasons=["identical_photos", "price_history"],
                          verdict_decided_by="operator@example.com",
                          verdict_decided_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
        ],
    }
    row = client.get("/autodedup/residual").json()["data"]["items"][0]
    assert row["verdict"]["reasons"] == ["identical_photos", "price_history"]


# ---------------------------------------------------- the candidate groups (§12, E56)
#
# The residual cohort, packed into cards. What is pinned here is the ROUTE's half of E56: the
# cohort statements it reads, one member statement per page (never one per card), the derived
# `reviewed` state, the seeded order, the lock rule on the write, and that no judge artefact
# reaches a card the operator has not ruled on.


@pytest.fixture(autouse=True)
def _drop_candidate_memo():
    """The packing is memoised per (generation, fingerprint) IN PROCESS, so one test's canned
    cohort would otherwise be served to the next under the same generation name."""
    candidate_groups.clear_cache()
    yield
    candidate_groups.clear_cache()


def _candidate_pair(lo: int, hi: int, **over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "listing_lo": lo,
        "listing_hi": hi,
        "score": 0.44,
        "zone": "band",
        "families": 5,  # ATTR | TXT
        "block_key": 500123,
        "block_grain": "o",
    }
    values.update(over)
    return _tuple(usql.CANDIDATE_PAIR_COLUMNS, **values)


def _fingerprint(n_pairs: int = 2, n_locks: int = 3) -> tuple[Any, ...]:
    return _tuple(
        usql.CANDIDATE_FINGERPRINT_COLUMNS,
        n_pairs=n_pairs,
        decided_at=datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc),
        n_locks=n_locks,
    )


def _listing_card(listing_id: int, **over: Any) -> tuple[Any, ...]:
    """The SAME card the groups queue renders, read over listing ids instead of membership."""
    values: dict[str, Any] = {
        "listing_id": listing_id,
        "source": "sreality",
        "source_url": f"https://www.sreality.cz/detail/{listing_id}",
        "category_main": "byt",
        "category_type": "prodej",
        "disposition": "2+kk",
        "area_m2": Decimal("52.0"),
        "floor": 2,
        "price_czk": 5_500_000,
        "first_seen_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "is_active": True,
        "cover_storage_path": f"listings/{listing_id}/1.jpg",
        "cover_sreality_url": f"https://img.example.invalid/{listing_id}.jpg",
        "n_images": 8,
    }
    values.update(over)
    return _tuple(usql.LISTING_CARD_COLUMNS, **values)


def _fan_out_store(conn: _FakeConn) -> _FakeConn:
    """ONE advert against each member of a merged group — the measured shape (§12).

    50 is unclustered; 201/202/203 are the members of g3's cluster 900. Three residual pairs,
    one candidate card, one question.
    """
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=3, n_locks=3)],
        "candidate_pairs": [
            _candidate_pair(50, 201, score=0.55),
            _candidate_pair(50, 202, score=0.48),
            _candidate_pair(50, 203, score=0.41),
        ],
        "candidate_locks": [(900, 201), (900, 202), (900, 203)],
        "listing_cards": [_listing_card(i) for i in (50, 201, 202, 203)],
        "pair_verdict_map": [],
    }
    return conn


def test_candidates_render_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/candidates")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}
    assert len(conn.calls) == 1


def test_a_store_with_no_clustering_is_an_empty_candidate_queue(client, conn):
    """E54: no pass resolves to no generation at all — never to a fabricated name, and never
    to a cohort of every scored pair (every pair is 'unclustered' in a pass that never ran)."""
    conn.latest_generation = None
    data = client.get("/autodedup/candidates").json()["data"]
    assert data["items"] == [] and data["total"] == 0 and data["generation"] is None
    assert all(sql != usql.CANDIDATE_PAIRS_SQL for sql, _ in conn.calls)


def test_the_fan_out_of_one_advert_against_a_group_is_ONE_card(client, conn):
    _fan_out_store(conn)
    data = client.get("/autodedup/candidates").json()["data"]
    assert data["generation"] == "g3"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["size"] == 4
    assert item["n_units"] == 2
    assert item["n_pairs"] == 3
    assert [m["listing_id"] for m in item["members"]] == [50, 201, 202, 203]
    # THE LOCK travels with every member: the three merged adverts carry their cluster key and
    # the lone one carries null, which is what makes the letters lockable on the card.
    assert [m["unit_lock"] for m in item["members"]] == [None, 900, 900, 900]
    assert item["locked_cluster_keys"] == [900]
    assert item["score_min"] == pytest.approx(0.55)
    assert item["sources"] == ["sreality"]


def test_a_candidate_card_carries_the_same_member_shape_the_groups_card_does(client, conn):
    _fan_out_store(conn)
    member = client.get("/autodedup/candidates").json()["data"]["items"][0]["members"][0]
    for key in ("listing_id", "source", "source_url", "category_main", "disposition",
                "area_m2", "floor", "price_czk", "first_seen_at", "last_seen_at",
                "is_active", "n_images", "cover", "images"):
        assert key in member
    # and NOT the advert text: that is the dialog's statement, over one card's members.
    assert "description" not in member and "title" not in member


def test_the_page_reads_its_members_in_ONE_statement(client, conn):
    """The N+1 the groups queue avoids by keying on `any(keys)`: a candidate page spans several
    clusters, so its members cannot come from `cluster_members` — but they still come once."""
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=2, n_locks=0)],
        "candidate_pairs": [_candidate_pair(1, 2, score=0.9), _candidate_pair(3, 4, score=0.8)],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in (1, 2, 3, 4)],
        "pair_verdict_map": [],
    }
    data = client.get("/autodedup/candidates").json()["data"]
    assert len(data["items"]) == 2
    assert sum(1 for sql, _ in conn.calls if sql == usql.LISTING_CARDS_SQL) == 1
    assert _last_call(conn, usql.LISTING_CARDS_SQL)["ids"] == [1, 2, 3, 4]
    assert _last_call(conn, usql.LISTING_CARDS_SQL)["card_frames"] == routes.GROUP_CARD_IMAGES


def test_the_cohort_is_the_pair_queues_own_floor_and_generation(client, conn):
    _fan_out_store(conn)
    client.get("/autodedup/candidates")
    params = _last_call(conn, usql.CANDIDATE_PAIRS_SQL)
    assert params["min_score"] == routes.RESIDUAL_MIN_SCORE
    assert params["generation"] == "g3"
    assert _last_call(conn, usql.CANDIDATE_CLUSTER_MEMBERS_SQL)["generation"] == "g3"


@pytest.mark.parametrize(
    "params",
    [
        {"min_score": 0.3},     # the candidate cohort has ONE floor, so there is no control
        {"source_pair": "bazos+sreality"},
        {"category_main": "byt"},
        {"nonsense": "1"},
    ],
)
def test_an_unknown_candidate_filter_is_refused(client, conn, params):
    assert client.get("/autodedup/candidates", params=params).status_code == 400
    assert conn.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "newest"},
        {"zone": "nonsense"},
        {"verdict": "same"},        # the candidate queue's verdict filter is reviewed/unreviewed
        {"block_grain": "x"},
        {"after": "not-a-key"},
        {"seed": "NOPE"},
    ],
)
def test_a_candidate_filter_value_outside_the_registry_is_refused(client, conn, params):
    assert client.get("/autodedup/candidates", params=params).status_code == 400
    assert conn.calls == []


def test_the_candidate_queue_pages_by_its_own_key(client, conn):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=3, n_locks=0)],
        "candidate_pairs": [
            _candidate_pair(1, 2, score=0.9),
            _candidate_pair(3, 4, score=0.6),
            _candidate_pair(5, 6, score=0.3),
        ],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in range(1, 7)],
        "pair_verdict_map": [],
    }
    first = client.get("/autodedup/candidates", params={"limit": 2}).json()["data"]
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["total"] == 3
    # weakest edge first, exactly like the groups queue's default.
    assert [i["score_min"] for i in first["items"]] == [pytest.approx(0.3), pytest.approx(0.6)]
    assert first["next_after"] == first["items"][-1]["candidate_key"]

    second = client.get(
        "/autodedup/candidates", params={"limit": 2, "after": first["next_after"]}
    ).json()["data"]
    assert len(second["items"]) == 1
    assert second["items"][0]["score_min"] == pytest.approx(0.9)
    assert second["has_more"] is False
    assert second["next_after"] is None
    seen = [i["candidate_key"] for i in first["items"] + second["items"]]
    assert len(seen) == len(set(seen)) == 3


def test_a_cursor_that_names_no_group_of_this_queue_is_refused(client, conn):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=1, n_locks=0)],
        "candidate_pairs": [_candidate_pair(1, 2)],
        "candidate_locks": [],
        "listing_cards": [_listing_card(1), _listing_card(2)],
        "pair_verdict_map": [],
    }
    resp = client.get("/autodedup/candidates", params={"after": "99-0123456789"})
    assert resp.status_code == 400


@pytest.mark.parametrize("sort", ["weakest", "strongest", "largest", "random"])
def test_every_order_is_total_and_stable(client, conn, sort):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=3, n_locks=0)],
        "candidate_pairs": [
            _candidate_pair(1, 2, score=0.9),
            _candidate_pair(3, 4, score=0.6),
            _candidate_pair(5, 6, score=0.3),
        ],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in range(1, 7)],
        "pair_verdict_map": [],
    }
    once = client.get("/autodedup/candidates", params={"sort": sort}).json()["data"]
    twice = client.get("/autodedup/candidates", params={"sort": sort}).json()["data"]
    assert [i["candidate_key"] for i in once["items"]] == [
        i["candidate_key"] for i in twice["items"]
    ]
    assert once["sort"] == sort and once["seed"] == "v1"


def test_a_second_seed_is_a_second_candidate_sample(client, conn):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=4, n_locks=0)],
        "candidate_pairs": [
            _candidate_pair(1, 2, score=0.9),
            _candidate_pair(3, 4, score=0.8),
            _candidate_pair(5, 6, score=0.7),
            _candidate_pair(7, 8, score=0.6),
        ],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in range(1, 9)],
        "pair_verdict_map": [],
    }
    one = client.get(
        "/autodedup/candidates", params={"sort": "random", "seed": "v1"}
    ).json()["data"]
    two = client.get(
        "/autodedup/candidates", params={"sort": "random", "seed": "v2"}
    ).json()["data"]
    assert two["seed"] == "v2"
    assert sorted(i["candidate_key"] for i in one["items"]) == sorted(
        i["candidate_key"] for i in two["items"]
    )
    assert [i["candidate_key"] for i in one["items"]] != [
        i["candidate_key"] for i in two["items"]
    ]


def test_reviewed_is_derived_from_the_pair_verdicts(client, conn):
    """There is no candidate row to carry a verdict, so the card's state is READ OFF the pairs:
    reviewed when every residual pair inside it carries one (by any operator)."""
    _fan_out_store(conn)
    conn.canned["pair_verdict_map"] = [
        _tuple(usql.CANDIDATE_VERDICT_COLUMNS, listing_lo=50, listing_hi=201, verdict="same"),
        _tuple(usql.CANDIDATE_VERDICT_COLUMNS, listing_lo=50, listing_hi=202,
               verdict="different"),
    ]
    item = client.get("/autodedup/candidates").json()["data"]["items"][0]
    assert item["n_pairs"] == 3
    assert item["n_pairs_reviewed"] == 2
    assert item["reviewed"] is False
    assert item["n_pairs_not_same"] == 1
    assert client.get(
        "/autodedup/candidates", params={"verdict": "reviewed"}
    ).json()["data"]["items"] == []
    assert len(
        client.get("/autodedup/candidates", params={"verdict": "unreviewed"})
        .json()["data"]["items"]
    ) == 1


def test_a_fully_ruled_card_is_reviewed(client, conn):
    _fan_out_store(conn)
    conn.canned["pair_verdict_map"] = [
        _tuple(usql.CANDIDATE_VERDICT_COLUMNS, listing_lo=50, listing_hi=hi, verdict="same")
        for hi in (201, 202, 203)
    ]
    data = client.get("/autodedup/candidates", params={"verdict": "reviewed"}).json()["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["reviewed"] is True
    assert data["items"][0]["n_pairs_not_same"] == 0


def test_the_zone_and_block_filters_narrow_the_built_cards(client, conn):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=2, n_locks=0)],
        "candidate_pairs": [
            _candidate_pair(1, 2, score=0.9, zone="band", block_key=500123),
            _candidate_pair(3, 4, score=0.8, zone="reject", block_key=777777),
        ],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in range(1, 5)],
        "pair_verdict_map": [],
    }
    banded = client.get("/autodedup/candidates", params={"zone": "band"}).json()["data"]
    assert len(banded["items"]) == 1 and banded["items"][0]["zones"] == {"band": 1}
    assert banded["total"] == 1
    blocked = client.get(
        "/autodedup/candidates", params={"block": 777777, "block_grain": "o"}
    ).json()["data"]
    assert len(blocked["items"]) == 1 and blocked["items"][0]["block_key"] == 777777
    assert client.get(
        "/autodedup/candidates", params={"block": 777777, "block_grain": "c"}
    ).json()["data"]["items"] == []


def test_the_candidate_queue_carries_no_judge_artefact_at_all(client, conn):
    """Blind mode is the page's default here (E55) and the QUEUE simply has nothing to leak:
    no judge verdict, no confidence, no tier, no has-judgement hint."""
    _fan_out_store(conn)
    item = client.get("/autodedup/candidates").json()["data"]["items"][0]
    flat = json.dumps(item)
    for leak in ("judge", "judgement", "confidence", "tier", "same_property"):
        assert leak not in flat
    assert all(sql != usql.JUDGEMENTS_LATEST_SQL for sql, _ in conn.calls)


def test_the_candidate_queue_carries_no_advert_text(client, conn):
    _fan_out_store(conn)
    client.get("/autodedup/candidates")
    assert all(sql != usql.MEMBER_TEXT_SQL for sql, _ in conn.calls)


def test_a_new_score_run_moves_the_fingerprint_and_the_cards_rebuild(client, conn):
    """The memo must not serve a packing the engine no longer produces."""
    _fan_out_store(conn)
    first = client.get("/autodedup/candidates").json()["data"]
    assert len(first["items"]) == 1
    reads = sum(1 for sql, _ in conn.calls if sql == usql.CANDIDATE_PAIRS_SQL)
    client.get("/autodedup/candidates")
    # unchanged fingerprint -> served from memory, the cohort is not re-read
    assert sum(1 for sql, _ in conn.calls if sql == usql.CANDIDATE_PAIRS_SQL) == reads
    conn.canned["candidate_fingerprint"] = [_fingerprint(n_pairs=4, n_locks=3)]
    conn.canned["candidate_pairs"] = [
        *conn.canned["candidate_pairs"],
        _candidate_pair(70, 71, score=0.6),
    ]
    conn.canned["listing_cards"] = [_listing_card(i) for i in (50, 70, 71, 201, 202, 203)]
    after = client.get("/autodedup/candidates").json()["data"]
    assert len(after["items"]) == 2
    assert sum(1 for sql, _ in conn.calls if sql == usql.CANDIDATE_PAIRS_SQL) == reads + 1


# ------------------------------------------------------------------ one candidate group


def _detail_store(conn: _FakeConn) -> str:
    _fan_out_store(conn)
    key = client_key(conn)
    conn.canned.update(
        {
            "images": [_image(i, i * 10, 1, 1) for i in (50, 201, 202, 203)],
            "member_text": [_member_text(i) for i in (50, 201, 202, 203)],
            "cluster_pairs": [
                _pair_row(50, 201, score=0.55, zone="band", decision="evidence_gate",
                          cluster_key=None),
                _pair_row(201, 202, score=0.93, zone="merge", decision="certificate:K-B",
                          cluster_key=900),
            ],
            "judgements": [_judgement(50, 201)],
            "member_verdicts": [],
        }
    )
    return key


def client_key(conn: _FakeConn) -> str:
    """The key the packing gives the fan-out card — computed, never typed into a fixture."""
    return candidate_groups.candidate_key([50, 201, 202, 203])


def test_candidate_detail_carries_the_text_the_pairs_and_the_judge(client, conn):
    key = _detail_store(conn)
    body = client.get(f"/autodedup/candidates/{key}").json()
    assert body["store_ready"] is True
    data = body["data"]
    assert data["candidate"]["candidate_key"] == key
    assert [m["listing_id"] for m in data["members"]] == [50, 201, 202, 203]
    # the DIALOG's own reason to exist: the advert's words, scrubbed (E28)
    assert data["members"][0]["title"] is not None
    assert "777 123 456" not in json.dumps(data["members"])
    assert "jan.novak@example.cz" not in json.dumps(data["members"])
    # every scored pair among the members, with why it wasn't merged — and which of them are
    # the questions THIS card asks
    by_pair = {(p["listing_lo"], p["listing_hi"]): p for p in data["pairs"]}
    assert by_pair[(50, 201)]["residual"] is True
    assert by_pair[(201, 202)]["residual"] is False
    assert by_pair[(50, 201)]["why_not_merged"]
    assert by_pair[(201, 202)]["certificate"] == "K-B"
    assert data["judgements"][0]["listing_lo"] == 50


def test_candidate_detail_hands_back_the_whole_album_not_the_card_gallery(client, conn):
    key = _detail_store(conn)
    data = client.get(f"/autodedup/candidates/{key}").json()["data"]
    assert any(sql == usql.LISTING_IMAGES_SQL for sql, _ in conn.calls)
    assert data["members"][0]["images"]


def test_an_unknown_candidate_group_is_a_404(client, conn):
    _fan_out_store(conn)
    assert client.get("/autodedup/candidates/99-0123456789").status_code == 404


def test_a_malformed_candidate_key_is_refused_before_it_is_looked_up(client, conn):
    assert client.get("/autodedup/candidates/not-a-key").status_code == 400
    assert conn.calls == []


def test_candidate_detail_renders_when_the_store_does_not_exist(client, conn):
    conn.ready = False
    resp = client.get("/autodedup/candidates/11-0123456789")
    assert resp.status_code == 200
    assert resp.json() == {"data": None, "store_ready": False}


# -------------------------------------------------------------- the candidate split write


def _candidate_split_body(conn: _FakeConn, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "candidate_key": client_key(conn),
        "generation": "g3",
        "units": [
            {"listing_id": 50, "unit": "A"},
            {"listing_id": 201, "unit": "B"},
            {"listing_id": 202, "unit": "B"},
            {"listing_id": 203, "unit": "B"},
        ],
        "relation": "same_building_different_unit",
    }
    body.update(over)
    return body


@pytest.fixture()
def candidate_conn(conn: _FakeConn) -> _FakeConn:
    _fan_out_store(conn)
    conn.canned["generation_exists"] = [(1,)]
    conn.canned["member_verdicts"] = []
    return conn


def test_a_candidate_split_rules_every_pair_that_crosses_the_units(
    admin_client, candidate_conn
):
    body = admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn)
    ).json()
    assert body["store_ready"] is True
    data = body["data"]
    # 50 against each of the three locked adverts; the three pairs INSIDE the lock are not
    # this route's to write.
    assert (data["n_pairs_same"], data["n_pairs_negative"]) == (0, 3)
    assert data["n_pairs_locked"] == 3
    assert data["must_not_link_written"] == 3
    written = {
        (p["listing_lo"], p["listing_hi"]): p["verdict"]
        for p in _calls(candidate_conn, usql.VERDICT_PAIR_UPSERT_SQL)
    }
    assert written == {
        (50, 201): "same_building_different_unit",
        (50, 202): "same_building_different_unit",
        (50, 203): "same_building_different_unit",
    }
    vetoes = {
        (p["listing_lo"], p["listing_hi"])
        for p in _calls(candidate_conn, usql.MUST_NOT_LINK_UPSERT_SQL)
    }
    assert vetoes == {(50, 201), (50, 202), (50, 203)}


def test_a_candidate_split_never_writes_inside_a_lock(admin_client, candidate_conn):
    """The pairs of an already-merged group are the GROUPS page's ruling: this route neither
    confirms them nor retracts the must-not-links a split there wrote."""
    admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn)
    )
    touched = {
        (p["listing_lo"], p["listing_hi"])
        for p in _calls(candidate_conn, usql.VERDICT_PAIR_UPSERT_SQL)
        + _calls(candidate_conn, usql.MUST_NOT_LINK_UPSERT_SQL)
        + _calls(candidate_conn, usql.MUST_NOT_LINK_RETRACT_SQL)
    }
    for inside in ((201, 202), (201, 203), (202, 203)):
        assert inside not in touched


def test_all_one_unit_confirms_every_crossing_pair_and_retracts_its_vetoes(
    admin_client, candidate_conn
):
    body = admin_client.post(
        "/autodedup/verdict/candidate-split",
        json=_candidate_split_body(
            candidate_conn,
            units=[{"listing_id": i, "unit": "A"} for i in (50, 201, 202, 203)],
        ),
    ).json()
    data = body["data"]
    assert (data["n_pairs_same"], data["n_pairs_negative"]) == (3, 0)
    assert data["must_not_link_retracted"] == 3
    assert all(
        sql != usql.MUST_NOT_LINK_UPSERT_SQL for sql, _ in candidate_conn.calls
    )


def test_a_candidate_split_writes_NO_cluster_verdict(admin_client, candidate_conn):
    """There is no cluster: a candidate group is a packing of residual pairs, not a proposal."""
    body = admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn)
    ).json()
    assert body["data"]["cluster_verdict"] is None
    assert all(
        sql != usql.VERDICT_CLUSTER_UPSERT_SQL for sql, _ in candidate_conn.calls
    )


def test_a_candidate_split_lands_in_one_transaction(admin_client, candidate_conn):
    admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn)
    )
    assert candidate_conn.transactions == 1
    assert [sql for sql, _ in candidate_conn.tx_calls].count(
        usql.VERDICT_PAIR_UPSERT_SQL
    ) == 3


def test_splitting_a_locked_group_is_refused_and_says_where_to_do_it(
    admin_client, candidate_conn
):
    resp = admin_client.post(
        "/autodedup/verdict/candidate-split",
        json=_candidate_split_body(
            candidate_conn,
            units=[
                {"listing_id": 50, "unit": "A"},
                {"listing_id": 201, "unit": "B"},
                {"listing_id": 202, "unit": "C"},
                {"listing_id": 203, "unit": "B"},
            ],
        ),
    )
    assert resp.status_code == 400
    assert "Groups page" in resp.json()["detail"]
    assert "900" in resp.json()["detail"]
    assert all(
        sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in candidate_conn.calls
    )


@pytest.mark.parametrize(
    "over",
    [
        {"units": [{"listing_id": 50, "unit": "A"}]},                      # a member left out
        {"units": [{"listing_id": i, "unit": "A"} for i in (50, 201, 202, 203, 99)]},
        {"units": [{"listing_id": 50, "unit": " "},
                   {"listing_id": 201, "unit": "B"},
                   {"listing_id": 202, "unit": "B"},
                   {"listing_id": 203, "unit": "B"}]},
        {"relation": "same"},
        {"candidate_key": "nonsense"},
        {"reasons": ["floor_plan_differs"]},
    ],
)
def test_a_malformed_candidate_split_is_refused(admin_client, candidate_conn, over):
    resp = admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn, **over)
    )
    assert resp.status_code == 400
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in candidate_conn.calls)


def test_reason_chips_are_refused_rather_than_silently_dropped(admin_client, candidate_conn):
    resp = admin_client.post(
        "/autodedup/verdict/candidate-split",
        json=_candidate_split_body(candidate_conn, reasons=["identical_photos"]),
    )
    assert resp.status_code == 400
    assert "note" in resp.json()["detail"]


def test_the_note_rides_on_every_pair_row_with_the_assignment(admin_client, candidate_conn):
    admin_client.post(
        "/autodedup/verdict/candidate-split",
        json=_candidate_split_body(candidate_conn, note="jiny dum stejneho projektu"),
    )
    note = _calls(candidate_conn, usql.VERDICT_PAIR_UPSERT_SQL)[0]["note"]
    assert note.startswith("jiny dum stejneho projektu · operator candidate split: ")
    assert "A: 50 | B: 201,202,203" in note


def test_a_candidate_split_that_takes_back_a_veto_asks_first(admin_client, candidate_conn):
    candidate_conn.canned["member_verdicts"] = [
        _verdict_row(listing_lo=50, listing_hi=201, verdict="different",
                     decided_by="operator@example.com")
    ]
    one_unit = _candidate_split_body(
        candidate_conn, units=[{"listing_id": i, "unit": "A"} for i in (50, 201, 202, 203)]
    )
    resp = admin_client.post("/autodedup/verdict/candidate-split", json=one_unit)
    assert resp.status_code == 409
    assert "50-201" in resp.json()["detail"]
    assert all(sql != usql.VERDICT_PAIR_UPSERT_SQL for sql, _ in candidate_conn.calls)

    confirmed = admin_client.post(
        "/autodedup/verdict/candidate-split", json={**one_unit, "confirm_retract": True}
    ).json()
    assert confirmed["data"]["reversed_pairs"] == [[50, 201]]
    assert confirmed["data"]["n_pairs_same"] == 3


def test_a_candidate_split_in_a_generation_that_never_clustered_is_a_404(
    admin_client, candidate_conn
):
    candidate_conn.canned["generation_exists"] = []
    resp = admin_client.post(
        "/autodedup/verdict/candidate-split",
        json=_candidate_split_body(candidate_conn, generation="g9"),
    )
    assert resp.status_code == 404
    assert all(sql != usql.CANDIDATE_PAIRS_SQL for sql, _ in candidate_conn.calls)


def test_a_candidate_split_against_a_store_that_does_not_exist_is_refused(admin_client, conn):
    key = candidate_groups.candidate_key([50, 201, 202, 203])
    conn.ready = False
    resp = admin_client.post(
        "/autodedup/verdict/candidate-split",
        json={
            "candidate_key": key,
            "generation": "g3",
            "units": [{"listing_id": 50, "unit": "A"}],
            "relation": "different",
        },
    )
    assert resp.status_code == 503
    assert "528" in resp.json()["detail"] or "not created" in resp.json()["detail"]


@pytest.mark.parametrize(
    "method,path",
    [("get", "/autodedup/candidates"), ("post", "/autodedup/verdict/candidate-split")],
)
def test_the_candidate_routes_require_admin(client, conn, method, path):
    """The router carries `require_admin`; this pins that the new routes are inside it."""
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    resp = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert resp.status_code in (401, 403)


# ------------------------------------------------------- the session counter on candidates


def test_the_session_counter_counts_candidate_CARDS(client, conn):
    conn.canned = {
        "candidate_fingerprint": [_fingerprint(n_pairs=3, n_locks=0)],
        "candidate_pairs": [
            _candidate_pair(1, 2, score=0.9),
            _candidate_pair(3, 4, score=0.8),
            _candidate_pair(5, 6, score=0.7),
        ],
        "candidate_locks": [],
        "listing_cards": [_listing_card(i) for i in range(1, 7)],
        "pair_verdict_map": [
            _tuple(usql.CANDIDATE_VERDICT_COLUMNS, listing_lo=1, listing_hi=2, verdict="same"),
            _tuple(usql.CANDIDATE_VERDICT_COLUMNS, listing_lo=3, listing_hi=4,
                   verdict="different"),
        ],
    }
    data = client.get(
        "/autodedup/validation-progress", params={"surface": "candidates"}
    ).json()["data"]
    assert data["grain"] == "candidate"
    assert data["total"] == {"n": 3, "n_reviewed": 2, "n_not_same": 1}
    # the sample is the first 100 of the seeded order over the whole generation — three here
    assert data["sample"]["n"] == 3
    assert data["seed"] == "v1"
    assert data["generation"] == "g3"
    # and it is counted WITHOUT the queue's own statements
    assert all(sql != usql.VALIDATION_RESIDUAL_SAMPLE_SQL for sql, _ in conn.calls)


def test_the_candidate_counter_renders_against_an_un_migrated_store(client, conn):
    conn.ready = False
    body = client.get(
        "/autodedup/validation-progress", params={"surface": "candidates"}
    ).json()
    assert body == {"data": None, "store_ready": False}


# --------------------------------------------- the candidate split feeds the D6 agreement


def test_candidate_split_verdicts_are_explicit_operator_labels_for_the_agreement(
    admin_client, candidate_conn
):
    """D6's agreement read takes EVERY pair verdict as an explicit operator label (§9/E55).

    The candidate split writes through the same `VERDICT_PAIR_UPSERT_SQL` with `kind = 'pair'`
    as the pair queue does, and `AGREEMENT_PAIRS_SQL`'s `explicit` CTE selects those rows with
    no predicate beyond the kind — so a card ruled here counts towards the gate exactly as a
    pair ruled one at a time does. Both halves are asserted, because either one alone would
    let the two drift apart."""
    admin_client.post(
        "/autodedup/verdict/candidate-split", json=_candidate_split_body(candidate_conn)
    )
    writes = _calls(candidate_conn, usql.VERDICT_PAIR_UPSERT_SQL)
    assert writes, "the split wrote no pair verdict"
    # `kind` is written as the literal 'pair' — the same row shape the pair queue writes.
    assert "INSERT INTO autodedup.verdicts (kind," in usql.VERDICT_PAIR_UPSERT_SQL
    assert "VALUES ('pair'," in usql.VERDICT_PAIR_UPSERT_SQL
    explicit = usql.AGREEMENT_PAIRS_SQL[
        usql.AGREEMENT_PAIRS_SQL.index("WITH explicit AS"):
        usql.AGREEMENT_PAIRS_SQL.index("confirmed AS")
    ]
    assert "FROM autodedup.verdicts v" in explicit
    assert "v.kind = 'pair'" in explicit
    # no generation, no cluster, no source: a pair verdict is a pair verdict
    for narrowing in ("generation", "cluster_key", "cluster_members", "decided_by"):
        assert narrowing not in explicit
    assert "'explicit'::text AS source" in usql.AGREEMENT_PAIRS_SQL
