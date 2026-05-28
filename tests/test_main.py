"""Tests for scraper.main._run_full — focused on the mark_inactive guard.

Hermetic: monkeypatches db.* functions and the SrealityClient builder so
no network is touched. Asserts that mark_inactive is called only when
the index walk is complete (limit is None) and that it is scoped per
category pair so a rental walk doesn't clobber sale listings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from scraper import main as scraper_main
from scraper.sreality_client import ListingGoneError

_FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_id_and_price_from_real_search_result():
    search = json.loads((_FIXTURES / "sample_search.json").read_text("utf-8"))
    results = search["results"]
    # results[0] is a hidden-price listing (price 0) → None, matching the parser.
    assert scraper_main._extract_id(results[0]) == results[0]["hash_id"]
    assert scraper_main._extract_price(results[0]) is None
    # A priced result extracts its summary price (same key order as the parser).
    priced = next(r for r in results if (r.get("price_summary_czk") or 0) > 0)
    assert scraper_main._extract_id(priced) == priced["hash_id"]
    assert scraper_main._extract_price(priced) == int(priced["price_summary_czk"])


class _FakeClient:
    """Yields a deterministic per-category id range so tests can assert
    that mark_inactive is scoped correctly."""

    pages_fetched = 1
    result_size = None  # unfiltered total reported by probe_result_size
    # Region-split simulation (opt-in): when result_size > SPLIT_THRESHOLD a
    # region client is built per kraj; these map region_id -> reported total
    # and (optionally) -> collected count (defaults to the reported total).
    region_result_size: dict[int, int] = {}
    region_collected: dict[int, int] = {}
    # District-split simulation (opt-in): when result_size > SPLIT_THRESHOLD
    # the category is walked per district (locality_district_id); these map
    # district_id -> reported total and -> collected count.
    district_result_size: dict[int, int] = {}
    district_collected: dict[int, int] = {}

    def __init__(
        self,
        category_main: int,
        category_type: int,
        country_id: int = 10001,
        limiter: object | None = None,
        locality_region_id: int | None = None,
        locality_district_id: int | None = None,
    ) -> None:
        self.category_main = category_main
        self.category_type = category_type
        self.country_id = country_id
        self.limiter = limiter
        self.locality_region_id = locality_region_id
        self.locality_district_id = locality_district_id
        if locality_district_id is not None:
            self.result_size = _FakeClient.district_result_size.get(
                locality_district_id, 0
            )
        elif locality_region_id is not None:
            self.result_size = _FakeClient.region_result_size.get(
                locality_region_id, 0
            )
        else:
            self.result_size = _FakeClient.result_size

    def probe_result_size(self):
        return self.result_size

    def iter_index(self):
        if self.locality_district_id is not None:
            d = self.locality_district_id
            n = _FakeClient.district_collected.get(
                d, _FakeClient.district_result_size.get(d, 0)
            )
            base = (
                self.category_main * 10**10
                + self.category_type * 10**9
                + d * 10**5
            )
            for i in range(n):
                yield {"hash_id": base + i, "price_czk": 1}
            return
        if self.locality_region_id is not None:
            n = _FakeClient.region_collected.get(
                self.locality_region_id,
                _FakeClient.region_result_size.get(self.locality_region_id, 0),
            )
            base = (
                self.category_main * 1_000_000
                + self.category_type * 100_000
                + self.locality_region_id * 1_000
            )
            for i in range(n):
                yield {"hash_id": base + i, "price_czk": 1}
            return
        # Distinct id range per (cm, ct) so the per-category seen_ids
        # set is observable in mark_inactive call args.
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {"hash_id": base + i, "price_czk": 10000 + i}


_FakeClient.total_entries = 5  # type: ignore[attr-defined]


@pytest.fixture()
def patched_db(monkeypatch):
    """Patch every db.* helper used by _run_full + the per-category client."""
    calls: dict[str, list] = {
        "mark_inactive": [],
        "touch_listings": [],
        "index_summary": [],
        "enqueue": [],
    }

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scraper_main.db, "connect", _FakeConn)
    monkeypatch.setattr(scraper_main.db, "connect_session", _FakeConn)

    def _fake_enqueue(_conn, entries, source="sreality"):
        e = list(entries)
        calls["enqueue"].append(e)
        return len(e)

    monkeypatch.setattr(scraper_main.db, "enqueue_detail", _fake_enqueue)
    monkeypatch.setattr(
        scraper_main.db, "index_summary",
        lambda _conn, _ids: (calls["index_summary"].append(set(_ids)) or {}),
    )
    monkeypatch.setattr(
        scraper_main.db, "touch_listings",
        lambda _conn, _ids: (calls["touch_listings"].append(list(_ids)) or 0),
    )
    monkeypatch.setattr(
        scraper_main.db, "active_failure_ids", lambda _conn, _ids: set(),
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_inactive",
        lambda _conn, cm, ct, ids: (
            calls["mark_inactive"].append((cm, ct, set(ids))) or 0
        ),
    )
    monkeypatch.setattr(
        scraper_main.db, "active_count", lambda _conn, _cm, _ct: 0,
    )
    # The pooled walk calls _fetch_detail (worker) then _write_result
    # (main thread); stub both so _run_full exercises planning + the pool
    # without real network or DB writes.
    monkeypatch.setattr(
        scraper_main, "_fetch_detail",
        lambda _client, sid: scraper_main.FetchResult(sid, "ok"),
    )
    monkeypatch.setattr(
        scraper_main, "_write_result",
        lambda _conn, _fr, _dry: ("unchanged", 0),
    )
    # Intercept SrealityClient construction in _build_client.
    monkeypatch.setattr(scraper_main, "SrealityClient", _FakeClient)
    return calls


def test_run_full_calls_mark_inactive_per_category_when_no_limit(patched_db):
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0
    # One mark_inactive call per category in CATEGORIES.
    assert len(patched_db["mark_inactive"]) == len(scraper_main.CATEGORIES)

    # Each call is scoped to its own (cm_text, ct_text) and carries the
    # ids that came from that category's index walk only.
    expected_pairs = {
        ("byt", "pronajem"),
        ("byt", "prodej"),
        ("dum", "pronajem"),
        ("dum", "prodej"),
        ("komercni", "pronajem"),
        ("komercni", "prodej"),
    }
    actual_pairs = {(cm, ct) for cm, ct, _ids in patched_db["mark_inactive"]}
    assert actual_pairs == expected_pairs

    # Spot-check that ids are category-scoped: byt/pronajem (1, 2) base = 12000.
    by_pair = {(cm, ct): ids for cm, ct, ids in patched_db["mark_inactive"]}
    assert by_pair[("byt", "pronajem")] == {12000, 12001, 12002, 12003, 12004}
    assert by_pair[("byt", "prodej")] == {11000, 11001, 11002, 11003, 11004}


def test_run_full_skips_mark_inactive_when_limit_set(patched_db):
    rc, _agg = scraper_main._run_full(limit=3, dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_run_full_skips_mark_inactive_when_limit_zero(patched_db):
    """limit=0 still means partial view, even if no listings were seen."""
    rc, _agg = scraper_main._run_full(limit=0, dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_dry_run_never_calls_mark_inactive(patched_db, monkeypatch):
    """dry_run skips the connection altogether, so mark_inactive can't run."""
    monkeypatch.setattr(scraper_main.db, "connect", lambda: None)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=True)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_run_full_isolates_one_crashing_category_marks_the_rest(
    patched_db, monkeypatch
):
    """A single category crashing mid-walk must neither propagate (taking the
    whole run down) nor discard the other categories' work: the crash is
    caught, that category's sweep is skipped, and every other category still
    walks and runs mark_inactive.
    """
    # CATEGORIES order: (1,2) byt/pronajem, (1,1) byt/prodej,
    # (2,2) dum/pronajem, ... — make dum/pronajem raise during iteration.
    def crashing_iter_index(self):
        if (self.category_main, self.category_type) == (2, 2):
            yield {"hash_id": 99999, "price_czk": 1}
            raise RuntimeError("simulated outage mid-iteration")
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {"hash_id": base + i, "price_czk": 10000 + i}

    monkeypatch.setattr(_FakeClient, "iter_index", crashing_iter_index)

    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0  # the crash did NOT propagate

    marked = {(cm, ct) for cm, ct, _ in patched_db["mark_inactive"]}
    # Only the crashing category is skipped; all others (before AND after it)
    # still get swept.
    assert ("dum", "pronajem") not in marked
    assert ("byt", "pronajem") in marked
    assert ("byt", "prodej") in marked
    assert ("dum", "prodej") in marked
    assert ("komercni", "pronajem") in marked
    assert ("komercni", "prodej") in marked


