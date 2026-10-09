"""Tests for scraper.main — sreality's index walk (the rule #3 nomination guard),
its detail fetch, and the module's CLI dispatch.

Hermetic: monkeypatches db.* functions and the SrealityClient builder so
no network is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from datetime import datetime, timedelta, timezone
import requests

from scraper import main as scraper_main
from scraper.listing_write import WriteOutcome
from scraper.portal import walk_is_complete
from scraper.sreality_client import ListingGoneError

_FIXTURES = Path(__file__).parent / "fixtures"


def _outcome(w: Any, result: str) -> WriteOutcome:
    return WriteOutcome(w.source, w.source_id_native, 1, result, 1, w.content_hash, 0)  # type: ignore[arg-type]


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


def test_extract_price_mirrors_db_placeholder_clamp():
    # The write boundary nulls "1 Kč dohodou" placeholders (db.sane_price_czk),
    # so the index side must too — otherwise stored NULL vs index 1 makes the
    # unchanged-compare refetch such listings on every walk, forever.
    assert scraper_main._extract_price({"price_czk": 1}) is None
    assert scraper_main._extract_price({"price_summary_czk": 1}) is None
    assert scraper_main._extract_price({"price_czk": 2}) == 2


class _FakeClient:
    """Yields a deterministic per-category id range so tests can assert
    that nomination is scoped correctly."""

    pages_fetched = 1
    # Unfiltered total reported by probe_result_size. Matches total_entries (5)
    # so the default fake walk is MEASURABLY complete, like the real API which
    # always reports result_size. It used to be None, back when an unmeasurable
    # walk was trusted as complete; scraper.portal.walk_is_complete now calls
    # that "unknown" and suppresses the sweep, so a None here would mean "no
    # delisting" rather than "full walk".
    result_size = 5
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
    # Rule #3 is STRUCTURAL since 2026-09-08: the gate reads WHY a page loop
    # stopped, not how much it collected, so the fake has to stamp a stop reason
    # exactly like SrealityClient.iter_index does. Default = sreality said there
    # is no more; district_stop_reason overrides one district (a 422 wall, a
    # barren page, a deadline) without touching any count.
    district_stop_reason: dict[int, str] = {}

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
        self.stop_reason: str | None = None

    def probe_result_size(self):
        return self.result_size

    def _natural_stop(self) -> str:
        return "empty_confirmed" if self.result_size == 0 else "declared_total_reached"

    def iter_index(self, on_page=None):
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
            self.stop_reason = _FakeClient.district_stop_reason.get(
                d, self._natural_stop()
            )
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
            self.stop_reason = self._natural_stop()
            return
        # Distinct id range per (cm, ct) so the per-category seen_ids
        # set is observable in the nomination call args.
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {"hash_id": base + i, "price_czk": 10000 + i}
        self.stop_reason = "declared_total_reached"


_FakeClient.total_entries = 5  # type: ignore[attr-defined]


@pytest.fixture()
def patched_db(monkeypatch):
    """Patch every db.* helper used by the index walk + the per-category client."""
    calls: dict[str, list] = {
        "nominated": [],
        "enqueue": [],
        "seen_key": [],
    }

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(scraper_main.db, "connect", _FakeConn)

    def _fake_enqueue(_conn, source, entries):
        e = list(entries)
        calls["enqueue"].append(e)
        return len(e)

    monkeypatch.setattr(scraper_main.db, "enqueue_detail", _fake_enqueue)
    monkeypatch.setattr(
        scraper_main.db, "index_summary_native", lambda _conn, _src, _ids: {},
    )
    monkeypatch.setattr(scraper_main.db, "touch_listings_by_id", lambda _conn, _ids: 0)
    monkeypatch.setattr(
        scraper_main.db, "active_failure_ids", lambda _conn, _ids: set(),
    )
    # Rule #3 (2026-09-07): the walk nominates unseen rows for a page check
    # instead of sweeping; record the nominations the way the old sweep calls
    # were recorded so every guard test keeps its meaning.
    monkeypatch.setattr(
        scraper_main.db, "presence_candidates",
        lambda _conn, source, cm, ct, ids, *, seen_key="native", **kw: (
            calls["seen_key"].append(seen_key)
            or calls["nominated"].append((cm, ct, set(ids))) or ([], 0)
        ),
    )
    monkeypatch.setattr(
        scraper_main.db, "enqueue_presence_checks",
        lambda _conn, source, cm, ct, cands, *, active_rows, subtype=None: (0, 0),
    )
    monkeypatch.setattr(
        scraper_main.db, "active_count",
        lambda _conn, _cm, _ct, *, source="sreality": 0,
    )
    # Intercept SrealityClient construction in _build_client.
    monkeypatch.setattr(scraper_main, "SrealityClient", _FakeClient)
    return calls


def test_sreality_portal_nominates_on_its_integer_ids():
    """Rule #3 since 2026-09-07: the framework index walk nominates unseen rows
    for a page check and the runner does it generically; sreality's walk returns
    its own integer ids, so it tells the runner to exclude them on
    listings.sreality_id. The old sweep seam is gone."""
    p = scraper_main.SrealityPortal()
    assert p.seen_key == "sreality_id"
    assert not hasattr(p, "mark_inactive")
    assert not hasattr(p, "mark_gone")


def test_walk_complete_tolerates_half_percent_short_walk():
    """0.995 gate (relaxed from 1.0): a 99.6% walk is complete (mid-walk jitter
    tolerated — the flip would have been suppressed at the old 1.0 gate); 99.4% is
    not. Sreality now shares scraper.portal.walk_is_complete with every portal."""
    assert walk_is_complete(996, 1000) is True
    assert walk_is_complete(994, 1000) is False
    # An unreported total used to return True here ("trust the walk"). That
    # fail-open was the DEFECT, not the spec: rule #3 delists only from a proven
    # walk, and a failed probe proves nothing.
    assert walk_is_complete(10, None) is False


# --- completeness guard -----------------------------------------------------


def test_walk_complete_thresholds():
    # No reported total → "unknown", NOT complete. These two asserted True
    # before, on the reasoning that an unmeasurable walk should be trusted so
    # delisting isn't silently disabled. That was the bug: "complete" is what
    # authorises nomination of everything the walk did not reach, and a probe
    # that failed cannot authorise anything. Suppressing nomination is the safe
    # direction — less delisting, never more.
    assert walk_is_complete(0, None) is False
    # A DECLARED zero is different: it is a measurement, not a failure to
    # measure. An empty district IS genuinely complete, and sreality's split
    # relies on that — 77 districts, most of them empty for a small category.
    # Conflating the two is exactly how a failed probe came to look like an
    # empty category.
    assert walk_is_complete(0, 0) is True
    assert walk_is_complete(5, 0) is False   # rows against a declared zero
    # Collected the FULL reported total (100%) → complete. Mild over-collection
    # (concurrent additions mid-walk) is still complete, up to 1.02x.
    assert walk_is_complete(100, 100) is True
    assert walk_is_complete(101, 100) is True
    # Beyond 1.02x the denominator itself is wrong (overlapping slices or
    # foreign stock), so contamination must not read as completeness.
    assert walk_is_complete(120, 100) is False
    # Anything short of 99.5% → incomplete, suppress the flip.
    assert walk_is_complete(99, 100) is False
    assert walk_is_complete(90, 100) is False
    assert walk_is_complete(10, 100) is False


# --- category coverage (delisting depends on a complete walk per pair) ------


def test_categories_is_the_full_category_cross_product():
    """Every category_main x category_type pair the parser knows must be
    walked: nomination is scoped per (source, category_main,
    category_type), so a missing slice never gets a complete walk and its
    delisted rows stay is_active=true forever (first the drazba/podil gap,
    then pozemek/ostatni). Every pair had nonzero live inventory when
    probed, so none of the slices is a wasted walk."""
    expected = {
        (cm, ct)
        for cm in scraper_main.parser.CATEGORY_MAIN
        for ct in scraper_main.parser.CATEGORY_TYPE
    }
    assert set(scraper_main.CATEGORIES) == expected
    # No duplicate pairs — each would double-walk and double-count.
    assert len(scraper_main.CATEGORIES) == len(expected)


def test_categories_include_pozemek_and_ostatni_sale():
    # The two concrete slices the parity fix is about (iDNES ingests both;
    # sreality's were stuck un-walked): land sale ~21k, other sale ~1.3k.
    assert (3, 1) in scraper_main.CATEGORIES  # pozemek / prodej
    assert (5, 1) in scraper_main.CATEGORIES  # ostatni / prodej


# --- category-order rotation (deadline fairness) ----------------------------


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
    a walk the deadline cuts short isn't permanently biased toward a fixed prefix."""
    leaders = {
        scraper_main._rotated_categories(scraper_main.CATEGORIES, off)[0]
        for off in range(len(scraper_main.CATEGORIES))
    }
    assert leaders == set(scraper_main.CATEGORIES)


