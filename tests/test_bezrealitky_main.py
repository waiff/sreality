"""bezrealitky_main gate + sweep: mark_inactive after a ~complete (>=99.5%)
walk, with the 24h staleness rail riding on every sweep."""

from __future__ import annotations

from scraper import bezrealitky_main
from scraper.portal import PortalConfig


def test_walk_complete_requires_near_full_walk():
    # Architectural rule #3: only infer delisting after a ~complete index walk.
    # The bar is hardcoded (INDEX_MIN_COMPLETENESS=0.995, tolerating mid-walk
    # churn), not operator-tunable — a genuinely truncated walk still reads
    # incomplete and skips the inactive sweep.
    assert bezrealitky_main._walk_complete(100, 100) is True
    assert bezrealitky_main._walk_complete(996, 1000) is True   # 0.4% deficit = churn
    assert bezrealitky_main._walk_complete(994, 1000) is False  # 0.6% deficit = truncated
    assert bezrealitky_main._walk_complete(99, 100) is False
    assert bezrealitky_main._walk_complete(90, 100) is False
    assert bezrealitky_main._walk_complete(0, None) is True   # unknown total → trust the walk


def test_mark_inactive_passes_staleness_rail(monkeypatch):
    # The rail (min_unseen_hours=24) must ride on every sweep — a regression
    # dropping it would silently re-expose churn-missed live rows to flips.
    monkeypatch.setattr(
        bezrealitky_main.db, "index_summary_native",
        lambda _c, _s, ids: {n: {"sreality_id": -i, "price_czk": 1, "last_seen_at": None}
                             for i, n in enumerate(sorted(ids), 1)},
    )
    captured: dict = {}
    monkeypatch.setattr(
        bezrealitky_main.db, "mark_inactive",
        lambda _c, cm, ct, pks, source, min_unseen_hours: (captured.update(
            cm=cm, ct=ct, pks=set(pks), source=source,
            min_unseen_hours=min_unseen_hours) or 5),
    )
    portal = bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky",
        supports_complete_walk=True,
        categories=[{"offer_type": "PRODEJ", "estate_type": "BYT"}],
        split_threshold=None,
    ))
    n = portal.mark_inactive(
        object(), {"offer_type": "PRODEJ", "estate_type": "BYT"}, {"x", "y"})
    assert n == 5
    assert captured["cm"] == "byt" and captured["ct"] == "prodej"
    assert captured["source"] == "bezrealitky"
    assert captured["min_unseen_hours"] == 24