# --- completeness guard -----------------------------------------------------


def test_walk_complete_thresholds():
    # No reported total → trust the walk (don't silently disable delisting).
    assert scraper_main._walk_complete(0, None) is True
    assert scraper_main._walk_complete(0, 0) is True
    # Covered enough of the reported total → complete.
    assert scraper_main._walk_complete(100, 100) is True
    assert scraper_main._walk_complete(90, 100) is True
    # Truncated walk → incomplete, suppress the flip.
    assert scraper_main._walk_complete(89, 100) is False
    assert scraper_main._walk_complete(10, 100) is False


def test_run_full_skips_mark_inactive_when_walk_incomplete(patched_db, monkeypatch):
    """result_size far above the collected count looks like a truncated
    walk, so mark_inactive must be skipped to avoid false delistings."""
    monkeypatch.setattr(_FakeClient, "result_size", 1000, raising=False)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []


def test_run_full_marks_inactive_when_walk_complete(patched_db, monkeypatch):
    """When the collected count matches the reported total the flip runs."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0
    assert len(patched_db["mark_inactive"]) == len(scraper_main.CATEGORIES)


# --- category-order rotation (detail-budget fairness) -----------------------


def test_rotated_categories_is_a_pure_rotation():
    cats = (("a",), ("b",), ("c",), ("d",))
    assert scraper_main._rotated_categories(cats, 0) == cats
    assert scraper_main._rotated_categories(cats, 1) == (("b",), ("c",), ("d",), ("a",))
    # offset wraps modulo length, so a full lap returns the original order.
    assert scraper_main._rotated_categories(cats, len(cats)) == cats
    assert scraper_main._rotated_categories(cats, len(cats) + 1) == (
        ("b",), ("c",), ("d",), ("a",),
    )


def test_rotated_categories_preserves_membership_and_handles_empty():
    # Every rotation is a permutation — same set, same length, no dupes/drops.
    for off in range(len(scraper_main.CATEGORIES) * 2):
        rotated = scraper_main._rotated_categories(scraper_main.CATEGORIES, off)
        assert set(rotated) == set(scraper_main.CATEGORIES)
        assert len(rotated) == len(scraper_main.CATEGORIES)
    assert scraper_main._rotated_categories((), 3) == ()


def test_rotation_gives_each_category_the_front_across_a_full_cycle():
    """Over len(CATEGORIES) consecutive offsets every category leads once, so
    the per-run detail budget isn't permanently biased toward a fixed prefix."""
    leaders = {
        scraper_main._rotated_categories(scraper_main.CATEGORIES, off)[0]
        for off in range(len(scraper_main.CATEGORIES))
    }
    assert leaders == set(scraper_main.CATEGORIES)


