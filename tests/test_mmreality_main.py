"""mmreality_main on the portal framework: MmRealityPortal (partial-walk, mixed
single index) seams + the main() that drives index-walk then detail-drain through
the shared runner, recording an 'index' + a 'detail' scrape_runs row tagged
source='mmreality'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import mmreality_main
from scraper.mmreality_main import MmRealityPortal
from scraper.portal import PortalConfig
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


def _config() -> PortalConfig:
    return PortalConfig(
        source="mmreality",
        supports_complete_walk=False,
        categories=[{"index": "nemovitosti"}],
        split_threshold=None,
    )


def _portal(**kw: Any) -> MmRealityPortal:
    return MmRealityPortal(_config(), **kw)


class _Limiter:
    def acquire(self) -> None:
        pass

    def penalize(self) -> None:
        pass


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(mmreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {"index_pages": 2, "listings_found_new": 4}),
    )
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 3}),
    )

    rc = mmreality_main.main(["--max-detail", "10"])
    assert rc == 0
    assert starts == [("index", "mmreality"), ("detail", "mmreality")]
    assert [kw["index_pages"] for _id, kw in finals] == [2, 0]
    assert finals[1][1]["listings_scraped_new"] == 3


def _stub_phases(monkeypatch, calls):
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(mmreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (calls.append(run_type) or len(calls)),
    )
    monkeypatch.setattr(mmreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))


def test_index_only_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert mmreality_main.main(["--index-only"]) == 0
    assert calls == ["index"]


def test_drain_only_skips_index(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert mmreality_main.main(["--drain-only", "--max-detail", "100"]) == 0
    assert calls == ["detail"]


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(mmreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))
    assert mmreality_main.main(["--dry-run"]) == 0
    assert starts["n"] == 0


# --- MmRealityPortal seams --------------------------------------------------


def test_portal_config_partial_walk():
    p = _portal()
    assert p.source == "mmreality"
    assert p.supports_complete_walk is False
    assert p.categories() == [{"index": "nemovitosti"}]
    # The index is mixed; no per-category label (category comes from detail JSON).
    assert p.category_labels({"index": "nemovitosti"}) == (None, None)


class _IdxClient:
    def __init__(self, *a, **k):
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_classifies_and_never_complete(monkeypatch):
    a, b, c = "944001", "944002", "944003"  # new, changed, unchanged
    base = "https://www.mmreality.cz/nemovitosti"
    page = SimpleNamespace(
        total=None, next_offset=None,
        items=[
            SimpleNamespace(source_id_native=a, detail_path=f"{base}/{a}/", price_text="5 000 000 Kč"),
            SimpleNamespace(source_id_native=b, detail_path=f"{base}/{b}/", price_text="6 000 000 Kč"),
            SimpleNamespace(source_id_native=c, detail_path=f"{base}/{c}/", price_text="7 000 000 Kč"),
        ],
    )
    monkeypatch.setattr(mmreality_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(mmreality_main, "MmRealityClient", _IdxClient)
    monkeypatch.setattr(
        mmreality_main, "index_price",
        lambda t: int("".join(c for c in (t or "") if c.isdigit()) or 0) or None,
    )
    monkeypatch.setattr(mmreality_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(
        mmreality_main.db, "index_summary_native",
        lambda _c, _s, ids: {
            b: {"sreality_id": -2, "price_czk": 5_500_000, "last_seen_at": None},  # differs
            c: {"sreality_id": -3, "price_czk": 7_000_000, "last_seen_at": None},  # same
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(mmreality_main.db, "touch_listings", lambda _c, pks: touched.update(pks=list(pks)))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mmreality_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                     or len(captured["entries"])),
    )
    seen, counts, total, pages, complete = _portal().walk_category(
        {"index": "nemovitosti"}, object(), False, _Limiter(),
    )
    assert seen == {a, b, c}
    assert total is None
    assert complete is False           # partial-walk portal: never mark_inactive
    assert touched["pks"] == [-3]
    refs = {e[0]: e for e in captured["entries"]}
    assert refs[a][3] == mmreality_main.db.QUEUE_PRIORITY_NEW
    assert refs[b][3] == mmreality_main.db.QUEUE_PRIORITY_CHANGED
    assert refs[a][1] == f"{base}/{a}/"
    assert c not in refs


def test_mark_inactive_is_noop():
    assert _portal().mark_inactive(object(), {"index": "nemovitosti"}, {"x", "y"}) == 0


class _DetailClient:
    def __init__(self, behavior):
        self._behavior = behavior

    def fetch_detail(self, ref):
        if self._behavior == "gone":
            raise ListingGoneError("/x", 404)
        if self._behavior == "boom":
            raise RuntimeError("network")
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok_passes_source_url(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_parse(html, *, source_url):
        captured["url"] = source_url
        return SimpleNamespace(raw={}, lat=50.0, lon=14.0)

    monkeypatch.setattr(mmreality_main, "parse_detail", fake_parse)
    ref = "https://www.mmreality.cz/nemovitosti/944445/"
    item = _portal().fetch_detail(_DetailClient("ok"), "944445", ref)
    assert item.kind == "ok"
    assert captured["url"] == ref


def test_fetch_detail_gone():
    item = _portal().fetch_detail(_DetailClient("gone"), "a", "/d/a")
    assert item.kind == "gone"


def test_fetch_detail_error():
    item = _portal().fetch_detail(_DetailClient("boom"), "a", "/d/a")
    assert item.kind == "error" and item.error


def test_write_details_ingests_and_counts(monkeypatch):
    listing = SimpleNamespace(raw={"image_urls": ["u1", "u2"]})
    items = [DrainItem("a", "ok", payload={
        "listing": listing, "html": "<h>", "status": 200, "url": "/d/a"})]
    monkeypatch.setattr(mmreality_main.db, "upsert_portal_raw_page", lambda *a, **k: 9)
    monkeypatch.setattr(mmreality_main.db, "ingest_scraped_listing", lambda _c, _l: (-5, "new"))
    monkeypatch.setattr(mmreality_main.db, "record_images", lambda _c, _pk, imgs: len(imgs))
    monkeypatch.setattr(mmreality_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


def test_mark_gone_flips_listing_inactive_native(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mmreality_main.db, "mark_listing_inactive_native",
        lambda _c, source, native_id: captured.update(source=source, native_id=native_id),
    )
    _portal().mark_gone(object(), "944445")
    assert captured == {"source": "mmreality", "native_id": "944445"}
