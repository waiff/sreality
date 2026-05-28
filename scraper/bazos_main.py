"""Orchestrator for the bazos.cz crawler — on the shared portal framework (Phase 4).

Runnable as `python -m scraper.bazos_main`. Bazos is now a `Portal` (BazosPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that stages raw
pages and enqueues listings into the shared `listing_detail_queue` (source='bazos',
migration 108), then a detail-drain that fetches + parses + ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (BazosClient) + parser (bazos_parser) +
config differ from sreality.

A one-category crawl is a partial walk, so `supports_complete_walk=False` and the
runner NEVER runs mark_inactive (architectural rule #3) — it only upserts.

Pilot scope: a single category at a time (the queue does not carry the category,
which `parse_detail` needs, so the drain assumes this portal's one category).
Multi-category bazos would encode the category in the queue — deferred.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from scraper import db, geocoding, portal_runner
from scraper.bazos_client import BazosClient, detail_url, index_url
from scraper.bazos_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    Geocoder,
    parse_detail,
    parse_index,
)
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "bazos"


class _CachingGeocoder:
    """Per-run memoised geocoder: collapses repeat street/locality queries to
    one Mapy.cz call each (a crawl's listings cluster by town). Caches misses
    too so a failing query isn't retried for every listing in that locality.

    Shared across the detail-drain worker pool. Dict get/set are atomic under
    the GIL and `geocoding.geocode` builds its own request session per call, so
    the worst concurrent case is a harmless duplicate lookup, never corruption."""

    def __init__(self, fn: Geocoder) -> None:
        self._fn = fn
        self._cache: dict[str, GeocodeResult | GeocodingError] = {}

    def __call__(self, query: str) -> GeocodeResult:
        key = " ".join(query.lower().split())
        cached = self._cache.get(key)
        if cached is not None:
            if isinstance(cached, GeocodingError):
                raise cached
            return cached
        try:
            result = self._fn(query)
        except GeocodingError as exc:
            self._cache[key] = exc
            raise
        self._cache[key] = result
        return result


def _build_geocoder() -> Geocoder | None:
    """A cached Mapy.cz geocoder, or None when MAPY_CZ_API_KEY is unset so the
    crawl still runs (coordinates then come from the CZ-guarded maps link)."""
    if not os.environ.get("MAPY_CZ_API_KEY"):
        LOG.info("GEOCODE skipped: MAPY_CZ_API_KEY unset; coords from maps link only")
        return None
    return _CachingGeocoder(geocoding.geocode)


class BazosPortal:
    """Bazos as a Portal: the seams the generic runner needs, wrapping the
    bazos client + parser. Single-category pilot (see module docstring)."""

    source = SOURCE
    supports_complete_walk = False
    index_rate = 0.5

    def __init__(
        self,
        *,
        sale_type: str,
        category: str,
        canon_main: str,
        canon_type: str,
        locality: str | None = None,
        radius_km: int | None = None,
        max_pages: int | None = None,
        geocoder: Geocoder | None = None,
    ) -> None:
        self._sale_type = sale_type
        self._category = category
        self._canon_main = canon_main
        self._canon_type = canon_type
        self._geocoder = geocoder
        self._locality = locality
        self._radius_km = radius_km
        self._max_pages = max_pages

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, str]]:
        return [{"sale_type": self._sale_type, "category": self._category}]

    def category_labels(self, category: dict[str, str]) -> tuple[str, str]:
        return (self._canon_main, self._canon_type)

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        # Bazos ingests single rows (not batched-prepared), so the transaction
        # pooler is fine — no session pooler needed.
        return db.connect()

    def walk_category(
        self, category: dict[str, str], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = BazosClient(limiter=limiter)
        seen: set[str] = set()
        entries: list[tuple[str, str | None, int | None, int]] = []
        pages = 0
        offset = 0
        while True:
            html, status = client.fetch_index(
                sale_type, cat, offset,
                locality=self._locality, radius_km=self._radius_km,
            )
            page = parse_index(html)
            pages += 1
            LOG.info(
                "INDEX offset=%d items=%d total=%s", offset, len(page.items), page.total
            )
            if conn is not None:
                db.upsert_portal_raw_page(
                    conn, source=SOURCE,
                    source_id_native=f"{sale_type}/{cat}/{offset}",
                    source_url=index_url(
                        sale_type, cat, offset,
                        locality=self._locality, radius_km=self._radius_km,
                    ),
                    page_kind="index", html=html, http_status=status,
                )
            for item in page.items:
                if item.source_id_native not in seen:
                    seen.add(item.source_id_native)
                    entries.append(
                        (item.source_id_native, item.detail_path, None,
                         db.QUEUE_PRIORITY_NEW)
                    )
            if self._max_pages and pages >= self._max_pages:
                break
            if not page.items or page.next_offset is None:
                break
            offset = page.next_offset
        enqueued = 0
        if conn is not None and entries:
            enqueued = db.enqueue_detail(conn, SOURCE, entries)
        LOG.info("ENQUEUE source=bazos enqueued=%d seen=%d", enqueued, len(seen))
        # Partial walk: result_size unknown, complete=False (never mark_inactive).
        return seen, {"found_new": len(seen), "enqueued": enqueued}, None, pages, False

    def mark_inactive(self, conn: Any, category: dict[str, str], seen: set[str]) -> int:
        return 0  # never called (supports_complete_walk=False)

    def active_count(self, conn: Any, category: dict[str, str]) -> int | None:
        return None

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> BazosClient:
        return BazosClient(limiter=limiter)

    def fetch_detail(
        self, client: BazosClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        try:
            listing = parse_detail(
                html, source_url=url,
                category_main=self._canon_main, category_type=self._canon_type,
                geocoder=self._geocoder,
            )
        except Exception as exc:
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
        # Partial-walk pilot: a gone detail is dequeued but NOT flipped inactive
        # (rule #3 — no delisting inference for a portal that can't prove a
        # complete walk). Documented limitation.
        pass

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; bazos has no
        # sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'bazos' AND claimed_at IS NULL AND given_up = false"
            )
            return int(cur.fetchone()[0])


def _finalize(run_id: int | None, agg: dict[str, Any]) -> None:
    if run_id is None or not agg:
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
                images_stored=agg.get("images_discovered", 0),
                errors=agg.get("errors", 0),
                by_category=agg.get("by_category", []),
            )
    except Exception as exc:
        LOG.warning("scrape_run_finalize failed: %s", exc)


def _run_phase(portal: BazosPortal, run_type: str, runner, dry_run: bool, **kw: Any) -> int:
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
        rc, agg = runner(portal, dry_run=dry_run, **kw)
    finally:
        if not dry_run:
            _finalize(run_id, agg)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    canon_type = SALE_TYPE.get(args.sale_type)
    canon_main = CATEGORY_MAIN.get(args.category)
    if canon_type is None or canon_main is None:
        LOG.error(
            "unmapped scope sale_type=%s category=%s", args.sale_type, args.category
        )
        return 2

    portal = BazosPortal(
        sale_type=args.sale_type, category=args.category,
        canon_main=canon_main, canon_type=canon_type,
        locality=args.locality, radius_km=args.radius_km, max_pages=args.max_pages,
        geocoder=_build_geocoder(),
    )

    # Index-walk (enqueue) then detail-drain (fetch + ingest), through the one
    # shared runner. Two scrape_runs rows ('index' + 'detail'), like sreality.
    rc = _run_phase(
        portal, "index", portal_runner.run_index_walk, args.dry_run,
    )
    if rc == 0:
        rc = _run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=args.max_detail, detail_workers=args.workers,
            detail_rate=args.rate,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="bazos.cz crawler (portal framework)")
    p.add_argument("--sale-type", default="prodam", choices=sorted(SALE_TYPE))
    p.add_argument("--category", default="byt", choices=sorted(CATEGORY_MAIN))
    p.add_argument("--locality", default=None)
    p.add_argument("--radius-km", type=int, default=None)
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages walked (pilot safety); omit for a full walk",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = drain the queue)",
    )
    p.add_argument("--workers", type=int, default=1, help="detail-fetch workers")
    p.add_argument(
        "--rate", type=float, default=0.5,
        help="requests/second ceiling (default 0.5 = one request per 2s)",
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
