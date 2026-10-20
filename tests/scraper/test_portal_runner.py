"""Hermetic tests for scraper.portal_runner: the generic index-walk + detail-drain
loops, driven by a fake Portal. The queue ops the runner calls directly
(db.claim_detail_batch / complete_detail / fail_detail / reclaim_stale_claims)
are monkeypatched.
"""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from datetime import datetime, timedelta, timezone

from scraper import delist_policy, portal_factory, portal_runner
from scraper.portal_runner import DrainItem


class _Conn:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.closed = False
        self.broken = False
        self.rolled_back = 0
        self._close_error = close_error

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _is_drop(exc: BaseException) -> bool:
    """A connection-drop style OperationalError (vs a deadlock/serialization
    rollback, which leaves the socket usable)."""
    return isinstance(exc, psycopg.OperationalError) and not isinstance(
        exc, (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
    )


class _FakePortal:
    source = "fake"
    index_rate = 1.0

    def __init__(self, *, categories=None, reached_end=True,
                 result_size=2, fetch_kinds=None, walk_fails=None, conn_close_error=None,
                 write_errors=None, reconnect_conns=False) -> None:
        self._categories = categories if categories is not None else ["A", "B"]
        # `reached_end` is STRUCTURAL: "the walk reached the portal's end", not
        # "the counts reconciled". `result_size` is the portal's declared total,
        # which drives the numeric verdict the runner now only logs.
        self._reached_end = reached_end
        self._result_size = result_size
        self._fetch_kinds = fetch_kinds or {}
        self._walk_fails = walk_fails or set()
        # Successive exceptions raised by write_details (None = let it succeed),
        # used to simulate a transient drop/deadlock on a flush.
        self._write_errors = list(write_errors or [])
        self._reconnect_conns = reconnect_conns
        self.conn = _Conn(close_error=conn_close_error)
        self.conns = [self.conn]            # every connection handed out, in order
        self.connect_drain_calls = 0
        self.calls: dict[str, list] = {
            "walk": [], "active_count": [],
            "write": [], "failure": [],
        }

    def categories(self):
        return list(self._categories)

    def category_labels(self, c):
        return (str(c), "t")

    def connect_index(self):
        return self.conn

    def connect_drain(self):
        self.connect_drain_calls += 1
        if self._reconnect_conns and self.connect_drain_calls > 1:
            c = _Conn()
            self.conns.append(c)
            return c
        return self.conn

    def walk_category(self, c, conn, dry_run, limiter, deadline=None):
        self.calls["walk"].append(c)
        self.calls.setdefault("deadline", []).append(deadline)
        if c in self._walk_fails:
            raise RuntimeError(f"blocked {c}")
        return ({1, 2}, {"found_new": 2, "enqueued": 2}, self._result_size, 1, self._reached_end)

    def active_count(self, conn, c):
        self.calls["active_count"].append(c)
        return 5

    def make_client(self, limiter):
        return object()

    def fetch_detail(self, client, native_id, ref):
        kind = self._fetch_kinds.get(native_id, "ok")
        return DrainItem(
            native_id=native_id, kind=kind,
            payload=native_id, error=("boom" if kind == "error" else None),
        )

    def write_details(self, conn, items):
        # Sorted: intra-batch order is NOT a contract. The drain collects fetch
        # results via as_completed(), which yields already-finished futures in
        # set order — memory-address dependent, so even detail_workers=1 can
        # buffer ["2","1"]. (Same posture as the `reached_end` capture below.)
        self.calls["write"].append(sorted(it.native_id for it in items))
        if self._write_errors:
            exc = self._write_errors.pop(0)
            if exc is not None:
                if _is_drop(exc):
                    conn.broken = True   # a drop kills the socket -> reconnect
                raise exc
        return {"new": len(items), "updated": 0, "unchanged": 0, "images_discovered": 0}

    def record_failure(self, conn, native_id, message):
        self.calls["failure"].append(native_id)


# --- run_index_walk ---------------------------------------------------------


def _nominations(monkeypatch):
    """Capture what the runner nominates for a page check (rule #3, 2026-09-07:
    a walk that reached the portal's end nominates the rows it did not see; the
    drain's page visit decides)."""
    cap: dict[str, list] = {"candidates": [], "queued": [], "orphans": []}

    def fake_candidates(conn, source, scope, seen, *, seen_key="native"):
        cap["candidates"].append((scope.get("category_main"), set(seen), seen_key))
        return ([("7", "https://x/7", None), ("8", "https://x/8", None)], 9)

    def fake_orphans(conn, source, scopes, seen, *, seen_key="native"):
        cap["orphans"].append(([dict(sc) for sc in scopes], seen))
        return cap.get("orphan_rows", ([], 0))

    def fake_enqueue(conn, source, cm, ct, candidates, *, active_rows, subtype=None):
        if candidates:                      # the real one queues nothing for an empty list
            cap["queued"].append((cm, [c[0] for c in candidates], active_rows))
            cap.setdefault("scopes", []).append((cm, ct, subtype))
        return len(candidates), 0

    monkeypatch.setattr(portal_runner.db, "presence_candidates", fake_candidates)
    monkeypatch.setattr(portal_runner.db, "orphan_candidates", fake_orphans)
    monkeypatch.setattr(portal_runner.db, "enqueue_presence_checks", fake_enqueue)
    return cap


def test_index_walk_nominates_unseen_rows_when_the_walk_reached_the_portals_end(monkeypatch):
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _, _ in cap["candidates"]] == ["A", "B"]
    assert cap["candidates"][0][1] == {1, 2}            # the walk's seen set is what is excluded
    assert [c for c, _, _ in cap["queued"]] == ["A", "B"]
    assert agg["index_pages"] == 2
    assert agg["listings_inactive"] == 0                # the walk itself flips nothing now
    assert sum(c["listings_to_verify"] for c in agg["by_category"]) == 4
    assert agg["listings_scraped_new"] == 0
    assert p.conn.closed
    # the structural verdict lands in scrape_runs.by_category, the orphans last; the numeric one
    # is only logged
    assert [(c["category_main"], c["walk_reached_end"]) for c in agg["by_category"]] == [
        ("A", True), ("B", True), (None, True),
    ]
    assert not any({"walk_coverage", "active_db"} & c.keys() for c in agg["by_category"])


def test_index_walk_nominates_nothing_when_our_own_stop_fired(monkeypatch):
    """A walk WE cut short (deadline, page cap, --limit, a slice never reached,
    an exception, an uncorroborated empty page) has an unseen set that is the
    part of the portal it never reached, not evidence -- even when the counts
    reconcile perfectly."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=False)
    _rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert cap["candidates"] == [] and cap["queued"] == []
    assert [(c["walk_reached_end"], c["collected"], c[portal_runner.DECLARED_TOTAL_KEY])
            for c in agg["by_category"]] == [(False, 2, 2), (False, 2, 2)]


def test_index_walk_nominates_a_short_count_and_alarms_on_it(monkeypatch, caplog):
    """The motivating case (ceskereality houses-for-sale, 20,964 rows): every
    unit paged to its own last page while the declared totals add up short. The
    numeric verdict is `incomplete` and must NOT veto -- it is logged, and the
    gap is nominated so the page decides."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(categories=["A"], reached_end=True, result_size=87)
    with caplog.at_level("WARNING"):
        _rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert [c for c, _, _ in cap["queued"]] == ["A"]
    assert agg["by_category"][0]["walk_reached_end"] is True
    line = [m for m in caplog.messages if m.startswith("COVERAGE cm=A")]
    assert len(line) == 1
    assert "(coverage=incomplete)" in line[0]
    assert "the gap is nominated" in line[0]


