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


# `PAIR_CATEGORIES_SQL`'s row: (category_type, category_main) of listing_lo, then listing_hi.
_FLATS_FOR_SALE = ("prodej", "byt", "prodej", "byt")


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
        elif sql in (usql.MUST_NOT_LINK_UPSERT_SQL, usql.MUST_NOT_LINK_RETRACT_SQL,
                     usql.VERDICT_PAIR_FROM_VETO_SQL):
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
            usql.RULINGS_GROUP_SQL: [], usql.DISSOLVED_CLOSURES_SQL: [],
            usql.PAIR_CATEGORIES_SQL: [_FLATS_FOR_SALE],
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
    assert first["why_not_merged"] == "automaticky zamítnuto: attr_contradictions"
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


def test_a_same_the_engine_dissolved_says_why_it_is_not_honoured(client, conn):
    """E926: the operator's `same` rulings form a closure the invariants refused (here a sale
    ruled one flat with a rental), so the lane holds the adverts apart. The page names that,
    not the stored pair's zone; a ruling whose closure was honoured, or that is not standing,
    reads the record of nothing."""
    conn.live = {seed_version_key(): SEED_VERSION, bootstrap_key(): False}
    conn.canned[usql.RULINGS_PAIR_SQL] = [
        _pair_ruling(zone=None, decision=None),
        _pair_ruling(ruling_id=8, listing_lo=13, listing_hi=14, zone="merge",
                     decision="model"),
        _pair_ruling(ruling_id=9, listing_lo=11, listing_hi=15, status="withdrawn",
                     verdict="unsure"),
    ]
    conn.canned[usql.DISSOLVED_CLOSURES_SQL] = [
        _tuple(usql.CONFLICT_COLUMNS, id=3, kind="invariant", listing_lo=11, listing_hi=12,
               invariant="category_type", created_at=AT,
               detail={"generation": LIVE, "members": [11, 12],
                       "must_link": [[11, 12]]}),
    ]
    first, other, withdrawn = client.get("/autodedup/rulings").json()["data"]["items"]
    assert first["why_not_merged"] == (
        "vaše rozhodnutí „stejné“ spojují prodej s pronájmem, což pevné pravidlo nedovolí, "
        "proto engine skupinu nesloučil (rozhodnutí „stejné“ v ní, celkem 1: 11–12)")
    assert other["why_not_merged"] == (
        "dvojice prošla, ale žádná skupina této generace nedrží oba inzeráty"), (
        "no record names 13 and 14: the stored pair's own reason stands")
    assert withdrawn["why_not_merged"] == "automaticky zamítnuto: attr_contradictions"
    assert conn.params(usql.DISSOLVED_CLOSURES_SQL) == {"generation": LIVE,
                                                        "ids": [11, 12, 13, 14]}


@pytest.mark.parametrize(("limb", "why"), [
    ("category_type", "spojují prodej s pronájmem, což pevné pravidlo nedovolí"),
    ("compat_class", "spojují neslučitelné druhy nemovitostí (např. byt a komerční prostor), "
                     "což pevné pravidlo nedovolí"),
    ("size", "spojují skupinu o 7 inzerátech, větší, než pevné pravidlo dovolí"),
    ("must_not_link", "odporují vašemu vlastnímu rozhodnutí „různé“ uvnitř téže skupiny"),
])
def test_each_limb_that_dissolves_a_closure_is_said_in_words(client, conn, limb, why):
    """E926: only four limbs can refuse a closure. Three are the engine's fixed rules; a
    must-not-link is the operator's own `different` inside it, and the page says so, not "a fixed
    rule". Every reason names the `same` rulings that form the closure — the ones to revisit —
    up to five of them, and how many there are."""
    conn.live = {seed_version_key(): SEED_VERSION, bootstrap_key(): False}
    conn.canned[usql.RULINGS_PAIR_SQL] = [_pair_ruling(zone=None, decision=None)]
    members = [11, 12, 13, 14, 15, 16, 17]
    conn.canned[usql.DISSOLVED_CLOSURES_SQL] = [
        _tuple(usql.CONFLICT_COLUMNS, id=3, kind="invariant", listing_lo=11, listing_hi=17,
               invariant=limb, created_at=AT,
               detail={"generation": LIVE, "members": members,
                       "must_link": [[lo, lo + 1] for lo in members[:-1]]}),
    ]
    (item,) = client.get("/autodedup/rulings").json()["data"]["items"]
    assert item["why_not_merged"] == (
        f"vaše rozhodnutí „stejné“ {why}, proto engine skupinu nesloučil (rozhodnutí „stejné“ "
        "v ní, celkem 6: 11–12, 12–13, 13–14, 14–15, 15–16 …)"), "no engine code, in words"


