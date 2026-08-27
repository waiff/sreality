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
walk partitioned on the 14 declared kraje is provable-complete:
`supports_complete_walk` (config-driven) lets the runner mark delisted listings
inactive under the completeness guard (architectural rule #3), source-scoped so it
only ever touches ceskereality rows (rule #15). The detail URL carries the
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
from scraper.location import CoordResolver
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
    default_config,
    deadline_reached,
    load_portal_config,
    classify_index_sighting,
    walk_is_complete,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "ceskereality"

# The completeness verdict — min ratio, over-collection ceiling, early-stop — is
# scraper.portal.walk_is_complete, one definition for all nine portals (rule #3).
# It replaced a local copy that returned True when the total was unmeasurable;
# the INACTIVE_MIN_UNSEEN_HOURS staleness rail below is the second, stronger guard.

# Only flip rows unseen for 12h+ — ~2 full walk cadences at the 6h schedule.
# last_seen_at is bumped for unchanged rows each walk (touch_listings) and for
# changed rows on a successful drain fetch — so a churn-missed live row is
# protected unless its detail fetches have ALSO failed for 12h+; even then the
# flip self-heals on the next index sighting (touch_listings reactivates). Passed
# EXPLICITLY on every sweep: db.mark_inactive_native applies NO rail by default.
# Tightened 24->12h for the real-time delisting SLO.
INACTIVE_MIN_UNSEEN_HOURS = 12

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
    """One (kraj[, subtype]) slice's outcome. `exhausted` — walked to the slice's
    own declared tail — is the ONLY positive one; every other outcome is missing
    evidence and forces the category incomplete (rule #3)."""

    kraj: str
    subtype: str | None
    rows: list[tuple[str, str, int | None]]
    declared_total: int | None
    pages: int
    outcome: SliceOutcome

    @property
    def positive(self) -> bool:
        return self.outcome == "exhausted"


class CeskerealityPortal:
    """ceskereality.cz as a Portal: the seams the generic runner needs, wrapping
    the ceskereality client + parser. Operational scope (categories, complete-walk
    capability) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 0.7

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
        # When set, the walk is partial so mark_inactive is suppressed.
        self._kraje = kraje
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct
        # per-(cm, ct) union of complete slices' seen ids + completed-slice
        # counts — the cross-slice delisting sweep buffer (see mark_inactive).
        self._sweep_seen: dict[tuple[str, str], set[str]] = {}
        self._sweep_done: dict[tuple[str, str], int] = {}
        # page > carry-forward > geocode. Replaces the parser's never-wired
        # geocoder plumbing: resolution now happens uniformly AFTER parse, same
        # as every other portal (scraper.location).
        self._coords = CoordResolver(SOURCE)

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
        self._coords.preload(conn)
        return conn

    def _archive_index_page(
        self, conn: Any, key: str, url: str, html: str, status: int,
        fresh_keys: set[str] | None,
    ) -> None:
        """W0 item 0n: search pages carry index-only signals (map markers)."""
        db.record_payload_churn_if_enabled(
            conn,
            source=SOURCE,
            source_id_native=key,
            page_kind="index",
            body=lambda: html.encode("utf-8"),
            content_type="text/html",
        )
        # KNOWN GAP (W2a-2): this skip guards upsert_portal_raw_page, so it
        # also suppresses the payload dual-write for an index page that
        # changed inside the freshness window. Deliberately unchanged here —
        # W2a-6's index-coverage audit measures the gap before P2 reworks it.
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
                    record_churn=False,
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
        re-read returns False (degraded), never True — a confirmation that cannot
        be obtained is not a confirmation.
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
        """Walk ONE (kraj[, subtype]) slice to its declared tail.

        `exhausted` is the only positive outcome — everything else forces the
        category incomplete (rule #3). The load-bearing case is a 200 carrying
        ZERO cards: that is the site's real degraded response (the 404 is not —
        `Retry-After: 3` accompanies every 404 here, nonexistent paths included),
        so it is only ever read as a finished slice when the page also proves it
        is the empty slice we asked for (`heading_names_kraj`).
        """
        rows: list[tuple[str, str, int | None]] = []
        declared_total: int | None = None
        last_page: int | None = None
        live_last: int | None = None
        page = 1
        page_cap = self._max_pages or _MAX_SLICE_PAGES

        def out(outcome: SliceOutcome, pages: int) -> SliceResult:
            return SliceResult(kraj, subtype, rows, declared_total, max(pages, 0), outcome)

        while True:
            if page > page_cap:
                return out("ceiling", page - 1)
            # Budget spent: stop BEFORE issuing another request and report the
            # slice as a deadline stop — the rows already collected still count.
            if deadline_reached(deadline):
                LOG.info(
                    "DEADLINE index walk stopped cm=%s ct=%s kraj=%s subtype=%s "
                    "after page=%d collected=%d",
                    cat, sale_type, kraj, subtype or "all", page - 1, len(rows),
                )
                return out("deadline", page - 1)
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
                return out("error", page - 1)
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
                    # has to carry the proof; anything else is degraded.
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
                        # long. It is not the only rail — the category's national
                        # cross-check now fails closed too — but it is the one
                        # that stops a bad zero entering the arithmetic at all.
                        if not self._confirm_slice_is_empty(client, url, kraj):
                            LOG.warning(
                                "SLICE cm=%s ct=%s kraj=%s subtype=%s looked empty "
                                "but did not confirm on re-read; treating as degraded",
                                cat, sale_type, kraj, subtype or "all",
                            )
                            return out("degraded", 1)
                        declared_total = 0
                        LOG.info(
                            "SLICE cm=%s ct=%s kraj=%s subtype=%s declared=0 "
                            "collected=0 pages=1 outcome=exhausted (empty-confirmed x2)",
                            cat, sale_type, kraj, subtype or "all",
                        )
                        return out("exhausted", 1)
                    return out("degraded", 1)
                last_page = max(1, -(-declared_total // _PER_PAGE))
                if last_page > _MAX_SLICE_PAGES:
                    # Past the site's own 99-page ceiling on a filtered URL: the
                    # tail is unreachable on this axis, so descend instead.
                    return out("ceiling", 1)
            if parsed.total is not None:
                live_last = max(1, -(-parsed.total // _PER_PAGE))
            if not parsed.items:
                if page == 1 and declared_total == 0:
                    return out("exhausted", 1)
                return out("degraded", page)
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
                break
            if parsed.next_offset is None:
                # The pager ended before the declared tail: a truncated page.
                return out("degraded", page)
            page += 1

        # Arithmetic gate: the shared two-sided verdict, on DISTINCT ids (pages
        # shift under a live walk, so len(rows) double-counts).
        if not walk_is_complete(len({r[0] for r in rows}), declared_total):
            return out("degraded", page)
        return out("exhausted", page)

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
                children.append(replace(parent, outcome="deadline"))
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
        # the un-walked kraje never report at all, so per-slice outcomes alone
        # would let a truncated walk claim complete=True. Passed to
        # walk_is_complete as stopped_early=, so one function owns it.
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
                "pages=%d outcome=%s",
                cat, sale_type, r.kraj, r.subtype or "all", r.declared_total,
                len({x[0] for x in r.rows}), r.pages, r.outcome,
            )
            for nid, ref, price in r.rows:
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    native_ids.append(nid)
                ref_map[nid] = ref
                price_map[nid] = price

        pages = sum(r.pages for r in results)
        declared_sum = sum(r.declared_total or 0 for r in results if r.positive)
        national = self._nationwide_total(client, sale_type, cat)
        kraje_seen = {r.kraj for r in results}
        LOG.info(
            "PARTITION cm=%s ct=%s kraje=%d slices=%d positive=%d collected=%d "
            "declared_sum=%d national=%s pages=%d",
            cat, sale_type, len(kraje_seen), len(results),
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
        # mark_inactive is safe only on a walk that PROVED it saw the whole
        # category (rule #3): every one of the 14 kraje represented, every slice
        # exhausted to its declared tail, and the union reconciling against BOTH
        # the summed per-kraj declared counts and — when it is measurable — the
        # nationwide total. declared_sum is the primary denominator because
        # _nationwide_total swallows its own exception: a failed probe must never
        # be the thing that suppresses every sweep forever, and equally must never
        # authorise one on its own (it is skipped only after 14 positive slices
        # already proved coverage).
        complete = (
            not self._max_pages
            and not self._kraje
            and not deadline_hit
            and kraje_seen == set(KRAJ_SLUGS)
            and all(r.positive for r in results)
            and walk_is_complete(len(seen), declared_sum, stopped_early=deadline_hit)
            # FAIL CLOSED. This was written `national is None or ...`, which
            # made an unmeasurable national probe *prove* completeness — and
            # _nationwide_total swallows its own exception, so the rail was
            # weakest exactly when it mattered. Throttling is correlated: if the
            # kraj pages are degraded, the national probe is degraded too. An
            # adversarial review reproduced complete=True with 5,200 of 5,600
            # rows collected and a whole kraj missing. Rule #3: an unproven walk
            # never authorises a sweep.
            and national is not None
            and walk_is_complete(len(seen), national)
        )
        result_size = national if national is not None else declared_sum
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, result_size, pages, complete

    def probe_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool,
        limiter: RateLimiter, probe_pages: int,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        """Newest-first delta probe (portal_runner.run_index_probe). The generic
        walk-under-page-cap fallback is useless here: walk_category enumerates
        region×facet slices even under --max-pages AND the default order is not
        newest — so the probe reads the /nejnovejsi/ sort slug on the www host
        (through the same proxied client), page by page with an early stop on
        the first all-known page. Diff + enqueue only; always complete=False so
        the caller can never be tempted into a delisting sweep (rule #3)."""
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

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return 0
        # Several index slices collapse onto one (cm, ct) — 'rodinne-domy' and
        # 'chaty-chalupy' both -> dum — and this runner-gated call sees only ONE
        # slice's ids, so a per-slice sweep flipped the sibling slice's listings
        # inactive every walk. Buffer each complete slice's ids and sweep once,
        # on the group's LAST complete slice, with the union. An incomplete/
        # failed sibling never reaches this call, so its group stays below the
        # expected slice count and the sweep is suppressed this walk
        # (over-retention only; the next walk retries).
        key = (cm, ct)
        group = self._sweep_seen.setdefault(key, set())
        group.update(seen)
        self._sweep_done[key] = self._sweep_done.get(key, 0) + 1
        expected = sum(1 for c in self._categories if self.category_labels(c) == key)
        if self._sweep_done[key] < expected:
            return 0
        return db.mark_inactive_native(
            conn, SOURCE, cm, ct, group,
            min_unseen_hours=INACTIVE_MIN_UNSEEN_HOURS,
        )

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
        # Page coords win -> carry a stored geom forward -> geocode the locality
        # (never fails the fetch; scraper.location).
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
    portal: CeskerealityPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
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
    p = argparse.ArgumentParser(description="ceskereality.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; suppresses "
             "mark_inactive). Omit for a full, complete walk.",
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
        "--kraj", action="append", default=None, choices=list(KRAJ_SLUGS),
        metavar="SLUG",
        help="limit the index walk to this kraj (repeatable; e.g. "
             "stredocesky-kraj) for an ad-hoc partial run. Suppresses "
             "mark_inactive. Omit = all 14 kraje. An unknown slug is an "
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
