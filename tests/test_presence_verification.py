"""Rule #3 since 2026-09-07: index absence NOMINATES, the page DECIDES.

A complete category walk no longer flips the rows it did not see. It queues
them for a detail fetch at the lowest priority; the drain visits each page, and
a positive gone signal flips the listing, a live page refreshes it, an error
leaves it for the next pass. These tests pin the two db pieces of that: the
nomination query (what is excluded, what is returned, in what order) and the
bounded enqueue (the old flip cap repurposed as a per-walk throttle, the
operator override still lifting it, the deferral recorded).
"""

from __future__ import annotations

from typing import Any

from scraper import db


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._result: Any = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM app_settings" in s:
            # psycopg hands jsonb back as a Python object; the cap parser
            # trusts only a dict and treats anything else as a broken knob.
            self._result = (self._conn.cap,) if self._conn.cap is not None else None
        elif s.startswith("SELECT count(*)"):
            self._result = (self._conn.active_rows,)
        elif "INSERT INTO listing_detail_queue" in s:
            self.rowcount = len(params["nids"])
        elif "INSERT INTO delist_flip_refusals" in s:
            self._conn.refusals.append(params)
        else:
            self._result = None

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> list[Any]:
        return list(self._conn.rows)


class _Conn:
    def __init__(self, *, rows=(), active_rows=0, cap=None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.refusals: list[Any] = []
        self.rows = list(rows)
        self.active_rows = active_rows
        self.cap = cap

    def cursor(self) -> _Cur:
        return _Cur(self)


# --- the nomination query --------------------------------------------------


def test_native_nomination_excludes_the_seen_set_and_returns_oldest_first():
    conn = _Conn(rows=[("a1", "https://p/a1", 100), ("a2", "https://p/a2", None)], active_rows=40)
    cands, active = db.presence_candidates(conn, "remax", "byt", "prodej", {"s1", "s2"})
    assert active == 40
    assert cands == [("a1", "https://p/a1", 100), ("a2", "https://p/a2", None)]
    count_sql, count_params = conn.executed[0]
    select_sql, select_params = conn.executed[1]
    assert "WHERE is_active = true AND source = %s AND category_main = %s AND category_type = %s" in count_sql
    assert count_params == ("remax", "byt", "prodej")
    assert "source_id_native <> ALL(%s)" in select_sql
    assert "ORDER BY last_seen_at ASC NULLS FIRST" in select_sql
    assert select_params[:3] == ("remax", "byt", "prodej")
    assert sorted(select_params[3]) == ["s1", "s2"]


def test_sreality_nomination_keys_on_sreality_id_and_carries_no_ref():
    """sreality's fetch derives the URL from the id, and its walk's seen set is
    integers -- the query must exclude on sreality_id and hand back ref=None."""
    conn = _Conn(rows=[("123", "https://www.sreality.cz/x", 5_000_000)], active_rows=3)
    cands, _ = db.presence_candidates(
        conn, "sreality", "byt", "prodej", {123, 456}, seen_key="sreality_id")
    assert cands == [("123", None, 5_000_000)]
    select_sql, select_params = conn.executed[1]
    assert "sreality_id <> ALL(%s)" in select_sql
    assert sorted(select_params[3]) == [123, 456]


def test_subtype_scoping_narrows_the_nomination():
    """bazos: chata + dum both -> dum, so a chata section's walk must not nominate
    every dum row it never intended to see."""
    conn = _Conn(rows=[], active_rows=0)
    db.presence_candidates(conn, "bazos", "dum", "prodej", {"x"}, subtype="chata", scope_subtype=True)
    for sql, params in conn.executed:
        assert "subtype IS NOT DISTINCT FROM %s" in sql
        assert params[3] == "chata"


def test_nomination_drops_null_ids_from_the_seen_set():
    conn = _Conn(rows=[], active_rows=0)
    db.presence_candidates(conn, "remax", "byt", "prodej", {"a", None})
    assert conn.executed[1][1][3] == ["a"]


# --- the bounded enqueue ---------------------------------------------------


def _cands(n: int) -> list[tuple[str, str | None, int | None]]:
    return [(f"n{i}", f"https://p/n{i}", None) for i in range(n)]


def test_small_scope_queues_everything_at_verify_priority():
    """Below min_rows the cap does not apply: small categories verify all."""
    conn = _Conn(cap={"fraction": 0.1, "min_rows": 2000, "overrides": []})
    queued, deferred = db.enqueue_presence_checks(
        conn, "remax", "byt", "prodej", _cands(300), active_rows=500)
    assert (queued, deferred) == (300, 0)
    ins = [(s, p) for s, p in conn.executed if "INSERT INTO listing_detail_queue" in s]
    assert len(ins) == 1
    assert set(ins[0][1]["prios"]) == {db.QUEUE_PRIORITY_VERIFY}
    assert conn.refusals == []


def test_verify_priority_sits_below_every_other_priority():
    """The claim order is priority DESC: a backlog of page checks must never be
    served before a new or price-changed listing."""
    assert db.QUEUE_PRIORITY_VERIFY < db.QUEUE_PRIORITY_NEW < db.QUEUE_PRIORITY_CHANGED < db.QUEUE_PRIORITY_FAILURE


def test_large_scope_is_throttled_to_the_cap_and_the_deferral_is_recorded():
    """40,000 stale rows do not become 40,000 fetches in one walk: the oldest
    share goes now, the rest next walk, and the row says so."""
    conn = _Conn(cap={"fraction": 0.1, "min_rows": 2000, "overrides": []})
    queued, deferred = db.enqueue_presence_checks(
        conn, "ceskereality", "byt", "pronajem", _cands(10_854), active_rows=16_924)
    assert queued == 1_692 and deferred == 10_854 - 1_692
    ins = [(s, p) for s, p in conn.executed if "INSERT INTO listing_detail_queue" in s]
    assert sum(len(p["nids"]) for _, p in ins) == 1_692
    assert ins[0][1]["nids"][0] == "n0"                     # oldest-unseen first
    assert conn.refusals == [("ceskereality", "byt", "pronajem", None, 10_854, 16_924, 1_692)]


def test_an_operator_override_lifts_the_throttle_for_its_scope():
    conn = _Conn(cap={"fraction": 0.1, "min_rows": 2000, "overrides": [
        {"source": "ceskereality", "category_main": "byt", "category_type": "pronajem",
         "max_rows": 20_000, "until": "2999-01-01T00:00:00Z", "reason": "verified"}]})
    queued, deferred = db.enqueue_presence_checks(
        conn, "ceskereality", "byt", "pronajem", _cands(10_854), active_rows=16_924)
    assert (queued, deferred) == (10_854, 0)
    assert conn.refusals == []


def test_a_malformed_cap_setting_still_throttles():
    """The cap fails closed: unreadable settings mean the defaults, not 'no cap'."""
    conn = _Conn(cap="not a json object")
    queued, deferred = db.enqueue_presence_checks(
        conn, "idnes", "byt", "prodej", _cands(5_000), active_rows=30_000)
    assert queued == 3_000 and deferred == 2_000


def test_nothing_to_nominate_touches_nothing():
    conn = _Conn(cap={"fraction": 0.1, "min_rows": 2000, "overrides": []})
    assert db.enqueue_presence_checks(conn, "remax", "byt", "prodej", [], active_rows=10) == (0, 0)
    assert conn.executed == []