def test_index_walk_skip_line_names_our_stop_not_the_count(monkeypatch, caplog):
    """The skip is about WHO stopped the walk. A reader of this line must not go
    looking for a count mismatch that is not there."""
    _nominations(monkeypatch)
    p = _FakePortal(categories=["A"], reached_end=False)
    with caplog.at_level("WARNING"):
        portal_runner.run_index_walk(p, dry_run=False)
    line = [m for m in caplog.messages if m.startswith("VERIFY skipped cm=A")]
    assert len(line) == 1
    assert "did not reach the portal's end" in line[0]
    assert "an unfinished walk nominates nothing" in line[0]


class _ReconcilingPortal(_FakePortal):
    """A walk whose counts come from the shared sighting diff, as every adapter's do."""

    price_change_min_pct = 0.0

    def walk_category(self, c, conn, dry_run, limiter, deadline=None):
        self.calls["walk"].append(c)
        sightings = {f"{c}1": (None, 100), f"{c}2": (None, 200), f"{c}3": (None, 300)}
        counts = portal_runner.reconcile_sightings(
            conn, self.source, sightings, min_change_pct=self.price_change_min_pct)
        return set(sightings), counts, 3, 1, False


def test_walk_counts_from_reconcile_reach_by_category(monkeypatch):
    stored = {"A1": {"id": 1, "price_czk": 100}, "B2": {"id": 2, "price_czk": 1}}
    monkeypatch.setattr(
        portal_runner.db, "index_summary_native",
        lambda _c, _s, ids: {n: stored[n] for n in ids if n in stored})
    monkeypatch.setattr(portal_runner.db, "touch_listings_by_id", lambda _c, ids: len(ids))
    monkeypatch.setattr(portal_runner.db, "enqueue_detail", lambda _c, _s, entries: len(entries))
    p = _ReconcilingPortal(reached_end=False)
    _rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert [(c["listings_found_new"], c["listings_enqueued"]) for c in agg["by_category"]] == [
        (2, 2), (2, 3),
    ]
    assert agg["listings_found_new"] == 4


def test_a_declared_scope_reaches_the_query_and_the_throttle(monkeypatch):
    """bazos narrows to a subtype, remax/maxima widen to an agenda: the nomination query, the
    throttle and the operator override all see the scope the portal declares."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, categories=["A"])
    p.presence_scope = lambda c: {"category_main": c, "category_type": "t", "subtype": "chata"}
    portal_runner.run_index_walk(p, dry_run=False)
    assert cap["scopes"] == [("A", "t", "chata")]
    p2 = _FakePortal(reached_end=True, categories=["A", "B"])
    p2.presence_scope = lambda c: {"category_type": "t"}
    cap2 = _nominations(monkeypatch)
    portal_runner.run_index_walk(p2, dry_run=False)
    assert cap2["scopes"] == [(None, "t", None)]           # one agenda: one nomination
    assert cap2["orphans"] == [([{"category_type": "t"}], {1, 2})]


def test_index_walk_that_saw_nothing_nominates_nothing(monkeypatch):
    """A measured zero is a complete walk, but an empty seen set would nominate
    the WHOLE scope (`<> ALL('{}')` is true for every row) -- exactly when the
    portal is least trustworthy (a retired slug, a throttled shell page)."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, categories=["A"])
    p.walk_category = lambda c, conn, dry_run, limiter, deadline=None: (set(), {"found_new": 0, "enqueued": 0}, 0, 1, True)
    portal_runner.run_index_walk(p, dry_run=False)
    assert cap["candidates"] == [] and cap["queued"] == []


def _indexless(reached_end: bool = True) -> _FakePortal:
    """`web`: one agenda descriptor with no labels, a walk that sees nothing by construction."""
    p = _FakePortal(reached_end=reached_end, categories=["agenda"])
    p.indexless = True
    p.category_labels = lambda c: (None, None)
    p.walk_category = lambda c, conn, dry_run, limiter, deadline=None: (
        set(), {"found_new": 0, "enqueued": 0}, 0, 0, reached_end)
    return p