# --- gone detection: _fetch_detail ----------------------------------------


class _RaisingClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_detail(self, sid: int) -> Any:
        raise self._exc


def test_listing_gone_is_gone_not_failure():
    client = _RaisingClient(ListingGoneError("https://x/estates/1", 200))
    assert scraper_main._fetch_detail(client, 12345).kind == "gone"


def test_404_http_error_is_gone():
    resp = requests.Response()
    resp.status_code = 404
    client = _RaisingClient(requests.HTTPError("404", response=resp))
    assert scraper_main._fetch_detail(client, 777).kind == "gone"


def test_500_http_error_is_failure():
    resp = requests.Response()
    resp.status_code = 500
    client = _RaisingClient(requests.HTTPError("500", response=resp))
    fr = scraper_main._fetch_detail(client, 888)
    assert fr.kind == "error"
    assert fr.source == "fetch"


# --- region-split walk (_walk_category_split) ------------------------------


def _split_args(conn=None):
    return dict(
        limiter=None,
        conn=conn if conn is not None else object(),
        dry_run=False,
    )


def test_walk_category_split_unions_districts(patched_db, monkeypatch):
    """A category over SPLIT_THRESHOLD is walked per district; the union of
    district seen_ids feeds nomination and the reported result_size is the
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
    # ...and since 2026-09-08 the walk DID reach the end: every district paged to
    # sreality's own last page, so the union over-collecting the national probe
    # 1.33x is a coverage signal (logged, and recorded in scrape_runs.by_category
    # by the runner), not a veto. The denominator this test exists to pin (rs) is
    # unaffected either way.
    assert complete is True
    assert walk_is_complete(len(_seen), rs) is False   # the coverage number still says so


def test_walk_category_split_truncated_district_suppresses_inactivation(
    patched_db, monkeypatch
):
    """A district the 422 wall cut short is a stop of OURS, so the whole category
    nominates nothing — one truncated slice still vetoes, it just has to be
    truncated for a REASON now instead of merely being short."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {1: 3000}, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_stop_reason", {1: "cap_wall"}, raising=False,
    )

    _seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert complete is False


