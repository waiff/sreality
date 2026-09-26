"""The rulings page's API (E920): `GET /autodedup/rulings` and the corrections it writes through
`POST /autodedup/verdict` with `supersedes`.

The connection is faked and dispatches on the statement constants, like the other review-page
tests; what the SQL itself returns is executed in `tests/test_verdicts_ledger_live.py`. The
contract asserted here: the filter registry (an unknown key or value is a 400, never an ignored
parameter), the parameters each filter becomes, keyset paging, each row's history, and that a
correction takes its key from the ruling it replaces, is refused while that ruling is no longer
the newest (409), and APPENDS — a withdrawal is an `unsure` row, never a delete of a ruling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from autodedup import apply_sql, incremental_sql, labels_sql
from autodedup import ui_sql as usql
from autodedup.incremental import GENERATION as LIVE
from autodedup.incremental import SEED_VERSION, bootstrap_key, seed_version_key

AT = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)


def _tuple(columns: tuple[str, ...], **values: Any) -> tuple[Any, ...]:
    unknown = sorted(set(values) - set(columns))
    assert not unknown, f"not columns of this statement: {unknown}"
    return tuple(values.get(name) for name in columns)


def _verdict(**over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "id": 7, "kind": "pair", "cluster_key": None, "listing_lo": 11, "listing_hi": 12,
        "verdict": "same", "weight": 1.0, "note": None, "reasons": [],
        "decided_by": "operator@example.com", "decided_at": AT, "generation": None,
        "member_ids": None,
    }
    values.update(over)
    return _tuple(usql.VERDICT_COLUMNS, **values)


def _pair_ruling(**over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "ruling_id": 7, "ruling_kind": "pair", "listing_lo": 11, "listing_hi": 12,
        "verdict": "same", "status": "standing", "source": "pair", "note": None,
        "reasons": [], "decided_by": "operator@example.com", "decided_at": AT, "n_rows": 1,
        "property_lo": 100, "property_hi": 200, "together_now": False, "zone": "reject",
        "score": 0.12, "decision": "auto_reject:attr_contradictions", "guard_veto": None,
        "engine_group_lo": 900, "engine_group_hi": None, "seen_lo": True, "seen_hi": True,
        "engine_view": "apart", "agreement": "disagrees",
    }
    values.update(over)
    return _tuple(usql.RULING_PAIR_COLUMNS, **values)


def _group_ruling(**over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "ruling_key": "31", "ruling_id": 31, "source": "group", "cluster_key": 501,
        "generation": "g4", "member_ids": [501, 502, 503], "set_recorded": True,
        "verdict": "same", "status": "standing", "reasons": ["identical_photos"],
        "decided_by": "operator@example.com", "decided_at": AT, "n_rows": 2, "n_members": 3,
        "property_ids": [40, 41], "n_properties": 2, "together_now": False,
        "n_engine_groups": 1, "n_grouped": 3, "n_seen": 3, "engine_view": "together",
        "agreement": "disagrees",
    }
    values.update(over)
    return _tuple(usql.RULING_GROUP_COLUMNS, **values)


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.calls.append((sql, dict(params or {})))
        if self._conn.open_tx:
            self._conn.tx_calls.append(sql)
        if "to_regclass" in sql:
            self._rows = [(self._conn.ready,)]
        elif sql == usql.LIVE_STREAM_SQL:
            keys = (params or {}).get("keys") or []
            self._rows = [(k, v) for k, v in self._conn.live.items() if k in keys]
        elif sql == usql.LATEST_GENERATION_SQL:
            self._rows = [("g15",)]
        elif sql in self._conn.canned:
            rows = list(self._conn.canned[sql])
            limit = (params or {}).get("limit")
            if sql in (usql.RULINGS_PAIR_SQL, usql.RULINGS_GROUP_SQL) and limit is not None:
                rows = rows[: int(limit)]
            self._rows = rows
        elif sql in (usql.MUST_NOT_LINK_UPSERT_SQL, usql.MUST_NOT_LINK_RETRACT_SQL):
            self._rows = []
        else:
            raise AssertionError(f"unexpected statement: {' '.join(sql.split())[:90]}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Tx:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.open_tx += 1
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self._conn.open_tx -= 1
        return False


class _Conn:
    def __init__(self) -> None:
        self.ready = True
        self.live: dict[str, Any] = {}
        self.canned: dict[str, list[tuple[Any, ...]]] = {
            usql.RULINGS_PAIR_FACETS_SQL: [], usql.RULINGS_GROUP_FACETS_SQL: [],
            usql.RULING_TOWNS_SQL: [], usql.PAIR_VERDICTS_SQL: [],
            usql.GROUP_RULING_HISTORY_SQL: [], usql.RULINGS_PAIR_SQL: [],
            usql.RULINGS_GROUP_SQL: [],
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tx_calls: list[str] = []
        self.open_tx = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def params(self, sql: str) -> dict[str, Any]:
        found = [p for s, p in self.calls if s == sql]
        assert found, f"never ran: {' '.join(sql.split())[:60]}"
        return found[-1]

    def ran(self, sql: str) -> bool:
        return any(s == sql for s, _ in self.calls)


@pytest.fixture()
def conn() -> _Conn:
    return _Conn()


@pytest.fixture()
def client(conn: _Conn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": "operator@example.com", "sub": "uuid-1"}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


# ------------------------------------------------------------------------------- the read


def test_an_unmigrated_store_renders_instead_of_failing(client, conn):
    conn.ready = False
    assert client.get("/autodedup/rulings").json() == {"data": None, "store_ready": False}


@pytest.mark.parametrize("query", [
    {"sort": "newest"},
    {"grain": "cluster"},
    {"verdict": "same_building_different_unit"},
    {"source": "group"},  # a group source asked of the pair grain
    {"grain": "group", "source": "implied"},
    {"status": "retracted"},
    {"engine": "maybe"},
    {"now": "merged"},
    {"town": "563510"},
    {"decided_from": "yesterday"},
    {"merge_group": "not-a-uuid"},
    {"after": "2026-09-26T08:00:00+00:00|11"},
    {"after": "tuesday|11|12"},
])
def test_the_filter_registry_refuses_what_it_does_not_name(client, conn, query):
    assert client.get("/autodedup/rulings", params=query).status_code == 400
    assert not conn.ran(usql.RULINGS_PAIR_SQL)


def test_every_filter_becomes_a_parameter_of_the_one_statement(client, conn):
    group = "6c9f0d8e-1b2a-4c3d-9e8f-001122334455"
    client.get("/autodedup/rulings", params={
        "verdict": "different", "source": "browse_merge", "status": "withdrawn",
        "engine": "disagrees", "now": "together", "decided_from": "2026-09-17",
        "decided_to": "2026-09-20", "town": "c:490245", "listing": 11, "property": 100,
        "merge_group": group.upper(), "generation": "g13"})
    params = conn.params(usql.RULINGS_PAIR_SQL)
    assert {k: params[k] for k in (
        "verdict", "source", "status", "engine", "together", "obec", "cast_obce", "listing",
        "property", "merge_group", "generation")} == {
        "verdict": "different", "source": "browse_merge", "status": "withdrawn",
        "engine": "disagrees", "together": True, "obec": None, "cast_obce": 490245,
        "listing": 11, "property": 100, "merge_group": group, "generation": "g13"}
    assert params["decided_from"].startswith("2026-09-17")
    assert params["decided_to"].startswith("2026-09-20")
    # The counts run the same filter, without the page's cursor.
    facets = conn.params(usql.RULINGS_PAIR_FACETS_SQL)
    assert facets["cast_obce"] == 490245 and "after_at" not in facets


def test_a_blank_filter_is_no_filter(client, conn):
    client.get("/autodedup/rulings", params={"verdict": "", "town": "", "now": ""})
    params = conn.params(usql.RULINGS_PAIR_SQL)
    assert (params["verdict"], params["obec"], params["together"]) == (None, None, None)


def test_the_engine_view_is_the_live_stream_once_it_is_live(client, conn):
    client.get("/autodedup/rulings")
    assert conn.params(usql.RULINGS_PAIR_SQL)["generation"] == "g15"
    conn.live = {seed_version_key(): SEED_VERSION, bootstrap_key(): False}
    body = client.get("/autodedup/rulings").json()
    assert body["data"]["generation"] == LIVE
    assert conn.params(usql.RULINGS_PAIR_SQL)["generation"] == LIVE


def test_a_pair_row_carries_the_engine_view_and_its_own_history(client, conn):
    conn.canned[usql.RULINGS_PAIR_SQL] = [
        _pair_ruling(),
        _pair_ruling(ruling_id=31, ruling_kind="cluster", listing_lo=21, listing_hi=22,
                     source="implied", group_cluster_key=501, group_generation="g4",
                     zone=None, decision=None, decided_at=AT),
    ]
    conn.canned[usql.PAIR_VERDICTS_SQL] = [
        _verdict(id=9, verdict="unsure", decided_at=LATER),
        _verdict(id=7),
        _verdict(id=8, listing_lo=13, listing_hi=14),
    ]
    conn.canned[usql.GROUP_RULING_HISTORY_SQL] = [
        _verdict(id=31, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g4", member_ids=[21, 22]),
        _verdict(id=30, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g5", member_ids=[21, 22, 23]),
    ]
    conn.canned[usql.RULINGS_PAIR_FACETS_SQL] = [
        ("total", None, 2), ("source", "pair", 1), ("source", "implied", 1),
        ("engine", "disagrees", 2),
    ]
    conn.canned[usql.RULING_TOWNS_SQL] = [("o", 563510, "Jablonec nad Nisou", 12)]
    data = client.get("/autodedup/rulings").json()["data"]
    first, implied = data["items"]
    assert first["certificate"] is None
    assert first["why_not_merged"] == "auto-rejected on attr_contradictions"
    assert [v["id"] for v in first["history"]] == [9, 7]
    assert implied["zone"] is None and implied["why_not_merged"] is None
    # An implied pair's history is its GROUP's, in the pass the group was ruled on.
    assert [v["id"] for v in implied["history"]] == [31]
    assert conn.params(usql.GROUP_RULING_HISTORY_SQL)["keys"] == [501]
    assert conn.params(usql.PAIR_VERDICTS_SQL) == {"los": [11, 21], "his": [12, 22]}
    assert data["total"] == 2
    assert data["facets"]["source"] == {"pair": 1, "implied": 1}
    assert data["facets"]["engine"] == {"disagrees": 2}
    assert data["towns"] == [{"grain": "o", "code": 563510, "name": "Jablonec nad Nisou",
                              "n": 12}]
    assert data["next_after"] is None


def test_the_page_is_keyset_paged_on_decided_at_and_the_pair(client, conn):
    conn.canned[usql.RULINGS_PAIR_SQL] = [
        _pair_ruling(listing_lo=11 + i, listing_hi=50 + i) for i in range(3)]
    body = client.get("/autodedup/rulings", params={"limit": 2}).json()["data"]
    assert len(body["items"]) == 2
    assert conn.params(usql.RULINGS_PAIR_SQL)["limit"] == 3
    assert body["next_after"] == f"{AT.isoformat()}|12|51"
    client.get("/autodedup/rulings", params={"after": body["next_after"]})
    params = conn.params(usql.RULINGS_PAIR_SQL)
    assert (params["after_at"], params["after_lo"], params["after_hi"]) == (
        AT.isoformat(), 12, 51)


def test_a_group_row_carries_its_set_and_only_its_own_passes_history(client, conn):
    conn.canned[usql.RULINGS_GROUP_SQL] = [
        _group_ruling(),
        _group_ruling(ruling_key="6c9f0d8e-1b2a-4c3d-9e8f-001122334455", ruling_id=None,
                      source="browse_merge", cluster_key=None, generation=None,
                      member_ids=[601, 602], n_members=2, property_ids=[70]),
    ]
    conn.canned[usql.GROUP_RULING_HISTORY_SQL] = [
        _verdict(id=31, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g4", member_ids=[501, 502, 503]),
        _verdict(id=29, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g3", member_ids=[501, 502]),
    ]
    data = client.get("/autodedup/rulings", params={"grain": "group", "limit": 1}).json()["data"]
    (group,) = data["items"]
    assert group["member_ids"] == [501, 502, 503]
    assert group["property_ids"] == [40, 41]
    assert [v["id"] for v in group["history"]] == [31]
    assert data["next_after"] == f"{AT.isoformat()}|31"
    browse = client.get("/autodedup/rulings", params={"grain": "group"}).json()["data"]["items"][1]
    assert browse["source"] == "browse_merge" and browse["history"] == []


# ----------------------------------------------------------------- the correction (supersedes)


def _post(client: Any, **body: Any) -> Any:
    return client.post("/autodedup/verdict", json=body)


def test_a_correction_of_a_ruling_that_does_not_exist_is_a_404(client, conn):
    conn.canned[usql.VERDICT_ONE_SQL] = []
    resp = _post(client, kind="pair", verdict="unsure", supersedes=99)
    assert resp.status_code == 404
    assert not conn.ran(usql.VERDICT_PAIR_APPEND_SQL)


def test_a_correction_of_a_ruling_that_is_no_longer_the_newest_is_a_409(client, conn):
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7)]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=8, verdict="different")]
    resp = _post(client, kind="pair", verdict="unsure", supersedes=7)
    assert resp.status_code == 409
    assert not conn.ran(usql.VERDICT_PAIR_APPEND_SQL)


@pytest.mark.parametrize("body", [
    {"kind": "cluster", "verdict": "unsure", "supersedes": 7},
    {"kind": "pair", "verdict": "unsure", "supersedes": 7, "listing_lo": 11, "listing_hi": 13},
    {"kind": "pair", "verdict": "unsure", "supersedes": 7, "cluster_key": 3},
])
def test_a_correction_that_contradicts_its_ruling_is_a_400(client, conn, body):
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7)]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=7)]
    assert client.post("/autodedup/verdict", json=body).status_code == 400


def test_a_withdrawal_appends_unsure_and_retracts_the_veto_in_one_transaction(client, conn):
    """No `PAIR_EXISTS_SQL`: the ruling being corrected proves the pair — a Browse merge's or a
    detach's pair the engine never stored is correctable (G10)."""
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(id=12, verdict="unsure",
                                                          note="nevím", decided_at=LATER)]
    body = _post(client, kind="pair", verdict="unsure", note="nevím", supersedes=7).json()
    written = conn.params(usql.VERDICT_PAIR_APPEND_SQL)
    assert (written["listing_lo"], written["listing_hi"], written["verdict"]) == (11, 12, "unsure")
    assert written["decided_by"] == "operator@example.com"
    assert not conn.ran(usql.PAIR_EXISTS_SQL)
    assert conn.ran(usql.MUST_NOT_LINK_RETRACT_SQL)
    assert not conn.ran(usql.MUST_NOT_LINK_UPSERT_SQL)
    assert conn.tx_calls == [usql.VERDICT_PAIR_APPEND_SQL, usql.MUST_NOT_LINK_RETRACT_SQL]
    assert body["data"]["verdict"]["id"] == 12
    assert body["data"]["superseded"]["id"] == 7
    assert body["data"]["must_not_link"] is False


