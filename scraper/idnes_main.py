"""Orchestrator for the reality.idnes.cz crawler — on the shared portal framework.

Runnable as `python -m scraper.idnes_main`. iDNES is a `Portal` (IdnesPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that stages raw
pages and enqueues listings into the shared `listing_detail_queue` (source='idnes',
migration 108), then a detail-drain that fetches + parses + ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (IdnesClient) + parser (idnes_parser) +
config differ from sreality/bazos (the modularity rule in CLAUDE.md).

A one-category crawl is a partial walk, so `supports_complete_walk=False` and the
runner NEVER runs mark_inactive (architectural rule #3) — it only upserts.

Pilot scope: a single category at a time (the queue does not carry the category,
which `parse_detail` needs, so the drain assumes this portal's one category).
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from scraper import db, geocoding, portal_runner
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.idnes_client import IdnesClient, detail_url, index_url
from scraper.idnes_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    Geocoder,
    parse_detail,
    parse_index,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "idnes"


class _CachingGeocoder:
    """Per-run memoised geocoder. idnes embeds precise per-listing coordinates, so
    geocoding is only a fallback for the rare coordless page; memoising still
    collapses repeat locality queries (and caches misses) across the worker pool."""

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
    if not os.environ.get("MAPY_CZ_API_KEY"):
        LOG.info("GEOCODE skipped: MAPY_CZ_API_KEY unset; coords from page pin only")
        return None
    return _CachingGeocoder(geocoding.geocode)


class IdnesPortal:
    """iDNES Reality as a Portal: the seams the generic runner needs, wrapping the
    idnes client + parser. Single-category pilot (see module docstring)."""

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
        max_pages: int | None = None,
        geocoder: Geocoder | None = None,
    ) -> None:
        self._sale_type = sale_type
        self._category = category
        self._canon_main = canon_main
        self._canon_type = canon_type
        self._locality = locality
        self._max_pages = max_pages
        self._geocoder = geocoder

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, str]]:
        return [{"sale_type": self._sale_type, "category": self._category}]

    def category_labels(self, category: dict[str, str]) -> tuple[str, str]:
        return (self._canon_main, self._canon_type)

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        return db.connect()

    def walk_category(
        self, category: dict[str, str], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = IdnesClient(limiter=limiter)
        seen: set[str] = set()
        entries: list[tuple[str, str | None, int | None, int]] = []
        pages = 0
        page: int | None = None       # None = the bare first page (idnes offset paging)
        while True:
            html, status = client.fetch_index(
                sale_type, cat, page, locality=self._locality
            )
            parsed = parse_index(html)
            pages += 1
            LOG.info(
                "INDEX page=%s items=%d total=%s", page, len(parsed.items), parsed.total
            )
            if conn is not None:
                db.upsert_portal_raw_page(
                    conn, source=SOURCE,
                    source_id_native=f"{sale_type}/{cat}/{page if page is not None else 0}",
                    source_url=index_url(sale_type, cat, page, locality=self._locality),
                    page_kind="index", html=html, http_status=status,
                )
            new_on_page = 0
            for item in parsed.items:
                if item.source_id_native not in seen:
                    seen.add(item.source_id_native)
                    new_on_page += 1
                    entries.append(
                        (item.source_id_native, detail_url(item.detail_path), None,
                         db.QUEUE_PRIORITY_NEW)
                    )
            if self._max_pages and pages >= self._max_pages:
                break
            # Stop on an empty page, no "next" link, or a page that added nothing
            # new (idnes clamps an out-of-range ?page to the last page rather than
            # 404ing, which would otherwise loop).
            if not parsed.items or parsed.next_offset is None or new_on_page == 0:
                break
            page = parsed.next_offset
        enqueued = 0
        if conn is not None and entries:
            enqueued = db.enqueue_detail(conn, SOURCE, entries)
        LOG.info("ENQUEUE source=idnes enqueued=%d seen=%d", enqueued, len(seen))
        # Partial walk: result_size unknown, complete=False (never mark_inactive).
        return seen, {"found_new": len(seen), "enqueued": enqueued}, None, pages, False

    def mark_inactive(self, conn: Any, category: dict[str, str], seen: set[str]) -> int:
        return 0  # never called (supports_complete_walk=False)

    def active_count(self, conn: Any, category: dict[str, str]) -> int | None:
        return None

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
        try:
            listing = parse_detail(
                html, source_url=url,
                category_main=self._canon_main, category_type=self._canon_type,
                geocoder=self._geocoder,
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
        # The queue (fail_detail) tracks attempts/give-up; idnes has no
        # sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'idnes' AND claimed_at IS NULL AND given_up = false"
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


def _run_phase(portal: IdnesPortal, run_type: str, runner, dry_run: bool, **kw: Any) -> int:
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

    portal = IdnesPortal(
        sale_type=args.sale_type, category=args.category,
        canon_main=canon_main, canon_type=canon_type,
        locality=args.locality, max_pages=args.max_pages,
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
    p = argparse.ArgumentParser(description="reality.idnes.cz crawler (portal framework)")
    p.add_argument("--sale-type", default="prodej", choices=sorted(SALE_TYPE))
    p.add_argument("--category", default="byty", choices=sorted(CATEGORY_MAIN))
    p.add_argument("--locality", default=None, help="idnes region path segment, e.g. praha")
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