def test_walk_category_split_district_one_row_short_still_reached_the_end(
    patched_db, monkeypatch
):
    """THE case this change exists for: a district that paged to sreality's own
    last page but collected one row less than its jittering declared total. The
    numeric gate called that a truncated walk and suppressed the whole category;
    structurally it is a finished walk."""
    # The real shape of the incident: a small district, one row short — 86 of 87
    # is 98.85%, under INDEX_MIN_COMPLETENESS, so the numeric gate failed it.
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 87, 2: 87}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {1: 86}, raising=False)

    _seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert complete is True
    assert walk_is_complete(86, 87) is False   # ...and the count still knows


def test_walk_category_split_deadline_between_districts_suppresses_nomination(
    patched_db, monkeypatch
):
    """The wall-clock budget expiring between districts leaves 77 - walked
    districts never reached (`slice_unreached`) plus our own `deadline` stop, so
    the category nominates nothing however much the walked part collected."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000, 2: 6000}, raising=False,
    )

    _seen, _counts, _rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, deadline=time.monotonic() - 1, **_split_args()
    )
    assert complete is False


def test_walk_category_split_union_shortfall_no_longer_vetoes(
    patched_db, monkeypatch
):
    """The rail this change deliberately trades away, pinned so the trade stays
    visible: the union falling short of the national probe (rows reachable under
    no locality_district_id) USED to suppress the category. It no longer does —
    every district reached sreality's end, so the category nominates and the
    shortfall survives as the national-fallback trigger and a coverage warning."""
    monkeypatch.setattr(_FakeClient, "result_size", 12000, raising=False)
    # Only one district populated; union (6000 + the 5 the fallback adds) is far
    # below the national 12000.
    monkeypatch.setattr(
        _FakeClient, "district_result_size", {1: 6000}, raising=False,
    )
    monkeypatch.setattr(_FakeClient, "district_collected", {}, raising=False)

    seen, _counts, rs, _pages, complete = scraper_main._walk_category_split(
        1, 2, **_split_args()
    )
    assert complete is True
    assert walk_is_complete(len(seen), rs) is False


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
    assert complete is True        # union now == national result_size (full walk)


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


def test_slice_stop_reason_fails_closed_when_nothing_stamped_one():
    """An abandoned generator leaves no stop reason. "I cannot classify this
    stop" is not evidence that sreality ended the walk, so it reads as ours."""
    class _NoReason:
        pass

    assert scraper_main._slice_stop_reason(_NoReason()) == "error"
    assert scraper_main.stop_is_portal_end(
        scraper_main._slice_stop_reason(_NoReason())
    ) is False


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