# --- gone detection in _process_one ----------------------------------------


def _patch_failure_helpers(monkeypatch) -> dict[str, list]:
    calls: dict[str, list] = {"inactive": [], "cleared": [], "failed": []}
    monkeypatch.setattr(
        scraper_main.db, "mark_listing_inactive",
        lambda _c, sid: calls["inactive"].append(sid),
    )
    monkeypatch.setattr(
        scraper_main.db, "clear_fetch_failure",
        lambda _c, sid: calls["cleared"].append(sid),
    )
    monkeypatch.setattr(
        scraper_main.db, "record_fetch_failure",
        lambda _c, sid, msg: calls["failed"].append(sid),
    )
    return calls


class _RaisingClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_detail(self, sid: int) -> Any:
        raise self._exc


def test_process_one_listing_gone_flips_inactive_not_failure(monkeypatch):
    calls = _patch_failure_helpers(monkeypatch)
    client = _RaisingClient(ListingGoneError("https://x/estates/1", 200))
    outcome, imgs = scraper_main._process_one(
        client, object(), 12345, dry_run=False
    )
    assert outcome == "gone"
    assert imgs == 0
    assert calls["inactive"] == [12345]
    assert calls["cleared"] == [12345]
    assert calls["failed"] == []  # a delisting is not a fetch failure


def test_process_one_404_http_error_is_gone(monkeypatch):
    calls = _patch_failure_helpers(monkeypatch)
    resp = requests.Response()
    resp.status_code = 404
    client = _RaisingClient(requests.HTTPError("404", response=resp))
    outcome, _imgs = scraper_main._process_one(
        client, object(), 777, dry_run=False
    )
    assert outcome == "gone"
    assert calls["inactive"] == [777]
    assert calls["failed"] == []


def test_process_one_500_http_error_is_failure(monkeypatch):
    calls = _patch_failure_helpers(monkeypatch)
    resp = requests.Response()
    resp.status_code = 500
    client = _RaisingClient(requests.HTTPError("500", response=resp))
    outcome, _imgs = scraper_main._process_one(
        client, object(), 888, dry_run=False
    )
    assert outcome == "errors"
    assert calls["failed"] == [888]
    assert calls["inactive"] == []