def test_no_standing_same_apart_reads_no_dissolved_closure(client, conn):
    conn.canned[usql.RULINGS_PAIR_SQL] = [_pair_ruling(verdict="different"),
                                          _pair_ruling(engine_view="together")]
    client.get("/autodedup/rulings")
    assert not conn.ran(usql.DISSOLVED_CLOSURES_SQL)


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
    """The ruling being corrected names the pair — a Browse merge's or a detach's pair the engine
    never stored is correctable (G10) — and its categories are read inside the write (E925)."""
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(id=12, verdict="unsure",
                                                          note="nevím", decided_at=LATER)]
    body = _post(client, kind="pair", verdict="unsure", note="nevím", supersedes=7).json()
    written = conn.params(usql.VERDICT_PAIR_APPEND_SQL)
    assert (written["listing_lo"], written["listing_hi"], written["verdict"]) == (11, 12, "unsure")
    assert written["decided_by"] == "operator@example.com"
    assert conn.params(usql.PAIR_CATEGORIES_SQL) == {"listing_lo": 11, "listing_hi": 12}
    assert conn.ran(usql.MUST_NOT_LINK_RETRACT_SQL)
    assert not conn.ran(usql.MUST_NOT_LINK_UPSERT_SQL)
    # The "still the newest" check runs over the LOCKED ruling, in the write's own transaction.
    assert conn.tx_calls == [usql.VERDICT_ONE_SQL, usql.PAIR_NEWEST_RULING_SQL,
                             usql.PAIR_CATEGORIES_SQL, usql.VERDICT_PAIR_FROM_VETO_SQL,
                             usql.VERDICT_PAIR_APPEND_SQL, usql.MUST_NOT_LINK_RETRACT_SQL]
    assert _flat(usql.VERDICT_ONE_SQL).endswith("FOR UPDATE")
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
    conn.canned[usql.CLUSTER_SET_NEWEST_RULING_SQL] = [old]
    conn.canned[usql.VERDICT_CLUSTER_APPEND_SQL] = [
        _verdict(id=40, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                 generation="g4", member_ids=[501, 502, 503], verdict="same")]
    body = _post(client, kind="cluster", verdict="same", supersedes=31).json()
    written = conn.params(usql.VERDICT_CLUSTER_APPEND_SQL)
    assert (written["cluster_key"], written["generation"], written["member_ids"]) == (
        501, "g4", [501, 502, 503])
    assert conn.params(usql.CLUSTER_SET_NEWEST_RULING_SQL) == {"member_ids": [501, 502, 503]}
    assert conn.tx_calls[:3] == [usql.VERDICT_ONE_SQL, usql.CLUSTER_NEWEST_RULING_SQL,
                                 usql.CLUSTER_SET_NEWEST_RULING_SQL]
    assert not conn.ran(usql.CLUSTER_EXISTS_SQL) and not conn.ran(usql.CLUSTER_MEMBER_IDS_SQL)
    # A group `same` retracts the operator's veto on every member pair (E52).
    retracted = [(p["listing_lo"], p["listing_hi"]) for s, p in conn.calls
                 if s == usql.MUST_NOT_LINK_RETRACT_SQL]
    assert retracted == [(501, 502), (501, 503), (502, 503)]
    assert body["data"]["must_not_link_retracted"] == 3


def test_a_group_ruling_a_newer_ruling_on_its_set_outranks_is_a_409(client, conn):
    """E920 iii: apply reads the newest word per SET, under whichever key or pass. A g4 `same`
    on {501, 502} that a g13 `different` on the same adverts outranks is no longer the word to
    correct, though it is still the newest under its own key."""
    old = _verdict(id=31, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=501,
                   generation="g4", member_ids=[501, 502], verdict="same")
    newer = _verdict(id=44, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=502,
                     generation="g13", member_ids=[502, 501], verdict="different",
                     decided_at=LATER)
    conn.canned[usql.VERDICT_ONE_SQL] = [old]
    conn.canned[usql.CLUSTER_NEWEST_RULING_SQL] = [old]
    conn.canned[usql.CLUSTER_SET_NEWEST_RULING_SQL] = [newer]
    resp = _post(client, kind="cluster", verdict="different", supersedes=31)
    assert resp.status_code == 409
    assert not conn.ran(usql.VERDICT_CLUSTER_APPEND_SQL)


