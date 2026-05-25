"""Tests for scraper.main._run_full — focused on the mark_inactive guard.

Hermetic: monkeypatches db.* functions and the SrealityClient builder so
no network is touched. Asserts that mark_inactive is called only when
the index walk is complete (limit is None) and that it is scoped per
category pair so a rental walk doesn't clobber sale listings.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from scraper import main as scraper_main
from scraper.sreality_client import ListingGoneError


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
                yield {"hash_id": base + i, "price_czk": {"value_raw": 1}}
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
                yield {"hash_id": base + i, "price_czk": {"value_raw": 1}}
            return
        # Distinct id range per (cm, ct) so the per-category seen_ids
        # set is observable in mark_inactive call args.
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {
                "hash_id": base + i,
                "price_czk": {"value_raw": 10000 + i},
            }


_FakeClient.total_entries = 5  # type: ignore[attr-defined]


@pytest.fixture()
def patched_db(monkeypatch):
    """Patch every db.* helper used by _run_full + the per-category client."""
    calls: dict[str, list] = {
        "mark_inactive": [],
        "touch_listings": [],
        "index_summary": [],
    }

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scraper_main.db, "connect", _FakeConn)
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
            yield {"hash_id": 99999, "price_czk": {"value_raw": 1}}
            raise RuntimeError("simulated outage mid-iteration")
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {
                "hash_id": base + i,
                "price_czk": {"value_raw": 10000 + i},
            }

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
            yield {"hash_id": i, "price_czk": {"value_raw": 1}}


def test_walk_category_pool_tallies_outcomes_and_decrements_budget(monkeypatch):
    """The thread pool processes every queued listing (a worker 'error' does
    NOT abort the loop), outcomes tally correctly, DB writes go only through
    _write_result, and the global refetch budget decrements once per listing."""
    monkeypatch.setattr(scraper_main.db, "index_summary", lambda _c, _ids: {})
    monkeypatch.setattr(scraper_main.db, "touch_listings", lambda _c, _ids: 0)
    monkeypatch.setattr(scraper_main.db, "active_failure_ids", lambda _c, _ids: set())

    writes: dict[str, list] = {"upsert": [], "gone": [], "fail": []}
    monkeypatch.setattr(
        scraper_main.db, "upsert_listing",
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
    district seen_ids and the summed result_size feed mark_inactive. Complete
    when every district is complete and the union covers the national total."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    seen, _counts, rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert len(seen) == 12000      # 6000 (district 1) + 6000 (district 2)
    assert rs == 12000             # summed district result_size
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


def test_walk_category_split_no_split_forces_single_walk(patched_db, monkeypatch):
    """--no-split (split=False) walks the category in a single pass even when
    it's over SPLIT_THRESHOLD, skips the result-size probe entirely, and never
    consults the district config. The big single walk falls short of the total
    so the sweep is (correctly) suppressed."""
    probe_calls: list[int] = []
    monkeypatch.setattr(
        _FakeClient, "probe_result_size",
        lambda self: probe_calls.append(1),
        raising=False,
    )
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, split=False, **_split_args()
    )
    assert len(seen) == 5          # single unfiltered walk, NOT the 12000 split
    assert probe_calls == []       # probe skipped — no extra request
    assert complete is False       # 5 << 12000 → sweep suppressed, no false delisting


def test_run_full_isolates_a_crashing_category(patched_db, monkeypatch):
    """A category whose walk raises must not crash the whole run — it's logged,
    its sweep skipped, and the remaining categories still walk + finalize."""
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated sreality outage")

    monkeypatch.setattr(scraper_main, "_walk_category_split", _boom)
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False)
    assert rc == 0                          # run completed, did not propagate
    assert patched_db["mark_inactive"] == []  # no sweep on a failed walk


def test_run_full_split_off_still_sweeps_small_categories(patched_db, monkeypatch):
    """split=False (--no-split) walks every category in a single pass and, for
    categories small enough to walk whole, still runs mark_inactive — only the
    too-large-to-walk-whole categories lose the sweep."""
    probe_calls: list[int] = []
    monkeypatch.setattr(
        _FakeClient, "probe_result_size",
        lambda self: probe_calls.append(1),
        raising=False,
    )
    rc, _agg = scraper_main._run_full(limit=None, dry_run=False, split=False)
    assert rc == 0
    assert len(patched_db["mark_inactive"]) == len(scraper_main.CATEGORIES)
    assert probe_calls == []                # no probe requests with split off


def test_run_full_http_block_logs_cleanly_without_traceback(
    patched_db, monkeypatch, caplog
):
    """A blocked index walk (HTTP error per category) is isolated, logged as a
    one-line 'blocked' warning (never a stack trace), and surfaces a single
    'RUN blocked' summary — without crashing the run or marking anything."""
    def _blocked(*_a, **_kw):
        raise requests.HTTPError("404 from sreality")

    monkeypatch.setattr(scraper_main, "_walk_category_split", _blocked)
    with caplog.at_level("WARNING", logger="scraper"):
        rc, _agg = scraper_main._run_full(limit=None, dry_run=False)

    assert rc == 0
    assert patched_db["mark_inactive"] == []   # nothing collected → nothing swept
    n_cats = len(scraper_main.CATEGORIES)
    blocked = [r for r in caplog.records if "CATEGORY walk blocked" in r.getMessage()]
    assert len(blocked) == n_cats
    summary = [r for r in caplog.records if "RUN blocked" in r.getMessage()]
    assert len(summary) == 1
    assert f"categories={n_cats}/{n_cats}" in summary[0].getMessage()
    # An expected HTTP block is logged via LOG.warning, never LOG.exception, so
    # no record carries traceback info.
    assert all(r.exc_info is None for r in caplog.records)


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
