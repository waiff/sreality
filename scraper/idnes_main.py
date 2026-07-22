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
from typing import Any

from scraper import db, portal_runner
from scraper.idnes_client import IdnesClient, detail_url
from scraper.idnes_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    category_from_url,
    index_price,
    parse_detail,
    parse_index,
)
from scraper.portal import (
    PortalConfig,
    default_config,
    load_portal_config,
    price_changed,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.location import CoordResolver
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "idnes"

# An index walk must collect ~the FULL page-reported total before it drives
# mark_inactive (rule #3). 100% is statistically unreachable on large
# categories — listings churn mid-walk, with observed real-walk deficits up to
# 0.24% — so a 1.0 bar suppressed every healthy sweep. 99.5% passes every
# healthy walk with 2x margin while a genuinely truncated walk (e.g.
# rate-limited 1,029/7,000) still reads incomplete; the
# INACTIVE_MIN_UNSEEN_HOURS staleness rail is the second, stronger guard.
# Not operator-tunable. Mirrors bazos_main / bezrealitky_main.
INDEX_MIN_COMPLETENESS = 0.995

# Only flip rows unseen for 12h+ — ~2 full walk cadences at the 6h schedule.
# last_seen_at is bumped for unchanged rows each walk (touch_listings) and for
# changed rows on a successful drain fetch — so a churn-missed live row is
# protected unless its detail fetches have ALSO failed for 12h+; even then the
# flip self-heals on the next index sighting (touch_listings reactivates).
# Tightened 24->12h for the real-time delisting SLO.
INACTIVE_MIN_UNSEEN_HOURS = 12


def _walk_complete(collected: int, total: int | None) -> bool:
    if not total or total <= 0:
        return True
    return collected >= total * INDEX_MIN_COMPLETENESS


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

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): idnes's default index
        # order is newest-first, so a page-capped walk IS the delta probe.
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
        # Preload (once, on the main thread) the stored coords so the worker-pool
        # fetch_detail carries them forward instead of re-geocoding (the 2026-06
        # Mapy-credit incident guard — rationale in scraper.location).
        self._coords.preload(conn)
        return conn

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = IdnesClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        total: int | None = None
        pages = 0
        page: int | None = None       # None = the bare first page (idnes offset paging)
        while True:
            html, status = client.fetch_index(sale_type, cat, page, locality=None)
            parsed = parse_index(html)
            pages += 1
            total = parsed.total if parsed.total is not None else total
            LOG.info("INDEX page=%s items=%d total=%s", page, len(parsed.items), total)
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
                break
            # Stop on an empty page, no "next" link, or a page that added nothing
            # new (idnes clamps an out-of-range ?page to the last page, which
            # would otherwise loop forever).
            if not parsed.items or parsed.next_offset is None or new_on_page == 0:
                break
            page = parsed.next_offset

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
            if price_map.get(nid) is not None and not price_changed(
                prev["price_czk"], price_map[nid], self._price_change_min_pct,
            ):
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
            "ENQUEUE source=idnes new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        complete = (not self._max_pages) and _walk_complete(len(seen), total)
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, total, pages, complete

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
            )
            pk, result = db.ingest_scraped_listing(conn, p["listing"])
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
