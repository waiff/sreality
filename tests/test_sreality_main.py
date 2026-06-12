"""sreality_main: the framework entrypoint for sreality (Phase 4) — drives
SrealityPortal through the shared portal_runner with the framework CLI dialect,
resolving limits CLI > portals registry > baked default, and recording an
'index' / a 'detail' scrape_runs row tagged source='sreality' with the
non-destructive drain finalize (PR #403 semantics).

The portal seams themselves (district-split walk, enqueue priorities,
mark_inactive completeness gate, batched drain writes, gone/error routing) are
covered by tests/test_main.py against scraper.main.SrealityPortal — the same
object this entrypoint drives.
"""

from __future__ import annotations

from typing import Any

from scraper import sreality_main
from scraper.main import SrealityPortal
from scraper.portal import PortalConfig, PortalLimits, default_config


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


def _config(limits: PortalLimits | None = None) -> PortalConfig:
    return PortalConfig(
        source="sreality",
        supports_complete_walk=True,
        categories=[{"category_main_cb": 1, "category_type_cb": 1}],
        split_threshold=10000,
        limits=limits or PortalLimits(),
    )


# --- main(): two-phase run recording ----------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {"index_pages": 7, "listings_found_new": 5,
                                           "by_category": [{"category_main": "byt"}]}),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 2,
                                           "listings_updated": 1}),
    )

    rc = sreality_main.main(["--max-detail", "10"])
    assert rc == 0
    assert starts == [("index", "sreality"), ("detail", "sreality")]
    assert [kw["index_pages"] for _id, kw in finals] == [7, 0]
    assert finals[1][1]["listings_scraped_new"] == 2


def test_drain_finalize_is_non_destructive_index_is_not(monkeypatch):
    finals: list[dict[str, Any]] = []
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start", lambda _c, run_type, source: 1
    )
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append(kw),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {"index_pages": 1}),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_updated": 3}),
    )

    assert sreality_main.main([]) == 0
    # The drain bumps its counters per chunk; finalize must not re-write them.
    assert [kw["bump_already_applied"] for kw in finals] == [False, True]


def test_drain_finalize_runs_even_when_runner_crashes(monkeypatch):
    finals: list[dict[str, Any]] = []
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start", lambda _c, run_type, source: 9
    )
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append(kw),
    )

    def _boom(portal, dry_run, **kw):
        raise RuntimeError("network fell over")

    monkeypatch.setattr(sreality_main.portal_runner, "run_detail_drain", _boom)

    try:
        sreality_main.main(["--drain-only"])
    except RuntimeError:
        pass
    # A crashed drain still stamps ended_at without zeroing per-chunk counters.
    assert len(finals) == 1
    assert finals[0]["bump_already_applied"] is True


def _stub_phases(monkeypatch, calls):
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (calls.append(run_type) or len(calls)),
    )
    monkeypatch.setattr(sreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {}),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {}),
    )


def test_index_only_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert sreality_main.main(["--index-only"]) == 0
    assert calls == ["index"]


def test_drain_only_skips_index(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert sreality_main.main(["--drain-only", "--max-detail", "100"]) == 0
    assert calls == ["detail"]


def test_failed_index_walk_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (1, {}),
    )
    assert sreality_main.main([]) == 1
    assert calls == ["index"]


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(sreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {}),
    )
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {}),
    )
    assert sreality_main.main(["--dry-run"]) == 0
    assert starts["n"] == 0


# --- operational-limit resolution: CLI > registry > baked default -----------


def _capture_drain(monkeypatch, captured):
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start", lambda _c, run_type, source: 1
    )
    monkeypatch.setattr(sreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (captured.update(kw, portal=portal) or (0, {})),
    )


def test_registry_limits_govern_when_flags_omitted(monkeypatch):
    captured: dict[str, Any] = {}
    registry = PortalLimits(
        index_rate=2.0, detail_workers=8, detail_rate=6.0, max_detail_per_run=12000,
    )
    monkeypatch.setattr(
        sreality_main, "_load_config", lambda dry_run: _config(registry)
    )
    _capture_drain(monkeypatch, captured)

    assert sreality_main.main(["--drain-only", "--max-seconds", "2400"]) == 0
    assert captured["detail_workers"] == 8
    assert captured["detail_rate"] == 6.0
    assert captured["max_claims"] == 12000
    assert captured["max_seconds"] == 2400.0
    assert isinstance(captured["portal"], SrealityPortal)
    assert captured["portal"].index_rate == 2.0


def test_cli_flags_override_registry(monkeypatch):
    captured: dict[str, Any] = {}
    registry = PortalLimits(detail_workers=8, detail_rate=6.0, max_detail_per_run=12000)
    monkeypatch.setattr(
        sreality_main, "_load_config", lambda dry_run: _config(registry)
    )
    _capture_drain(monkeypatch, captured)

    rc = sreality_main.main(
        ["--drain-only", "--max-detail", "50", "--workers", "2", "--rate", "1.5"]
    )
    assert rc == 0
    assert captured["max_claims"] == 50
    assert captured["detail_workers"] == 2
    assert captured["detail_rate"] == 1.5


def test_max_seconds_reaches_the_index_walk(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(sreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        sreality_main.db, "scrape_run_start", lambda _c, run_type, source: 1
    )
    monkeypatch.setattr(sreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (captured.update(kw) or (0, {})),
    )
    assert sreality_main.main(["--index-only", "--max-seconds", "1200"]) == 0
    assert captured["max_seconds"] == 1200.0
    assert captured["run_id"] == 1


def test_load_config_falls_back_to_baked_default(monkeypatch):
    def _no_db():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(sreality_main.db, "connect", _no_db)
    config = sreality_main._load_config(dry_run=False)
    assert config == default_config("sreality")
    assert config.split_threshold == 10000
    assert config.supports_complete_walk is True
