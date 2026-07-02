"""Orchestrator for the realitymix.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.realitymix_main`. realitymix is a `Portal`
(RealitymixPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages the HTML search results and enqueues new/price-changed ids
into the shared `listing_detail_queue` (source='realitymix', migration 108), then
a detail-drain that fetches each listing page, parses it to a `ScrapedListing`,
and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1
matching). No bespoke pipeline — only the per-portal fetcher (RealitymixClient) +
parser (realitymix_parser) + config differ from sreality/idnes/ceskereality (the
modularity rule in CLAUDE.md).

Two deliberate differences from the ceskereality template:
- The detail URL (`/detail/{obec}/{slug}-{id}.html`) does NOT encode the
  category, so `parse_detail` derives it from the page's BreadcrumbList JSON-LD
  instead of from the URL — self-contained, one config walks all 12 (cm × ct).
- `walk_category` drives `?stranka` straight to `ceil(total / PER_PAGE)` (the
  page-reported total) rather than trusting a pager "next" arrow, and treats a
  barren page as transient (one retry). This is the lesson from ceskereality's
  reverted #637: an arrow-trusting walk stops early on a throttled/degraded page.
  realitymix is nginx (not Cloudflare) and paginates reliably to the exact total
  with no deep-pagination cap, so a per-category walk is provable-complete →
  `supports_complete_walk` lets the runner mark delisted listings inactive under
  the completeness guard (rule #3), source-scoped (rule #15). Coordinates come
  straight from the page's `data-gps-lat/-lon`, so there is no geocoding step.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from math import ceil
from typing import Any

from scraper import db, portal_runner
from scraper.geocoding import geocode
from scraper.portal import (
    PortalConfig,
    default_config,
    load_portal_config,
    price_changed,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter
from scraper.realitymix_client import RealitymixClient, detail_url
from scraper.realitymix_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    _in_cz_bbox,
    index_price,
    parse_detail,
    parse_index,
)

LOG = logging.getLogger(__name__)
SOURCE = "realitymix"
PER_PAGE = 20  # realitymix renders 20 results per ?stranka page

# An index walk that collected at least this fraction of the page-reported total
# is treated as complete enough to drive mark_inactive (the framework standard,
# rule #3); the 24h min_unseen_hours rail in db.mark_inactive is the real safety.
INDEX_MIN_COMPLETENESS = 0.995


def _walk_complete(collected: int, total: int | None) -> bool:
    if not total or total <= 0:
        return True
    return collected >= total * INDEX_MIN_COMPLETENESS


# Geocode tiers too coarse to store: a region/country centroid drops the pin in
# the middle of the country, worse than NULL for map + radius filter. A
# municipality (town) centroid is KEPT — it recovers the admin hierarchy
# (obec/okres/region, derived from geom by the listings trigger) and a rough map
# placement, the same posture as idnes. Mirrors idnes_main._GEOCODE_SKIP_TYPES.
_GEOCODE_SKIP_TYPES = frozenset({"regional.region", "regional.country"})


def _geocode_fallback(listing: Any) -> Any:
    """Fill lat/lon by geocoding the listing's locality when the page carried no
    #print-map (the ~28% map-less case). No-op when coords are already present,
    the locality is missing, MAPY_CZ_API_KEY is unset, the match is too coarse, or
    Mapy.cz fails — geocoding must never break a detail fetch. Stamps
    raw['coords']={'source':'geocode',...} so the Mapy-sourced rows are auditable."""
    if listing.lat is not None and listing.lon is not None:
        return listing
    if not listing.locality:
        return listing
    try:
        result = geocode(listing.locality, timeout_s=5.0, max_retries=1)
    except Exception:  # noqa: BLE001 - geocoding (incl. unset key) must never fail the fetch
        return listing
    # Too coarse (region/country centroid) or outside the CZ bbox (a foreign
    # mis-match for an ambiguous locality) -> worse than NULL. The bbox guard
    # matches the backfill so the drain and the one-off pass agree.
    if result.matched_type in _GEOCODE_SKIP_TYPES or not _in_cz_bbox(result.lat, result.lng):
        return listing
    raw = {**listing.raw, "coords": {"source": "geocode", "confidence": result.confidence,
                                     "matched_type": result.matched_type}}
    return replace(listing, lat=result.lat, lon=result.lng, raw=raw)


class RealitymixPortal:
    """realitymix.cz as a Portal: the seams the generic runner needs, wrapping the
    realitymix client + parser. Operational scope (categories, complete-walk
    capability, rates) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self._price_change_min_pct = config.limits.price_change_min_pct
        # stored (lat, lon) per native id at drain start; a refetch whose page
        # carries no coords gets them carried forward instead of re-geocoded — geom
        # is never wiped and a Mapy credit is only ever spent once per listing.
        self._have_geom: dict[str, tuple[float, float]] | None = None

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
        # Preload (once, on the main thread) the stored coords of already-placed
        # rows so the worker-pool fetch_detail carries them forward instead of
        # re-geocoding a map-less page. Without this, every coords-less realitymix
        # page (the ~28%) would re-geocode on EVERY refetch — the runaway that
        # exhausted the Mapy key on idnes (2026-06). A bare skip isn't enough
        # either: ingesting lat=None wipes the stored geom ("geom = EXCLUDED.geom")
        # and flips the content hash, oscillating snapshots. New/still-missing rows
        # still geocode (once).
        if self._have_geom is None:
            self._have_geom = db.native_ids_with_geom(conn, SOURCE)
            LOG.info(
                "GEOCODE preload have_geom=%d (carry stored coords forward on refetch)",
                len(self._have_geom),
            )
        return conn

    def _fill_coords(self, native_id: str, listing: Any) -> Any:
        """Page-provided coords win; else carry the stored coordinate forward;
        geocode only when neither the page nor the DB has one. A None have-geom map
        (never preloaded) falls every coords-less listing through to the geocoder."""
        if listing.lat is not None and listing.lon is not None:
            return listing
        stored = (self._have_geom or {}).get(native_id)
        if stored is not None:
            # Mark the carried coord so provenance is stable across refetches (a
            # geocoded row's raw.coords would otherwise flip back to {source:None}
            # on the next map-less refetch). Stable 'carry_forward' keeps the
            # geocode/Mapy-sourced rows attributable (source != 'page').
            raw = {**listing.raw, "coords": {"source": "carry_forward"}}
            return replace(listing, lat=stored[0], lon=stored[1], raw=raw)
        return _geocode_fallback(listing)

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = RealitymixClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        total: int | None = None
        pages = 0
        page = 1
        barren_retried = False
        while True:
            try:
                html, _ = client.fetch_index(sale_type, cat, page)
            except ListingGoneError:
                # A 404 on a ?stranka past the end (total off by one) — end of category.
                break
            parsed = parse_index(html)
            pages += 1
            if parsed.total is not None:
                total = parsed.total
            LOG.info("INDEX page=%d items=%d total=%s", page, len(parsed.items), total)
            new_on_page = 0
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in ref_map:
                    native_ids.append(nid)
                    new_on_page += 1
                ref_map[nid] = detail_url(item.detail_path)
                price_map[nid] = index_price(item.price_text)

            if self._max_pages and pages >= self._max_pages:
                break

            last_page = ceil(total / PER_PAGE) if total else None
            if not parsed.items:
                # A barren page at/below the reported last page -> a transient
                # throttle/degrade: retry it once before concluding the category
                # ended (the #637 lesson — incl. a throttled FINAL page, page ==
                # last_page). Without a total, an empty page is the genuine end.
                if last_page is not None and page <= last_page and not barren_retried:
                    barren_retried = True
                    continue
                break
            barren_retried = False
            if last_page is not None:
                if page >= last_page:
                    break
            elif new_on_page == 0:
                break  # no total + nothing new (clamped out-of-range) -> stop
            page += 1

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
                unchanged_pks.append(prev["sreality_id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings(conn, unchanged_pks)

        entries = (
            [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=realitymix new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        complete = (not self._max_pages) and _walk_complete(len(seen), total)
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, total, pages, complete

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
    def make_client(self, limiter: RateLimiter) -> RealitymixClient:
        return RealitymixClient(limiter=limiter)

    def fetch_detail(
        self, client: RealitymixClient, native_id: str, detail_ref: str | None,
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
        # Page coords win -> carry a stored geom forward -> geocode the locality
        # (map-less listings), at most once per listing.
        listing = self._fill_coords(native_id, listing)
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
        existing = db.index_summary_native(conn, SOURCE, [native_id])
        row = existing.get(native_id)
        if row is not None:
            db.mark_listing_inactive(conn, row["sreality_id"])

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'realitymix' AND claimed_at IS NULL AND given_up = false"
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
    portal: RealitymixPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
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
    portal = RealitymixPortal(config, max_pages=args.max_pages)

    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # realitymix is large (~48k), so production runs the cadence split via the
    # workflows (--index-only feeds --drain-only). A bare combined run (omit both
    # flags) still works for ad-hoc/local use; the split flags are the norm.
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
    p = argparse.ArgumentParser(description="realitymix.cz scraper (portal framework)")
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
        help="wall-clock budget; the phase stops claiming + finalizes cleanly "
             "before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue + mark_inactive only (no detail drain)",
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
