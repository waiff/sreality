"""The Judge page's API: `GET /autodedup/judgements` and the judge surface of the progress strip.

The connection is faked and dispatches on the statement constants, like the other review-page
tests; what the SQL itself returns is executed in `tests/test_judgements_live.py`. Asserted here:
the filter registry (an unknown key or value is a 400, never an ignored parameter), the
parameters each filter becomes, the default reason (suggested while that set holds a pair, else
everything, echoed), keyset paging on the page's own order, each row's shape, and the
not-migrated answer.
"""

from __future__ import annotations


from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import autodedup as routes
from autodedup import ui_sql as usql
from autodedup.incremental import GENERATION as LIVE
from autodedup.incremental import SEED_VERSION, bootstrap_key, seed_version_key

AT = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
HASH = "0" * 31 + "a"


def _judged(**over: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "listing_lo": 11, "listing_hi": 12, "ruled": False,
        "operator_ruling_id": None, "operator_verdict": None, "operator_note": None,
        "operator_reasons": None, "operator_decided_by": None, "operator_decided_at": None,
        "judge_tier": "vision", "judge_verdict": "different_property", "judge_confidence": 0.93,
        "judge_model": "gpt-5-mini", "judge_key_evidence": ["jiné patro"],
        "judge_contradicting_evidence": ["stejná adresa"],
        "is_sample": True, "is_operator": False, "is_engine": True, "is_unsure": False,
        "engine_view": "apart", "obec_name": "Jablonec nad Nisou", "cast_obce_name": None,
        "zone": "band", "score": 0.61, "decision": "certificate:K-A:evidence_gate",
        "guard_veto": None, "block": 0, "sort_hash": HASH,
    }
    values.update(over)
    unknown = sorted(set(values) - set(usql.JUDGED_PAIR_COLUMNS))
    assert not unknown, unknown
    return tuple(values.get(name) for name in usql.JUDGED_PAIR_COLUMNS)


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
        if sql in self._conn.raises:
            raise self._conn.raises[sql]
        if "to_regclass" in sql:
            self._rows = [(self._conn.ready,)]
        elif sql == usql.LIVE_STREAM_SQL:
            keys = (params or {}).get("keys") or []
            self._rows = [(k, v) for k, v in self._conn.live.items() if k in keys]
        elif sql == usql.LATEST_GENERATION_SQL:
            self._rows = [("g15",)]
        elif sql == usql.JUDGEMENTS_FACETS_SQL:
            reason = (params or {}).get("reason")
            self._rows = list(self._conn.facets.get(reason, []))
        elif sql == usql.JUDGEMENTS_SQL:
            rows = list(self._conn.pages)
            self._rows = rows[: int((params or {})["limit"])]
        elif sql in self._conn.canned:
            self._rows = list(self._conn.canned[sql])
        elif sql == usql.JIT_OFF:
            assert self._conn.open_tx, "SET LOCAL outside a transaction is a no-op"
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
        self.open_tx = 0
        self.ready = True
        self.live: dict[str, Any] = {}
        self.raises: dict[str, BaseException] = {}
        self.facets: dict[str, list[tuple[Any, ...]]] = {
            "suggested": [("total", None, 1), ("reason", "suggested", 1)],
        }
        self.pages: list[tuple[Any, ...]] = [_judged()]
        self.canned: dict[str, list[tuple[Any, ...]]] = {
            usql.VALIDATION_JUDGE_SAMPLE_SQL: [(100, 37, 5)],
            usql.VALIDATION_JUDGE_TOTAL_SQL: [(412, 60, 9)],
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def params(self, sql: str) -> dict[str, Any]:
        found = [p for s, p in self.calls if s == sql]
        assert found, f"never ran: {' '.join(sql.split())[:60]}"
        return found[-1]

    def all_params(self, sql: str) -> list[dict[str, Any]]:
        return [p for s, p in self.calls if s == sql]

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


# ------------------------------------------------------------------------ the filter registry


@pytest.mark.parametrize("query", [
    {"sort": "random"},
    {"has_judgement": "1"},
    {"stratum": "g2:s3_mf_band"},
    {"reason": "everything"},
    {"reason": "operator"},
    {"judge": "same_property"},
    {"judge": "none"},
    {"tier": "oss"},
    {"ruled": "2"},
    {"operator": "maybe"},
    {"engine": "together"},
    {"town": "o:563510"},
    {"after": f"suggested|0|0|{HASH}|11"},
    {"after": f"0|0|{HASH}|11|12"},
    {"after": f"operator|0|0|{HASH}|11|12"},
    {"after": f"suggested|0|3|{HASH}|11|12"},
    {"after": f"suggested|2|0|{HASH}|11|12"},
    {"after": "suggested|0|0|not-a-hash|11|12"},
    {"after": f"suggested|0|0|{HASH}|eleven|12"},
    {"after": f"sample|0|0|{HASH}|11|12", "reason": "engine"},
])
def test_the_filter_registry_refuses_what_it_does_not_name(client, conn, query):
    assert client.get("/autodedup/judgements", params=query).status_code == 400
    assert not conn.ran(usql.JUDGEMENTS_SQL)


def test_every_filter_becomes_a_parameter_of_the_statements(client, conn):
    conn.facets["unsure"] = [("total", None, 1)]
    client.get("/autodedup/judgements", params={
        "reason": "unsure", "judge": "different", "tier": "gold", "ruled": 1,
        "operator": "disagrees", "engine": "agrees", "generation": "g13"})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert {k: params[k] for k in (
        "reason", "judge", "tier", "ruled", "operator", "engine", "generation", "seed",
        "sample_size", "judge_sure")} == {
        "reason": "unsure", "judge": "different", "tier": "gold", "ruled": True,
        "operator": "disagrees", "engine": "agrees", "generation": "g13",
        "seed": routes.DEFAULT_SEED, "sample_size": routes.VALIDATION_SAMPLE_SIZE,
        "judge_sure": routes.JUDGE_SURE}
    # The counts run the same filter, without the page's cursor.
    facets = conn.params(usql.JUDGEMENTS_FACETS_SQL)
    assert facets["tier"] == "gold" and "after_hash" not in facets


def test_a_blank_filter_is_no_filter(client, conn):
    client.get("/autodedup/judgements", params={"judge": "", "tier": ""})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert (params["judge"], params["tier"]) == (None, None)
    assert params["after_hash"] is None


# ------------------------------------------------------------------------- the default reason


def test_a_missing_reason_is_the_suggested_set_while_it_holds_a_pair(client, conn):
    data = client.get("/autodedup/judgements").json()["data"]
    assert data["reason"] == "suggested"
    assert [p["reason"] for p in conn.all_params(usql.JUDGEMENTS_FACETS_SQL)] == ["suggested"]
    assert conn.params(usql.JUDGEMENTS_SQL)["reason"] == "suggested"


def test_an_empty_suggestion_set_opens_on_everything_in_one_request(client, conn):
    conn.facets = {"suggested": [("total", None, 0)], "all": [("total", None, 3)]}
    data = client.get("/autodedup/judgements").json()["data"]
    assert data["reason"] == "all" and data["total"] == 3
    assert [p["reason"] for p in conn.all_params(usql.JUDGEMENTS_FACETS_SQL)] == [
        "suggested", "all"]
    assert conn.params(usql.JUDGEMENTS_SQL)["reason"] == "all"


def test_an_asked_reason_is_kept_even_when_it_is_empty(client, conn):
    conn.facets = {"engine": [("total", None, 0)]}
    conn.pages = []
    data = client.get("/autodedup/judgements", params={"reason": "engine"}).json()["data"]
    assert data["reason"] == "engine" and data["items"] == [] and data["total"] == 0
    assert len(conn.all_params(usql.JUDGEMENTS_FACETS_SQL)) == 1


# --------------------------------------------------------------------------------- the rows


def test_a_row_carries_the_judge_the_reasons_the_engine_and_the_town(client, conn):
    conn.facets["suggested"] = [
        ("total", None, 1), ("judge", "different", 1), ("tier", "vision", 1),
        ("ruled", "0", 1), ("operator", "none", 1), ("engine", "disagrees", 1),
        ("reason", "suggested", 1), ("reason", "sample", 1), ("reason", "engine", 1),
    ]
    data = client.get("/autodedup/judgements").json()["data"]
    assert data["generation"] == "g15"
    item = data["items"][0]
    assert item == {
        "listing_lo": 11, "listing_hi": 12, "verdict": None,
        "judgement": {
            "tier": "vision", "verdict": "different_property", "confidence": 0.93,
            "model": "gpt-5-mini", "key_evidence": ["jiné patro"],
            "contradicting_evidence": ["stejná adresa"]},
        "reasons": ["sample", "engine"], "engine_view": "apart",
        "obec_name": "Jablonec nad Nisou", "cast_obce_name": None, "zone": "band",
        "score": 0.61, "guard_veto": None, "certificate": "K-A",
        "why_not_merged": "drženo v pásmu kontroly: méně než dva nezávislé druhy důkazů"}
    assert data["facets"]["reason"] == {"suggested": 1, "sample": 1, "engine": 1}
    assert data["facets"]["ruled"] == {"0": 1}
    assert "towns" not in data


@pytest.mark.parametrize("zone", ["merge", "band", "reject"])
def test_a_pair_the_engine_holds_together_carries_no_reason_it_was_not_merged(
        client, conn, zone):
    """A merged pair was not "not merged": a merge-zone pair in one group, or a band pair joined
    through a third advert, must not read "dvojice prošla, ale ..." or "skóre v pásmu ..."."""
    conn.pages = [_judged(engine_view="together", zone=zone, decision=f"{zone}:score")]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert item["engine_view"] == "together" and item["zone"] == zone
    assert item["why_not_merged"] is None


def test_a_ruled_row_carries_the_operators_word_and_codes_as_a_verdict_row(client, conn):
    conn.pages = [_judged(ruled=True, operator_ruling_id=41, operator_verdict="same",
                          operator_note="stejná kuchyň", operator_reasons=["photos_same"],
                          operator_decided_by="op@example.com", operator_decided_at=AT)]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert item["verdict"] == {
        "id": 41, "kind": "pair", "cluster_key": None, "listing_lo": 11, "listing_hi": 12,
        "verdict": "same", "note": "stejná kuchyň", "reasons": ["photos_same"],
        "decided_by": "op@example.com", "decided_at": AT.isoformat()}


def test_a_nevim_is_a_stored_word_the_buttons_show(client, conn):
    """The buttons show the "Nevím" the operator gave. Whether a word opens the judge is the SPA's
    one gate (`revealsJudge`), read off this word: the payload carries no second "ruled" (E55)."""
    conn.pages = [_judged(ruled=False, operator_ruling_id=42, operator_verdict="unsure",
                          operator_decided_by="op@example.com", operator_decided_at=AT)]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert "ruled" not in item
    assert item["verdict"]["verdict"] == "unsure" and item["verdict"]["reasons"] == []


def test_a_sampled_pair_the_judge_has_not_read_has_no_judgement_and_no_engine_row(client, conn):
    conn.pages = [_judged(judge_tier=None, judge_verdict=None, judge_confidence=None,
                          judge_model=None, judge_key_evidence=None,
                          judge_contradicting_evidence=None, is_engine=False,
                          engine_view="unseen", zone=None, score=None, decision=None)]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert item["judgement"] is None
    assert item["reasons"] == ["sample"]
    assert item["why_not_merged"] is None and item["certificate"] is None


def test_the_cursor_is_the_pages_own_order_key_and_its_selection(client, conn):
    conn.pages = [_judged(), _judged(listing_lo=13, listing_hi=14, block=1, sort_hash="f" * 32)]
    data = client.get("/autodedup/judgements", params={"limit": 1}).json()["data"]
    assert data["next_after"] == f"suggested|0|0|{HASH}|11|12"
    client.get("/autodedup/judgements", params={"after": data["next_after"]})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert (params["reason"], params["after_ruled"], params["after_block"],
            params["after_hash"], params["after_lo"], params["after_hi"]) == (
        "suggested", 0, 0, HASH, 11, 12)
    assert params["limit"] == routes.JUDGEMENTS_PAGE_SIZE + 1


def test_a_later_page_runs_the_page_statement_alone(client, conn):
    """The counts are read once, with the first page: "Načíst další" runs one statement."""
    conn.facets = {"suggested": [("total", None, 0)], "all": [("total", None, 3)]}
    conn.pages = [_judged(), _judged(listing_lo=13, listing_hi=14, block=2)]
    first = client.get("/autodedup/judgements", params={"limit": 1}).json()["data"]
    assert first["reason"] == "all" and first["next_after"].startswith("all|")
    conn.calls.clear()
    later = client.get("/autodedup/judgements", params={"after": first["next_after"]}).json()
    assert not conn.ran(usql.JUDGEMENTS_FACETS_SQL)
    # The selection the first page fell back to holds for the whole walk.
    assert conn.params(usql.JUDGEMENTS_SQL)["reason"] == "all"
    assert later["data"]["reason"] == "all"
    assert (later["data"]["total"], later["data"]["facets"]) == (None, None)


def test_the_engine_view_is_the_live_stream_once_it_is_live(client, conn):
    conn.live = {seed_version_key(): SEED_VERSION, bootstrap_key(): False}
    body = client.get("/autodedup/judgements").json()
    assert body["data"]["generation"] == LIVE
    assert conn.params(usql.JUDGEMENTS_SQL)["generation"] == LIVE


# ------------------------------------------------------------------------ a store behind code


def test_an_unmigrated_store_renders_instead_of_failing(client, conn):
    conn.ready = False
    assert client.get("/autodedup/judgements").json() == {"data": None, "store_ready": False}


def test_a_store_behind_the_code_reads_as_not_ready(client, conn):
    """A column the statements read and the store lacks: the page says the store is behind
    rather than failing."""
    if not routes._UNDEFINED_COLUMN:
        pytest.skip("psycopg absent")
    conn.raises[usql.JUDGEMENTS_FACETS_SQL] = routes._UNDEFINED_COLUMN[0](
        'column e.stratum does not exist')
    assert client.get("/autodedup/judgements").json() == {"data": None, "store_ready": False}


def test_the_page_never_reads_the_marks_list_stamp():
    """Migration 576's `stratum` is the marks' training provenance, not a filter: no Judge
    statement reads it off `autodedup.judgements` (the sealed draw's own name partitions it)."""
    for sql in (usql.JUDGEMENTS_SQL, usql.JUDGEMENTS_FACETS_SQL,
                usql.VALIDATION_JUDGE_SAMPLE_SQL, usql.VALIDATION_JUDGE_TOTAL_SQL):
        flat = _flat(sql)
        assert "j.stratum" not in flat and "m.stratum" not in flat and "g.stratum" not in flat


# ------------------------------------------------------------------------- the progress strip


def test_the_judge_surface_counts_pairs_against_the_sealed_sample(client, conn):
    data = client.get("/autodedup/validation-progress",
                      params={"surface": "judge"}).json()["data"]
    assert data["surface"] == "judge" and data["grain"] == "pair"
    assert data["sample"] == {"n": 100, "n_reviewed": 37, "n_not_same": 5}
    assert data["total"] == {"n": 412, "n_reviewed": 60, "n_not_same": 9}
    params = conn.params(usql.VALIDATION_JUDGE_SAMPLE_SQL)
    assert params["sample_size"] == routes.VALIDATION_SAMPLE_SIZE
    assert params["seed"] == routes.DEFAULT_SEED


# ----------------------------------------------------------------------------- the statements


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_one_definition_of_the_operators_word_the_headline_and_the_engines_read():
    """The Judge page reads the rulings page's `rulings` CTE, the residual queue's headline rule
    (tiers and their rank; `oss`, the rented arm, is not the judge anywhere) and the rulings
    page's four sources of an advert the generation read — never a copy of any of them."""
    for sql in (usql.RULINGS_PAIR_SQL, usql.JUDGEMENTS_SQL, usql.JUDGEMENTS_FACETS_SQL,
                usql.VALIDATION_JUDGE_SAMPLE_SQL, usql.VALIDATION_JUDGE_TOTAL_SQL):
        assert usql._OPERATOR_WORDS in sql
    for sql, alias in ((usql.RESIDUAL_SQL, "jj"), (usql.JUDGEMENTS_SQL, "m")):
        assert f"tier IN {usql._JUDGE_TIERS}" in sql
        assert usql._TIER_RANK.format(t=alias) in sql
    assert "'oss'" not in usql.JUDGEMENTS_SQL
    assert usql._engine_seen("a.listing_id") in usql._ENGINE_VIEW
    for sql in (usql.RULINGS_PAIR_SQL, usql.RULINGS_PAIR_FACETS_SQL):
        assert usql._engine_seen("r.listing_lo") in sql
    for sql in (usql.JUDGEMENTS_SQL, usql.JUDGEMENTS_FACETS_SQL):
        assert usql._ENGINE_VIEW in sql


def test_the_generation_is_read_for_the_populations_adverts_only():
    """At the country-wide roll-out the generation holds millions of pairs: every read of its
    three tables before the page's LIMIT is the engine view's, on (generation, advert) for an
    advert of the population, so the read grows with the judged pairs."""
    chain = _flat(usql.JUDGEMENTS_SQL.split(", page AS (", 1)[0])
    view = _flat(usql._ENGINE_VIEW)
    assert view in chain and view in _flat(usql.JUDGEMENTS_FACETS_SQL)
    for table, n in (("autodedup.cluster_members", 2), ("autodedup.rt_fp", 1),
                     ("autodedup.pairs", 2)):
        assert chain.count(table) == view.count(table) == n
    assert view.count("= a.listing_id") == view.count("generation = %(generation)s::text") == 5
    assert "FROM population r UNION SELECT r.listing_hi FROM population r" in view


def test_the_order_is_built_from_the_autodedup_store_alone():
    """On production a probe into a large public table is a disk read: one per judged pair cost
    10 s (2026-09-29). The selection, the reasons, the caps and the order read the autodedup
    store once, joined by hash; a row's town and stored pair are read for the page's rows only,
    after its LIMIT. The counts and the strip read no public table at all."""
    chain, tail = usql.JUDGEMENTS_SQL.split(", page AS (", 1)
    for sql in (chain, usql.JUDGEMENTS_FACETS_SQL, usql.VALIDATION_JUDGE_SAMPLE_SQL,
                usql.VALIDATION_JUDGE_TOTAL_SQL):
        assert "public." not in sql and "LEFT JOIN LATERAL" not in sql
    head, after_limit = tail.split("LIMIT %(limit)s::int\n)", 1)
    assert "public." not in head and "JOIN public.listing_location ll" in after_limit
    assert "JOIN autodedup.pairs p" in after_limit


def test_the_order_is_one_seeded_order_and_never_the_reason():
    """Position must not tell a blind operator what the judge said: after `ruled` and the
    sample block, the seeded hash orders every row."""
    flat = _flat(usql.JUDGEMENTS_SQL)
    order = "ORDER BY f.ruled::int, f.block, f.sort_hash, f.listing_lo, f.listing_hi"
    assert f"{order} LIMIT %(limit)s::int )" in flat and flat.endswith(order)
    assert "md5(r.listing_lo::text || ':' || r.listing_hi::text || %(seed)s::text)" in flat
    assert "md5(e.listing_lo::text || ':' || e.listing_hi::text || %(seed)s::text)" in flat


def test_every_reason_is_capped_by_the_one_sample_size():
    flat = _flat(usql.JUDGEMENTS_SQL)
    assert flat.count("draw_rank <= %(sample_size)s::int") == 1
    assert flat.count("LIMIT %(sample_size)s::int") == 3
    assert "PARTITION BY e.stratum" in flat


def test_the_judge_statements_run_without_jit_in_their_own_transaction(client, conn):
    """JIT compiles the wide CTE chain for seconds to run a millisecond plan (measured in CI's
    replay); the route turns it off for its own transaction only."""
    client.get("/autodedup/judgements")
    client.get("/autodedup/validation-progress", params={"surface": "judge"})
    assert sum(1 for sql, _ in conn.calls if sql == usql.JIT_OFF) == 2
