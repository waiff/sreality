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
        "listing_lo": 11, "listing_hi": 12, "stratum": "g2:s3_mf_band", "ruled": False,
        "operator_ruling_id": None, "operator_verdict": None, "operator_source": None,
        "operator_note": None, "operator_decided_by": None, "operator_decided_at": None,
        "judge_tier": "vision", "judge_verdict": "different_property", "judge_confidence": 0.93,
        "judge_model": "gpt-5-mini", "judge_key_evidence": ["jiné patro"],
        "judge_contradicting_evidence": ["stejná adresa"], "tiers_split": False,
        "is_sample": True, "is_operator": False, "is_engine": True, "is_unsure": False,
        "primary_reason": "sample", "operator_agreement": "none",
        "engine_agreement": "disagrees", "engine_view": "together", "together_now": False,
        "obec_kod": 563510, "obec_name": "Jablonec nad Nisou", "cast_obce_kod": None,
        "cast_obce_name": None, "zone": "band", "score": 0.61,
        "decision": "certificate:K-A:evidence_gate", "guard_veto": None, "block": 0,
        "sort_hash": HASH,
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
            usql.JUDGED_TOWNS_SQL: [("o", 563510, "Jablonec nad Nisou", 7)],
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
    {"reason": "everything"},
    {"judge": "same_property"},
    {"tier": "oss"},
    {"ruled": "2"},
    {"operator": "maybe"},
    {"engine": "together"},
    {"town": "563510"},
    {"stratum": "g2:\x07bell"},
    {"stratum": "x" * 201},
    {"after": f"0|0|{HASH}|11"},
    {"after": f"0|3|{HASH}|11|12"},
    {"after": f"2|0|{HASH}|11|12"},
    {"after": "0|0|not-a-hash|11|12"},
    {"after": f"0|0|{HASH}|eleven|12"},
])
def test_the_filter_registry_refuses_what_it_does_not_name(client, conn, query):
    assert client.get("/autodedup/judgements", params=query).status_code == 400
    assert not conn.ran(usql.JUDGEMENTS_SQL)


def test_every_filter_becomes_a_parameter_of_the_statements(client, conn):
    conn.facets["operator"] = [("total", None, 1)]
    client.get("/autodedup/judgements", params={
        "reason": "operator", "judge": "different", "tier": "gold",
        "stratum": "b:band_not_merged|band|model>=0.95|nofact", "town": "c:490245",
        "ruled": 1, "operator": "disagrees", "engine": "agrees", "generation": "g13"})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert {k: params[k] for k in (
        "reason", "judge", "tier", "stratum", "obec", "cast_obce", "ruled", "operator",
        "engine", "generation", "seed", "sample_size", "judge_sure")} == {
        "reason": "operator", "judge": "different", "tier": "gold",
        "stratum": "b:band_not_merged|band|model>=0.95|nofact", "obec": None,
        "cast_obce": 490245, "ruled": True, "operator": "disagrees", "engine": "agrees",
        "generation": "g13", "seed": routes.DEFAULT_SEED,
        "sample_size": routes.VALIDATION_SAMPLE_SIZE, "judge_sure": routes.JUDGE_SURE}
    # The counts run the same filter, without the page's cursor.
    facets = conn.params(usql.JUDGEMENTS_FACETS_SQL)
    assert facets["cast_obce"] == 490245 and "after_hash" not in facets


def test_a_blank_filter_is_no_filter(client, conn):
    client.get("/autodedup/judgements", params={"judge": "", "town": "", "stratum": ""})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert (params["judge"], params["obec"], params["stratum"]) == (None, None, None)
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
        ("stratum", "g2:s3_mf_band", 1), ("stratum", None, 4), ("ruled", "0", 1),
        ("operator", "none", 1), ("engine", "disagrees", 1),
        ("reason", "suggested", 1), ("reason", "sample", 1), ("reason", "engine", 1),
    ]
    data = client.get("/autodedup/judgements").json()["data"]
    assert data["generation"] == "g15"
    item = data["items"][0]
    assert item["judgement"] == {
        "tier": "vision", "verdict": "different_property", "confidence": 0.93,
        "model": "gpt-5-mini", "key_evidence": ["jiné patro"],
        "contradicting_evidence": ["stejná adresa"]}
    assert item["reasons"] == ["sample", "engine"] and item["primary_reason"] == "sample"
    assert item["ruled"] is False and item["verdict"] is None
    assert item["stratum"] == "g2:s3_mf_band"
    assert item["engine_view"] == "together" and item["engine_agreement"] == "disagrees"
    assert item["certificate"] == "K-A"
    assert "druhy důkazů" in item["why_not_merged"]
    assert item["obec_name"] == "Jablonec nad Nisou"
    assert data["facets"]["stratum"] == {"g2:s3_mf_band": 1}
    assert data["facets"]["reason"] == {"suggested": 1, "sample": 1, "engine": 1}
    assert data["facets"]["ruled"] == {"0": 1}
    assert data["towns"] == [{"grain": "o", "code": 563510, "name": "Jablonec nad Nisou",
                              "n": 7}]
    assert conn.params(usql.JUDGED_TOWNS_SQL) == {"seed": routes.DEFAULT_SEED,
                                                  "limit": routes.RULING_TOWNS_LIMIT}


