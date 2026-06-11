"""bezrealitky_main completeness gate: mark_inactive only after a 100% walk."""

from __future__ import annotations

from scraper import bezrealitky_main


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
