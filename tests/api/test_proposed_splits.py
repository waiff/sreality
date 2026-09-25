"""GET /autodedup/proposed-splits[/{property_id}] — Decision 9: engine splits are propose-only.

The connection is faked and dispatches on the statement (the adapter's and the review pages' own
constants, reused). Live properties: 10 is split by the generation (two groups, and one advert it
never saw), 20 is one group, 30 carries a must-not-link, 40 has an unseen advert beside one group.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from autodedup import apply_sql as asql
from autodedup import ui_sql as usql
from toolkit import property_identity as pi

AT = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
ADVERTS = [  # property, listing, source, active, canonical, cluster, seen
    (10, 101, "sreality", True, 101, 900, True), (10, 102, "idnes", True, 101, 900, True),
    (10, 103, "bazos", True, 101, None, True), (10, 104, "remax", True, 101, None, False),
    (20, 201, "sreality", True, 201, 950, True), (20, 202, "idnes", True, 201, 950, True),
    (30, 301, "sreality", True, 301, None, True), (30, 302, "idnes", False, 301, None, False),
    (40, 401, "sreality", True, 401, 960, True), (40, 402, "idnes", True, 401, None, False),
]


def _row(columns: tuple[str, ...], **values: Any) -> tuple[Any, ...]:
    return tuple(values.get(c) for c in columns)


CANNED = {
    asql.MUST_NOT_LINK_SQL: [(301, 302, "operator")],
    usql.MEMBER_PAIR_VERDICTS_SQL: [_row(
        usql.VERDICT_COLUMNS, listing_lo=101, listing_hi=103, verdict="different",
        note="other floor", reasons=["floor"], decided_by="op@example.com", decided_at=AT)],
    usql.CLUSTER_CONFLICTS_SQL: [
        _row(usql.CONFLICT_COLUMNS, kind="invariant", listing_lo=102, listing_hi=103,
             invariant="floor_spread", detail={"generation": "g12"}),
        _row(usql.CONFLICT_COLUMNS, kind="invariant", listing_lo=101, listing_hi=103,
             invariant="size", detail={"generation": "g11"})],
    usql.CLUSTER_PAIRS_SQL: [
        _row(usql.PAIR_COLUMNS, listing_lo=101, listing_hi=103, zone="reject",
             decision="auto_reject:area"),
        _row(usql.PAIR_COLUMNS, listing_lo=102, listing_hi=103, zone="veto", guard_veto="floor"),
        _row(usql.PAIR_COLUMNS, listing_lo=101, listing_hi=102, zone="merge",
             decision="certificate:K-A")],
    pi._LIVE_MOVES_SQL: [(103, 5, "grp", 10, 13, "operator", AT)],
    usql.LATEST_GENERATION_SQL: [("g12",)],
}


class _Conn:
    def __init__(self) -> None:
        self.ready, self.calls = True, []

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        if "to_regclass" in sql:
            self.rows = [(self.ready,)]
        elif sql == usql.PROPOSED_SPLIT_ADVERTS_SQL:
            self.rows = [r for r in ADVERTS if params["property_id"] in (None, r[0])]
        else:
            self.rows = CANNED[sql]

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


@pytest.fixture()
def conn() -> _Conn:
    return _Conn()


@pytest.fixture()
def client(conn: _Conn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def _advert(lid: int, source: str, origin: int | None = None) -> dict[str, Any]:
    return {"listing_id": lid, "source": source, "is_active": True, "origin_property_id": origin}


def test_the_list_is_every_split_the_latest_generation_proposes(client, conn):
    data = client.get("/autodedup/proposed-splits").json()["data"]
    assert (data["generation"], data["total"], data["next_after"]) == ("g12", 2, None)
    first, second = data["items"]
    assert first["property_id"] == 10 and first["canonical_listing_id"] == 101
    assert first["groups"] == [
        {"cluster_key": 900, "adverts": [_advert(101, "sreality"), _advert(102, "idnes")]},
        {"cluster_key": None, "adverts": [_advert(103, "bazos", origin=13)]}]
    assert first["unseen"] == [_advert(104, "remax")]
    assert first["splits"] == [
        {"listing_lo": 101, "listing_hi": 103, "reason_source": "pair",
         "reason": "reject: auto_reject:area",
         "ruling": {"verdict": "different", "decided_by": "op@example.com", "note": "other floor",
                    "decided_at": AT.isoformat(), "reasons": ["floor"]}},
        {"listing_lo": 102, "listing_hi": 103, "reason_source": "conflict",
         "reason": "invariant: floor_spread", "ruling": None}]
    assert (first["proposed"], first["ruled"]) == (True, False)
    assert second["property_id"] == 30 and second["splits"] == [
        {"listing_lo": 301, "listing_hi": 302, "reason_source": "must_not_link",
         "reason": "must_not_link (operator)", "ruling": None}]
    assert not any(s.lstrip().lower().startswith(("insert", "update", "delete"))
                   for s, _p in conn.calls), "the proposals are read-only"


def test_the_list_pages_by_property_id(client):
    page = client.get("/autodedup/proposed-splits?limit=1").json()["data"]
    assert [i["property_id"] for i in page["items"]] == [10] and page["next_after"] == 10
    rest = client.get("/autodedup/proposed-splits?limit=1&after=10").json()["data"]
    assert [i["property_id"] for i in rest["items"]] == [30] and rest["next_after"] is None


def test_one_property_is_the_generations_view_of_it_proposal_or_not(client):
    one = client.get("/autodedup/proposed-splits/20?generation=g12").json()["data"]
    assert (one["generation"], one["property_id"], one["proposed"]) == ("g12", 20, False)
    assert [[a["listing_id"] for a in g["adverts"]] for g in one["groups"]] == [[201, 202]]
    unseen = client.get("/autodedup/proposed-splits/40").json()["data"]
    assert unseen["proposed"] is False and unseen["unseen"] == [_advert(402, "idnes")]
    assert client.get("/autodedup/proposed-splits/99").status_code == 404


def test_an_unmigrated_store_renders_and_unknown_filters_are_refused(client, conn):
    assert client.get("/autodedup/proposed-splits?block=1").status_code == 400
    conn.ready = False
    assert client.get("/autodedup/proposed-splits").json() == {"data": None, "store_ready": False}