def test_an_indexless_portals_ads_are_all_orphans_of_its_empty_walk(monkeypatch, caplog):
    """`web` has no index: its walk sees nothing BY CONSTRUCTION, so the empty-seen refusal
    would mean its ads are never re-checked and never delisted. Its unit has no scope, so every
    active ad is an orphan, nominated with no seen set (the query's empty-array guard stays)."""
    cap = _nominations(monkeypatch)
    cap["orphan_rows"] = ([("https://a.example/1", "https://a.example/1", None)], 1)
    with caplog.at_level("WARNING", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_index_walk(_indexless(), dry_run=False)
    assert rc == 0 and cap["candidates"] == []
    assert cap["orphans"] == [([], None)]
    assert cap["queued"] == [(None, ["https://a.example/1"], 1)]
    assert cap["scopes"] == [(None, "", None)]
    assert agg["by_category"][0]["walk_reached_end"] is True
    assert not [r for r in caplog.records if "VERIFY skipped" in r.getMessage()]
    assert not [r for r in caplog.records if r.getMessage().startswith("COVERAGE")]


def test_an_indexless_walk_cut_short_nominates_nothing(monkeypatch):
    cap = _nominations(monkeypatch)
    cap["orphan_rows"] = ([("x", "x", None)], 1)
    portal_runner.run_index_walk(_indexless(reached_end=False), dry_run=False)
    assert cap["orphans"] == [] and cap["queued"] == []


def test_the_drains_gone_step_is_one_function_an_adapter_can_call(monkeypatch):
    """`web` learns a page is gone inside its write step (a redirect to a portal ad): it calls
    the runner's gone step, never a copy of rule 3's hysteresis."""
    calls: list[Any] = []
    monkeypatch.setattr(portal_runner.db, "gone_evidence", lambda _c, src, nid: (False, None))
    monkeypatch.setattr(portal_runner.db, "complete_detail",
                        lambda _c, src, ids, outcome="written": calls.append((src, ids, outcome)))
    assert portal_runner.record_gone_verdict(object(), "web", "u") == "no_row"
    assert calls == [("web", ["u"], "gone")]
    monkeypatch.setattr(portal_runner.db, "gone_evidence", lambda _c, src, nid: _CONFIRMING_PRIOR)
    monkeypatch.setattr(portal_runner.db, "mark_listing_inactive", lambda _c, src, nid: True)
    assert portal_runner.record_gone_verdict(object(), "web", "u") == "flipped"
    assert calls[-1] == ("web", ["u"], "flipped")


def test_a_page_that_is_another_portals_ad_is_rekeyed_under_that_portal(monkeypatch):
    """One identity per ad: the target is queued at NEW under its own portal (sreality by id,
    never by URL) and the row that led there gets the drain's own gone verdict."""
    from scraper.portal_identity import Resolved

    queued: list[Any] = []
    gone: list[Any] = []
    monkeypatch.setattr(portal_runner.db, "enqueue_detail",
                        lambda _c, src, entries: queued.append((src, entries)) or 1)
    monkeypatch.setattr(portal_runner, "record_gone_verdict",
                        lambda _c, src, nid, now=None: gone.append((src, nid)) or "no_row")
    target = Resolved("sreality", "2557083724", "https://www.sreality.cz/detail/x/2557083724")
    assert portal_runner.rekey_detail(object(), "web", "https://bit.example/a", target) == "no_row"
    assert queued == [("sreality", [("2557083724", None, None, portal_runner.db.QUEUE_PRIORITY_NEW)])]
    assert gone == [("web", "https://bit.example/a")]


class _SlicedPortal(_FakePortal):
    """Index slices that collapse onto one label (realitymix's `domy` + `chaty` -> dum): each
    slice sees its own ids, and slice "A2" shares its scope with "A1"."""

    def __init__(self, seen_by: dict[str, set[Any]], ended: set[str] | None = None) -> None:
        super().__init__(categories=list(seen_by))
        self._seen_by, self._ended = seen_by, ended if ended is not None else set(seen_by)

    def category_labels(self, c):
        return (c[0], "t")

    def walk_category(self, c, conn, dry_run, limiter, deadline=None):
        self.calls["walk"].append(c)
        return (set(self._seen_by[c]), {"found_new": 0, "enqueued": 0}, len(self._seen_by[c]), 1,
                c in self._ended)


def test_slices_sharing_a_scope_nominate_once_with_their_union(monkeypatch):
    """Nominating from ONE slice's seen set would nominate its sibling's whole population every
    walk: the scope nominates once, on its last slice, excluding every id its slices saw."""
    cap = _nominations(monkeypatch)
    _rc, agg = portal_runner.run_index_walk(
        _SlicedPortal({"A1": {"a"}, "B1": {"b"}, "A2": {"c"}}), dry_run=False)
    assert cap["candidates"] == [("B", {"b"}, "native"), ("A", {"a", "c"}, "native")]
    assert [c["listings_to_verify"] for c in agg["by_category"]] == [0, 2, 2, 0]   # + orphans


def test_a_sibling_slice_short_of_the_end_holds_back_its_scope(monkeypatch):
    cap = _nominations(monkeypatch)
    portal_runner.run_index_walk(
        _SlicedPortal({"A1": {"a"}, "B1": {"b"}, "A2": {"c"}}, ended={"A1", "B1"}), dry_run=False)
    assert cap["candidates"] == [("B", {"b"}, "native")]
    assert cap["orphans"] == []                            # not every unit reached the end


def test_a_slice_that_saw_nothing_still_counts_toward_its_scope(monkeypatch):
    """A nationwide-empty sibling slug (pronajem/chaty) saw nothing; it still reached the end, or
    its scope would wait for it for ever and never nominate again."""
    cap = _nominations(monkeypatch)
    portal_runner.run_index_walk(_SlicedPortal({"A1": {"a"}, "A2": set()}), dry_run=False)
    assert cap["candidates"] == [("A", {"a"}, "native")]


def test_an_all_null_seen_set_takes_the_saw_nothing_branch(monkeypatch, caplog):
    """A walk whose every id parsed to None saw nothing it can name: the NULLs are
    dropped, and the emptied set takes the saw-nothing branch, never the whole scope."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, categories=["A"])
    p.walk_category = lambda c, conn, dry_run, limiter, deadline=None: ({None}, {"found_new": 0, "enqueued": 0}, 1, 1, True)
    with caplog.at_level("WARNING", logger="scraper.portal_runner"):
        portal_runner.run_index_walk(p, dry_run=False)
    assert cap["candidates"] == [] and cap["queued"] == [] and cap["orphans"] == []
    assert any(m.startswith("VERIFY dropped 1 NULL id(s) from the seen set cm=A ct=t")
               for m in caplog.messages)
    assert any(m.startswith("VERIFY skipped cm=A ct=t: the walk saw no listings")
               for m in caplog.messages)
    assert "VERIFY orphans skipped: the walk saw no listings" in caplog.messages


def test_a_whole_end_reached_walk_nominates_the_ads_no_unit_covers(monkeypatch, caplog):
    """Rule #3 made total. A sighting is label-blind, a unit's nomination is not: an active ad
    whose label no unit walks (a NULL, an auction) is nominated once every unit reached the end,
    against every id the walk saw, at the flip cap of a scope with no category."""
    cap = _nominations(monkeypatch)
    cap["orphan_rows"] = ([("9", "https://x/9", None)], 3)
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        _rc, agg = portal_runner.run_index_walk(_FakePortal(reached_end=True), dry_run=False)
    assert cap["orphans"] == [(
        [{"category_main": "A", "category_type": "t"}, {"category_main": "B", "category_type": "t"}],
        {1, 2})]
    assert cap["queued"][-1] == (None, ["9"], 3) and cap["scopes"][-1] == (None, "", None)
    assert "VERIFY orphans candidates=1 queued=1 deferred=0 active=3" in caplog.messages
    # Recorded as one more by_category entry, NULL-labelled (an indexless unit's shape), so the
    # checks that sum `verify_deferred` and age a unit's end-reached walks see the orphans too.
    orphans = agg["by_category"][-1]
    assert (orphans["category_main"], orphans["category_type"], orphans["walk_reached_end"],
            orphans["listings_to_verify"], orphans["verify_deferred"],
            orphans[portal_runner.DECLARED_TOTAL_KEY], orphans["collected"]) == (
        None, None, True, 1, 0, 0, 0)
    assert set(orphans) == set(agg["by_category"][0])
    assert sum(c["listings_to_verify"] for c in agg["by_category"]) == 5


def test_a_failed_orphan_query_is_an_error_and_an_entry_short_of_the_end(monkeypatch, caplog):
    """Not one log line: the run counts the error and the orphans' entry did not reach the end,
    so the walks check ages their newest end-reached walk exactly as it ages a failed unit's."""
    _nominations(monkeypatch)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(portal_runner.db, "orphan_candidates", broken)
    with caplog.at_level("ERROR", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_index_walk(_FakePortal(reached_end=True), dry_run=False)
    assert rc == 0 and agg["errors"] == 1
    orphans = agg["by_category"][-1]
    assert (orphans["category_main"], orphans["walk_reached_end"],
            orphans[portal_runner.DECLARED_TOTAL_KEY], orphans["listings_to_verify"]) == (
        None, False, None, 0)
    assert any(m.startswith("VERIFY orphans failed") for m in caplog.messages)


@pytest.mark.parametrize("why", ["stopped_short", "failed", "narrowed", "dry_run"])
def test_orphans_wait_for_a_walk_of_the_whole_portal(monkeypatch, why):
    """An ad outside every walked unit is an orphan only when every unit of the portal's agenda
    was walked to its end: a unit cut short, a failed one, or a dispatch narrowed to some units
    (bazos --sale-type/--category) leaves the rest of the portal unproven."""
    cap = _nominations(monkeypatch)
    cap["orphan_rows"] = ([("9", "https://x/9", None)], 3)
    p = _FakePortal(reached_end=True, walk_fails={"A"} if why == "failed" else None)
    if why == "stopped_short":
        p = _SlicedPortal({"A1": {"a"}, "B1": {"b"}}, ended={"B1"})
    p.narrowed = why == "narrowed"
    portal_runner.run_index_walk(p, dry_run=why == "dry_run")
    assert cap["orphans"] == []


def test_null_ids_are_dropped_before_nomination(monkeypatch):
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, categories=["A"])
    p.walk_category = lambda c, conn, dry_run, limiter, deadline=None: ({"a", None}, {"found_new": 0, "enqueued": 0}, 2, 1, True)
    portal_runner.run_index_walk(p, dry_run=False)
    assert [seen for _cm, seen, _key in cap["candidates"]] == [{"a"}]


def test_index_walk_dry_run_uses_no_connection(monkeypatch):
    cap = _nominations(monkeypatch)
    p = _FakePortal()
    portal_runner.run_index_walk(p, dry_run=True)
    # walk still runs (conn=None passed through) but nothing is nominated
    assert p.calls["walk"] == ["A", "B"]
    assert cap["queued"] == []


def test_index_walk_bumps_index_pages_per_committed_category(monkeypatch):
    _nominations(monkeypatch)
    # With a run_id, each category's pages are committed immediately so Health
    # liveness survives a SIGKILL before finalize.
    p = _FakePortal(reached_end=True)
    bumps: list[tuple[int, int]] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_index_pages",
        lambda conn, run_id, n: bumps.append((run_id, n)),
    )
    portal_runner.run_index_walk(p, dry_run=False, run_id=42)
    assert bumps == [(42, 1), (42, 1)]  # one bump per category (cat_pages=1)