def test_superseded_is_a_status_the_filter_names(client, conn):
    assert client.get("/autodedup/rulings",
                      params={"grain": "group", "status": "superseded"}).status_code == 200
    assert conn.params(usql.RULINGS_GROUP_SQL)["status"] == "superseded"
    flat = _flat(usql.RULINGS_GROUP_SQL)
    assert "THEN 'superseded'" in flat and "LEFT JOIN set_newest sn ON sn.member_set" in flat
    # The implied pairs come out of the newest ruling per SET, across keys and passes.
    pair = _flat(usql.RULINGS_PAIR_SQL)
    assert "SELECT DISTINCT ON (s.member_set)" in pair and "FROM set_rulings g" in pair


def test_a_legacy_group_ruling_with_no_set_can_still_be_withdrawn(client, conn):
    old = _verdict(id=5, kind="cluster", listing_lo=None, listing_hi=None, cluster_key=77,
                   generation=None, member_ids=None, verdict="different")
    conn.canned[usql.VERDICT_ONE_SQL] = [old]
    conn.canned[usql.CLUSTER_NEWEST_RULING_SQL] = [old]
    conn.canned[usql.VERDICT_CLUSTER_APPEND_SQL] = [old]
    assert _post(client, kind="cluster", verdict="unsure", supersedes=5).status_code == 200
    written = conn.params(usql.VERDICT_CLUSTER_APPEND_SQL)
    assert (written["generation"], written["member_ids"]) == (None, None)


def test_any_two_adverts_that_exist_can_be_ruled(client, conn):
    """E924: the guard asks whether both ADVERTS exist, never whether the engine stored the pair
    — a duplicate the engine missed has no row, and it is the ruling the operator must give."""
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(verdict="same")]
    assert _post(client, kind="pair", verdict="same", listing_lo=11,
                 listing_hi=12).status_code == 200
    assert conn.params(usql.PAIR_CATEGORIES_SQL) == {"listing_lo": 11, "listing_hi": 12}
    flat = " ".join(usql.PAIR_CATEGORIES_SQL.split())
    assert "FROM public.listings" in flat
    for table in ("autodedup.verdicts", "autodedup.must_not_link", "autodedup.pairs"):
        assert table not in flat


def test_a_pair_naming_an_advert_that_does_not_exist_is_a_404_and_writes_nothing(client, conn):
    conn.canned[usql.PAIR_CATEGORIES_SQL] = []
    response = _post(client, kind="pair", verdict="same", listing_lo=11, listing_hi=12)
    assert response.status_code == 404
    assert response.json()["detail"] == "one of the two adverts does not exist"
    assert not conn.ran(usql.VERDICT_PAIR_APPEND_SQL)
    assert not conn.ran(usql.MUST_NOT_LINK_UPSERT_SQL)


# ------------------------------------ a `same` rule 15 forbids is refused at write time (E925)

_NOTHING_WRITTEN = (usql.VERDICT_PAIR_FROM_VETO_SQL, usql.VERDICT_PAIR_APPEND_SQL,
                    usql.MUST_NOT_LINK_UPSERT_SQL, usql.MUST_NOT_LINK_RETRACT_SQL)


@pytest.mark.parametrize(("sides", "named"), [
    (("pronajem", "byt", "prodej", "byt"), ("Inzerát typu Pronájem", "typu Prodej")),
    (("prodej", "byt", "prodej", "komercni"), ("v kategorii Byty", "v kategorii Komerční")),
    (("prodej", "komercni", "prodej", "byt"), ("v kategorii Komerční", "v kategorii Byty")),
    (("prodej", "byt", "prodej", "pozemek"), ("v kategorii Byty", "v kategorii Pozemky")),
    (("prodej", "pozemek", "prodej", "ostatni"), ("v kategorii Pozemky", "v kategorii Ostatní")),
], ids=["rent vs sale", "flat vs commercial", "commercial vs flat", "flat vs land",
        "land vs other"])
def test_a_same_between_two_properties_is_a_422_in_czech_and_writes_nothing(
        client, conn, sides, named):
    """422, never 409: the rulings page reads every 409 as "ruled again since the page loaded"
    and swaps the detail for that text."""
    conn.canned[usql.PAIR_CATEGORIES_SQL] = [sides]
    response = _post(client, kind="pair", verdict="same", listing_lo=11, listing_hi=12)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert all(part in detail for part in named)
    assert "systém nikdy nespojí" in detail and "nelze označit jako stejné" in detail
    assert not any(conn.ran(sql) for sql in _NOTHING_WRITTEN)