# --- pooled detail fetch in _walk_category ---------------------------------


class _IdxClient:
    pages_fetched = 1
    result_size = None

    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    def iter_index(self):
        for i in self._ids:
            yield {"hash_id": i, "price_czk": 1}


def test_walk_category_pool_tallies_outcomes_and_decrements_budget(monkeypatch):
    """The thread pool processes every queued listing (a worker 'error' does
    NOT abort the loop), outcomes tally correctly, DB writes go only through
    _write_result, and the global refetch budget decrements once per listing."""
    monkeypatch.setattr(scraper_main.db, "index_summary", lambda _c, _ids: {})
    monkeypatch.setattr(scraper_main.db, "touch_listings", lambda _c, _ids: 0)
    monkeypatch.setattr(scraper_main.db, "active_failure_ids", lambda _c, _ids: set())

    writes: dict[str, list] = {"upsert": [], "gone": [], "fail": []}
    monkeypatch.setattr(
        scraper_main.db, "upsert_listing_with_property",
        lambda _c, row, raw, h: (writes["upsert"].append(h) or "new"),
    )
    monkeypatch.setattr(scraper_main.db, "record_images", lambda _c, sid, imgs: 0)
    monkeypatch.setattr(
        scraper_main.db, "mark_listing_inactive",
        lambda _c, sid: writes["gone"].append(sid),
    )
    monkeypatch.setattr(
        scraper_main.db, "record_fetch_failure",
        lambda _c, sid, msg: writes["fail"].append(sid),
    )
    monkeypatch.setattr(scraper_main.db, "clear_fetch_failure", lambda _c, sid: None)

    def fake_fetch(_client, sid):
        if sid == 11:
            return scraper_main.FetchResult(sid, "gone")
        if sid == 13:
            return scraper_main.FetchResult(
                sid, "error", error=RuntimeError("boom"), source="fetch"
            )
        return scraper_main.FetchResult(
            sid, "ok", row={"price_czk": 1}, raw={}, images=[], content_hash="h" * 8
        )

    monkeypatch.setattr(scraper_main, "_fetch_detail", fake_fetch)

    budget: list[int | None] = [10]
    seen, counts = scraper_main._walk_category(
        _IdxClient([10, 11, 12, 13]),
        object(),
        cat_limit=None,
        dry_run=False,
        refetch_budget=budget,
        detail_workers=3,
    )

    assert seen == {10, 11, 12, 13}
    assert counts["new"] == 2      # 10, 12 upserted
    assert counts["gone"] == 1     # 11
    assert counts["errors"] == 1   # 13 — error did not abort the pool
    assert writes["gone"] == [11]
    assert writes["fail"] == [13]
    assert budget[0] == 6          # 10 - 4 processed


def test_walk_category_reserves_budget_for_new_listings(monkeypatch):
    """Under the per-run cap, new listings get a reserved share of the budget
    so a large failure-retry backlog can't starve new-listing intake."""
    # 100-103 already exist with a stored price differing from the index price
    # (so they qualify for refetch) AND are flagged as active failures (so they
    # take retry priority). 1-4 are genuinely new.
    existing = {s: {"price_czk": 2, "last_seen_at": None} for s in (100, 101, 102, 103)}
    monkeypatch.setattr(scraper_main.db, "index_summary", lambda _c, _ids: existing)
    monkeypatch.setattr(scraper_main.db, "touch_listings", lambda _c, _ids: 0)
    monkeypatch.setattr(
        scraper_main.db, "active_failure_ids", lambda _c, ids: {s for s in ids if s >= 100}
    )
    monkeypatch.setattr(scraper_main.db, "upsert_listing", lambda _c, r, raw, h: "updated")
    monkeypatch.setattr(scraper_main.db, "record_images", lambda _c, s, i: 0)
    monkeypatch.setattr(scraper_main.db, "clear_fetch_failure", lambda _c, s: None)

    fetched: list[int] = []

    def fake_fetch(_client, sid):
        fetched.append(sid)
        return scraper_main.FetchResult(
            sid, "ok", row={"price_czk": 9}, raw={}, images=[], content_hash="h" * 8
        )

    monkeypatch.setattr(scraper_main, "_fetch_detail", fake_fetch)

    _seen, _counts = scraper_main._walk_category(
        _IdxClient([1, 2, 3, 4, 100, 101, 102, 103]),
        object(), cat_limit=None, dry_run=False,
        refetch_budget=[4], detail_workers=2,
    )
    assert len(fetched) == 4            # cap respected
    assert {1, 2}.issubset(set(fetched))  # new listings kept their reserved slots