def test_a_ruled_row_carries_the_operators_word_as_a_verdict_row(client, conn):
    conn.pages = [_judged(ruled=True, operator_ruling_id=41, operator_verdict="same",
                          operator_source="implied", operator_decided_by="op@example.com",
                          operator_decided_at=AT, operator_agreement="disagrees")]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert item["ruled"] is True and item["operator_source"] == "implied"
    assert item["verdict"]["verdict"] == "same" and item["verdict"]["id"] == 41
    assert (item["verdict"]["listing_lo"], item["verdict"]["listing_hi"]) == (11, 12)
    assert item["operator_agreement"] == "disagrees"


def test_a_sampled_pair_the_judge_has_not_read_has_no_judgement_and_no_engine_row(client, conn):
    conn.pages = [_judged(judge_tier=None, judge_verdict=None, judge_confidence=None,
                          judge_model=None, judge_key_evidence=None,
                          judge_contradicting_evidence=None, is_engine=False,
                          engine_agreement="none", engine_view="unseen", zone=None,
                          score=None, decision=None, stratum="g2:control")]
    item = client.get("/autodedup/judgements").json()["data"]["items"][0]
    assert item["judgement"] is None
    assert item["reasons"] == ["sample"]
    assert item["why_not_merged"] is None and item["certificate"] is None


def test_the_cursor_is_the_pages_own_order_key(client, conn):
    conn.pages = [_judged(), _judged(listing_lo=13, listing_hi=14, block=1, sort_hash="f" * 32)]
    data = client.get("/autodedup/judgements", params={"limit": 1}).json()["data"]
    assert data["next_after"] == f"0|0|{HASH}|11|12"
    client.get("/autodedup/judgements", params={"after": data["next_after"]})
    params = conn.params(usql.JUDGEMENTS_SQL)
    assert (params["after_ruled"], params["after_block"], params["after_hash"],
            params["after_lo"], params["after_hi"]) == (0, 0, HASH, 11, 12)
    assert params["limit"] == routes.JUDGEMENTS_PAGE_SIZE + 1


def test_the_engine_view_is_the_live_stream_once_it_is_live(client, conn):
    conn.live = {seed_version_key(): SEED_VERSION, bootstrap_key(): False}
    body = client.get("/autodedup/judgements").json()
    assert body["data"]["generation"] == LIVE
    assert conn.params(usql.JUDGEMENTS_SQL)["generation"] == LIVE


# ------------------------------------------------------------------------ a store behind code


def test_an_unmigrated_store_renders_instead_of_failing(client, conn):
    conn.ready = False
    assert client.get("/autodedup/judgements").json() == {"data": None, "store_ready": False}


def test_a_store_without_the_stratum_column_reads_as_not_ready(client, conn):
    """Migration 576 is applied before the code merges; if it is not, the page says the store
    is behind rather than failing."""
    if not routes._UNDEFINED_COLUMN:
        pytest.skip("psycopg absent")
    conn.raises[usql.JUDGEMENTS_FACETS_SQL] = routes._UNDEFINED_COLUMN[0](
        'column j.stratum does not exist')
    assert client.get("/autodedup/judgements").json() == {"data": None, "store_ready": False}


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


def test_one_definition_of_the_operators_word_and_of_the_judges_headline():
    """The Judge page reads the rulings page's `rulings` CTE and the residual queue's headline,
    never a copy of either — and `oss`, the rented arm, is not the judge anywhere."""
    assert usql._OPERATOR_WORDS in usql.RULINGS_PAIR_SQL
    assert usql._OPERATOR_WORDS in usql.JUDGEMENTS_SQL
    assert usql._OPERATOR_WORDS in usql.VALIDATION_JUDGE_SAMPLE_SQL
    headline = usql._judge_best("r.listing_lo", "r.listing_hi")
    assert headline in usql.JUDGEMENTS_SQL
    assert usql._judge_best("p.listing_lo", "p.listing_hi") in usql.RESIDUAL_SQL
    assert "jj.tier IN ('gold', 'vision', 'text')" in headline
    assert "'oss'" not in usql.JUDGEMENTS_SQL
    assert usql._PAIR_CONTEXT_JOINS in usql.RULINGS_PAIR_SQL
    assert usql._PAIR_CONTEXT_JOINS in usql.JUDGEMENTS_SQL


def test_the_order_is_one_seeded_order_and_never_the_reason():
    """Position must not tell a blind operator what the judge said: after `ruled` and the
    sample block, the seeded hash orders every row."""
    flat = _flat(usql.JUDGEMENTS_SQL)
    assert flat.endswith(
        "ORDER BY f.ruled::int, f.block, f.sort_hash, f.listing_lo, f.listing_hi "
        "LIMIT %(limit)s::int")
    assert "md5(j.listing_lo::text || ':' || j.listing_hi::text || %(seed)s::text)" in flat
    assert "md5(e.listing_lo::text || ':' || e.listing_hi::text || %(seed)s::text)" in flat


def test_every_reason_is_capped_by_the_one_sample_size():
    flat = _flat(usql.JUDGEMENTS_SQL)
    assert flat.count("<= %(sample_size)s::int") == 4
    assert "PARTITION BY e.stratum" in flat


def test_the_judge_statements_run_without_jit_in_their_own_transaction(client, conn):
    """JIT compiles the wide CTE chain for seconds to run a millisecond plan (measured in CI's
    replay); the route turns it off for its own transaction only."""
    client.get("/autodedup/judgements")
    client.get("/autodedup/validation-progress", params={"surface": "judge"})
    assert sum(1 for sql, _ in conn.calls if sql == usql.JIT_OFF) == 2
