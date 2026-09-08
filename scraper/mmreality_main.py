"""Orchestrator for the mmreality.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.mmreality_main`. M&M Reality is a `Portal`
(MmRealityPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages each per-(sale type, property type) index and enqueues
new/price-changed ids into the shared `listing_detail_queue` (source='mmreality',
migration 108), then a detail-drain that fetches each listing page, parses its
embedded `:property` estate object to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (MmRealityClient) + parser
(mmreality_parser) + config differ (the modularity rule in CLAUDE.md).

Until 2026-09 this walked the bare `/nemovitosti/` feed as "a single mixed index
with no result total" and was parked on `supports_complete_walk=false` for it.
Both halves of that were wrong: the feed's own SSR `metadata.count` equals the
PRODEJ total (the 1,518 rentals never appeared in it — they were never scraped),
and every per-type URL (`/nemovitosti/{prodej|pronajem}/{byty|domy|pozemky|
komercni-objekty|ostatni}/`) declares its own count. So the walk is now one
category per (sale type, property type) — ten in all, partitioning the portal
exactly (the per-type prodej counts sum to the prodej total) — each proved by
the shared `walk_is_complete` arithmetic against that declared count, recorded
in the slice ledger (`portal_index_slices`, one row per category), and swept by
the source- and category-scoped `mark_inactive_native` behind the 12 h staleness
rail. The flag itself is flipped by the coverage gate from ledger evidence, not
here. Coordinates come from the estate JSON; a coords-less row falls back to
carry-forward + locality geocoding via the shared scraper.location resolver.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import Any

from scraper import db, portal_runner
from scraper.location import CoordResolver
from scraper.mmreality_client import MmRealityClient, detail_url
from scraper.mmreality_parser import (
    GROUP_SLUGS, NoPropertyObject, PropertyMismatch, index_price, live_groups, parse_detail,
    parse_index,
)
from scraper.portal import (
    PortalConfig,
    default_config,
    deadline_reached,
    load_portal_config,
    walk_is_complete,
    classify_index_sighting,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "mmreality"

# Index slug -> canonical label. The slugs are the portal's own "Typ nemovitosti"
# groups, which also drive the detail JSON's `group.name` the parser maps with
# the same prefixes — so a listing's index category and its stored category_main
# agree, which is what makes a category-scoped sweep safe.
CATEGORY_MAIN: dict[str, str] = {
    "byty": "byt",
    "domy": "dum",
    "pozemky": "pozemek",
    "komercni-objekty": "komercni",
    "ostatni": "ostatni",
}
SALE_TYPE: dict[str, str] = {"prodej": "prodej", "pronajem": "pronajem"}

# No staleness rail any more (rule #3, 2026-09-07): a complete walk nominates
# the rows it did not see for a page check and the drain's fetch decides, so a
# single walk-miss costs one fetch, never a live listing.

# The ledger key for a category walked whole. mmreality's per-type indexes page
# to their tail (the largest, pozemky/prodej, is ~300 pages of 12), so there is
# no second axis; one row per category is the ledger's whole shape.
SLICE_KEY = "national"

# The site's own <title> suffix, in both encodings a body can carry it.
_REF_ID_RE = re.compile(r"/nemovitosti/(\d+)")


def _trusted_detail_ref(native_id: str, detail_ref: str | None) -> str | None:
    """Drop a detail_ref whose own id contradicts the row's.

    An mmreality detail URL carries the listing id, so a disagreement means the
    QUEUE row is wrong, not the portal. Fetching it anyway reads a DIFFERENT
    listing's page, and that is not a harmless miss: if the other listing is
    removed, its page is a positive gone signal (PropertyMismatch /
    NoPropertyObject, both raised before the parsed-id belt below) and this
    listing gets delisted on the strength of it. That happened on 2026-09-08 --
    36 live listings flipped from refs left behind by the substitute-card bug
    (#1316/#1317), whose stored source_url pointed at the dead page that had been
    fetched. The id-derived URL is always canonical, so fall back to it.
    """
    if not detail_ref:
        return None
    match = _REF_ID_RE.search(detail_ref)
    if match and match.group(1) != str(native_id):
        LOG.warning(
            "DETAIL id=%s ignoring detail_ref that points at %s",
            native_id, match.group(1),
        )
        return None
    return detail_ref


_SITE_TITLE = "| M&M Reality"
_SITE_TITLE_ESCAPED = "| M&amp;M Reality"


class MmRealityPortal:
    """M&M Reality as a Portal: the seams the generic runner needs, wrapping the
    mmreality client + parser. Operational scope comes from the `portals`
    registry config; one category per (sale type, property type) index."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        usable = [
            c for c in (config.categories or [])
            if c.get("sale_type") in SALE_TYPE and c.get("category") in CATEGORY_MAIN
        ]
        if not usable:
            # A registry row still on the pre-2026-09 shape ({"index": ...}) —
            # or none at all — walks the baked-in ten rather than nothing; the
            # gate reads the DB row's own categories, so this fallback cannot
            # widen what the gate demands, only what the walk covers.
            if config.categories:
                LOG.warning(
                    "mmreality: config categories %r carry no (sale_type, category) "
                    "pairs; walking the baked-in per-type list", config.categories,
                )
            usable = [dict(c) for c in default_config(SOURCE).categories]
        self._categories = usable
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct
        # page > carry-forward > geocode (an estate-JSON row without coords had
        # NO coords path until now).
        self._coords = CoordResolver(SOURCE)

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        """The always-on worker's newest-first probe re-caps the walk between
        its peek and its deepening (portal_runner.run_index_probe). The per-type
        indexes list newest first by default (`order: createdAt`), so the
        generic page-capped walk is a valid delta probe here."""
        self._max_pages = pages

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
        # Single-row ingest (ingest_scraped_listing), not batched prepared writes,
        # so the transaction pooler is fine — no session pooler needed.
        conn = db.connect()
        self._coords.preload(conn)
        return conn

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = MmRealityClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        pages = 0
        page: int | None = None  # None = the bare first page
        declared: int | None = None
        stopped_early = False
        outcome = "exhausted"

        while True:
            if deadline_reached(deadline):
                LOG.info(
                    "INDEX time budget reached sale=%s cat=%s before page=%s "
                    "(pages=%d walked); stopping early, walk incomplete",
                    sale_type, cat, page or 1, pages,
                )
                stopped_early, outcome = True, "deadline"
                break
            html, status = client.fetch_index(sale_type, cat, page)
            parsed = parse_index(html)
            pages += 1
            if pages == 1:
                declared = parsed.total
            LOG.info(
                "INDEX sale=%s cat=%s page=%s items=%d declared=%s",
                sale_type, cat, page or 1, len(parsed.items), declared,
            )
            new_on_page = 0
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in ref_map:
                    native_ids.append(nid)
                    new_on_page += 1
                ref_map[nid] = detail_url(item.detail_path)
                # Same clamps as the stored price so the unchanged-compare can't
                # see a value the write boundary would have nulled.
                price_map[nid] = db.sane_price_czk(index_price(item.price_text))
            if self._max_pages and pages >= self._max_pages:
                # A page cap is a partial walk by definition (rule #3).
                stopped_early, outcome = True, "ceiling"
                break
            # Stop on an empty page, no "next" link, or a page that added nothing
            # new (a clamped out-of-range page would otherwise loop forever).
            if not parsed.items or parsed.next_offset is None or new_on_page == 0:
                break
            if parsed.next_offset <= (page or 1):
                LOG.warning(
                    "INDEX sale=%s cat=%s pager did not advance at page=%s (next=%s); "
                    "stopping", sale_type, cat, page or 1, parsed.next_offset,
                )
                outcome = "degraded"
                break
            page = parsed.next_offset

        seen = set(native_ids)
        # The shared two-sided verdict against the portal's OWN count for this
        # index. An unmeasurable count (no SSR state on page 1) is `unknown`,
        # never complete; a deadline or page cap short-circuits it.
        complete = walk_is_complete(len(seen), declared, stopped_early=stopped_early)
        if not complete and outcome == "exhausted":
            outcome = "degraded"
        LOG.info(
            "RECONCILE-INDEX sale=%s cat=%s declared=%s collected=%d pages=%d "
            "outcome=%s complete=%s",
            sale_type, cat, declared, len(seen), pages, outcome, complete,
        )
        if not self._max_pages:
            # A page-capped walk is a probe or a bounded test, not coverage: the
            # ledger is latest-wins, so recording its "ceiling" every few minutes
            # would overwrite the real walk's "exhausted" and hold the coverage
            # gate shut for good (the always-on worker probes under a cap).
            self._record_slice(conn, category, outcome, declared, len(seen), pages)

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
            "ENQUEUE source=mmreality sale=%s cat=%s new=%d changed=%d unchanged=%d "
            "enqueued=%d",
            sale_type, cat, len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, declared, pages, complete

    def _record_slice(
        self, conn: Any, category: dict[str, Any], outcome: str,
        declared: int | None, collected: int, pages: int,
    ) -> None:
        """One ledger row per category: the coverage gate (migration 455) reads
        "every slice of every declared category exhausted inside the window",
        and `exhausted` is the only positive outcome it accepts."""
        if conn is None:
            return
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return
        db.record_index_slice(
            conn, source=SOURCE, category_main=cm, category_type=ct,
            slice_key=SLICE_KEY, outcome=outcome, declared_total=declared,
            collected=collected, pages=pages,
        )

    def live_categories(self, limiter: RateLimiter) -> tuple[set[str], set[str]] | None:
        """The coverage alarm's input (runner: _record_category_drift): the
        property-type groups the portal lists right now, per sale type, vs the
        ones this walk is configured to cover. Two requests, the sale-type root
        pages. None if either page carries no SSR state -- an unreadable page
        must not read as 'the portal dropped every category'."""
        client = MmRealityClient(limiter=limiter)
        live: set[str] = set()
        for sale in sorted({c["sale_type"] for c in self._categories}):
            html, _status = client.fetch_sale_root(sale)
            groups = live_groups(html)
            if groups is None:
                return None
            live.update(f"{sale}/{GROUP_SLUGS.get(g, f'group-{g}')}" for g in groups)
        configured = {f"{c['sale_type']}/{c['category']}" for c in self._categories}
        return live, configured

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> MmRealityClient:
        return MmRealityClient(limiter=limiter)

    def fetch_detail(
        self, client: MmRealityClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        ref = _trusted_detail_ref(native_id, detail_ref)
        url = detail_url(ref or native_id)
        try:
            html, status = client.fetch_detail(ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        try:
            listing = parse_detail(html, source_url=url)
        except PropertyMismatch as exc:
            # A 200 with the old title and only "similar" cards: the portal no
            # longer presents this listing. Positive gone signal (rule #3).
            LOG.info("DETAIL id=%s gone: %s", native_id, exc)
            return DrainItem(native_id=native_id, kind="gone")
        except NoPropertyObject as exc:
            # The same page with no cards at all (removed plots, 2026-09-07):
            # gone when it is the site's own page, an error for any other 200
            # body (a challenge page, a blank proxy answer).
            if _SITE_TITLE in html or _SITE_TITLE_ESCAPED in html:
                LOG.info("DETAIL id=%s gone: %s (site page, no object)", native_id, exc)
                return DrainItem(native_id=native_id, kind="gone")
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        parsed_id = getattr(listing, "source_id_native", None)
        if parsed_id and str(parsed_id) != str(native_id):
            # Belt on top of the parser: never write another listing's data
            # under a fetch for this one.
            return DrainItem(
                native_id=native_id, kind="error",
                error=f"parsed id {parsed_id} != fetched id {native_id}",
            )
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
        # A gone detail is a definitive per-listing delisting signal even for a
        # partial-walk portal — flip just that one (source-scoped, rule #15).
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = %s AND claimed_at IS NULL AND given_up = false",
                (SOURCE,),
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
    portal = MmRealityPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Omitting both flags runs the index walk then the detail drain in one job
    # (the pilot's combined run, bounded by --max-pages / --max-detail). The
    # split flags exist so a large backfill can be cadence-split like sreality.
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
    p = argparse.ArgumentParser(description="mmreality.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages walked (pilot safety). Omit for a full walk.",
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
        "--max-seconds", type=float, default=None,
        help="wall-clock budget for the detail drain; it stops claiming + "
             "finalizes cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
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