# --- region-split walk (_walk_category_split) ------------------------------


def _split_args(conn=None):
    return dict(
        limiter=None,
        conn=conn if conn is not None else object(),
        cat_limit=None,
        dry_run=False,
        refetch_budget=[None],
        cat_refetch_cap=None,
        detail_workers=2,
    )


def test_walk_category_split_unions_districts(patched_db, monkeypatch):
    """A category over SPLIT_THRESHOLD is walked per district; the union of
    district seen_ids feeds mark_inactive and the reported result_size is the
    national probe. Complete when every district is complete and the union
    covers the national total."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    seen, _counts, rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert len(seen) == 12000      # 6000 (district 1) + 6000 (district 2)
    assert rs == 12000             # national probe total
    assert complete is True


def test_walk_category_split_reports_probe_not_summed_districts(
    patched_db, monkeypatch
):
    """The reported result_size is sreality's national probe total, NOT the sum
    of per-district totals. Summing double-counts areas covered by two filters
    (the Praha okres/sub-code overlap that inflated reconciliation drift), so a
    walk whose districts sum to more than the national total must still report
    the national total as the denominator."""
    # National total 12000, but the districts sum to 16000 (simulating an
    # overlap where the same listings are counted under two district filters).
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 8000, 2: 8000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    _seen, _counts, rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert rs == 12000             # probe total, not summed_drs (16000)
    assert complete is True


def test_walk_category_split_truncated_district_suppresses_inactivation(
    patched_db, monkeypatch
):
    """If any district's own walk is incomplete, the whole category is treated
    as incomplete so mark_inactive is skipped (no false delisting)."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {1: 3000}, raising=False)

    _seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert complete is False


def test_walk_category_split_missing_district_suppresses_inactivation(
    patched_db, monkeypatch
):
    """If the walked districts don't cover the national total (a district
    missing from DISTRICT_IDS), the union falls short of the national probe so
    the category is incomplete — guards against false mass-delisting even when
    every district we DID walk was itself complete."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    # Only one district populated; union (6000) << national (12000).
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    _seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert complete is False


def test_walk_category_split_national_fallback_closes_gap(patched_db, monkeypatch):
    """Every walked district is complete but the union still falls short of the
    national total (listings with no covered district_id). A national un-split
    fallback pass unions in the remainder so the category can complete."""
    # Over SPLIT_THRESHOLD (10000) so the split runs; districts sum to only
    # 6000, far below the 12000 national total → the fallback must fire.
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 3000, 2: 3000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)
    # The un-split national walk (no district/region) yields total_entries ids,
    # in an id range disjoint from the district walks.
    monkeypatch.setattr(_FakeClient, "total_entries", 6000, raising=False)

    seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert len(seen) == 12000      # 3000 + 3000 districts + 6000 national-fallback
    assert complete is True        # union now >= 90% of national result_size


def test_split_cap_counts_only_fetches_not_unchanged(monkeypatch):
    """The per-category refetch cap must count only ACTUAL fetches, never the
    bulk-touched 'unchanged' listings. Otherwise unchanged touches in the first
    districts exhaust the per-category cap and every genuinely-new listing in
    later districts is deferred forever — which silently starved the detail
    backlog of the big split categories."""
    caps_seen: list[int | None] = []

    class _FC:
        result_size = 500
        pages_fetched = 1
        def probe_result_size(self):
            return 50000  # over SPLIT_THRESHOLD → force the per-district split

    monkeypatch.setattr(scraper_main, "_build_client", lambda *a, **k: _FC())

    def fake_walk_category(
        client, conn, cat_limit, dry_run, budget, district_cap, workers,
        enqueue_only=False,
    ):
        caps_seen.append(district_cap)
        # Each district: 300 unchanged (touched, NOT fetched) + 10 real fetches.
        return (set(), {"unchanged": 300, "new": 10, "found_new": 10})

    monkeypatch.setattr(scraper_main, "_walk_category", fake_walk_category)

    scraper_main._walk_category_split(
        1, 2, limiter=None, conn=object(), cat_limit=None, dry_run=False,
        refetch_budget=[100000], cat_refetch_cap=700, detail_workers=1,
    )
    # Only the 10 fetches/district count: after 10 districts cat_refetched=100,
    # so the 11th district still gets 700-100=600. With the bug (counting the
    # 310 unchanged+fetches), the cap would hit 0 after ~3 districts.
    assert caps_seen[0] == 700
    assert caps_seen[10] == 600


def test_walk_category_no_split_under_threshold(patched_db, monkeypatch):
    """Below the threshold there's a single unfiltered walk; district config
    is never consulted."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 999}, raising=False,
    )

    seen, _counts, rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert len(seen) == 5          # total_entries, NOT district 1's 999
    assert rs == 5
    assert complete is True