def test_index_walk_does_not_bump_without_run_id(monkeypatch):
    _nominations(monkeypatch)
    p = _FakePortal()
    bumps: list = []
    monkeypatch.setattr(
        portal_runner.db, "bump_index_pages",
        lambda *a: bumps.append(a),
    )
    portal_runner.run_index_walk(p, dry_run=False)  # no run_id
    assert bumps == []


def test_index_walk_clean_stops_when_budget_already_blown(monkeypatch):
    # max_seconds with a deadline in the past -> stop before any category, finalize
    # cleanly (no SIGKILL). monotonic: first call sets the deadline, later calls
    # are past it.
    p = _FakePortal(reached_end=True)
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 9999.0

    monkeypatch.setattr(portal_runner.time, "monotonic", fake_monotonic)
    cap = _nominations(monkeypatch)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False, max_seconds=10)
    assert rc == 0
    assert p.calls["walk"] == []            # budget blown -> no category walked
    assert cap["queued"] == []              # nothing walked -> nothing nominated (rule #3)
    assert agg["index_pages"] == 0


def test_index_walk_runs_all_when_budget_not_reached(monkeypatch):
    _nominations(monkeypatch)
    # A generous budget never trips the deadline -> full walk, same as no budget.
    p = _FakePortal(reached_end=True)
    monkeypatch.setattr(portal_runner.time, "monotonic", lambda: 0.0)
    portal_runner.run_index_walk(p, dry_run=False, max_seconds=10_000)
    assert p.calls["walk"] == ["A", "B"]


def test_index_walk_one_failed_category_stays_green_with_error_recorded(monkeypatch):
    # One category's walk raises -> the run stays rc=0 (partial failure is
    # tolerated) but the failure is COUNTED in the aggregate, and the other
    # category is still walked + its unseen rows nominated.
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, walk_fails={"A"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert agg["errors"] == 1
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _, _ in cap["queued"]] == ["B"]   # failed cat nominates nothing
    assert agg["index_pages"] == 1          # only B contributed pages
    assert sum(c["listings_to_verify"] for c in agg["by_category"]) == 2


def test_index_walk_all_categories_failed_returns_nonzero_rc(monkeypatch):
    # EVERY category failed -> the portal is fully blocked (e.g. WAF 403s the
    # runner egress); the run must go red, not record a green zero-listing walk.
    cap = _nominations(monkeypatch)
    p = _FakePortal(reached_end=True, walk_fails={"A", "B"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc != 0
    assert agg["errors"] == 2
    assert cap["queued"] == []
    assert agg["index_pages"] == 0


def test_every_by_category_entry_says_how_its_declared_total_reads(monkeypatch):
    """The health checks read a unit's declared total from these keys and never from a portal's
    name: the total under DECLARED_TOTAL_KEY, beside total_is_count (the total is what the walk
    saw) and declared_is_upper_bound. A portal that declares neither, as these fakes, says False."""
    _nominations(monkeypatch)
    _rc, agg = portal_runner.run_index_walk(_FakePortal(result_size=87), dry_run=False)
    # scripts/checks/scraping.py reads every stored walk's declared total under this name
    assert portal_runner.DECLARED_TOTAL_KEY == "sreality_result_size"
    assert [(c[portal_runner.DECLARED_TOTAL_KEY], c["total_is_count"],
             c["declared_is_upper_bound"]) for c in agg["by_category"]] == [
        (87, False, False), (87, False, False), (0, False, False)]   # the orphans declare 0
    counted = _FakePortal(walk_fails={"B"})
    counted.total_is_count = True
    counted.coverage_denominator_is_upper_bound = True
    _rc, agg = portal_runner.run_index_walk(counted, dry_run=False)
    # the failed category's entry carries them too
    assert [(c["total_is_count"], c["declared_is_upper_bound"]) for c in agg["by_category"]] == [
        (True, True), (True, True)]


def test_an_upper_bound_denominator_logs_its_coverage_gap_at_info(monkeypatch, caplog):
    """A national total its slices cannot reach would warn on every walk: noise, not an alarm."""
    _nominations(monkeypatch)
    p = _FakePortal(categories=["A"], reached_end=True, result_size=87)
    p.coverage_denominator_is_upper_bound = True
    with caplog.at_level("INFO"):
        portal_runner.run_index_walk(p, dry_run=False)
    (line,) = [r for r in caplog.records if r.getMessage().startswith("COVERAGE cm=A")]
    assert line.levelname == "INFO"


def test_only_the_portals_whose_total_is_what_they_saw_declare_it() -> None:
    """remax and maxima state only per-agenda totals, so their walk returns what it saw as each
    category's total. Data cannot tell (56 of 110 units had declared = collected on every walk in
    14 d), so each adapter declares it and every other portal keeps PortalDefaults' False."""
    declared = {
        source for source, (module, cls) in portal_factory.PORTAL_CLASSES.items()
        if getattr(getattr(importlib.import_module(module), cls), "total_is_count", False)}
    assert declared == {"maxima", "remax"}
    assert portal_runner.PortalDefaults.total_is_count is False


# --- run_detail_drain -------------------------------------------------------


# A gone verdict the ledger already confirms (an earlier verdict older than the dwell): the
# tests' default, so "a confirmed gone flips" reads as before; the hysteresis tests below
# swap in a younger / absent / refuted prior.
_CONFIRMING_PRIOR = (True, datetime.now(timezone.utc) - delist_policy.GONE_DWELL - timedelta(hours=1))


def _patch_queue(monkeypatch, claim_batches, evidence=_CONFIRMING_PRIOR):
    cap = {"complete": [], "complete_outcomes": [], "fail": [], "claim_n": [], "reclaim": 0,
           "flip": [], "evidence": []}
    monkeypatch.setattr(
        portal_runner.db, "mark_listing_inactive",
        lambda _c, src, nid: cap["flip"].append((src, nid)) or True,
    )
    monkeypatch.setattr(
        portal_runner.db, "gone_evidence",
        lambda _c, src, nid: cap["evidence"].append((src, nid)) or evidence,
    )
    it = iter(list(claim_batches) + [[]])
    monkeypatch.setattr(
        portal_runner.db, "reclaim_stale_claims",
        lambda _c, _src: cap.__setitem__("reclaim", cap["reclaim"] + 1) or 0,
    )

    def _claim(_c, _src, n):
        cap["claim_n"].append(n)
        return next(it, [])

    monkeypatch.setattr(portal_runner.db, "claim_detail_batch", _claim)

    def _complete(_c, _src, ids, outcome="written"):
        cap["complete"].append(sorted(ids))
        cap["complete_outcomes"].append(outcome)

    monkeypatch.setattr(portal_runner.db, "complete_detail", _complete)
    monkeypatch.setattr(
        portal_runner.db, "fail_detail",
        lambda _c, _src, ids, msg, **k: cap["fail"].append(sorted(ids)),
    )
    monkeypatch.setattr(portal_runner.db, "claimable_counts", lambda _c, _s=None: {})
    return cap


def test_detail_drain_batches_and_completes(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]])
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"]]
    assert cap["complete"] == [["1", "2"]]
    assert agg["listings_scraped_new"] == 2
    assert p.conn.closed