def test_a_flip_to_different_writes_the_veto(client, conn):
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7)]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=7)]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(id=13, verdict="different")]
    body = _post(client, kind="pair", verdict="different", supersedes=7,
                 listing_lo=11, listing_hi=12).json()
    assert body["data"]["must_not_link"] is True
    assert conn.params(usql.MUST_NOT_LINK_UPSERT_SQL)["reason"] == "operator: different"


def test_a_group_correction_copies_its_pass_and_its_set(client, conn):
    """E58: the new word is about the set the old one was about — copied, never re-resolved
    from a clustering that may have moved or been pruned since."""
    old = _verdict(id=31, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                   generation="g4", member_ids=[501, 502, 503], verdict="different")
    conn.canned[usql.VERDICT_ONE_SQL] = [old]
    conn.canned[usql.CLUSTER_NEWEST_RULING_SQL] = [old]
    conn.canned[usql.VERDICT_CLUSTER_APPEND_SQL] = [
        _verdict(id=40, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g4", member_ids=[501, 502, 503], verdict="same")]
    body = _post(client, kind="cluster", verdict="same", supersedes=31).json()
    written = conn.params(usql.VERDICT_CLUSTER_APPEND_SQL)
    assert (written["cluster_key"], written["generation"], written["member_ids"]) == (
        501, "g4", [501, 502, 503])
    assert not conn.ran(usql.CLUSTER_EXISTS_SQL) and not conn.ran(usql.CLUSTER_MEMBER_IDS_SQL)
    # A group `same` retracts the operator's veto on every member pair (E52).
    retracted = [(p["listing_lo"], p["listing_hi"]) for s, p in conn.calls
                 if s == usql.MUST_NOT_LINK_RETRACT_SQL]
    assert retracted == [(501, 502), (501, 503), (502, 503)]
    assert body["data"]["must_not_link_retracted"] == 3


def test_a_legacy_group_ruling_with_no_set_can_still_be_withdrawn(client, conn):
    old = _verdict(id=5, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=77,
                   generation=None, member_ids=None, verdict="different")
    conn.canned[usql.VERDICT_ONE_SQL] = [old]
    conn.canned[usql.CLUSTER_NEWEST_RULING_SQL] = [old]
    conn.canned[usql.VERDICT_CLUSTER_APPEND_SQL] = [old]
    assert _post(client, kind="cluster", verdict="unsure", supersedes=5).status_code == 200
    written = conn.params(usql.VERDICT_CLUSTER_APPEND_SQL)
    assert (written["generation"], written["member_ids"]) == (None, None)


def test_a_pair_the_engine_never_stored_can_be_ruled_when_anything_was_said_about_it(client,
                                                                                    conn):
    """G10: `PAIR_EXISTS_SQL` asks the rulings and the vetoes as well as the scored pairs."""
    conn.canned[usql.PAIR_EXISTS_SQL] = [(1,)]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(verdict="same")]
    assert _post(client, kind="pair", verdict="same", listing_lo=11,
                 listing_hi=12).status_code == 200
    flat = " ".join(usql.PAIR_EXISTS_SQL.split())
    for table in ("autodedup.verdicts", "autodedup.must_not_link", "autodedup.pairs"):
        assert f"FROM {table}" in flat


# ------------------------------------------------------ newest wins, at every reader (the lane)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.parametrize("sql", [
    incremental_sql.RT_MUST_LINK_SQL,
    apply_sql.PAIR_VERDICTS_SQL,
    labels_sql.PAIR_VERDICTS_SQL,
    usql.OPERATOR_PAIR_VERDICTS_SQL,
], ids=["lane must-links", "apply negatives", "labels", "candidate queue"])
def test_every_pair_reader_takes_the_newest_row_of_each_pair(sql):
    """A flip or a withdrawal is picked up with no code change: each reader keeps the NEWEST row
    per pair (`decided_at desc, id desc`) across every decider, then filters on its verdict — so
    a withdrawn `same` stops being a must-link and a flipped `different` becomes a negative."""
    flat = _flat(sql).lower()
    assert "distinct on (x.listing_lo, x.listing_hi)" in flat or \
        "distinct on (v.listing_lo, v.listing_hi)" in flat
    assert "decided_at desc, x.id desc" in flat or "decided_at desc, v.id desc" in flat
    assert "decided_by" not in flat.split("order by")[1]


def test_the_lane_binds_only_a_pair_whose_newest_word_is_same():
    flat = _flat(incremental_sql.RT_MUST_LINK_SQL)
    assert flat.endswith("where v.verdict = 'same'")


def test_the_writes_order_newest_exactly_as_the_readers_do():
    for sql in (usql.VERDICT_PAIR_APPEND_SQL, usql.VERDICT_CLUSTER_APPEND_SQL,
                usql.PAIR_NEWEST_RULING_SQL, usql.CLUSTER_NEWEST_RULING_SQL):
        assert "ORDER BY x.decided_at DESC, x.id DESC" in _flat(sql) or \
            "ORDER BY v.decided_at DESC, v.id DESC" in _flat(sql)


def test_the_progress_counts_count_rulings_not_rows():
    """G8: with history kept, a count over rows would count a withdrawn ruling twice."""
    for sql in (usql.VERDICT_COUNTS_SQL, usql.REASON_COUNTS_SQL):
        flat = _flat(sql)
        assert "SELECT DISTINCT ON (x.kind, x.listing_lo, x.listing_hi, x.cluster_key," in flat
        assert "x.decided_at DESC, x.id DESC" in flat
