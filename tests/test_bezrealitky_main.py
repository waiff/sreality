"""bezrealitky_main completeness gate: mark_inactive only after a 100% walk."""

from __future__ import annotations

from scraper import bezrealitky_main


def test_walk_complete_requires_full_walk():
    # Architectural rule #3: only infer delisting after a FULL index walk. The
    # 100% bar is hardcoded (INDEX_MIN_COMPLETENESS=1.0), not operator-tunable,
    # so anything short of the reported totalCount reads incomplete and skips
    # the inactive sweep.
    assert bezrealitky_main._walk_complete(100, 100) is True
    assert bezrealitky_main._walk_complete(99, 100) is False
    assert bezrealitky_main._walk_complete(90, 100) is False
    assert bezrealitky_main._walk_complete(0, None) is True   # unknown total → trust the walk