def test_detail_drain_routes_gone_and_error(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("10", None, None, None), ("11", None, None, None), ("12", None, None, None)]])
    p = _FakePortal(fetch_kinds={"11": "gone", "12": "error"})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert cap["flip"] == [(p.source, "11")]
    assert p.calls["failure"] == ["12"]
    assert cap["fail"] == [["12"]]
    assert sorted(x for b in p.calls["write"] for x in b) == ["10"]
    completions = list(zip(cap["complete"], cap["complete_outcomes"]))
    assert (["11"], "flipped") in completions
    assert (["10"], "written") in completions
    assert sorted(x for b in cap["complete"] for x in b) == ["10", "11"]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 1


def test_detail_drain_respects_max_claims(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]])
    p = _FakePortal()
    portal_runner.run_detail_drain(p, 2, False, detail_workers=1, detail_rate=1.0)
    assert cap["claim_n"] == [2]


def test_detail_drain_dry_run_does_not_claim(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None)]])
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(p, 50, True, detail_workers=1, detail_rate=1.0)
    assert rc == 0 and agg == {}
    assert cap["claim_n"] == []   # dry-run never claims


def test_detail_drain_bumps_counts_per_chunk_without_double_count(monkeypatch):
    # With a run_id the counts are persisted per chunk (crash/SIGKILL-survivable);
    # the SUM of bumps must equal the final agg, NOT 2x (finalize won't re-write
    # them). A small batch size forces both in-loop and post-loop flushes.
    monkeypatch.setattr(portal_runner, "DETAIL_BATCH_SIZE", 2)
    cap = _patch_queue(monkeypatch, [
        [("1", None, None, None), ("2", None, None, None), ("3", None, None, None)],
        [("4", None, None, None), ("5", None, None, None)],
    ])
    bumps: list[dict[str, int]] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda conn, run_id, **kw: bumps.append({"run_id": run_id, **kw}),
    )
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, run_id=7)
    assert rc == 0
    assert bumps and all(b["run_id"] == 7 for b in bumps)
    assert sum(b["scraped_new"] for b in bumps) == agg["listings_scraped_new"] == 5
    assert sum(b["found_new"] for b in bumps) == 5
    assert sum(b["updated"] for b in bumps) == agg["listings_updated"] == 0


def test_detail_drain_does_not_bump_without_run_id(monkeypatch):
    _patch_queue(monkeypatch, [[("1", None, None, None)]])
    bumps: list = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda *a, **k: bumps.append((a, k)),
    )
    p = _FakePortal()
    portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert bumps == []   # no run_id -> never bumps


def test_detail_drain_time_budget_finalizes_cleanly(monkeypatch):
    # A wall-clock budget makes the drain stop + finalize rather than overrun the
    # job timeout (which would leave a 'stuck' scrape_run). monotonic() jumps far
    # past the tiny budget on the first loop check, so it stops before claiming.
    cap = _patch_queue(monkeypatch, [[("1", None, None, None)]])
    seq = iter(range(0, 1_000_000, 1000))
    monkeypatch.setattr(portal_runner.time, "monotonic", lambda: float(next(seq)))
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, max_seconds=1.0)
    assert rc == 0
    assert cap["claim_n"] == []      # budget exceeded → stopped before claiming
    assert p.conn.closed             # but finalized cleanly (no stuck run)


@pytest.mark.parametrize("reason,refused,stopped,line", [
    ("cap", 1, 0, "DRAIN stopped source=fake: the shared rate budget refused the run; "
                  "2 claims handed back untouched"),
    ("deadline", 0, 1, "DRAIN time budget reached mid-chunk; 2 claims handed back "
                       "source=fake"),
])
def test_detail_drain_hands_back_what_the_ledger_refused_and_stops(
    monkeypatch, caplog, reason, refused, stopped, line,
):
    # Every portal's fetch_detail turns the refusal into an ordinary "error" item.
    # Recording it would spend an attempt of a listing that did nothing wrong (five
    # and it is given up), so the claim goes back untouched and the drain stops. A
    # worker drain that runs out of time mid-chunk stops the same way, but that is a
    # healthy pass with a backlog, not a blocked portal, and is counted apart.
    cap = _patch_queue(monkeypatch, [
        [("1", None, None, None), ("2", None, None, None),
         ("3", None, None, None)],
        [("4", None, None, None)],
    ])
    released: list[list[str]] = []
    monkeypatch.setattr(
        portal_runner.db, "release_claims",
        lambda _c, _src, ids: released.append(sorted(ids)) or len(ids))

    class _Refused(_FakePortal):
        def make_client(self, limiter):
            self.limiter = limiter
            return object()

        def fetch_detail(self, client, native_id, ref):
            if native_id == "1":
                return super().fetch_detail(client, native_id, ref)
            self.limiter.refused, self.limiter.refused_reason = True, reason
            return DrainItem(native_id=native_id, kind="error", error="refused")

    p = _Refused()
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_detail_drain(
            p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert released == [["2", "3"]]
    assert cap["fail"] == [] and p.calls["failure"] == []
    assert len(cap["claim_n"]) == 1                 # the second chunk is never claimed
    assert p.calls["write"] == [["1"]]
    assert (agg["budget_refused"], agg["deadline_stopped"], agg["errors"]) == (
        refused, stopped, 0)
    assert line in caplog.messages


@pytest.mark.parametrize("max_claims,lease_n", [(3, 3), (200, 20), (None, 20)])
def test_detail_drain_hands_its_bound_and_a_lease_sized_to_its_work_to_the_limiter(
    monkeypatch, max_claims, lease_n,
):
    # Every leased slot moves the shared frontier, used or not: a worker drain with three
    # rows waiting leased twenty every 30 s and pushed the other callers back.
    seen: dict[str, Any] = {}

    def fake_build(source, rate, shared, **kw):
        seen.update(kw)
        return portal_runner.RateLimiter(rate)

    monkeypatch.setattr(portal_runner, "build_rate_limiter", fake_build)
    _patch_queue(monkeypatch, [])
    before = time.monotonic()
    portal_runner.run_detail_drain(
        _FakePortal(), max_claims, False, detail_workers=1, detail_rate=1.0,
        max_seconds=120.0, max_wait_s=120.0)
    # The deadline too: the loop checks its budget only between chunks, and a 403 storm
    # on a one-worker portal held one chunk under the worker's pass lock for an hour.
    assert before + 120.0 <= seen.pop("deadline") <= time.monotonic() + 120.0
    assert seen == {"lease_n": lease_n, "max_wait_s": 120.0}
    # An Actions drain (no bound) keeps its old behaviour: no deadline in the limiter.
    portal_runner.run_detail_drain(
        _FakePortal(), max_claims, False, detail_workers=1, detail_rate=1.0,
        max_seconds=120.0)
    assert seen["deadline"] is None and seen["max_wait_s"] is None


def test_detail_drain_swallows_teardown_close_failure(monkeypatch):
    # The pooler can silently drop the long-held drain connection; the teardown
    # conn.close() then raises OperationalError. Every batch already committed and
    # the caller finalizes the scrape_run on a SEPARATE connection, so a teardown
    # failure must NOT red the run (the historical ~1% false-red on detail_drain).
    _patch_queue(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]])
    p = _FakePortal(conn_close_error=OSError("server closed the connection unexpectedly"))
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0                            # did NOT propagate the close failure
    assert p.calls["write"] == [["1", "2"]]   # the listing writes still committed
    assert agg["listings_scraped_new"] == 2
    assert p.conn.closed                      # close() was attempted


