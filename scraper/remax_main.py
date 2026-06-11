"""Orchestrator for the remax-czech.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.remax_main`. RE/MAX is a `Portal` (RemaxPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that pages the
search results and enqueues new/price-changed ids into the shared
`listing_detail_queue` (source='remax', migration 108), then a detail-drain that
fetches each listing page, parses it to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + singleton property). No bespoke
pipeline — only the per-portal fetcher (RemaxClient) + parser (remax_parser) +
config differ (the modularity rule in CLAUDE.md).

remax exposes its catalogue as TWO mixed indexes — sale (`sale=1`) and rent
(`sale=2`) — each spanning every property category with no per-category URL. The
config descriptors are therefore per (category_main, category_type): each
`walk_category` walks (or reuses, via an agenda-level cache) that offer-type's
full index, then keeps the slice whose card-title category maps to the
descriptor. This gives the runner real (cm, ct) labels — the Health
reconciliation joins listings on those — while fetching each agenda's pages only
once per run. The drain re-derives each listing's category from the detail page
("Typ nemovitosti" + title), so the queue stays category-agnostic.

Like every portal's first cut (bazos/idnes/bezrealitky/mmreality/maxima all
started this way), remax ships as a PILOT with `supports_complete_walk=false`: the
runner never marks listings inactive from index-absence (remax reports a per-
AGENDA total, and the per-category slice is title-derived — not a portal-reported
per-(cm,ct) total — so a safe per-category completeness check isn't available). A
gone detail fetch (404/410 or a redirect off the detail path) still flips that one
listing inactive. `mark_inactive` / `active_count` are implemented source-scoped so
promotion to complete-walk is a one-flag change.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.portal import PortalConfig, default_config, load_portal_config
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter
from scraper.remax_client import RemaxClient, detail_url
from scraper.remax_parser import category_of, index_price, parse_detail, parse_index

LOG = logging.getLogger(__name__)
SOURCE = "remax"
_PAGE_SIZE = 21  # remax serves 21 cards per search page


class _AgendaWalk:
    """One offer-type's (sale or rent) collected index, walked once and shared
    across that agenda's per-category descriptors."""

    def __init__(
        self, native_ids: list[str], ref_map: dict[str, str],
        price_map: dict[str, int | None], cat_map: dict[str, str | None],
        total: int | None, pages: int,
    ) -> None:
        self.native_ids = native_ids
        self.ref_map = ref_map
        self.price_map = price_map
        self.cat_map = cat_map  # id -> category_main (from the card title)
        self.total = total
        self.pages = pages


class RemaxPortal:
    """remax-czech.cz as a Portal: the seams the generic runner needs, wrapping
    the remax client + parser. Two mixed indexes (sale=1 / sale=2); each config
    descriptor pairs a category with its offer-type flag and keeps the
    title-derived slice for its category."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self._agenda_cache: dict[int, _AgendaWalk] = {}

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (category.get("category_main"), category.get("category_type"))

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        return db.connect()

    def _walk_agenda(
        self, sale: int, conn: Any, limiter: RateLimiter,
    ) -> tuple[_AgendaWalk, int]:
        """Walk one offer-type's full mixed index once; cache it for the agenda's
        other category descriptors. Returns (walk, pages_fetched_this_call) so the
        runner counts each agenda's pages exactly once (0 on a cache hit)."""
        cached = self._agenda_cache.get(sale)
        if cached is not None:
            return cached, 0

        client = RemaxClient(limiter=limiter)
        native_ids: list[str] = []
        ref_map: dict[str, str] = {}
        price_map: dict[str, int | None] = {}
        cat_map: dict[str, str | None] = {}
        total: int | None = None
        pages = 0
        page = 1
        while True:
            html, status = client.fetch_index(sale=sale, stranka=page)
            parsed = parse_index(html)
            pages += 1
            total = parsed.total if parsed.total is not None else total
            LOG.info("INDEX sale=%d page=%d items=%d total=%s", sale, page, len(parsed.items), total)
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
                cat_map[nid] = category_of(None, item.title)
            if self._max_pages and pages >= self._max_pages:
                break
            # Stop on an empty page, a page adding nothing new, or once the
            # collected count reaches the reported total (a clamped out-of-range
            # page would otherwise loop forever).
            if not parsed.items or new_on_page == 0:
                break
            if total is not None and len(native_ids) >= total:
                break
            page += 1

        walk = _AgendaWalk(native_ids, ref_map, price_map, cat_map, total, pages)
        self._agenda_cache[sale] = walk
        return walk, pages

    @staticmethod
    def _belongs(mapped: str | None, cm: str | None) -> bool:
        """Whether an id's title-derived category `mapped` belongs to descriptor
        category `cm`. 'ostatni' is the catch-all, so an un-derivable category (a
        new remax type) is never silently dropped."""
        if mapped == cm:
            return True
        return cm == "ostatni" and mapped is None

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        cm = category.get("category_main")
        sale = int(category.get("sale") or 1)
        walk, pages = self._walk_agenda(sale, conn, limiter)

        native_ids = [n for n in walk.native_ids if self._belongs(walk.cat_map.get(n), cm)]
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
            if walk.price_map.get(nid) is not None and prev["price_czk"] == walk.price_map[nid]:
                unchanged_pks.append(prev["sreality_id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings(conn, unchanged_pks)

        entries = (
            [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=remax cm=%s ct=%s new=%d changed=%d unchanged=%d enqueued=%d",
            cm, category.get("category_type"), len(new_ids), len(changed),
            len(unchanged_pks), enqueued,
        )
        # remax reports a per-AGENDA total, not per-category, so the per-category
        # "portal expected" is what this category collected — index% is then 100%
        # by construction. supports_complete_walk=false keeps the runner from
        # marking inactive regardless (pilot); this only labels the Health row.
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, len(seen), pages, False

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return 0
        existing = db.index_summary_native(conn, SOURCE, list(seen))
        pks = {v["sreality_id"] for v in existing.values()}
        return db.mark_inactive(conn, cm, ct, pks, source=SOURCE)

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> RemaxClient:
        return RemaxClient(limiter=limiter)

    def fetch_detail(
        self, client: RemaxClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        try:
            listing = parse_detail(html, source_url=url)
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
            )
            pk, result = db.ingest_scraped_listing(conn, p["listing"])
            image_urls = p["listing"].raw.get("image_urls") or []
            images = [{"url": u, "sequence": seq} for seq, u in enumerate(image_urls)]
            inserted = db.record_images(conn, pk, images)
            db.mark_portal_page_parsed(conn, page_id)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
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
    portal: RemaxPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
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
    portal = RemaxPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Omitting both flags runs the index walk then the detail drain in one job,
    # bounded by --max-pages / --max-detail (+ --max-seconds). The split flags
    # exist so a large backfill can be cadence-split like sreality/idnes.
    rc = 0
    if not args.drain_only:
        rc = _run_phase(portal, "index", portal_runner.run_index_walk, args.dry_run)
    if rc == 0 and not args.index_only:
        rc = _run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="remax-czech.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap search pages walked per agenda (ad-hoc partial run). Omit for the full walk.",
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
        help="walk the search index + enqueue only (no detail drain)",
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