@pytest.mark.parametrize("sides", [
    ("prodej", "dum", "prodej", "komercni"),  # rule 15's cross-types: dům, komerční, pozemek
    ("prodej", "dum", "prodej", "pozemek"),
    ("prodej", "pozemek", "prodej", "komercni"),
    ("prodej", "byt", "prodej", "byt"),
    (None, "byt", "prodej", None),  # unknown is never a conflict
], ids=["house vs commercial", "house vs land", "land vs commercial", "flat vs flat", "unknown"])
def test_a_same_rule_15_allows_is_written(client, conn, sides):
    conn.canned[usql.PAIR_CATEGORIES_SQL] = [sides]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(verdict="same")]
    response = _post(client, kind="pair", verdict="same", listing_lo=11, listing_hi=12)
    assert response.status_code == 200
    assert conn.params(usql.VERDICT_PAIR_APPEND_SQL)["verdict"] == "same"


@pytest.mark.parametrize("verdict", ["different", "same_building_different_unit", "unsure"])
def test_only_same_is_refused_a_negative_between_two_properties_is_consistent(
        client, conn, verdict):
    conn.canned[usql.PAIR_CATEGORIES_SQL] = [("pronajem", "byt", "prodej", "komercni")]
    conn.canned[usql.VERDICT_PAIR_APPEND_SQL] = [_verdict(verdict=verdict)]
    response = _post(client, kind="pair", verdict=verdict, listing_lo=11, listing_hi=12)
    assert response.status_code == 200
    assert conn.params(usql.VERDICT_PAIR_APPEND_SQL)["verdict"] == verdict


def test_a_correction_to_same_between_two_properties_is_refused_too(client, conn):
    """A rent/sale `different` is a sound ruling; flipping it on the rulings page is the same
    `same` the typed path refuses, read over the ruling's own pair inside the write."""
    conn.canned[usql.VERDICT_ONE_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.PAIR_NEWEST_RULING_SQL] = [_verdict(id=7, verdict="different")]
    conn.canned[usql.PAIR_CATEGORIES_SQL] = [("pronajem", "byt", "prodej", "byt")]
    response = _post(client, kind="pair", verdict="same", supersedes=7)
    assert response.status_code == 422
    assert "Pronájem" in response.json()["detail"]
    assert conn.params(usql.PAIR_CATEGORIES_SQL) == {"listing_lo": 11, "listing_hi": 12}
    assert conn.tx_calls == [usql.VERDICT_ONE_SQL, usql.PAIR_NEWEST_RULING_SQL,
                             usql.PAIR_CATEGORIES_SQL]
    assert not any(conn.ran(sql) for sql in _NOTHING_WRITTEN)


def test_the_route_and_the_merge_chokepoint_read_one_gate():
    """No second definition: the route refuses exactly what `merge_property_set` refuses."""
    from api.routes import autodedup as routes
    from toolkit import property_identity
    from toolkit.property_identity import CategoryClash, category_clash

    assert routes.category_clash is property_identity.category_clash
    assert str(CategoryClash(*category_clash(("pronajem", "byt"), ("prodej", "byt")))) == (
        "category_type mismatch (pronajem vs prodej); refusing to merge")
    assert str(CategoryClash(*category_clash(("prodej", "byt"), ("prodej", "komercni")))) == (
        "category_main mismatch (byt vs komercni); refusing to merge")
    assert category_clash(("prodej", "dum"), ("prodej", "komercni")) is None
    assert category_clash(("prodej", "pozemek"), ("prodej", "dum")) is None
    assert category_clash(("prodej", "komercni"), ("prodej", "pozemek")) is None
    assert category_clash(("prodej", "byt"), ("prodej", "pozemek")) == (
        "category_main", "byt", "pozemek")


def test_the_category_refusal_names_the_cross_types_rule_15_allows(client, conn):
    """E935: the 422 states the rule as it stands, so it names land beside house and commercial."""
    conn.canned[usql.PAIR_CATEGORIES_SQL] = [("prodej", "byt", "prodej", "pozemek")]
    response = _post(client, kind="pair", verdict="same", listing_lo=11, listing_hi=12)
    assert response.status_code == 422
    assert "výjimkou jsou dům, komerční objekt a pozemek" in response.json()["detail"]
    assert "jediná výjimka" not in response.json()["detail"]


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
    for sql in (usql.VERDICT_COUNTS_SQL,):
        flat = _flat(sql)
        assert "SELECT DISTINCT ON (x.kind, x.listing_lo, x.listing_hi, x.cluster_key," in flat
        assert "x.decided_at DESC, x.id DESC" in flat