def test_parse_shard_parses_and_validates():
    assert scraper_main._parse_shard(None) is None
    assert scraper_main._parse_shard("") is None
    assert scraper_main._parse_shard("0/4") == (0, 4)
    assert scraper_main._parse_shard("3/4") == (3, 4)


@pytest.mark.parametrize("bad", ["4/4", "5/4", "-1/4", "1", "1/0", "a/b"])
def test_parse_shard_rejects_bad_input(bad):
    with pytest.raises(SystemExit):
        scraper_main._parse_shard(bad)


def test_parse_sources_splits_and_trims():
    assert scraper_main._parse_sources(None) is None
    assert scraper_main._parse_sources("") is None
    assert scraper_main._parse_sources("idnes") == ("idnes",)
    assert scraper_main._parse_sources(" idnes , bazos ") == ("idnes", "bazos")


def test_images_only_passes_shard_and_sources_through(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(scraper_main.db, "connect", lambda: _NoopConn())
    monkeypatch.setattr(
        scraper_main, "_run_image_downloads",
        lambda **k: captured.update(k) or {"images_stored": 0, "by_category": {}},
    )
    rc = scraper_main.main(
        ["--images-only", "--image-shard", "1/4", "--image-sources", "idnes,bazos"]
    )
    assert rc == 0
    assert captured["shard"] == (1, 4)
    assert captured["sources"] == ("idnes", "bazos")


def test_main_finalizes_run_even_when_the_walk_crashes(monkeypatch):
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

    monkeypatch.setattr(scraper_main, "_run_index_walk", _boom)
    with pytest.raises(RuntimeError):
        scraper_main.main(["--index-only"])
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


def test_index_walk_enqueues_and_nominates(patched_db, monkeypatch):
    """The index-walk enqueues every category's new ids and nominates once per
    category under the completeness guard (result_size=5 == collected)."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    rc, agg = scraper_main._run_index_walk(dry_run=False)
    assert rc == 0
    assert len(patched_db["nominated"]) == len(scraper_main.CATEGORIES)
    assert len(patched_db["enqueue"]) == len(scraper_main.CATEGORIES)
    # index_summary returns {} (fixture) -> every id is new (priority 0).
    all_entries = [e for batch in patched_db["enqueue"] for e in batch]
    assert all_entries
    assert all(
        prio == scraper_main.db.QUEUE_PRIORITY_NEW
        for _nid, _ref, _p, prio in all_entries
    )
    # No detail writes happen in the index-walk.
    assert agg["listings_scraped_new"] == 0
    assert agg["listings_updated"] == 0
    assert agg["index_pages"] >= 1


def test_index_walk_dry_run_writes_nothing(patched_db):
    """dry_run -> conn is None -> no enqueue, no nomination."""
    rc, _agg = scraper_main._run_index_walk(dry_run=True)
    assert rc == 0
    assert patched_db["enqueue"] == []
    assert patched_db["nominated"] == []


def test_index_walk_skips_inactive_when_incomplete(patched_db, monkeypatch):
    """A walk stopped by one of OUR stops (here the 422 deep-pagination wall)
    still enqueues but must NOT nominate. Note what no longer suppresses it:
    collected << result_size."""
    monkeypatch.setattr(_FakeClient, "result_size", 1000, raising=False)

    def capped_iter_index(self, on_page=None):
        base = self.category_main * 10000 + self.category_type * 1000
        for i in range(_FakeClient.total_entries):
            yield {"hash_id": base + i, "price_czk": 10000 + i}
        self.stop_reason = "cap_wall"

    monkeypatch.setattr(_FakeClient, "iter_index", capped_iter_index)
    rc, _agg = scraper_main._run_index_walk(dry_run=False)
    assert rc == 0
    assert patched_db["nominated"] == []
    assert patched_db["enqueue"]  # enqueue is independent of the walk's end


def test_walk_category_enqueue_assigns_priorities(monkeypatch):
    """failure-retry (2) > price-changed (1) > new (0); unchanged ids skipped."""
    monkeypatch.setattr(_FakeClient, "result_size", 5, raising=False)
    # (1,2): ids 12000..12004, idx price 10000..10004.
    monkeypatch.setattr(
        scraper_main.db, "index_summary_native",
        lambda _c, _src, ids: {
            "12000": {"id": 1, "price_czk": 10000, "last_seen_at": None},  # same price -> unchanged
            "12001": {"id": 2, "price_czk": 999, "last_seen_at": None},    # diff price -> changed
        },
    )
    monkeypatch.setattr(scraper_main.db, "touch_listings_by_id", lambda _c, ids: 0)
    monkeypatch.setattr(scraper_main.db, "active_failure_ids", lambda _c, ids: {12001})
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        scraper_main.db, "enqueue_detail",
        lambda _c, source, entries: (
            captured.__setitem__("e", list(entries)) or len(captured["e"])
        ),
    )
    client = _FakeClient(category_main=1, category_type=2)
    seen, counts = scraper_main._walk_category(client, object(), False)
    by_prio = {int(nid): prio for nid, _ref, _p, prio in captured["e"]}
    assert by_prio[12001] == scraper_main.db.QUEUE_PRIORITY_FAILURE  # changed AND failed -> failure
    assert by_prio[12002] == scraper_main.db.QUEUE_PRIORITY_NEW
    assert 12000 not in by_prio   # unchanged -> not enqueued
    assert counts["enqueued"] == 4


def _make_fr(sid: int, kind: str):
    if kind == "ok":
        return scraper_main.FetchResult(
            sid, "ok", row={"sreality_id": sid}, raw={}, images=[],
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
    monkeypatch.setattr(scraper_main.db, "reclaim_stale_claims", lambda _c, _src, **k: 0)
    # rule #3 hysteresis: the ledger already confirms every gone verdict in these tests
    monkeypatch.setattr(
        scraper_main.db, "gone_evidence",
        lambda _c, _src, _nid: (True, datetime.now(timezone.utc) - timedelta(days=1)))
    it = iter(list(claim_batches) + [[]])

    def _claim(_c, _source, n):
        captured["claim_n"].append(n)
        return next(it, [])

    monkeypatch.setattr(scraper_main.db, "claim_detail_batch", _claim)
    monkeypatch.setattr(scraper_main, "_build_client", lambda *a, **k: object())
    monkeypatch.setattr(
        scraper_main, "_fetch_detail",
        lambda _client, sid: _make_fr(sid, fetch_kind(sid)),
    )

    def _write(_c, writes):
        captured["write"].append(sorted(int(w.source_id_native) for w in writes))
        return [_outcome(w, "new") for w in writes]

    monkeypatch.setattr(scraper_main.listing_write, "write_listings", _write)
    monkeypatch.setattr(
        scraper_main.db, "complete_detail",
        lambda _c, _src, ids, outcome="written": captured["complete"].append(sorted(ids)),
    )
    monkeypatch.setattr(
        scraper_main.db, "fail_detail",
        lambda _c, _src, ids, msg, **k: captured["fail"].append(sorted(ids)),
    )
    monkeypatch.setattr(
        scraper_main.db, "record_fetch_failure",
        lambda _c, sid, msg: captured["failure"].append(sid),
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_listing_inactive",
        lambda _c, source, nid: captured["gone"].append((source, nid)),
    )
    return captured


def test_detail_drain_batches_and_completes(monkeypatch):
    cap = _drain_patches(
        monkeypatch,
        [[("1", None, None, None), ("2", None, None, None), ("3", None, None, None)]],
        lambda s: "ok",
    )
    rc, agg = scraper_main._run_detail_drain(max_claims=None, dry_run=False, detail_workers=1)
    assert rc == 0
    assert cap["write"] == [[1, 2, 3]]              # one partial flush at end
    assert cap["complete"] == [["1", "2", "3"]]     # dequeued by native_id
    assert agg["listings_scraped_new"] == 3


def test_detail_drain_routes_gone_and_error(monkeypatch):
    kinds = {10: "ok", 11: "gone", 12: "error"}
    cap = _drain_patches(
        monkeypatch,
        [[("10", None, None, None), ("11", None, None, None), ("12", None, None, None)]],
        lambda s: kinds[s],
    )
    rc, agg = scraper_main._run_detail_drain(max_claims=None, dry_run=False, detail_workers=1)
    assert rc == 0
    assert cap["gone"] == [("sreality", "11")]  # gone -> mark_listing_inactive
    assert cap["failure"] == [12]           # error -> record_fetch_failure
    assert cap["fail"] == [["12"]]          # error -> queue attempts++ (by native_id)
    assert sorted(x for b in cap["write"] for x in b) == [10]
    # 11 (gone) and 10 (ok flush) both dequeued; 12 (error) stays queued.
    assert sorted(x for b in cap["complete"] for x in b) == ["10", "11"]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 1


def test_detail_drain_respects_max_claims_cap(monkeypatch):
    cap = _drain_patches(monkeypatch, [[("1", None, None, None), ("2", None, None, None)]], lambda s: "ok")
    scraper_main._run_detail_drain(max_claims=2, dry_run=False, detail_workers=1)
    # First claim is sized to the cap and, once met, the loop stops (one claim).
    assert cap["claim_n"] == [2]


def test_detail_drain_dry_run_does_not_claim(monkeypatch):
    class _CountCur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): pass
        def fetchall(self): return [("sreality", 7)]

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
        lambda **k: (captured.__setitem__("called", "index") or (0, {})),
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
    with pytest.raises(SystemExit):
        scraper_main.main(["--index-only", "--drain-only"])
