"""Orchestrator for the reality.idnes.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.idnes_main`. iDNES is a `Portal` (IdnesPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that pages the
HTML search results and enqueues new/price-changed ids into the shared
`listing_detail_queue` (source='idnes', migration 108), then a detail-drain that
fetches each listing page, parses it to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (IdnesClient) + parser (idnes_parser) +
config differ from sreality/bezrealitky (the modularity rule in CLAUDE.md).

Unlike bazos (a partial-walk classifieds crawler), idnes's search pages carry a
result total and have no deep-pagination cap, so a per-category walk is
provable-complete: `supports_complete_walk` (config-driven) lets the runner mark
delisted listings inactive under the completeness guard (architectural rule #3),
source-scoped so it only ever touches idnes rows (rule #15). The detail URL
carries the category (`/detail/{sale}/{cat}/…`), so the drain derives each
listing's category from its own URL — one config walks many categories without
the queue-encodes-category limitation that constrains bazos. Coordinates come
straight from the page's embedded map config when present; when the page omits
it (~a third of listings) the drain carries an already-stored coordinate
forward, and only a never-placed listing falls back to geocoding the locality
via Mapy.cz (the shared `scraper.location.CoordResolver`) so those listings
still appear on the map and in radius/location filters instead of being
silently dropped.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

from scraper import db, portal_runner
from scraper.idnes_client import ABROAD_SL, IdnesClient, detail_url
from scraper.idnes_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    category_from_url,
    index_price,
    parse_detail,
    parse_index,
    sub_places,
)
from scraper.portal import (
    ABROAD_SLICE,
    CZ_KRAJ_SLUGS,
    PortalConfig,
    default_config,
    load_portal_config,
    classify_index_sighting,
    deadline_reached,
    walk_coverage,
    walk_is_complete,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.location import CoordResolver
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "idnes"

# Only flip rows unseen for 12h+ — ~2 full walk cadences at the 6h schedule.
# last_seen_at is bumped for unchanged rows each walk (touch_listings) and for
# changed rows on a successful drain fetch — so a churn-missed live row is
# protected unless its detail fetches have ALSO failed for 12h+; even then the
# flip self-heals on the next index sighting (touch_listings reactivates).
# Tightened 24->12h for the real-time delisting SLO.
INACTIVE_MIN_UNSEEN_HOURS = 12

# A loop guard, not a budget: idnes clamps an out-of-range ?page to the last page
# and serves it again, so a pager bug could otherwise page forever against a live
# URL. It was 400 and that was too low — the abroad bucket is 12,054 rows over
# 482 pages, so the guard fired mid-walk and, because a `ceiling` did not descend,
# the slice ended at 10,400 with no attempt to split it. 600 clears the largest
# real place with room to spare; anything beyond that is a bug, not a big region.
_MAX_SLICE_PAGES = 600

# How many levels a slice may descend when paging cannot reach its own tail.
# Two is enough for every place the site actually publishes: kraj -> okres on the
# Czech side, abroad -> country on the other. Deeper only mattered for the price
# ladder, which is gone.
_DESCENT_DEPTH = 2


@dataclass(frozen=True)
class SliceWalk:
    """One slice's outcome. Only `exhausted` — walked to the slice's OWN declared
    tail — is positive; `deadline`, `error`, `degraded`, `ceiling` and
    `incomplete` all mean missing evidence, and any of them forces the whole
    category incomplete (rule #3)."""

    slice_key: str
    rows: list[tuple[str, str, int | None]]   # (native_id, detail_ref, index_price)
    declared_total: int | None
    pages: int
    outcome: str


class IdnesPortal:
    """iDNES Reality as a Portal: the seams the generic runner needs, wrapping the
    idnes client + parser. Operational scope (categories, complete-walk
    capability) comes from the `portals` registry config."""

    source = SOURCE
    # idnes is a large portal walked page-by-page (≈26 listings/page, tens of
    # thousands per category), so the index needs a faster ceiling than the
    # classifieds pilots. The detail-fetch rate is the (slower) drain CLI arg.
    # The class value is the baked floor; the instance reads it from config.
    index_rate = 3.0

    def __init__(
        self,
        config: PortalConfig,
        *,
        max_pages: int | None = None,
        price_change_min_pct: float | None = None,
    ) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        # CLI override > per-portal config (the standard limits chain). Absorbs
        # the daily FX re-display drift of idnes's foreign inventory so the
        # walk doesn't enqueue phantom "price changed" refetches (see
        # PortalLimits.price_change_min_pct).
        self._price_change_min_pct = (
            price_change_min_pct if price_change_min_pct is not None
            else config.limits.price_change_min_pct
        )
        # page > carry-forward > geocode; preloaded once in connect_drain (the
        # 2026-06 Mapy-credit incident guard — see scraper.location).
        self._coords = CoordResolver(SOURCE)
        # Read once in connect_index, before the runner asks for categories.
        # Empty = every slice sorts as never-walked, which is the safe default:
        # it walks everything rather than skipping on stale bookkeeping.
        self._staleness: dict[tuple[str, str, str], float] = {}

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): idnes's default index
        # order is newest-first, so a page-capped walk IS the delta probe.
        self._max_pages = pages

    def categories(self) -> list[dict[str, Any]]:
        """Stalest category first.

        Slice ordering alone is not enough. The runner walks categories in
        order, so if the first one exhausts the budget every run, the later ones
        starve no matter how their own slices are sorted — which is exactly what
        happened: 8 of 10 idnes categories were never walked at all. Ranking a
        category by its STALEST slice means the one that just consumed the
        budget goes last next time, and the rotation covers the portal.
        """
        cats = list(self._categories)
        if not self._staleness:
            return cats

        def worst(category: dict[str, Any]) -> float:
            cm, ct = self.category_labels(category)
            if cm is None or ct is None:
                return float("inf")
            # A slice with no row is "never walked" = infinitely stale, so a
            # category holding one always outranks a fully-walked category.
            return max(
                self._staleness.get((cm, ct, key), float("inf"))
                for key in self.SLICES
            )

        return sorted(cats, key=worst, reverse=True)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            CATEGORY_MAIN.get(category.get("category")),
            SALE_TYPE.get(category.get("sale_type")),
        )

    def connect_index(self) -> Any:
        conn = db.connect()
        # The runner calls this BEFORE categories(), which is what lets both the
        # category order and the slice order come from one ledger read.
        self._staleness = db.slice_staleness(conn, SOURCE)
        return conn

    def connect_drain(self) -> Any:
        # Single-row ingest (ingest_scraped_listing), not batched prepared writes,
        # so the transaction pooler is fine — no session pooler needed.
        conn = db.connect()
        # Preload (once, on the main thread) the stored coords so the worker-pool
        # fetch_detail carries them forward instead of re-geocoding (the 2026-06
        # Mapy-credit incident guard — rationale in scraper.location).
        self._coords.preload(conn)
        return conn

    # --- the sliced walk ---
    #
    # Why this is not a single loop over ?page=N any more: idnes has no
    # pagination cap (page 1,052 of prodej/byty serves the declared tail and
    # 1,060 404s), so the catalogue is fully REACHABLE. It was not being reached
    # because a walk that runs out of budget restarts at the first category's
    # first page next time, so the same head got re-walked while 8 of 10
    # categories were never touched at all. Cutting each category into the 14
    # kraje plus the abroad bucket gives units that (a) finish inside any
    # plausible budget, (b) each declare their own total, so "did we reach the
    # end" is answerable 15 times instead of once, and (c) can be REMEMBERED
    # between runs (portal_index_slices, migration 454) so coverage accumulates.
    SLICES: tuple[str, ...] = CZ_KRAJ_SLUGS + (ABROAD_SLICE,)

    def _place(self, slice_key: str) -> tuple[str | None, str | None]:
        """(locality, sl) for a top-level slice key."""
        return (None, ABROAD_SL) if slice_key == ABROAD_SLICE else (slice_key, None)

    def _walk_place(
        self, client: IdnesClient, sale_type: str, cat: str, *,
        locality: str | None, sl: str | None, deadline: float | None,
        label: str,
    ) -> tuple[list[tuple[str, str, int | None]], int | None, int, str, str | None]:
        """Page one place to its own tail.

        Returns (rows, declared_total, pages, outcome, first_page_html). The HTML
        is kept because it advertises the places one level down, which is the
        descent path when this one cannot be enumerated.
        """
        rows: list[tuple[str, str, int | None]] = []
        ref: set[str] = set()
        declared: int | None = None
        first_html: str | None = None
        pages = 0
        page: int | None = None
        while True:
            if deadline_reached(deadline):
                LOG.info("SLICE deadline cm=%s ct=%s place=%s pages=%d collected=%d",
                         cat, sale_type, label, pages, len(rows))
                return rows, declared, pages, "deadline", first_html
            try:
                html, _ = client.fetch_index(
                    sale_type, cat, page, locality=locality, sl=sl)
            except Exception as exc:  # noqa: BLE001 - one place must not kill the walk
                LOG.warning("SLICE error cm=%s ct=%s place=%s page=%s: %s",
                            cat, sale_type, label, page, exc)
                return rows, declared, pages, "error", first_html
            parsed = parse_index(html)
            pages += 1
            if first_html is None:
                first_html = html
            if declared is None:
                declared = parsed.total
                if declared is None and parsed.empty_confirmed:
                    # An empty place publishes no count — identical to a degraded
                    # page — so it only counts as finished when idnes says so.
                    return rows, 0, pages, "exhausted", first_html
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in ref:
                    ref.add(nid)
                    rows.append((nid, detail_url(item.detail_path),
                                 db.sane_price_czk(index_price(item.price_text))))
            if pages >= _MAX_SLICE_PAGES:
                LOG.warning("SLICE ceiling cm=%s ct=%s place=%s at %d pages",
                            cat, sale_type, label, pages)
                return rows, declared, pages, "ceiling", first_html
            # STOP WHEN THE PAGER STOPS ADVANCING — progress in the CURSOR, not
            # novelty in the CONTENT.
            #
            # An early version stopped on a page that added no new rows, to guard
            # against idnes clamping an out-of-range ?page to the last page. That
            # is wrong: idnes's ordering is unstable between requests, so pages
            # overlap and a legitimately mid-walk page can be entirely rows we
            # already hold — it ended Prague at 594 of 3,839.
            #
            # But dropping it left nothing to catch a URL that does not paginate
            # AT ALL, and one exists: a price-filtered search ignores ?page= and
            # reports next=1 forever. The walk then re-fetched the same 26 rows
            # until the page cap — 1,492 pages on one slice. Requiring the pager
            # to move FORWARD separates the two cleanly: repeats are fine,
            # standing still is not.
            current = page or 0
            if not parsed.items or parsed.next_offset is None:
                break
            if parsed.next_offset <= current:
                LOG.warning(
                    "SLICE cm=%s ct=%s place=%s pager did not advance at page=%s "
                    "(next=%s) — this URL does not paginate; stopping",
                    cat, sale_type, label, page, parsed.next_offset,
                )
                break
            page = parsed.next_offset
        verdict = walk_coverage(len(rows), declared, stopped_early=False)
        outcome = "exhausted" if verdict == "complete" else (
            "degraded" if verdict == "unknown" else "incomplete")
        return rows, declared, pages, outcome, first_html

    def _walk_tree(
        self, client: IdnesClient, sale_type: str, cat: str, *,
        locality: str | None, sl: str | None, label: str,
        deadline: float | None, depth: int,
        visited: set[tuple[str | None, str | None]],
    ) -> tuple[list[tuple[str, str, int | None]], int | None, int, str]:
        """Walk a place, descending if paging alone cannot reach its tail.

        Two descent axes, tried in that order. PLACE first, because it is the
        site's own hierarchy and (on the Czech side) very nearly a partition:
        a kraj links its okresy, Prague links its ten obvody, the abroad bucket
        links one `s-l` value per country — and those 38 countries sum EXACTLY to
        the abroad total. PRICE second, as the fallback for a place that has no
        sub-places at all: Spain is 8,613 flats over 345 pages and advertises no
        regions, so without it that slice could never finish, and one unfinished
        slice holds its whole category open forever.

        The parent's own rows are always kept. That is not an optimisation, it is
        what makes either axis work: neither axis is a true partition, and the
        unfiltered walk is what holds the remainder each one drops — the 60
        Prague listings too vaguely addressed for any obvod, the 6 Spanish ones
        with no price at all.

        Returns (rows, declared, pages, outcome).
        """
        visited.add((locality, sl))
        rows, declared, pages, outcome, html = self._walk_place(
            client, sale_type, cat, locality=locality, sl=sl,
            deadline=deadline, label=label)
        # Descend on a COVERAGE shortfall, not on a fetch problem.
        #   incomplete — paged to the pager's own end and came up short
        #   ceiling    — too big to page through at all, which is the same
        #                shortfall stated more emphatically
        # Both have a declared total to measure a union against, and both are
        # fixed by asking a narrower question. An `error` is a transport failure
        # and a `degraded` page carries no total at all; descending on either
        # would multiply failed requests and relabel a fetch problem as a
        # coverage one.
        if outcome not in ("incomplete", "ceiling") or depth <= 0:
            return rows, declared, pages, outcome

        # PLACE is the only descent axis. A price-band axis was tried and removed:
        # a price-filtered idnes search IGNORES ?page= — it serves page one
        # forever and reports next=1 — so a band could never yield more than 26
        # rows however long the walk ran. Its motivating case never materialised
        # either: Spain is the one place with no sub-places, and the abroad slice
        # that contains it completes at the parent level (12,054 of 12,054). A
        # place with no children now stays honestly incomplete instead.
        children: list[tuple[str | None, str | None, str]] = []
        if html is not None:
            paths, sls = sub_places(html, sale_type, cat, exclude=set(self.SLICES))
            children = (
                [(c, None, c) for c in paths if (c, None) not in visited]
                + [(None, c, c) for c in sls if (None, c) not in visited]
            )
        if not children:
            return rows, declared, pages, outcome

        LOG.info("SLICE descend cm=%s ct=%s place=%s collected=%d declared=%s "
                 "-> %d children (depth %d)",
                 cat, sale_type, label, len(rows), declared, len(children), depth)
        merged = {r[0]: r for r in rows}
        for c_loc, c_sl, c_label in children:
            if deadline_reached(deadline):
                return list(merged.values()), declared, pages, "deadline"
            c_rows, _cd, c_pages, _co = self._walk_tree(
                client, sale_type, cat, locality=c_loc, sl=c_sl,
                label=f"{label}/{c_label}", deadline=deadline,
                depth=depth - 1, visited=visited)
            pages += c_pages
            for r in c_rows:
                merged.setdefault(r[0], r)
        rows = list(merged.values())
        verdict = walk_coverage(len(rows), declared, stopped_early=False)
        outcome = "exhausted" if verdict == "complete" else (
            "degraded" if verdict == "unknown" else "incomplete")
        return rows, declared, pages, outcome

    def _walk_slice(
        self, client: IdnesClient, sale_type: str, cat: str, slice_key: str,
        *, deadline: float | None,
    ) -> SliceWalk:
        """Walk one slice, descending where paging alone cannot reach its tail.

        WHY A DESCENT IS NEEDED AT ALL. idnes's result ordering is not stable
        between requests, so successive pages of one query overlap, and the loss
        compounds with page count. Measured live: stredocesky-kraj (67 pages)
        returned its declared 1,675 EXACTLY, while praha (154 pages) returned
        2,948 of 3,839 — 27% of page slots were rows already seen. Paging harder
        does not help; the pager genuinely ends there.

        WHY THE PARENT WALK IS KEPT rather than replaced by its children. Neither
        alone is enough, and this is the measurement that decided the design:

            parent praha alone      2,948 / 3,840   76.8%   FAILS
            its ten obvody alone    3,777 / 3,840   98.4%   FAILS
            the UNION of both       3,830 / 3,840   99.74%  PASSES

        The children are individually near-exact but cannot hold a listing whose
        address is too vague to file under any obvod (60 such in Prague); the
        parent walk is the catch-all that does. It is the same shape one level
        up, where the 14 kraje miss the 44% that only ?s-l=STAT-XX holds.

        The descent recurses (`_DESCENT_DEPTH`) because one level is not always
        enough — the abroad bucket is three times Prague's size, so it splits by
        country and a large country may need splitting again. `visited` stops a
        page that links back to its parent from walking it twice.

        The child list is SCRAPED, not declared, which on ceskereality would be a
        mistake (its facet block is a top-10-by-popularity list, not a partition).
        It is safe here only because the arithmetic checks it: a missing child
        leaves the union short and the slice stays incomplete, and a spurious
        child can only add rows of the same category, which cannot push the union
        past the declared total. The link list never has to be trusted.
        """
        locality, sl = self._place(slice_key)
        rows, declared, pages, outcome = self._walk_tree(
            client, sale_type, cat, locality=locality, sl=sl, label=slice_key,
            deadline=deadline, depth=_DESCENT_DEPTH, visited=set())
        LOG.info("SLICE cm=%s ct=%s slice=%s declared=%s collected=%d pages=%d outcome=%s",
                 cat, sale_type, slice_key, declared, len(rows), pages, outcome)
        return SliceWalk(slice_key, rows, declared, pages, outcome)

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        cm, ct = self.category_labels(category)
        client = IdnesClient(limiter=limiter)

        # The realtime probe caps pages to read the newest-first head of the
        # NATIONAL list; slicing would scatter that head across 15 requests and
        # defeat the probe's whole purpose. Page-capped runs keep the flat walk
        # (and, being partial, never drive mark_inactive — rule #3).
        if self._max_pages:
            return self._walk_flat(client, sale_type, cat, conn, deadline)

        # The portal's own claim about the whole category, fetched once. It is
        # the denominator the slice union has to satisfy, and it is what catches
        # a slice vocabulary that has silently stopped covering the category.
        national: int | None = None
        try:
            html, _ = client.fetch_index(sale_type, cat, None)
            national = parse_index(html).total
        except Exception as exc:  # noqa: BLE001
            LOG.warning("NATIONAL probe failed cm=%s ct=%s: %s", cat, sale_type, exc)
        pages = 1 if national is not None else 0

        order = self._slice_order(cm, ct)
        collected: dict[str, tuple[str, int | None]] = {}
        results: list[SliceWalk] = []
        for slice_key in order:
            if deadline_reached(deadline):
                LOG.info("CATEGORY cm=%s ct=%s stopped at the budget with %d/%d "
                         "slices walked; the rest keep their staleness and go "
                         "first next run", cat, sale_type, len(results), len(order))
                break
            result = self._walk_slice(
                client, sale_type, cat, slice_key, deadline=deadline)
            results.append(result)
            pages += result.pages
            for nid, ref, price in result.rows:
                collected[nid] = (ref, price)
            if conn is not None and cm and ct:
                db.record_index_slice(
                    conn, source=SOURCE, category_main=cm, category_type=ct,
                    slice_key=slice_key, outcome=result.outcome,
                    declared_total=result.declared_total,
                    collected=len(result.rows), pages=result.pages,
                )

        seen = set(collected)
        # EVERY slice must have been walked AND finished. Anything less is a
        # walk with a hole in it, and a hole is exactly what mark_inactive would
        # read as "these listings are gone".
        all_walked = len(results) == len(order)
        all_positive = all(r.outcome == db.SLICE_OUTCOME_POSITIVE for r in results)
        complete = bool(
            all_walked and all_positive
            and walk_is_complete(len(seen), national, stopped_early=False)
        )
        LOG.info(
            "CATEGORY cm=%s ct=%s slices=%d/%d positive=%s national=%s collected=%d "
            "pages=%d complete=%s",
            cat, sale_type, len(results), len(order), all_positive, national,
            len(seen), pages, complete,
        )
        counts = self._reconcile(conn, collected)
        return seen, counts, national, pages, complete

    def _slice_order(self, cm: str | None, ct: str | None) -> list[str]:
        """Least-recently-walked first, never-walked before everything.

        A slice with no ledger row sorts to infinity, not to zero: treating an
        unknown slice as fresh would sort exactly the never-walked ones LAST,
        which is the starvation the ledger exists to end.
        """
        stale = self._staleness
        if not stale or cm is None or ct is None:
            return list(self.SLICES)
        return sorted(
            self.SLICES,
            key=lambda k: -stale.get((cm, ct, k), float("inf")),
        )

    def _reconcile(
        self, conn: Any, collected: dict[str, tuple[str, int | None]],
    ) -> dict[str, int]:
        """Touch what is unchanged, enqueue what is new or repriced."""
        native_ids = list(collected)
        existing = (
            db.index_summary_native(conn, SOURCE, native_ids)
            if conn is not None and native_ids else {}
        )
        new_ids = [n for n in native_ids if n not in existing]
        changed: list[str] = []
        unchanged_pks: list[int] = []
        for nid in native_ids:
            prev = existing.get(nid)
            if prev is None:
                continue
            if classify_index_sighting(
                prev, collected[nid][1], self._price_change_min_pct,
            ) == "unchanged":
                unchanged_pks.append(prev["id"])
            else:
                changed.append(nid)
        if conn is not None and unchanged_pks:
            db.touch_listings_by_id(conn, unchanged_pks)
        entries = (
            [(n, collected[n][0], collected[n][1], db.QUEUE_PRIORITY_CHANGED)
             for n in changed]
            + [(n, collected[n][0], collected[n][1], db.QUEUE_PRIORITY_NEW)
               for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=idnes new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        return {"found_new": len(new_ids), "enqueued": enqueued}

    def _walk_flat(
        self, client: IdnesClient, sale_type: str, cat: str, conn: Any,
        deadline: float | None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        """The page-capped probe path: the newest-first head of the national
        list. Always incomplete by construction — it is a delta probe, not a
        walk — so it can never drive mark_inactive."""
        collected: dict[str, tuple[str, int | None]] = {}
        total: int | None = None
        pages = 0
        page: int | None = None
        while True:
            if deadline_reached(deadline):
                break
            html, _ = client.fetch_index(sale_type, cat, page)
            parsed = parse_index(html)
            pages += 1
            total = parsed.total if parsed.total is not None else total
            new_on_page = 0
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in collected:
                    new_on_page += 1
                collected[nid] = (detail_url(item.detail_path),
                                  db.sane_price_czk(index_price(item.price_text)))
            LOG.info("INDEX page=%s items=%d total=%s", page, len(parsed.items), total)
            if pages >= self._max_pages:
                break
            if not parsed.items or parsed.next_offset is None or new_on_page == 0:
                break
            page = parsed.next_offset
        counts = self._reconcile(conn, collected)
        return set(collected), counts, total, pages, False

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return 0
        # Sweep on the native id the index actually walked, not on a PK set
        # resolved back out of the DB: under listing-identity Gate 2 a
        # non-sreality row carries sreality_id = NULL, and one NULL inside
        # `<> ALL(...)` makes the whole predicate NULL — the sweep would become
        # a permanent no-op for the entire portal (rule #3).
        return db.mark_inactive_native(
            conn, SOURCE, cm, ct, seen,
            min_unseen_hours=INACTIVE_MIN_UNSEEN_HOURS,
        )

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> IdnesClient:
        return IdnesClient(limiter=limiter)

    def fetch_detail(
        self, client: IdnesClient, native_id: str, detail_ref: str | None,
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
        listing = self._coords.fill(native_id, listing)
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
                churn_observation=it.observation_id,
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
        # Complete-walk portal: a gone detail flips that one listing inactive
        # immediately (mirrors sreality), then the runner dequeues it. Keyed on the
        # native id directly (not a sreality_id round-trip): post-Gate-2 the row's
        # sreality_id is NULL, so the legacy mark_listing_inactive would no-op.
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'idnes' AND claimed_at IS NULL AND given_up = false"
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


def _finalize(run_id: int | None, agg: dict[str, Any], *, drain: bool = False) -> None:
    if run_id is None or (not agg and not drain):
        return
    try:
        with db.connect() as conn:
            db.scrape_run_finalize(
                conn, run_id,
                index_pages=agg.get("index_pages", 0),
                listings_found_new=agg.get("listings_found_new", 0),
                listings_scraped_new=agg.get("listings_scraped_new", 0),
                listings_updated=agg.get("listings_updated", 0),
                listings_inactive=agg.get("listings_inactive", 0),
                images_discovered=agg.get("images_discovered", 0),
                images_stored=0,  # crawl records image-URL rows only; bytes uploaded async by images.yml
                errors=agg.get("errors", 0),
                by_category=agg.get("by_category", []),
                bump_already_applied=drain,
            )
    except Exception as exc:
        LOG.warning("scrape_run_finalize failed: %s", exc)


def _run_phase(
    portal: IdnesPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
) -> int:
    run_id: int | None = None
    if not dry_run:
        try:
            with db.connect() as conn:
                run_id = db.scrape_run_start(conn, run_type, source=SOURCE)
        except Exception as exc:
            LOG.warning("scrape_run_start failed: %s", exc)
    agg: dict[str, Any] = {}
    rc = 0
    try:
        kw = {**kw, "run_id": run_id}
        rc, agg = runner(portal, dry_run=dry_run, **kw)
    finally:
        if not dry_run:
            _finalize(run_id, agg, drain=runner is portal_runner.run_detail_drain)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    portal = IdnesPortal(
        config,
        max_pages=args.max_pages,
        price_change_min_pct=args.price_change_min_pct,
    )

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No mark_inactive, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

    # Cadence split, like sreality (rule #19): --index-only walks + enqueues
    # (and marks inactive under the completeness guard); --drain-only fetches +
    # ingests a bounded slice of the queue. idnes is large (~2400 index pages,
    # tens of thousands of details), so a combined run can't do both inside one
    # job — the full index eats the window. Omitting both flags runs both phases
    # (the dispatch-only combined fallback).
    rc = 0
    if not args.drain_only:
        rc = _run_phase(
            portal, "index", portal_runner.run_index_walk, args.dry_run,
            max_seconds=args.max_seconds,
        )
    if rc == 0 and not args.index_only:
        rc = _run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="reality.idnes.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; suppresses "
             "mark_inactive). Omit for a full, complete walk.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = drain the queue)",
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
        "--price-change-min-pct", type=float, default=None,
        help="relative index-price move below which a listing reads as "
             "unchanged in the walk diff (default: per-portal config; "
             "0 = exact compare)",
    )
    p.add_argument(
        "--max-seconds", type=float, default=None,
        help="wall-clock budget for the detail drain; it stops claiming + "
             "finalizes cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue + mark_inactive only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages index page(s) per category, then exit — never "
             "mark_inactive, no detail drain, no scrape_runs row",
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
