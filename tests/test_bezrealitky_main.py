"""bezrealitky's nomination gate (rule #3): the 5th element of walk_category is
STRUCTURAL — did the walk reach the portal's end of list — not a count ratio.

The numeric verdict (walk_coverage / walk_is_complete) still describes the walk
and is recorded by the runner, but it no longer vetoes: a walk that paged an
offset-list until the portal ran out of adverts has finished, even when the
declared totalCount says it should have found more. What still vetoes is a stop
of OURS — a deadline, a page cap, an exception, or a barren page (an items-less
HTTP 200, which on this portal is byte-identical to a soft-blocked GraphQL
answer) that a second read could not corroborate.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from scraper import bezrealitky_main
from scraper.portal import PortalConfig, walk_coverage

Answer = tuple[list[dict[str, Any]], int]


def _ads(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"id": f"brk-{start + i}", "price": 1_000_000} for i in range(n)]


class _FakeClient:
    """A scripted `listAdverts`: each offset maps to the answers it gives, in
    order, so a re-read of the SAME offset can differ from the first read (that
    is exactly what the barren rule turns on). The last answer repeats."""

    def __init__(self, script: dict[int, list[Answer | Exception]]) -> None:
        self.script = script
        self.calls: list[int] = []

    def search(
        self, offer_type: str, estate_type: str, *, limit: int, offset: int,
        include_imports: bool = True, include_short_term: bool = True,
    ) -> Answer:
        self.calls.append(offset)
        answers = self.script[offset]
        answer = answers[min(self.calls.count(offset) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _portal(**kwargs: Any) -> bezrealitky_main.BezrealitkyPortal:
    return bezrealitky_main.BezrealitkyPortal(PortalConfig(
        source="bezrealitky",
        supports_complete_walk=True,
        categories=[{"offer_type": "PRODEJ", "estate_type": "BYT"}],
        split_threshold=None,
    ), **kwargs)


def _walk(
    monkeypatch: pytest.MonkeyPatch, script: dict[int, list[Answer | Exception]],
    *, max_pages: int | None = None, deadline: float | None = None,
) -> tuple[tuple[set[str], dict[str, int], int | None, int, bool], _FakeClient]:
    client = _FakeClient(script)
    monkeypatch.setattr(bezrealitky_main, "BezrealitkyClient", lambda **_kw: client)
    portal = _portal(max_pages=max_pages)
    result = portal.walk_category(
        {"offer_type": "PRODEJ", "estate_type": "BYT"}, None, True, None, deadline,
    )
    return result, client


def test_catching_the_declared_total_reaches_the_portals_end(monkeypatch):
    # The healthy exit: offset caught the portal's OWN count, so the page after
    # the last one is never requested.
    (seen, _counts, declared, pages, reached_end), client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 250)],
        100: [(_ads(100, 100), 250)],
        200: [(_ads(50, 200), 250)],
    })
    assert reached_end is True
    assert (len(seen), declared, pages) == (250, 250, 3)
    assert client.calls == [0, 100, 200]


def test_an_over_declared_total_that_ran_dry_still_reaches_the_end(monkeypatch):
    # THE case this change exists for: the portal declares 300, the list runs
    # out at 240, and the walk is FINISHED — the empty page past the position
    # the total implies is corroborated by a second read. The numeric verdict is
    # still "incomplete" and no longer has a vote.
    (seen, _counts, declared, _pages, reached_end), client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 300)],
        100: [(_ads(100, 100), 300)],
        200: [(_ads(40, 200), 300)],
        240: [([], 0)],
    })
    assert reached_end is True
    assert (len(seen), declared) == (240, 300)
    assert walk_coverage(len(seen), declared) == "incomplete"
    assert client.calls == [0, 100, 200, 240, 240]  # the empty page was re-read


def test_an_offset_the_api_ignores_is_our_stop(monkeypatch):
    """listAdverts is an offset API with no pager to read, so the only proof the
    list advanced is that a page carried an advert we did not already hold. A
    resolver that clamps or ignores `offset` (a cache serving the head page)
    would otherwise march `offset` to the declared total on 100 distinct ids and
    exit `declared_total_reached` — reporting the portal's end over 0.5% of the
    category."""
    (seen, _counts, declared, pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 20_000)],
        100: [(_ads(100, 0), 20_000)],
    })
    assert (len(seen), declared, pages) == (100, 20_000, 2)
    assert reached_end is False


def test_a_total_that_steps_down_below_the_offset_is_not_an_end(monkeypatch):
    """`offset >= declared_total` is a PORTAL end, so the count may only ever be
    latched upward: a totalCount that degrades mid-walk (a partial index, a
    filtered count) below the offset already reached would otherwise end the walk
    on a FULL page of items three pages into a 20,000-row category."""
    (seen, _counts, declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 20_000)],
        100: [(_ads(100, 100), 20_000)],
        200: [(_ads(100, 200), 150)],
        300: [([], 0)],
    })
    assert (len(seen), declared) == (300, 20_000)
    # It keeps paging; the empty page four pages in is a soft block's shape and
    # nothing corroborates it, so the walk still nominates nothing.
    assert reached_end is False


def test_a_barren_page_mid_walk_is_our_stop(monkeypatch):
    # An items-less 200 a page and a half into a 1,000-row category is what a
    # soft block looks like; it is never the end, however many times it repeats.
    (seen, _counts, declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 1000)],
        100: [([], 0)],
    })
    assert reached_end is False
    assert (len(seen), declared) == (100, 1000)  # the barren page did not zero it


def test_a_throttled_true_last_page_is_not_confirmed(monkeypatch):
    # The #637 lesson: the last page the declared total implies coming back
    # empty is exactly the page a throttle is most likely to eat. Nothing past
    # the implied last page was ever requested, so there is no corroboration.
    (_seen, _counts, _declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 250)],
        100: [(_ads(100, 100), 250)],
        200: [([], 0)],
    })
    assert reached_end is False


def test_a_barren_page_whose_re_read_carries_items_is_our_stop(monkeypatch):
    # The re-read disagreeing with the first read proves the emptiness was
    # transient, not the end of the list.
    (_seen, _counts, _declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 300)],
        100: [(_ads(100, 100), 300)],
        200: [(_ads(40, 200), 300)],
        240: [([], 0), (_ads(60, 240), 300)],
    })
    assert reached_end is False


def test_an_unreadable_re_read_is_not_a_confirmation(monkeypatch):
    # A confirmation that cannot be obtained is not a confirmation: the failed
    # re-read is swallowed (the walk's own work is already done) and the
    # category simply nominates nothing.
    (seen, _counts, _declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 300)],
        100: [(_ads(100, 100), 300)],
        200: [(_ads(40, 200), 300)],
        240: [([], 0), RuntimeError("graphql errors: throttled")],
    })
    assert reached_end is False
    assert len(seen) == 240


def test_an_empty_category_is_a_measured_zero(monkeypatch):
    # Both reads agree the scope holds nothing. The runner's empty-seen guard is
    # what keeps this from nominating a whole category (portal_runner).
    (seen, _counts, declared, pages, reached_end), client = _walk(monkeypatch, {
        0: [([], 0)],
    })
    assert reached_end is True
    assert (seen, declared, pages) == (set(), 0, 1)
    assert client.calls == [0, 0]


def test_an_empty_first_page_that_declares_stock_is_barren(monkeypatch):
    # The shell answer with a real count preserved: the portal says 5,000 and
    # hands over nothing, which is a block, not an end.
    (_seen, _counts, declared, _pages, reached_end), _client = _walk(monkeypatch, {
        0: [([], 5000)],
    })
    assert reached_end is False
    assert declared is None


def test_a_page_cap_is_our_stop_even_when_the_list_ended(monkeypatch):
    # --max-pages (and the delta probe's page cap) is an operator restriction on
    # this run: whatever the loop did underneath it, the walk may not nominate.
    (_seen, _counts, _declared, pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 100)],
    }, max_pages=1)
    assert (reached_end, pages) == (False, 1)


def test_a_fired_deadline_is_our_stop(monkeypatch):
    (_seen, _counts, _declared, pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 1000)],
        100: [(_ads(100, 100), 1000)],
    }, deadline=time.monotonic() - 1)
    assert (reached_end, pages) == (False, 1)


def test_a_walk_that_finishes_on_the_budget_still_reaches_the_end(monkeypatch):
    # Ordering: the natural end is tested BEFORE the deadline, so a walk that
    # read the portal's last page exactly as the budget expired is finished, not
    # truncated — it has nothing left to walk.
    (_seen, _counts, _declared, pages, reached_end), _client = _walk(monkeypatch, {
        0: [(_ads(100, 0), 100)],
    }, deadline=time.monotonic() - 1)
    assert (reached_end, pages) == (True, 1)


def test_a_failing_page_aborts_the_category(monkeypatch):
    # Index errors are not swallowed here: they propagate to the runner, which
    # records the category as failed and forces reached_end=False.
    with pytest.raises(RuntimeError):
        _walk(monkeypatch, {0: [RuntimeError("graphql errors: rate limited")]})


def test_delisting_uses_the_runners_default_nomination():
    """Rule #3 since 2026-09-07: the walk nominates unseen rows for a page
    check and the runner does it generically, keyed on the native id the index
    walked (a NULL sreality_id under listing-identity Gate 2 can never poison
    it). bezrealitky has no special scoping, so it carries neither the old sweep
    seam nor an override."""
    p = _portal()
    assert not hasattr(p, "mark_inactive")
    assert not hasattr(p, "presence_candidates")
    assert getattr(p, "seen_key", "native") == "native"


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
