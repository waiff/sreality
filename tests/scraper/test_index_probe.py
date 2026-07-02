"""Hermetic tests for the newest-first delta probe (Wave C-2):
portal_runner.run_index_probe driven by a fake Portal, the agenda-cache-aware
set_index_page_cap seams, and every portal main's --probe CLI wiring.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from scraper import db, portal_runner
from scraper.maxima_main import MaximaPortal
from scraper.portal import default_config
from scraper.remax_main import RemaxPortal


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _ProbePortal:
    """A newest-first portal: walk_category returns the scripted results in call
    order; set_index_page_cap records the cap history."""

    source = "fake"
    index_rate = 100.0
    supports_complete_walk = True

    def __init__(self, *, categories=None, walk_results=None, walk_fails=None) -> None:
        self._categories = categories if categories is not None else ["A"]
        self._walk_results = list(walk_results or [])
        self._walk_fails = walk_fails or set()
        self.caps: list[int | None] = []
        self.conn = _Conn()
        self.calls: dict[str, list] = {"walk": [], "mark_inactive": [], "active_count": []}

    def categories(self):
        return list(self._categories)

    def category_labels(self, c):
        return (str(c), "t")

    def connect_index(self):
        return self.conn

    def set_index_page_cap(self, pages):
        self.caps.append(pages)

    def walk_category(self, c, conn, dry_run, limiter):
        self.calls["walk"].append(c)
        if c in self._walk_fails:
            raise RuntimeError(f"blocked {c}")
        if self._walk_results:
            return self._walk_results.pop(0)
        # complete=True on purpose: the probe must not sweep even on a walk
        # that CLAIMS completeness (belt on top of the max_pages gate).
        return ({"n1", "n2"}, {"found_new": 2, "enqueued": 2}, 10, 1, True)

    def mark_inactive(self, conn, c, seen):
        self.calls["mark_inactive"].append((c, set(seen)))
        return len(seen)

    def active_count(self, conn, c):
        self.calls["active_count"].append(c)
        return 5


# --- run_index_probe: discovery-only invariants -----------------------------


def test_probe_never_calls_mark_inactive():
    # Even a complete-walk-capable portal whose walk reports complete=True must
    # never sweep from a probe: a first-page diff can't prove a delisting.
    p = _ProbePortal(categories=["A", "B"])
    rc, agg = portal_runner.run_index_probe(p, dry_run=False)
    assert rc == 0
    assert p.calls["walk"] == ["A", "B"]
    assert p.calls["mark_inactive"] == []
    assert p.calls["active_count"] == []
    assert p.conn.closed


def test_probe_writes_no_scrape_run_bookkeeping(monkeypatch):
    # The probe is the images-only precedent: no scrape_runs row, no per-category
    # index_pages bump (both would masquerade as an index walk to Health, which
    # keys liveness + reconciliation on index_pages > 0 rows).
    touched: list = []
    monkeypatch.setattr(db, "scrape_run_start", lambda *a, **k: touched.append("start") or 1)
    monkeypatch.setattr(db, "bump_index_pages", lambda *a, **k: touched.append("bump"))
    monkeypatch.setattr(db, "scrape_run_finalize", lambda *a, **k: touched.append("final"))
    p = _ProbePortal()
    rc, _ = portal_runner.run_index_probe(p, dry_run=False)
    assert rc == 0
    assert touched == []


def test_probe_peeks_page_one_and_early_stops_on_zero_unknown():
    # Page 1 yields zero unknown ids -> the category stops there: exactly ONE
    # walk at cap 1, no deepening even though probe_pages allows 3.
    p = _ProbePortal(walk_results=[
        (set(), {"found_new": 0, "enqueued": 0}, 10, 1, False),
    ])
    rc, agg = portal_runner.run_index_probe(p, dry_run=False, probe_pages=3)
    assert rc == 0
    assert p.caps == [1]
    assert len(p.calls["walk"]) == 1
    assert agg["index_pages"] == 1
    assert agg["early_stopped"] == 1


def test_probe_deepens_when_page_one_has_unknown_ids():
    # Page 1 found something -> re-walk at probe_pages; the deep walk's counts
    # REPLACE the peek's (its diff re-covers page 1) while pages sum (honest
    # fetch accounting: 1 peeked + 3 deepened).
    p = _ProbePortal(walk_results=[
        ({"a"}, {"found_new": 1, "enqueued": 1}, 10, 1, False),
        ({"a", "b", "c"}, {"found_new": 3, "enqueued": 3}, 10, 3, False),
    ])
    rc, agg = portal_runner.run_index_probe(p, dry_run=False, probe_pages=3)
    assert rc == 0
    assert p.caps == [1, 3]
    assert len(p.calls["walk"]) == 2
    assert agg["listings_found_new"] == 3       # deep walk's counts, not 1 + 3
    assert agg["listings_enqueued"] == 3
    assert agg["index_pages"] == 4              # 1 (peek) + 3 (deepen)
    assert agg["early_stopped"] == 0


def test_probe_pages_one_never_deepens():
    p = _ProbePortal(walk_results=[
        ({"a"}, {"found_new": 1, "enqueued": 1}, 10, 1, False),
    ])
    rc, agg = portal_runner.run_index_probe(p, dry_run=False, probe_pages=1)
    assert rc == 0
    assert p.caps == [1]
    assert len(p.calls["walk"]) == 1
    assert agg["listings_found_new"] == 1


def test_probe_prefers_bespoke_probe_category():
    # A portal with probe_category (ceskereality) bypasses the generic
    # walk-under-cap fallback entirely.
    p = _ProbePortal()
    seen_args: list = []

    def probe_category(category, conn, dry_run, limiter, probe_pages):
        seen_args.append((category, dry_run, probe_pages))
        return ({"x"}, {"found_new": 1, "enqueued": 1}, 99, 1, False)

    p.probe_category = probe_category
    rc, agg = portal_runner.run_index_probe(p, dry_run=False, probe_pages=2)
    assert rc == 0
    assert seen_args == [("A", False, 2)]
    assert p.calls["walk"] == []                # never fell back to walk_category
    assert p.caps == []
    assert agg["listings_found_new"] == 1


def test_probe_one_failed_category_stays_green_with_error_counted():
    p = _ProbePortal(categories=["A", "B"], walk_fails={"A"})
    rc, agg = portal_runner.run_index_probe(p, dry_run=False)
    assert rc == 0
    assert agg["errors"] == 1
    assert agg["listings_enqueued"] == 2        # B still probed + enqueued


def test_probe_all_categories_failed_returns_nonzero_rc():
    p = _ProbePortal(categories=["A", "B"], walk_fails={"A", "B"})
    rc, agg = portal_runner.run_index_probe(p, dry_run=False)
    assert rc != 0
    assert agg["errors"] == 2


def test_probe_requires_a_probe_seam():
    # sreality's portal implements neither seam (its count-probe lane is a
    # separate design) -> a loud error, not a silent full walk.
    bare = SimpleNamespace(source="sreality", index_rate=1.0)
    with pytest.raises(TypeError):
        portal_runner.run_index_probe(bare, dry_run=True)


def test_probe_dry_run_uses_no_connection():
    p = _ProbePortal()
    portal_runner.run_index_probe(p, dry_run=True)
    assert p.calls["walk"] == ["A"]
    assert not p.conn.closed                    # never opened


# --- agenda-cached portals: cap change must drop the cached walk -------------


@pytest.mark.parametrize("cls,source", [(RemaxPortal, "remax"), (MaximaPortal, "maxima")])
def test_set_index_page_cap_clears_agenda_cache_on_change(cls, source):
    portal = cls(default_config(source))
    sentinel = object()
    portal._agenda_cache[1] = sentinel
    portal.set_index_page_cap(1)                # None -> 1: a cap change
    assert portal._agenda_cache == {}
    portal._agenda_cache[1] = sentinel
    portal.set_index_page_cap(1)                # same cap: keep the cached walk
    assert portal._agenda_cache == {1: sentinel}
    portal.set_index_page_cap(3)                # deepen: drop the shallow walk
    assert portal._agenda_cache == {}


# --- CLI: --probe on every portal main ---------------------------------------

_MAIN_MODULES = [
    "bazos_main",
    "bezrealitky_main",
    "ceskereality_main",
    "idnes_main",
    "maxima_main",
    "realitymix_main",
    "remax_main",
]


@pytest.mark.parametrize("mod_name", _MAIN_MODULES)
def test_main_probe_flag_runs_probe_only_and_records_no_scrape_run(monkeypatch, mod_name):
    mod = importlib.import_module(f"scraper.{mod_name}")
    monkeypatch.setattr(db, "connect", lambda: _Conn())
    starts: list = []
    monkeypatch.setattr(db, "scrape_run_start", lambda *a, **k: starts.append(a) or 1)
    monkeypatch.setattr(db, "scrape_run_finalize", lambda *a, **k: None)
    probes: list = []
    monkeypatch.setattr(
        portal_runner, "run_index_probe",
        lambda portal, dry_run, probe_pages: probes.append(
            (portal.source, dry_run, probe_pages)) or (0, {}),
    )
    walks: list = []
    drains: list = []
    monkeypatch.setattr(
        portal_runner, "run_index_walk", lambda *a, **k: walks.append(1) or (0, {}))
    monkeypatch.setattr(
        portal_runner, "run_detail_drain", lambda *a, **k: drains.append(1) or (0, {}))

    rc = mod.main(["--probe", "--probe-pages", "2"])
    assert rc == 0
    assert probes == [(mod.SOURCE, False, 2)]
    assert walks == [] and drains == []
    assert starts == []                         # a probe writes NO scrape_runs row