def test_run_full_isolates_a_crashing_category(patched_db, monkeypatch):
    """A category whose walk raises must not crash the whole run — it's logged,
    its sweep skipped, and the remaining categories still walk + finalize."""
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated sreality outage")

    monkeypatch.setattr(scraper_main, "_walk_category_split", _boom)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0                          # run completed, did not propagate
    assert patched_db["mark_inactive"] == []  # no sweep on a failed walk


def test_run_full_records_reconciliation_fields(patched_db, monkeypatch):
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    monkeypatch.setattr(scraper_main.db, "active_count", lambda _c, _cm, _ct: 42)

    rc, agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0
    cats = agg["by_category"]
    assert cats
    for c in cats:
        assert c["sreality_result_size"] == 5
        assert c["collected"] == 5
        assert c["active_db"] == 42


# --- images-only runs are not scrape runs ----------------------------------


class _NoopConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def test_images_only_does_not_open_scrape_run(monkeypatch):
    """The image-only backfill must not write a scrape_runs row — it has no
    index walk and was polluting 'last scrape' / liveness / reconciliation."""
    calls = {"start": 0, "finalize": 0}
    monkeypatch.setattr(scraper_main.db, "connect", lambda: _NoopConn())
    monkeypatch.setattr(
        scraper_main.db, "scrape_run_start",
        lambda *a, **k: (calls.__setitem__("start", calls["start"] + 1) or 1),
    )
    monkeypatch.setattr(
        scraper_main.db, "scrape_run_finalize",
        lambda *a, **k: calls.__setitem__("finalize", calls["finalize"] + 1),
    )
    monkeypatch.setattr(
        scraper_main, "_run_image_downloads",
        lambda **k: {"images_stored": 0, "by_category": {}},
    )
    rc = scraper_main.main(["--images-only"])
    assert rc == 0
    assert calls["start"] == 0
    assert calls["finalize"] == 0


def test_main_finalizes_run_even_when_scrape_crashes(monkeypatch):
    """If the scrape work raises, main() must still finalize the run row in its
    `finally` — otherwise the row is orphaned ('stuck') and freezes Health."""
    calls = {"start": 0, "finalize": 0}
    monkeypatch.setattr(scraper_main.db, "connect", lambda: _NoopConn())
    monkeypatch.setattr(
        scraper_main.db, "scrape_run_start",
        lambda *a, **k: (calls.__setitem__("start", calls["start"] + 1) or 1),
    )
    monkeypatch.setattr(
        scraper_main.db, "scrape_run_finalize",
        lambda *a, **k: calls.__setitem__("finalize", calls["finalize"] + 1),
    )

    def _boom(**_k):
        raise RuntimeError("simulated scrape crash")

    monkeypatch.setattr(scraper_main, "_run_full", _boom)
    with pytest.raises(RuntimeError):
        scraper_main.main(["--no-image-downloads", "--no-condition-scoring"])
    assert calls["start"] == 1
    assert calls["finalize"] == 1   # finalized despite the crash — no stuck row


def test_sweep_stuck_scrape_runs_stamps_ended_at():
    """A GH job SIGKILLed at the timeout can't self-finalize; the API startup
    sweep stamps ended_at on orphaned scrape_runs so they stop reading 'stuck'.
    Capture the UPDATE and confirm it only targets un-ended rows past the cutoff."""
    captured: dict[str, Any] = {}

    class _FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
        def fetchall(self):
            return [(1,), (2,)]

    class _FakeConn:
        def cursor(self): return _FakeCursor()
        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

    n = scraper_main.db.sweep_stuck_scrape_runs(_FakeConn(), older_than_minutes=90)
    assert n == 2
    assert "ended_at IS NULL" in captured["sql"]
    assert "ended_at = now()" in captured["sql"]
    assert captured["params"] == (90,)


# --- Phase 2: index-walk / detail-drain split ------------------------------