def test_detail_drain_swallows_counts_bump_failure(monkeypatch):
    # Counts are post-commit bookkeeping; a transient pooler reset on the bump
    # must not red a drain whose listing data already committed.
    _patch_queue(monkeypatch, [[("1", None, None, None)]])

    def _boom(*a, **k):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(portal_runner.db, "bump_scrape_run_counts", _boom)
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, run_id=9)
    assert rc == 0                            # bump failure swallowed
    assert p.calls["write"] == [["1"]]        # data still committed
    assert agg["listings_scraped_new"] == 1


# --- transient-DB resilience (db.run_resilient on the drain's hot path) ------


def test_detail_drain_retries_flush_deadlock_on_same_conn(monkeypatch):
    # A deadlock victim on the batch upsert (sreality's historical ~1% red): the
    # flush is retried on the SAME connection (no reconnect) and the run stays
    # green. The batch write is idempotent, so the replay re-commits identically.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = _patch_queue(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]])
    p = _FakePortal(write_errors=[psycopg.errors.DeadlockDetected("deadlock detected"), None])
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"], ["1", "2"]]  # attempted twice (retry)
    assert cap["complete"] == [["1", "2"]]               # dequeued once, after success
    assert agg["listings_scraped_new"] == 2              # counts applied once, not doubled
    assert p.connect_drain_calls == 1                    # NO reconnect for a deadlock
    assert p.conn.rolled_back == 1                        # aborted txn cleared before retry


def test_detail_drain_reconnects_on_dropped_flush(monkeypatch):
    # The pooler drops the long-held connection mid-flush (realitymix's observed
    # 'SSL error: unexpected eof while reading'): run_resilient reconnects and
    # retries on a fresh connection, and the run stays green instead of reding.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = _patch_queue(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]])
    drop = psycopg.OperationalError("SSL error: unexpected eof while reading")
    p = _FakePortal(write_errors=[drop, None], reconnect_conns=True)
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"], ["1", "2"]]
    assert cap["complete"] == [["1", "2"]]
    assert agg["listings_scraped_new"] == 2
    assert p.connect_drain_calls == 2          # reconnected after the drop
    assert p.conns[0].closed                   # the broken connection was closed


def test_detail_drain_reds_on_persistent_db_outage(monkeypatch):
    # A genuine sustained outage must still surface (not spin forever): the flush
    # exhausts its retry budget and the exception propagates -> the run reds.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    _patch_queue(monkeypatch, [[("1", None, None, None)]])
    drop = psycopg.OperationalError("connection refused")
    p = _FakePortal(write_errors=[drop] * 8, reconnect_conns=True)
    with pytest.raises(psycopg.OperationalError):
        portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert len(p.calls["write"]) == portal_runner.db._RESILIENT_ATTEMPTS  # bounded, then give up


def test_detail_drain_gone_path_survives_transient_drop(monkeypatch):
    # The per-item gone bookkeeping (mark inactive + dequeue) is resilient too: a
    # drop while completing a gone listing reconnects rather than reding the run.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = {"complete": [], "calls": 0}
    monkeypatch.setattr(
        portal_runner.db, "reclaim_stale_claims", lambda *a, **k: 0)
    batches = iter([[("9", None, None, None)], []])
    monkeypatch.setattr(
        portal_runner.db, "claim_detail_batch", lambda *a, **k: next(batches, []))

    def _complete(_c, _src, ids, outcome="written"):
        cap["calls"] += 1
        if cap["calls"] == 1:
            _c.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        cap["complete"].append(sorted(ids))

    monkeypatch.setattr(portal_runner.db, "complete_detail", _complete)
    flips: list = []
    monkeypatch.setattr(
        portal_runner.db, "mark_listing_inactive",
        lambda _c, src, nid: flips.append((src, nid)) or True)
    monkeypatch.setattr(portal_runner.db, "gone_evidence", lambda *_a: _CONFIRMING_PRIOR)
    p = _FakePortal(fetch_kinds={"9": "gone"}, reconnect_conns=True)
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert agg["listings_inactive"] == 1
    assert cap["complete"] == [["9"]]          # dequeued after the reconnect retry
    assert p.connect_drain_calls == 2          # reconnected for the gone op
    # The whole op replays; the guarded flip makes the second call a no-op.
    assert flips == [("fake", "9"), ("fake", "9")]


def test_a_failed_flip_is_retried_not_completed_as_gone(monkeypatch, caplog):
    """A flip that raised used to be logged and then COMPLETED as gone: the queue
    row vanished while the listing stayed active. Now it is a failure, retried."""
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]])

    def _broken(_c, _src, _nid):
        raise psycopg.errors.UndefinedColumn("x")

    monkeypatch.setattr(portal_runner.db, "mark_listing_inactive", _broken)
    p = _FakePortal(fetch_kinds={"9": "gone"})
    with caplog.at_level("WARNING", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert cap["complete"] == []
    assert cap["fail"] == [["9"]]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 0
    assert any(m.startswith("could not mark id=9 inactive:") for m in caplog.messages)


def test_a_transient_flip_error_retries_inside_the_op(monkeypatch):
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]])
    calls = {"n": 0}

    def _flaky(_c, _src, _nid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("deadlock detected")
        return True

    monkeypatch.setattr(portal_runner.db, "mark_listing_inactive", _flaky)
    p = _FakePortal(fetch_kinds={"9": "gone"})
    _rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert calls["n"] == 2
    assert list(zip(cap["complete"], cap["complete_outcomes"])) == [(["9"], "flipped")]
    assert cap["fail"] == []
    assert agg["listings_inactive"] == 1 and agg["errors"] == 0


@pytest.mark.parametrize(("priority", "level"), [
    (portal_runner.db.QUEUE_PRIORITY_NEW, "INFO"),
    (portal_runner.db.QUEUE_PRIORITY_CHANGED, "WARNING"),
    (portal_runner.db.QUEUE_PRIORITY_VERIFY, "WARNING"),
])
def test_a_gone_flip_matching_no_listing_warns_only_for_a_row_we_held(monkeypatch, caplog, priority, level):
    """A NEW id has no listings row until its first write, so a gone first fetch
    matching nothing is routine; any other priority means the natural key broke."""
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]], evidence=(False, None))
    monkeypatch.setattr(portal_runner.db, "queue_priorities",
                        lambda _c, _s, nids: {n: priority for n in nids})
    p = _FakePortal(fetch_kinds={"9": "gone"})
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        _rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    hits = [r for r in caplog.records if r.getMessage() == "gone flip matched no listing source=fake id=9"]
    assert [r.levelname for r in hits] == [level]
    assert cap["flip"] == []  # nothing to flip, so the writer is not even asked
    assert list(zip(cap["complete"], cap["complete_outcomes"])) == [(["9"], "gone")]
    assert agg["listings_inactive"] == 1 and agg["errors"] == 0


