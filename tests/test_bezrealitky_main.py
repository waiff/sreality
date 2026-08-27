"""bezrealitky_main gate + sweep: mark_inactive after a ~complete (>=99.5%)
walk, with the 24h staleness rail riding on every sweep."""

from __future__ import annotations

import pytest

from scraper import bezrealitky_main
from scraper.portal import PortalConfig, walk_coverage, walk_is_complete


def test_walk_complete_requires_near_full_walk():
    # Architectural rule #3: only infer delisting after a ~complete index walk.
    # The bar is hardcoded (INDEX_MIN_COMPLETENESS=0.995, tolerating mid-walk
    # churn), not operator-tunable — a genuinely truncated walk still reads
    # incomplete and skips the inactive sweep. bezrealitky no longer owns a
    # private copy of this rule; scraper.portal.walk_is_complete is the one
    # definition for all nine portals.
    assert walk_is_complete(100, 100) is True
    assert walk_is_complete(996, 1000) is True   # 0.4% deficit = churn
    assert walk_is_complete(994, 1000) is False  # 0.6% deficit = truncated
    assert walk_is_complete(99, 100) is False
    assert walk_is_complete(90, 100) is False


def test_unmeasurable_walk_is_unknown_not_complete():
    # This case used to assert `_walk_complete(0, None) is True` ("unknown total
    # → trust the walk"). That expectation was the DEFECT, not the spec: an
    # unmeasured walk was authorising mark_inactive to delist everything it had
    # not reached. "I could not measure it" is `unknown`, and only `complete`
    # opens the delisting gate.
    assert walk_coverage(0, None) == "unknown"
    assert walk_is_complete(0, None) is False
    assert walk_is_complete(500, 0) is False    # totalCount=0 alongside real rows


def test_overcollection_is_not_completeness():
    # Collecting materially MORE than bezrealitky declared means the slices
    # overlapped or foreign stock leaked in, so the denominator is wrong;
    # contamination must not read as a proven-complete walk.
    assert walk_is_complete(1020, 1000) is True    # within the 1.02x tolerance
    assert walk_is_complete(1500, 1000) is False


def test_stopped_early_short_circuits_the_ratio():
    # The deadline/page-cap exit the walk detects outranks the arithmetic: a
    # portal that under-reports totalCount can clear 99.5% having walked a
    # fraction of the pages.
    assert walk_is_complete(1000, 1000, stopped_early=True) is False
    assert walk_coverage(1000, 1000, stopped_early=True) == "incomplete"


def _portal() -> bezrealitky_main.BezrealitkyPortal:
    return bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky",
        supports_complete_walk=True,
        categories=[{"offer_type": "PRODEJ", "estate_type": "BYT"}],
        split_threshold=None,
    ))


def test_mark_inactive_sweeps_on_native_ids_not_resolved_pks(monkeypatch):
    # Listing-identity Gate 2: non-sreality rows carry sreality_id = NULL, and
    # ONE NULL inside `<> ALL(...)` makes the predicate NULL for every row —
    # the whole portal's delisting sweep would become a permanent no-op
    # (rule #3). The sweep must key on the native id the index walked.
    # The rail (min_unseen_hours=12) must ride on every sweep — a regression
    # dropping it would silently re-expose churn-missed live rows to flips.
    monkeypatch.setattr(
        bezrealitky_main.db, "mark_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed sweep must not be used"),
    )
    captured: dict = {}
    monkeypatch.setattr(
        bezrealitky_main.db, "mark_inactive_native",
        lambda _c, source, cm, ct, natives, min_unseen_hours: (captured.update(
            cm=cm, ct=ct, natives=set(natives), source=source,
            min_unseen_hours=min_unseen_hours) or 5),
    )
    n = _portal().mark_inactive(
        object(), {"offer_type": "PRODEJ", "estate_type": "BYT"}, {"x", "y"})
    assert n == 5
    assert captured["cm"] == "byt" and captured["ct"] == "prodej"
    assert captured["source"] == "bezrealitky"
    assert captured["natives"] == {"x", "y"}   # raw walked ids, no PK round-trip
    assert captured["min_unseen_hours"] == 12


def test_mark_gone_flips_native_inactive(monkeypatch):
    # Gate 2: the gone-flip keys on the native id (mark_listing_inactive_native),
    # NOT a sreality_id resolved out of the DB — a post-Gate-2 bezrealitky row has
    # sreality_id = NULL, so the legacy sreality_id-keyed flip would silently no-op.
    captured: dict = {}
    monkeypatch.setattr(
        bezrealitky_main.db, "mark_listing_inactive_native",
        lambda _c, source, nid: captured.update(source=source, nid=nid),
    )
    monkeypatch.setattr(
        bezrealitky_main.db, "mark_listing_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed gone-flip must not be used"),
    )
    _portal().mark_gone(object(), "brk-123")
    assert captured == {"source": "bezrealitky", "nid": "brk-123"}