def test_index_walk_enqueues_and_marks_inactive(patched_db, monkeypatch):
    """The index-walk enqueues every category's new ids and runs mark_inactive
    once per category under the completeness guard (result_size=5 == collected)."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    rc, agg = scraper_main._run_index_walk(dry_run=False)
    assert rc == 0
    assert len(patched_db["mark_inactive"]) == len(scraper_main.CATEGORIES)
    assert len(patched_db["enqueue"]) == len(scraper_main.CATEGORIES)
    # index_summary returns {} (fixture) -> every id is new (priority 0).
    all_entries = [e for batch in patched_db["enqueue"] for e in batch]
    assert all_entries
    assert all(prio == scraper_main.db.QUEUE_PRIORITY_NEW for _sid, _p, prio in all_entries)
    # No detail writes happen in the index-walk.
    assert agg["listings_scraped_new"] == 0
    assert agg["listings_updated"] == 0
    assert agg["index_pages"] >= 1


def test_index_walk_dry_run_writes_nothing(patched_db):
    """dry_run -> conn is None -> no enqueue, no mark_inactive."""
    rc, _agg = scraper_main._run_index_walk(dry_run=True)
    assert rc == 0
    assert patched_db["enqueue"] == []
    assert patched_db["mark_inactive"] == []


def test_index_walk_skips_inactive_when_incomplete(patched_db, monkeypatch):
    """A truncated walk (collected << result_size) still enqueues but must NOT
    mark_inactive — same completeness guard as the legacy full run."""
    monkeypatch.setattr(_FakeClient, "result_size", 1000, raising=False)
    rc, _agg = scraper_main._run_index_walk(dry_run=False)
    assert rc == 0
    assert patched_db["mark_inactive"] == []
    assert patched_db["enqueue"]  # enqueue is independent of completeness


def test_walk_category_enqueue_assigns_priorities(monkeypatch):
    """failure-retry (2) > price-changed (1) > new (0); unchanged ids skipped."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    # (1,2): ids 12000..12004, idx price 10000..10004.
    monkeypatch.setattr(
        scraper_main.db, "index_summary",
        lambda _c, ids: {
            12000: {"price_czk": 10000, "last_seen_at": None},  # same price -> unchanged
            12001: {"price_czk": 999, "last_seen_at": None},    # diff price -> changed
        },
    )
    monkeypatch.setattr(scraper_main.db, "touch_listings", lambda _c, ids: 0)
    monkeypatch.setattr(scraper_main.db, "active_failure_ids", lambda _c, ids: {12001})
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        scraper_main.db, "enqueue_detail",
        lambda _c, entries, source="sreality": (
            captured.__setitem__("e", list(entries)) or len(captured["e"])
        ),
    )
    client = _FakeClient(category_main=1, category_type=2)
    seen, counts = scraper_main._walk_category(
        client, object(), None, False, [None], None, 1, enqueue_only=True,
    )
    by_prio = {sid: prio for sid, _p, prio in captured["e"]}
    assert by_prio[12001] == scraper_main.db.QUEUE_PRIORITY_FAILURE  # changed AND failed -> failure
    assert by_prio[12002] == scraper_main.db.QUEUE_PRIORITY_NEW
    assert 12000 not in by_prio   # unchanged -> not enqueued
    assert counts["enqueued"] == 4
    assert counts["new"] == 0 and counts["updated"] == 0   # no detail outcomes


def _make_fr(sid: int, kind: str):
    if kind == "ok":
        return scraper_main.FetchResult(
            sid, "ok", row={"sreality_id": sid}, raw={}, images=[], content_hash="h",
        )
    return scraper_main.FetchResult(sid, kind, source="fetch")


def _drain_patches(monkeypatch, claim_batches, fetch_kind):
    captured: dict[str, list] = {
        "write": [], "complete": [], "fail": [], "failure": [],
        "gone": [], "claim_n": [],
    }

    class _Conn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scraper_main.db, "connect_session", lambda: _Conn())
    monkeypatch.setattr(scraper_main.db, "reclaim_stale_claims", lambda _c, **k: 0)
    it = iter(list(claim_batches) + [[]])

    def _claim(_c, n):
        captured["claim_n"].append(n)
        return next(it, [])

    monkeypatch.setattr(scraper_main.db, "claim_detail_batch", _claim)
    monkeypatch.setattr(scraper_main, "_build_client", lambda *a, **k: object())
    monkeypatch.setattr(
        scraper_main, "_fetch_detail",
        lambda _client, sid: _make_fr(sid, fetch_kind(sid)),
    )

    def _write(_c, buf):
        captured["write"].append(sorted(fr.sid for fr in buf))
        return {"new": len(buf), "updated": 0, "unchanged": 0, "images_discovered": 0}

    monkeypatch.setattr(scraper_main.db, "write_detail_batch", _write)
    monkeypatch.setattr(
        scraper_main.db, "complete_detail",
        lambda _c, ids: captured["complete"].append(sorted(ids)),
    )
    monkeypatch.setattr(
        scraper_main.db, "fail_detail",
        lambda _c, ids, msg, **k: captured["fail"].append(sorted(ids)),
    )
    monkeypatch.setattr(
        scraper_main.db, "record_fetch_failure",
        lambda _c, sid, msg: captured["failure"].append(sid),
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_listing_inactive",
        lambda _c, sid: captured["gone"].append(sid),
    )
    monkeypatch.setattr(scraper_main.db, "clear_fetch_failure", lambda _c, sid: None)
    return captured