def test_a_flip_or_an_already_inactive_row_logs_no_match(monkeypatch, caplog):
    cap = _patch_queue(monkeypatch, [[("8", None, None, None), ("9", None, None, None)]])
    monkeypatch.setattr(portal_runner.db, "mark_listing_inactive", lambda _c, _s, nid: nid == "8")
    p = _FakePortal(fetch_kinds={"8": "gone", "9": "gone"})
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert sorted(x for b in cap["complete"] for x in b) == ["8", "9"]
    assert not any("matched no listing" in m for m in caplog.messages)


# --- rule #3's hysteresis: one verdict records, the second (dwell later) flips ----------


def test_a_first_gone_verdict_is_recorded_not_flipped(monkeypatch, caplog):
    """The ledger holds no earlier verdict: the queue row completes as gone (the ledger row
    IS the evidence), the listing stays active for the next walk to re-nominate, and the
    run counts it as pending, not inactive."""
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]], evidence=(True, None))
    p = _FakePortal(fetch_kinds={"9": "gone"})
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert cap["evidence"] == [(p.source, "9")]
    assert cap["flip"] == []
    assert list(zip(cap["complete"], cap["complete_outcomes"])) == [(["9"], "gone")]
    assert cap["fail"] == []
    assert agg["listings_inactive"] == 0 and agg["errors"] == 0
    assert any(m.startswith("DETAIL id=9 gone (first verdict recorded, not flipped") for m in caplog.messages)
    assert any("gone=0 pending=1" in m for m in caplog.messages)


def test_a_second_verdict_inside_the_dwell_is_still_pending(monkeypatch):
    young = (True, datetime.now(timezone.utc) - delist_policy.GONE_DWELL + timedelta(minutes=5))
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]], evidence=young)
    p = _FakePortal(fetch_kinds={"9": "gone"})
    _rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert cap["flip"] == []
    assert list(zip(cap["complete"], cap["complete_outcomes"])) == [(["9"], "gone")]
    assert agg["listings_inactive"] == 0


def test_a_verdict_the_dwell_after_the_first_flips(monkeypatch, caplog):
    cap = _patch_queue(monkeypatch, [[("9", None, None, None)]])  # the confirming default
    p = _FakePortal(fetch_kinds={"9": "gone"})
    with caplog.at_level("INFO", logger="scraper.portal_runner"):
        _rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert cap["flip"] == [(p.source, "9")]
    assert list(zip(cap["complete"], cap["complete_outcomes"])) == [(["9"], "flipped")]
    assert agg["listings_inactive"] == 1
    assert "DETAIL id=9 gone (is_active=false)" in caplog.messages


def test_drain_record_failure_drop_on_queue_bump_does_not_replay_ledger(monkeypatch):
    # The failure path bumps TWO non-idempotent counters: record_failure
    # (listing_fetch_failures.attempts+1) then fail_detail (queue attempts+1). They
    # are SPLIT into two run_resilient calls so a transient drop on the queue bump
    # retries ONLY fail_detail — it must never replay (double-advance) the already-
    # committed ledger bump, which would retire a still-retryable listing early.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    record_calls = {"n": 0}
    fail_calls = {"n": 0}

    def _record_failure(_c, _nid, _msg):
        record_calls["n"] += 1

    def _fail_detail(c, _src, _ids, _msg, **k):
        fail_calls["n"] += 1
        if fail_calls["n"] == 1:          # the queue bump drops the first time
            c.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(portal_runner.db, "fail_detail", _fail_detail)
    conns: list[_Conn] = []

    def _reconnect() -> _Conn:
        c = _Conn()
        conns.append(c)
        return c

    portal = SimpleNamespace(source="fake", record_failure=_record_failure)
    conn0 = _Conn()
    out = portal_runner._drain_record_failure(portal, conn0, "55", "boom", _reconnect)

    assert record_calls["n"] == 1     # ledger bump applied EXACTLY once (not replayed)
    assert fail_calls["n"] == 2       # queue bump retried after the drop
    assert conn0.broken and conn0.closed
    assert out is conns[-1]           # returned the reconnected conn


def test_run_resilient_closes_self_opened_conn_on_exhaustion(monkeypatch):
    # On the give-up path run_resilient must close a connection IT opened (the
    # caller never received it), but never the caller's original.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    opened: list[_Conn] = []

    def _reconnect() -> _Conn:
        c = _Conn()
        c.broken = True          # every reconnect yields an already-doomed socket
        opened.append(c)
        return c

    original = _Conn()
    original.broken = True

    def _always_drops(_c):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        portal_runner.db.run_resilient(
            original, _always_drops, reconnect=_reconnect, attempts=3, base_delay=0)

    assert original.closed                         # broken original closed in-loop
    assert opened and all(c.closed for c in opened)  # every self-opened conn closed


# --- run_phase: scrape_runs lifecycle + honest crash accounting -------------
#
# W0.2. This lifecycle used to be copy-pasted into every `*_main.py`, and the
# `finally: _finalize(run_id, {}, drain=True)` in all nine copies is why the six
# portals that crashed on 2026-08-26 recorded `scrape_runs.errors = 0`.


class _PhaseRecorder:
    """Captures what run_phase writes to scrape_runs, via the db seam."""

    def __init__(self, monkeypatch, *, run_id: int | None = 42) -> None:
        self.starts: list[tuple[str, str]] = []
        self.finals: list[tuple[int, dict[str, Any]]] = []
        self.bumps: list[tuple[int, dict[str, Any]]] = []
        self.connects = 0

        def _connect() -> _Conn:
            self.connects += 1
            return _Conn()

        monkeypatch.setattr(portal_runner.db, "connect", _connect)
        monkeypatch.setattr(
            portal_runner.db, "scrape_run_start",
            lambda _c, run_type, source: (
                self.starts.append((run_type, source)) or run_id),
        )
        monkeypatch.setattr(
            portal_runner.db, "scrape_run_finalize",
            lambda _c, rid, **kw: self.finals.append((rid, kw)),
        )
        monkeypatch.setattr(
            portal_runner.db, "bump_scrape_run_counts",
            lambda _c, rid, **kw: self.bumps.append((rid, kw)),
        )


def test_run_phase_finalizes_a_clean_index_run(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 4, "errors": 0}), False,
    )
    assert rc == 0
    assert rec.starts == [("index", "fake")]        # source comes off the portal
    assert rec.finals[0][1]["index_pages"] == 4
    assert rec.finals[0][1]["errors"] == 0
    assert rec.bumps == []                          # nothing crashed


