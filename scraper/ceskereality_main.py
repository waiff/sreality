"""Orchestrator for the ceskereality.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.ceskereality_main`. ceskereality is a `Portal`
(CeskerealityPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages the HTML search results and enqueues new/price-changed ids
into the shared `listing_detail_queue` (source='ceskereality', migration 108),
then a detail-drain that fetches each listing page, parses it to a
`ScrapedListing`, and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency
+ Tier-1 matching). No bespoke pipeline — only the per-portal fetcher
(CeskerealityClient) + parser (ceskereality_parser) + config differ from
sreality/idnes (the modularity rule in CLAUDE.md).

ceskereality's search pages carry a result total (the meta "Máme tady N…"), and a
FILTERED search URL pages deep and row-faithfully (verified: /prodej/byty/praha/
?strana=93 returns exactly 3 items = the declared 1843, and ?strana=94 404s), so a
walk partitioned on the 14 declared kraje can page each slice to its own tail.
Nomination is STRUCTURAL since 2026-09-08 (architectural rule #3): a category all
of whose slices ended because ceskereality said there was no more — a disabled next
arrow, the region's own declared tail, a confirmed-empty region — nominates its
unseen rows for a page check, and the PAGE decides; no portal flips a row from index
absence any more. The count is a logged COVERAGE alarm, never a gate. Nomination is
source-scoped, so it only ever touches ceskereality rows (rule #15). The detail URL carries the
category (`/{sale}/{cat}/…`), so the drain derives each listing's category from
its own URL — one config walks many categories. Coordinates come straight from the
page's `data-coord-lat`/`data-coord-lng`, so there is no geocoding step.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, replace
from typing import Any, Literal

from scraper import db, portal_runner
from scraper.ceskereality_client import (
    KRAJ_SLUGS,
    SUBTYPE_SLUGS,
    CeskerealityClient,
    detail_url,
    search_url,
)
from scraper.ceskereality_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    category_from_url,
    extract_facet_slugs,
    heading_names_kraj,
    index_price,
    parse_detail,
    parse_index,
)
from scraper.portal import (
    PortalConfig,
    StopReason,
    default_config,
    deadline_reached,
    load_portal_config,
    classify_index_sighting,
    stop_is_portal_end,
    walk_is_complete,
    walk_reached_end,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "ceskereality"

# The nomination verdict is STRUCTURAL (rule #3, 2026-09-08): a category
# nominates when every one of the 14 kraje stopped because ceskereality said
# there was no more — the pager's disabled next arrow, the region's own declared
# tail, or a confirmed-empty region — and no stop of OURS (deadline, page cap,
# --kraje, a fetch error, a barren page) fired anywhere in it.
# scraper.portal.walk_reached_end spells that conjunction for all nine portals.
# The arithmetic — scraper.portal.walk_is_complete, min ratio + over-collection
# ceiling — stays exactly where it was but no longer gates: it is the slice
# ledger's `outcome`, the descent trigger, and a coverage alarm. A region one row
# short of its own declared count is a finished walk over a live index, not a
# truncated one; that one row kept a 20,964-row category from nominating anything.
# No staleness rail any more (rule #3, 2026-09-07): a walk that reached the end
# nominates the rows it did not see for a page check and the drain's fetch
# decides, so a single walk-miss costs one fetch, never a live listing.

# The 12-page cap is NOT a site-wide law — it belongs to UNFILTERED category URLs
# (/prodej/byty/?strana=13 = 404) and to nothing else. A FILTERED URL caps at 99
# pages / 1,980 rows (measured 2026-08-27 on /prodej/rodinne-domy/stredocesky-kraj/:
# ?strana=99 serves 20 cards, ?strana=100 is a 404; the national pagination widget
# maxes at 99 too). So 99 is the site's ceiling on a kraj slice, and a slice whose
# declared total needs more than that descends onto the subtype axis rather than
# quietly losing its tail. _PROBE_MAX_PAGES is the OTHER cap: --probe reads the
# unfiltered /nejnovejsi/ URL, which is exactly the shape that really does 404 at 13.
_MAX_SLICE_PAGES = 99
_PROBE_MAX_PAGES = 12
_PER_PAGE = 20

# ceskereality's default index order is NOT newest-first, but every category page
# links a newest-first sort variant at /{sale}/{category}/nejnovejsi/ (live-verified
# 2026-07-02: 200 on www, standard i-estate cards + "Máme tady N" total + ?strana
# paging) — it fits search_url's sub_slug slot, so the delta probe reads it on the
# nationwide www host instead of enumerating the region×facet slices.
_PROBE_SUB_SLUG = "nejnovejsi"


SliceOutcome = Literal["exhausted", "deadline", "ceiling", "error", "degraded"]


@dataclass(frozen=True)
class SliceResult:
    """One (kraj[, subtype]) slice, carrying BOTH verdicts.

    `outcome` is the arithmetic one and is unchanged — it is what the slice
    ledger stores and what scripts/coverage_gate.py counts as `exhausted`.
    `stop` is the structural one: why the page loop ended. Only `stop` decides
    whether the category may nominate (rule #3), so a slice that paged to the
    portal's end one row short of its own declared count reads `degraded` in the
    ledger and `reached_end` at the gate.
    """

    kraj: str
    subtype: str | None
    rows: list[tuple[str, str, int | None]]
    declared_total: int | None
    pages: int
    outcome: SliceOutcome
    stop: StopReason

    @property
    def positive(self) -> bool:
        return self.outcome == "exhausted"

    @property
    def reached_end(self) -> bool:
        return stop_is_portal_end(self.stop)


class CeskerealityPortal:
    """ceskereality.cz as a Portal: the seams the generic runner needs, wrapping
    the ceskereality client + parser. Operational scope (categories, complete-walk
    capability) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 0.7
    # The denominator this portal reports to the runner is the NATIONAL total, and
    # the kraj union cannot reach it by construction: listings filed under no kraj
    # at all sit in no slice (rentals: 4,732 of 4,771, twelve walks in a row). So
    # the runner's generic COVERAGE warning would fire on every category of every
    # walk — an alarm that is on by construction is noise that hides the real one.
    # The gap is still recorded in scrape_runs.by_category, and this portal emits
    # its own scoped kraj-sum-vs-national warning below.
    coverage_denominator_is_upper_bound = True

    def __init__(
        self,
        config: PortalConfig,
        *,
        max_pages: int | None = None,
        kraje: tuple[str, ...] | None = None,
    ) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        # A kraj subset to walk (for an ad-hoc one-kraj test); None = all 14.
        # When set, the walk is partial so it nominates nothing (rule #3).
        self._kraje = kraje
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct
        # per-(cm, ct) union of complete slices' seen ids + completed-slice
        # counts — the cross-slice nomination buffer (see presence_candidates).
        self._sweep_seen: dict[tuple[str, str], set[str]] = {}
        self._sweep_done: dict[tuple[str, str], int] = {}

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            CATEGORY_MAIN.get(category.get("category")),
            SALE_TYPE.get(category.get("sale_type")),
        )

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        conn = db.connect()
        return conn

    def _archive_index_page(
        self, conn: Any, key: str, url: str, html: str, status: int,
        fresh_keys: set[str] | None,
    ) -> None:
        """W0 item 0n: search pages carry index-only signals (map markers)."""
        if fresh_keys is None or key not in fresh_keys:
            try:
                db.upsert_portal_raw_page(
                    conn,
                    source=SOURCE,
                    source_id_native=key,
                    source_url=url,
                    page_kind="index",
                    html=html,
                    http_status=status,
                    refresh_after_hours=db.INDEX_ARCHIVE_REFRESH_HOURS,
                )
                if fresh_keys is not None:
                    fresh_keys.add(key)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("INDEX archive failed url=%s: %s", url, exc)

    def _confirm_slice_is_empty(
        self, client: CeskerealityClient, url: str, kraj: str,
    ) -> bool:
        """Re-read the SAME url that looked empty; True only if it is empty again.

        Takes the url rather than rebuilding it, so the confirmation cannot drift
        onto a different page than the one it is confirming. Any failure to
        re-read returns False (barren), never True — a confirmation that cannot
        be obtained is not a confirmation. This is the barren rule's second read,
        for the empty page 1 and for a blank page mid-slice alike.
        """
        try:
            html, _status = client.fetch_search(url)
        except Exception as exc:  # noqa: BLE001 - an unreadable re-read proves nothing
            LOG.warning("SLICE empty re-read failed kraj=%s: %s", kraj, exc)
            return False
        reread = parse_index(html)
        return not reread.items and reread.total in (None, 0)

    def _walk_slice(
        self, client: CeskerealityClient, sale_type: str, cat: str, kraj: str, *,
        subtype: str | None = None, conn: Any = None,
        archive_week: str | None = None, fresh_keys: set[str] | None = None,
        deadline: float | None = None,
    ) -> SliceResult:
        """Walk ONE (kraj[, subtype]) slice until ceskereality says there is no more.

        Two verdicts come back (see SliceResult): `stop`, the structural one that
        gates nomination, and `outcome`, the arithmetic one the ledger keeps. The
        load-bearing case is a 200 carrying ZERO cards: that is the site's real
        degraded response (the 404 is not — `Retry-After: 3` accompanies every 404
        here, nonexistent paths included), so it is `barren` — a stop of OURS —
        until the page proves it is the empty slice we asked for
        (`heading_names_kraj` plus a second read, or a declared zero).
        """
        rows: list[tuple[str, str, int | None]] = []
        declared_total: int | None = None
        last_page: int | None = None
        live_last: int | None = None
        page = 1
        page_cap = self._max_pages or _MAX_SLICE_PAGES

        def out(
            stop: StopReason, pages: int, *, outcome: SliceOutcome | None = None,
        ) -> SliceResult:
            # The ledger's outcome stays the shared two-sided arithmetic verdict,
            # on DISTINCT ids (pages shift under a live walk, so len(rows)
            # double-counts). It no longer decides anything here — `stop` does.
            if outcome is None:
                outcome = (
                    "exhausted"
                    if walk_is_complete(len({r[0] for r in rows}), declared_total)
                    else "degraded"
                )
            return SliceResult(
                kraj, subtype, rows, declared_total, max(pages, 0), outcome, stop)

        stop: StopReason
        while True:
            if page > page_cap:
                return out("page_cap", page - 1, outcome="ceiling")
            # Budget spent: stop BEFORE issuing another request and report the
            # slice as a deadline stop — the rows already collected still count.
            if deadline_reached(deadline):
                LOG.info(
                    "DEADLINE index walk stopped cm=%s ct=%s kraj=%s subtype=%s "
                    "after page=%d collected=%d",
                    cat, sale_type, kraj, subtype or "all", page - 1, len(rows),
                )
                return out("deadline", page - 1, outcome="deadline")
            url = search_url(
                sale_type, cat, kraj=kraj, subtype=subtype,
                page=page if page > 1 else None,
            )
            try:
                html, status = client.fetch_search(url)
            except Exception as exc:  # noqa: BLE001 - one slice must not kill the walk
                # NOT a clean finish: a fetch that failed is missing evidence, and
                # ListingGoneError here is a 404 we did not expect to exist.
                LOG.warning("SLICE error kraj=%s subtype=%s page=%d: %s",
                            kraj, subtype or "all", page, exc)
                return out("error", page - 1, outcome="error")
            if conn is not None and archive_week is not None:
                # v2/ prefix: portal_raw_pages is UNIQUE(source, source_id_native,
                # page_kind), and without it the dead v1 subdomain/facet keys and
                # these kraj keys would interleave in one table indistinguishably.
                key = f"v2/{sale_type}/{cat}/{kraj}/{subtype or 'all'}/{page}/{archive_week}"
                # W2a-0: the instrument's denominator is FETCHES, never archive
                # writes — recorded ahead of the client-side freshness skip, the
                # same shape as sreality's and remax's archivers.
                self._archive_index_page(conn, key, url, html, status, fresh_keys)
            parsed = parse_index(html)
            if page == 1:
                declared_total = parsed.total
                if declared_total is None:
                    # No "Máme tady N" at all. A genuinely empty slice looks EXACTLY
                    # like this and there is no count to fail closed on, so the H1
                    # has to carry the proof; anything else is a stop of ours.
                    if not parsed.items and heading_names_kraj(html, kraj):
                        # CONFIRM THE ZERO BY READING IT TWICE.
                        #
                        # The site publishes no "no results" string — an empty
                        # slice renders the shell with an empty results block and
                        # no count phrase. That is byte-for-byte the shape of a
                        # THROTTLED page, because the count comes from the same
                        # query as the cards and vanishes with them. An
                        # adversarial review reproduced complete=True with a
                        # whole kraj missing on exactly this path, so the H1
                        # alone cannot carry the proof.
                        #
                        # A throttle is transient; a genuinely empty kraj is
                        # stable. Reading it twice separates them, and costs one
                        # extra request only for slices that are already one page
                        # long. It is the barren rule (scraper.portal.StopReason):
                        # an items-less 200 is ours until it is corroborated, and
                        # this is the only corroboration this site offers.
                        if not self._confirm_slice_is_empty(client, url, kraj):
                            LOG.warning(
                                "SLICE cm=%s ct=%s kraj=%s subtype=%s looked empty "
                                "but did not confirm on re-read; treating as barren",
                                cat, sale_type, kraj, subtype or "all",
                            )
                            return out("barren", 1, outcome="degraded")
                        declared_total = 0
                        LOG.info(
                            "SLICE cm=%s ct=%s kraj=%s subtype=%s declared=0 "
                            "collected=0 pages=1 outcome=exhausted (empty-confirmed x2)",
                            cat, sale_type, kraj, subtype or "all",
                        )
                        return out("empty_confirmed", 1, outcome="exhausted")
                    if not parsed.items:
                        # Zero cards, no count, and an H1 that does not name the
                        # kraj we asked for: an items-less 200 nobody corroborated.
                        return out("barren", 1, outcome="degraded")
                    # Cards but no count. The count renders from the same query as
                    # the cards, so a page that kept one and lost the other is a
                    # broken page, not a tail — unmeasurable is never an end.
                    return out("error", 1, outcome="degraded")
                last_page = max(1, -(-declared_total // _PER_PAGE))
                if last_page > _MAX_SLICE_PAGES:
                    # Past the site's own 99-page ceiling on a filtered URL: the
                    # tail is unreachable on this axis, so descend instead.
                    return out("page_cap", 1, outcome="ceiling")
            if parsed.total is not None:
                live_last = max(1, -(-parsed.total // _PER_PAGE))
            # ITEMS FIRST, ahead of the pager: a blocked page carries no next
            # arrow either, and reading one as the last page is how a soft block
            # becomes a nomination.
            if not parsed.items:
                if page == 1 and declared_total == 0:
                    # A measured zero with zero collected: the portal's own answer.
                    return out("empty_confirmed", 1, outcome="exhausted")
                # BARREN. It is the portal's end only if the page is past what
                # the declared total implies AND a re-read of the SAME url is
                # empty again. Position first: it is the necessary half, and a
                # page we already suspect is a soft block is not worth a second
                # request we could not use anyway. On this site the position half
                # effectively never holds — the loop bounds itself at the declared
                # tail and deliberately never fetches the 404 beyond it — so a
                # blank page mid-slice stays a stop of ours.
                if (
                    last_page is not None and page > last_page
                    and self._confirm_slice_is_empty(client, url, kraj)
                ):
                    return out("empty_confirmed", page - 1)
                return out("barren", page, outcome="degraded")
            for item in parsed.items:
                rows.append((
                    item.source_id_native,
                    detail_url(item.detail_path),
                    index_price(item.price_text),
                ))
            # The tail can move under a live walk (~7-11 rows/10 min), so believe
            # whichever declared count says we are done first.
            stop_at = min(x for x in (last_page, live_last) if x is not None)
            if page >= stop_at:
                # The portal's own count says this is the tail; a disabled next
                # arrow on the same page is the stronger way to say it.
                stop = (
                    "pager_end" if parsed.next_offset is None
                    else "declared_total_reached"
                )
                break
            if parsed.next_offset is None:
                if not parsed.pager_end_marker:
                    # No next arrow AND no disabled one either: this page carries no
                    # pagination block at all, which is a truncated body / edge shell,
                    # not the site saying "there is no more". Reading absence as an
                    # end would turn any partially rendered page that still carries a
                    # card into a category-wide nomination.
                    LOG.warning(
                        "SLICE cm=%s ct=%s kraj=%s subtype=%s page=%d of %d rendered "
                        "no pagination block; that is not an end-of-results",
                        cat, sale_type, kraj, subtype or "all", page, stop_at,
                    )
                    return out("barren", page, outcome="degraded")
                # The pager ended BEFORE the declared tail. That is still the
                # PORTAL's end — the arrow is disabled on the last page — not a
                # truncation of ours. The shortfall lands in `outcome` and in this
                # line; the rows we did not see are nominated, and their pages
                # decide. Reading it as a failure is what kept a 20,964-row
                # category silent.
                LOG.warning(
                    "SLICE cm=%s ct=%s kraj=%s subtype=%s pager ended at page=%d "
                    "of %d: collected=%d declared=%s",
                    cat, sale_type, kraj, subtype or "all", page, stop_at,
                    len({r[0] for r in rows}), declared_total,
                )
                stop = "pager_end"
                break
            page += 1

        return out(stop, page)

    def _nationwide_total(self, client: CeskerealityClient, sale_type: str, cat: str) -> int | None:
        """The www result total — the portal-reported count for the RECONCILE +
        the category verdict's cross-check (the kraj slices report their own
        subsets, which is the primary denominator)."""
        try:
            html, _ = client.fetch_index(sale_type, cat, None)
            return parse_index(html).total
        except Exception:                   # noqa: BLE001
            return None

    def _descend_slice(
        self, client: CeskerealityClient, sale_type: str, cat: str,
        parent: SliceResult, *, conn: Any = None, archive_week: str | None = None,
        fresh_keys: set[str] | None = None, deadline: float | None = None,
    ) -> list[SliceResult]:
        """The second axis, depth EXACTLY one: a kraj past the 99-page ceiling is
        re-walked per subtype. Declared slugs where we measured them (subtype is a
        true partition within a kraj: the 10 rodinne-domy slugs summed to 2,312 in
        stredocesky — the kraj total exactly); the rendered facets otherwise, and
        either way this SELF-VERIFIES the children's declared sum against the
        parent's, so a missing subtype reads incomplete instead of silently
        dropping the residue."""
        kraj = parent.kraj
        subtypes: tuple[str, ...] = SUBTYPE_SLUGS.get(cat, ())
        if not subtypes:
            try:
                html, _ = client.fetch_search(search_url(sale_type, cat, kraj=kraj))
                subtypes = tuple(
                    s for s in extract_facet_slugs(html, sale_type, cat)
                    if s.startswith(f"{cat}-")
                )
            except Exception as exc:        # noqa: BLE001
                LOG.warning("DESCENT facet probe failed kraj=%s: %s", kraj, exc)
                subtypes = ()
        if not subtypes:
            LOG.warning("DESCENT no subtype axis cm=%s ct=%s kraj=%s declared=%s",
                        cat, sale_type, kraj, parent.declared_total)
            return [parent]                 # still 'ceiling' -> category incomplete
        children: list[SliceResult] = []
        for slug in subtypes:
            if deadline_reached(deadline):
                children.append(replace(parent, outcome="deadline", stop="deadline"))
                return children
            children.append(self._walk_slice(
                client, sale_type, cat, kraj, subtype=slug, conn=conn,
                archive_week=archive_week, fresh_keys=fresh_keys, deadline=deadline,
            ))
        child_declared = sum(c.declared_total or 0 for c in children)
        if not walk_is_complete(child_declared, parent.declared_total):
            LOG.warning(
                "DESCENT residue cm=%s ct=%s kraj=%s children_declared=%d parent=%s",
                cat, sale_type, kraj, child_declared, parent.declared_total,
            )
            children.append(replace(parent, rows=[], pages=0, outcome="ceiling"))
        return children

    def _record_slices(
        self, conn: Any, category: dict[str, Any], kraje: tuple[str, ...] | list[str],
        results: list[SliceResult], deadline_hit: bool,
    ) -> None:
        """Write this category's coverage to the slice ledger, ONE ROW PER KRAJ.

        The aggregation is the whole point. A kraj past the page ceiling is
        re-walked per subtype, so it comes back as several SliceResults sharing
        one kraj — and recording those individually would poison the ledger the
        first time a kraj stopped needing the descent: the `kraj/subtype-*` rows
        from the old shape would linger, never be re-walked, and age forever.
        The gate reads "every slice of this category exhausted inside the
        window", so one permanently stale row is enough to hold the portal parked
        for good. Collapsing to the kraj — the axis that is actually stable —
        means the ledger's key set never changes shape.

        A kraj counts as exhausted only when EVERY result for it did; the union
        of its rows is what it collected. A kraj the deadline never reached is
        not written at all, so it keeps its old timestamp and sorts first next
        run (see db.slice_staleness — absent means never walked, not fresh).
        """
        if conn is None:
            return
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return
        by_kraj: dict[str, list[SliceResult]] = {}
        for r in results:
            by_kraj.setdefault(r.kraj, []).append(r)
        for kraj, parts in by_kraj.items():
            positive = all(p.positive for p in parts)
            outcome = "exhausted" if positive else next(
                (p.outcome for p in parts if not p.positive), "incomplete")
            # Children partition their parent, so their declared totals sum; an
            # un-descended kraj is the single-element case of the same sum.
            declared = sum(p.declared_total or 0 for p in parts) or None
            db.record_index_slice(
                conn, source=SOURCE, category_main=cm, category_type=ct,
                slice_key=kraj, outcome=outcome, declared_total=declared,
                collected=len({x[0] for p in parts for x in p.rows}),
                pages=sum(p.pages for p in parts),
            )

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = CeskerealityClient(limiter=limiter)
        kraje = self._kraje or KRAJ_SLUGS

        archive_week = db.index_archive_week() if conn is not None else None
        fresh_keys: set[str] = set()
        if conn is not None:
            try:
                fresh_keys = db.fresh_index_page_keys(
                    conn, SOURCE, hours=db.INDEX_ARCHIVE_REFRESH_HOURS
                )
            except Exception as exc:  # noqa: BLE001 - optimisation only
                LOG.warning("INDEX archive preload failed: %s", exc)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        seen_ids: set[str] = set()
        # A deadline stop poisons the WHOLE category verdict, not just its slice:
        # the un-walked kraje never report at all, so per-slice stops alone would
        # let a truncated walk claim it reached the portal's end. It is folded
        # into `our_stop` below, and the kraje it never reached are seeded as
        # `slice_unreached`.
        deadline_hit = False
        results: list[SliceResult] = []
        for kraj in kraje:
            if deadline_reached(deadline):
                deadline_hit = True
                break
            r = self._walk_slice(
                client, sale_type, cat, kraj, conn=conn, archive_week=archive_week,
                fresh_keys=fresh_keys, deadline=deadline,
            )
            if r.outcome == "ceiling" and not self._max_pages:
                results.extend(self._descend_slice(
                    client, sale_type, cat, r, conn=conn, archive_week=archive_week,
                    fresh_keys=fresh_keys, deadline=deadline,
                ))
            else:
                results.append(r)

        for r in results:
            LOG.info(
                "SLICE cm=%s ct=%s kraj=%s subtype=%s declared=%s collected=%d "
                "pages=%d outcome=%s stop=%s",
                cat, sale_type, r.kraj, r.subtype or "all", r.declared_total,
                len({x[0] for x in r.rows}), r.pages, r.outcome, r.stop,
            )
            for nid, ref, price in r.rows:
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    native_ids.append(nid)
                ref_map[nid] = ref
                price_map[nid] = price

        self._record_slices(conn, category, kraje, results, deadline_hit)

        pages = sum(r.pages for r in results)
        # Summed over the slices that REACHED THE PORTAL'S END, not the ones that
        # cleared 99.5% of their own count: a region walked to its last page is a
        # region whose declared count we believe, however live the tail was.
        declared_sum = sum(r.declared_total or 0 for r in results if r.reached_end)
        national = self._nationwide_total(client, sale_type, cat)
        kraje_seen = {r.kraj for r in results}
        LOG.info(
            "PARTITION cm=%s ct=%s kraje=%d slices=%d reached_end=%d positive=%d "
            "collected=%d declared_sum=%d national=%s pages=%d",
            cat, sale_type, len(kraje_seen), len(results),
            sum(1 for r in results if r.reached_end),
            sum(1 for r in results if r.positive), len(seen_ids), declared_sum,
            national, pages,
        )

        seen = set(native_ids)
        existing = (
            db.index_summary_native(conn, SOURCE, native_ids)
            if conn is not None else {}
        )
        new_ids = [n for n in native_ids if n not in existing]
        changed: list[str] = []
        unchanged_pks: list[int] = []
        for nid in native_ids:
            prev = existing.get(nid)
            if prev is None:
                continue
            if classify_index_sighting(
                prev, price_map.get(nid), self._price_change_min_pct,
            ) == "unchanged":
                unchanged_pks.append(prev["id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings_by_id(conn, unchanged_pks)

        entries = (
            [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=ceskereality new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        # A walk that reached the portal's end NOMINATES (rule #3); it no longer
        # delists, and since 2026-09-08 the verdict is STRUCTURAL: did every one
        # of the 14 kraje stop because ceskereality said there was no more? A
        # region one row short of its own declared count paged to its last page
        # and IS finished — the count is a live number, not a contract, and
        # demanding it kept a 20,964-row category from nominating anything.
        # What still fails the category is a stop of OURS anywhere in it: a
        # barren page, a fetch error, the page ceiling, --max-pages, --kraje, the
        # deadline, or a kraj the loop never reached.
        #
        # The nationwide total is deliberately NOT part of the verdict either.
        # It includes listings filed under no kraj at all -- foreign flats in the
        # domestic tree, Czech flats with no region set -- so the kraj union can
        # never reach it (rentals: 4,732 of 4,771, twelve walks in a row), and a
        # verdict that demanded it parked the category for good. Those listings
        # are nominated on every walk instead, and their page decides. The
        # national count is still fetched and logged (PARTITION / RECONCILE) as
        # the coverage signal it always was.
        stops_by_kraj: dict[str, list[StopReason]] = {}
        for r in results:
            stops_by_kraj.setdefault(r.kraj, []).append(r.stop)
        # Seeded from the DECLARED 14, never from the slices that ran: an empty
        # list makes all() vacuously true, so a category whose slices all failed
        # to start would otherwise report the portal's end.
        slice_stops = [
            s for kraj in KRAJ_SLUGS
            for s in (stops_by_kraj.get(kraj) or ["slice_unreached"])
        ]
        ends = {s: stop_is_portal_end(s) for s in set(slice_stops)}
        reached_end = walk_reached_end(
            portal_end=all(ends[s] for s in slice_stops),
            our_stop=(
                bool(self._max_pages) or bool(self._kraje) or deadline_hit
                or kraje_seen != set(KRAJ_SLUGS)
                or any(not ends[s] for s in slice_stops)
            ),
        )
        if reached_end and national is not None and declared_sum < 0.97 * national:
            # Not a gate (rule #3 nominates, the page decides), a coverage
            # alarm: the region-less tail is under 1%, so a kraj-sum this far
            # below national means a region came back short or empty.
            LOG.warning(
                "COVERAGE cm=%s ct=%s kraj-sum=%d national=%d: a region may be "
                "missing from this walk; its rows are nominated, not deleted",
                cat, sale_type, declared_sum, national,
            )
        result_size = national if national is not None else declared_sum
        return (
            seen, {"found_new": len(new_ids), "enqueued": enqueued}, result_size,
            pages, reached_end,
        )

    def probe_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool,
        limiter: RateLimiter, probe_pages: int,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        """Newest-first delta probe (portal_runner.run_index_probe). The generic
        walk-under-page-cap fallback is useless here: walk_category enumerates
        region×facet slices even under --max-pages AND the default order is not
        newest — so the probe reads the /nejnovejsi/ sort slug on the www host
        (through the same proxied client), page by page with an early stop on
        the first all-known page. Diff + enqueue only; always reached_end=False,
        so a probe can never nominate anything for a page check (rule #3)."""
        sale_type, cat = category["sale_type"], category["category"]
        client = CeskerealityClient(limiter=limiter)
        seen: set[str] = set()
        total: int | None = None
        pages = 0
        found_new = 0
        enqueued = 0
        for page in range(1, min(max(1, probe_pages), _PROBE_MAX_PAGES) + 1):
            url = search_url(
                sale_type, cat, subtype=_PROBE_SUB_SLUG,
                page=page if page > 1 else None,
            )
            try:
                html, _ = client.fetch_search(url)
            except ListingGoneError:
                break
            parsed = parse_index(html)
            pages += 1
            if parsed.total is not None:
                total = parsed.total
            if not parsed.items:
                break
            rows = [
                (it.source_id_native, detail_url(it.detail_path),
                 index_price(it.price_text))
                for it in parsed.items if it.source_id_native not in seen
            ]
            seen.update(nid for nid, _, _ in rows)
            existing = (
                db.index_summary_native(conn, SOURCE, [nid for nid, _, _ in rows])
                if conn is not None else {}
            )
            new_entries: list[tuple[str, str, int | None, int]] = []
            changed_entries: list[tuple[str, str, int | None, int]] = []
            unchanged_pks: list[int] = []
            for nid, ref, price in rows:
                prev = existing.get(nid)
                verdict = classify_index_sighting(
                    prev, price, self._price_change_min_pct,
                )
                if verdict == "new":
                    new_entries.append((nid, ref, price, db.QUEUE_PRIORITY_NEW))
                elif verdict == "changed":
                    changed_entries.append(
                        (nid, ref, price, db.QUEUE_PRIORITY_CHANGED))
                else:
                    unchanged_pks.append(prev["id"])
            if conn is not None and unchanged_pks:
                db.touch_listings_by_id(conn, unchanged_pks)
            entries = changed_entries + new_entries
            if conn is not None and entries:
                enqueued += db.enqueue_detail(conn, SOURCE, entries)
            found_new += len(new_entries)
            LOG.info(
                "PROBE page cm=%s ct=%s page=%d new=%d changed=%d unchanged=%d",
                cat, sale_type, page, len(new_entries), len(changed_entries),
                len(unchanged_pks),
            )
            if not new_entries or parsed.next_offset is None:
                break
        return seen, {"found_new": found_new, "enqueued": enqueued}, total, pages, False

    def note_empty_slice(self, category: dict[str, Any]) -> None:
        """A slice the runner refused to nominate from (it saw nothing) still counts
        as one of the group's slices — otherwise a nationwide-empty sibling slug
        would hold its whole (category_main, category_type) group below `expected`
        for ever and the group would never nominate again. It contributes no ids."""
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return
        key = (cm, ct)
        self._sweep_seen.setdefault(key, set())
        self._sweep_done[key] = self._sweep_done.get(key, 0) + 1

    def presence_candidates(
        self, conn: Any, category: dict[str, Any], seen: set[str],
    ) -> tuple[list[tuple[str, str | None, int | None]], int] | None:
        """Nominate this category's unseen rows for a page check (rule #3,
        2026-09-07) -- once per (category_main, category_type), with the UNION
        of its slices' seen ids.

        Several index slices collapse onto one canonical category ('rodinne-domy'
        and 'chaty-chalupy' both -> dum), and the runner calls this per slice, so
        nominating from ONE slice's seen set would nominate the sibling slice's
        whole population every walk. Buffer each complete slice's ids and
        nominate on the group's LAST complete slice. An incomplete or failed
        sibling never reaches this call, so its group stays below the expected
        slice count and nothing is nominated this walk (the next walk retries).

        Region-less listings (foreign, or a Czech flat filed under no kraj) are
        in no slice, so they are nominated every walk; the page check then keeps
        the live ones and closes the dead ones -- the only way to know.
        """
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        key = (cm, ct)
        group = self._sweep_seen.setdefault(key, set())
        group.update(seen)
        self._sweep_done[key] = self._sweep_done.get(key, 0) + 1
        expected = sum(1 for c in self._categories if self.category_labels(c) == key)
        if self._sweep_done[key] < expected:
            return None
        return db.presence_candidates(conn, SOURCE, cm, ct, group)

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> CeskerealityClient:
        return CeskerealityClient(limiter=limiter)

    def fetch_detail(
        self, client: CeskerealityClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        cm, ct = category_from_url(url)
        try:
            listing = parse_detail(
                html, source_url=url, category_main=cm, category_type=ct,
            )
        except Exception as exc:  # noqa: BLE001
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        return DrainItem(
            native_id=native_id, kind="ok",
            payload={"listing": listing, "html": html, "status": status, "url": url},
        )

    def write_details(self, conn: Any, items: list[DrainItem]) -> dict[str, int]:
        counts = {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}
        for it in items:
            p = it.payload
            page_id = db.upsert_portal_raw_page(
                conn, source=SOURCE, source_id_native=it.native_id,
                source_url=p["url"], page_kind="detail",
                html=p["html"], http_status=p["status"],
                # W2a-0 churn instrument: this whole write_details is replayed on
                # a transient pooler drop, so the counter bump inside needs the
                # item's per-fetch token to make the replay a no-op.
            )
            pk, result = db.ingest_scraped_listing(
                conn, p["listing"], discovery_seq=it.discovery_seq,
                discovered_at=it.discovered_at)
            image_urls = p["listing"].raw.get("image_urls") or []
            inserted = db.record_media(conn, pk, image_urls)
            db.mark_portal_page_parsed(conn, page_id)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        # Keyed on the native id directly (not a sreality_id round-trip): post-Gate-2
        # the row's sreality_id is NULL, so the legacy mark_listing_inactive no-ops.
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'ceskereality' AND claimed_at IS NULL AND given_up = false"
            )
            return int(cur.fetchone()[0])


def _load_config(dry_run: bool) -> PortalConfig:
    if dry_run:
        return default_config(SOURCE)
    try:
        with db.connect() as conn:
            return load_portal_config(conn, SOURCE)
    except Exception as exc:
        LOG.warning("load_portal_config failed: %s; using baked-in default", exc)
        return default_config(SOURCE)




def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    kraje = tuple(args.kraj) if args.kraj else None
    portal = CeskerealityPortal(config, max_pages=args.max_pages, kraje=kraje)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): the /nejnovejsi/ sort slug on the www
    # host, diff + enqueue only. No mark_inactive, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

    # ceskereality is mid-sized (~26k listings), so a combined run (omit both
    # --index-only / --drain-only) does the full index walk + a bounded drain in
    # one job. The split flags exist for parity / tuning if it ever outgrows that.
    rc = 0
    if not args.drain_only:
        rc = portal_runner.run_phase(
            portal, "index", portal_runner.run_index_walk, args.dry_run,
            max_seconds=args.max_seconds,
        )
    if rc == 0 and not args.index_only:
        rc = portal_runner.run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ceskereality.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; nominates "
             "nothing). Omit for a full walk to the portal's own end.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = per-portal config / drain the queue)",
    )
    p.add_argument(
        "--workers", type=int, default=None,
        help="detail-fetch workers (default: per-portal config)",
    )
    p.add_argument(
        "--rate", type=float, default=None,
        help="detail-fetch requests/second ceiling (default: per-portal config)",
    )
    p.add_argument(
        "--max-seconds", type=float, default=None,
        help="wall-clock budget for EITHER phase (the index walk consumes it as "
             "its per-category deadline); the phase stops claiming + finalizes "
             "cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue + nominate unseen rows for a page check "
             "only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
    )
    p.add_argument(
        "--kraj", action="append", default=None, choices=list(KRAJ_SLUGS),
        metavar="SLUG",
        help="limit the index walk to this kraj (repeatable; e.g. "
             "stredocesky-kraj) for an ad-hoc partial run. Nominates "
             "nothing. Omit = all 14 kraje. An unknown slug is an "
             "argparse error, never a silently-404ing walk.",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages page(s) of the www /nejnovejsi/ sort per category, "
             "then exit — never mark_inactive, no detail drain, no scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="index pages per category for --probe (default 1)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