def test_detail_drain_batches_and_completes(monkeypatch):
    cap = _drain_patches(monkeypatch, [[(1, None), (2, None), (3, None)]], lambda s: "ok")
    rc, agg = scraper_main._run_detail_drain(max_claims=None, dry_run=False, detail_workers=1)
    assert rc == 0
    assert cap["write"] == [[1, 2, 3]]      # one partial flush at end
    assert cap["complete"] == [[1, 2, 3]]   # dequeued after the write
    assert agg["listings_scraped_new"] == 3


def test_detail_drain_routes_gone_and_error(monkeypatch):
    kinds = {10: "ok", 11: "gone", 12: "error"}
    cap = _drain_patches(monkeypatch, [[(10, None), (11, None), (12, None)]], lambda s: kinds[s])
    rc, agg = scraper_main._run_detail_drain(max_claims=None, dry_run=False, detail_workers=1)
    assert rc == 0
    assert cap["gone"] == [11]              # gone -> mark_listing_inactive
    assert cap["failure"] == [12]           # error -> record_fetch_failure
    assert cap["fail"] == [[12]]            # error -> queue attempts++
    assert sorted(x for b in cap["write"] for x in b) == [10]
    # 11 (gone) and 10 (ok flush) both dequeued; 12 (error) stays queued.
    assert sorted(x for b in cap["complete"] for x in b) == [10, 11]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 1


def test_detail_drain_respects_max_claims_cap(monkeypatch):
    cap = _drain_patches(monkeypatch, [[(1, None), (2, None)]], lambda s: "ok")
    scraper_main._run_detail_drain(max_claims=2, dry_run=False, detail_workers=1)
    # First claim is sized to the cap and, once met, the loop stops (one claim).
    assert cap["claim_n"] == [2]


def test_detail_drain_dry_run_does_not_claim(monkeypatch):
    class _CountCur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): pass
        def fetchone(self): return (7,)

    class _CountConn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return _CountCur()

    monkeypatch.setattr(scraper_main.db, "connect", lambda: _CountConn())
    claimed = {"n": 0}
    monkeypatch.setattr(
        scraper_main.db, "claim_detail_batch",
        lambda *a: claimed.__setitem__("n", claimed["n"] + 1) or [],
    )
    rc, agg = scraper_main._run_detail_drain(max_claims=50, dry_run=True)
    assert rc == 0 and agg == {}
    assert claimed["n"] == 0


def _dispatch_patches(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(scraper_main.db, "connect", lambda: _NoopConn())
    monkeypatch.setattr(
        scraper_main.db, "scrape_run_start",
        lambda _c, rt, **k: (captured.__setitem__("run_type", rt) or 1),
    )
    monkeypatch.setattr(scraper_main.db, "scrape_run_finalize", lambda *a, **k: None)
    monkeypatch.setattr(
        scraper_main, "_run_index_walk",
        lambda dry_run: (captured.__setitem__("called", "index") or (0, {})),
    )
    monkeypatch.setattr(
        scraper_main, "_run_detail_drain",
        lambda **k: (captured.__setitem__("called", "drain") or (0, {})),
    )
    return captured


def test_index_only_dispatches_index_walk_with_index_run_type(monkeypatch):
    captured = _dispatch_patches(monkeypatch)
    rc = scraper_main.main(["--index-only"])
    assert rc == 0
    assert captured["called"] == "index"
    assert captured["run_type"] == "index"


def test_drain_only_dispatches_detail_drain_with_detail_run_type(monkeypatch):
    captured = _dispatch_patches(monkeypatch)
    rc = scraper_main.main(["--drain-only"])
    assert rc == 0
    assert captured["called"] == "drain"
    assert captured["run_type"] == "detail"


def test_index_and_drain_only_mutually_exclusive(monkeypatch):
    _dispatch_patches(monkeypatch)
    assert scraper_main.main(["--index-only", "--drain-only"]) == 2


def test_index_only_rejects_limit(monkeypatch):
    _dispatch_patches(monkeypatch)
    assert scraper_main.main(["--index-only", "--limit", "5"]) == 2