def test_run_phase_crash_bumps_errors_and_leaves_ended_at_unstamped(monkeypatch):
    """The regression this wave exists for: a drain that dies mid-flight must not
    be recorded as a finished run with errors=0."""
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise psycopg.errors.CheckViolation(
            'new row for relation "listings" violates check constraint '
            '"listings_area_basis_check"'
        )

    monkeypatch.setattr(portal_runner, "run_detail_drain", _crash)
    with pytest.raises(psycopg.errors.CheckViolation):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)

    assert rec.bumps == [(42, {"errors": 1})]   # the run says it failed
    assert rec.finals == []                     # and never claims it completed


def test_run_phase_crash_on_the_index_lane_is_recorded_too(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("index walk fell over")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "index", _crash, False)
    # The index lane previously returned early from _finalize on an empty agg, so a
    # crashed walk wrote nothing at all — not even an error.
    assert rec.bumps == [(42, {"errors": 1})]
    assert rec.finals == []


def test_run_phase_records_the_crash_before_the_exception_propagates(monkeypatch):
    """Ordering matters: the process may be dying, so the error has to be on the
    row before the exception leaves run_phase."""
    _PhaseRecorder(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda _c, rid, **kw: seen.append("bumped"),
    )

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)
        seen.append("propagated")
    assert seen == ["bumped"]


def test_run_phase_uses_drain_finalize_semantics_only_for_the_drain(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    p = _FakePortal()
    monkeypatch.setattr(
        portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_updated": 3}),
    )
    portal_runner.run_phase(
        p, "index", lambda portal, dry_run, **kw: (0, {"index_pages": 1}), False)
    portal_runner.run_phase(p, "detail", portal_runner.run_detail_drain, False)
    # The drain persists counters per chunk, so its finalize must not re-write them.
    assert [kw["bump_already_applied"] for _rid, kw in rec.finals] == [False, True]


def test_run_phase_dry_run_touches_no_scrape_run(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 2}), True)
    assert rc == 0
    assert rec.starts == [] and rec.finals == [] and rec.bumps == []


def test_run_phase_dry_run_crash_records_nothing(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, True)
    assert rec.bumps == []


def test_run_phase_survives_a_failed_scrape_run_start(monkeypatch):
    """No run row (a DB hiccup at start) must not become a crash of its own."""
    rec = _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "scrape_run_start",
        lambda _c, run_type, source: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 1}), False)
    assert rc == 0
    assert rec.finals == []          # run_id is None -> nothing to finalize
    assert rec.bumps == []


def test_run_phase_crash_recording_failure_never_masks_the_real_exception(monkeypatch):
    """Bookkeeping is best-effort: if the DB is the thing that is down, the caller
    must still see the ORIGINAL error, not a secondary one from the bump."""
    _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda *_a, **_k: (_ for _ in ()).throw(psycopg.OperationalError("db gone")),
    )

    def _crash(portal, dry_run, **kw):
        raise ValueError("the real problem")

    with pytest.raises(ValueError, match="the real problem"):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)


# --- the crash log line: what says which portal and lane died ---------------


def test_run_phase_crash_logs_its_portal_and_lane(monkeypatch, caplog):
    """The chokepoint has portal + lane in scope, and the crash line carries both; the
    workflow's job cell carries the line as the run's reason."""
    _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise psycopg.errors.CheckViolation(
            'new row for relation "listings" violates check constraint '
            '"listings_area_basis_check"'
        )

    with caplog.at_level("ERROR", logger="scraper.portal_runner"):
        with pytest.raises(psycopg.errors.CheckViolation):
            portal_runner.run_phase(_FakePortal(), "detail", _crash, False)
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PHASE")]
    assert "source=fake lane=detail" in line and "listings_area_basis_check" in line


def test_a_crash_with_no_scrape_run_row_is_logged_and_writes_nothing(
        monkeypatch, caplog):
    """No row to bump means nothing to write: the crash is logged and re-raised."""
    rec = _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "scrape_run_start",
        lambda _c, run_type, source: (_ for _ in ()).throw(RuntimeError("no db")),
    )

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with caplog.at_level("ERROR", logger="scraper.portal_runner"):
        with pytest.raises(RuntimeError, match="boom"):
            portal_runner.run_phase(_FakePortal(), "index", _crash, False)
    assert rec.bumps == []
    assert rec.connects == 1, "the failed scrape_run_start's connection only"
    assert any(r.getMessage().startswith("PHASE crashed (source=fake lane=index)")
               for r in caplog.records)


def test_run_phase_passes_run_id_and_kwargs_through_to_the_runner(monkeypatch):
    _PhaseRecorder(monkeypatch)
    seen: dict[str, Any] = {}

    def _runner(portal, dry_run, **kw):
        seen.update(kw)
        return (0, {})

    portal_runner.run_phase(
        _FakePortal(), "detail", _runner, False, max_claims=17, detail_workers=3)
    assert seen["run_id"] == 42
    assert seen["max_claims"] == 17 and seen["detail_workers"] == 3


# --- the gone-rate breaker (rule #3's last rail) ---------------------------


def test_drain_breaker_stops_flipping_when_ingest_fetches_mostly_read_gone(monkeypatch, caplog):
    """A portal answering every page with its gone signal (consent redirect,
    WAF 404s) is not the market. Ingest rows were on the index minutes ago, so
    once a majority of them read gone the run stops flipping and records the
    rest as failures to retry later."""
    ids = [str(i) for i in range(1, 26)]
    cap = _patch_queue(monkeypatch, [[(i, None, None, None) for i in ids]])
    p = _FakePortal(fetch_kinds={i: "gone" for i in ids})
    with caplog.at_level("ERROR", logger="scraper.portal_runner"):
        rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    # The 20th observation completes the sample and trips the breaker before
    # that item is routed, so 19 flipped and the remaining 6 were recorded as
    # failures to retry later.
    flipped = delist_policy.GoneRateBreaker.MIN_SAMPLE - 1
    assert len(cap["flip"]) == flipped
    assert len(p.calls["failure"]) == 25 - flipped
    assert agg["listings_inactive"] == flipped
    tripped = [r for r in caplog.records if r.getMessage().startswith("DRAIN gone-rate breaker:")]
    assert len(tripped) == 1 and tripped[0].name == "scraper.portal_runner"
    assert tripped[0].getMessage() == (
        "DRAIN gone-rate breaker: 20 of 20 ingest fetches read gone this run -- the portal, "
        "not the market source=fake; further gone verdicts this run are recorded as "
        "failures, not flips")


def test_drain_breaker_exempts_presence_checks(monkeypatch):
    """A presence-check backlog of truly dead listings legitimately reads ~100% gone;
    the runner must hand the breaker the VERIFY exemption, or every later gone
    verdict that run would become a failure and the listings would stay active."""
    ids = [str(i) for i in range(1, 26)]
    cap = _patch_queue(monkeypatch, [[(i, None, None, None) for i in ids]])
    monkeypatch.setattr(portal_runner.db, "queue_priorities",
                        lambda _c, _s, nids: {n: portal_runner.db.QUEUE_PRIORITY_VERIFY for n in nids})
    p = _FakePortal(fetch_kinds={i: "gone" for i in ids})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert len(cap["flip"]) == 25
    assert p.calls["failure"] == []
    assert agg["listings_inactive"] == 25 and agg["errors"] == 0
