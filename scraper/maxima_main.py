"""Orchestrator for the nemovitosti.maxima.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.maxima_main`. Maxima is a `Portal` (MaximaPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that pages the
catalogue HTML and enqueues new/price-changed ids into the shared
`listing_detail_queue` (source='maxima', migration 108), then a detail-drain that
fetches each listing page, parses it to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (MaximaClient) + parser (maxima_parser) +
config differ from sreality/idnes (the modularity rule in CLAUDE.md).

Maxima is a SINGLE small agency catalogue (~220 listings) on ONE mixed index (no
per-category URL), so unlike idnes there is one "category" descriptor and the
walk pages the whole catalogue. The category is encoded in the native id's leading
letter + the title verb, so the drain derives each listing's category from the
detail page itself (`maxima_parser.parse_detail`), not from the queue.

Like every portal's first cut (bazos/idnes/bezrealitky all started this way), maxima
ships as a PILOT with `supports_complete_walk=false`: the runner never marks listings
inactive from index-absence. A gone detail fetch (404/410) still flips that one
listing inactive. The whole-catalogue walk IS provably complete (the index reports a
total), so promotion to complete-walk + delisting sweep is a deliberate later
migration (as bazos got in 113) once the pilot proves stable.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.maxima_client import MaximaClient, detail_url, index_url
from scraper.maxima_parser import index_price, parse_detail, parse_index
from scraper.portal import PortalConfig, default_config, load_portal_config
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "maxima"

# The whole-catalogue descriptor: maxima has one mixed index, so one "category".
_CATALOGUE = {"label": "all"}


class MaximaPortal:
    """nemovitosti.maxima.cz as a Portal: the seams the generic runner needs,
    wrapping the maxima client + parser. A single-agency catalogue walked as one
    mixed index; the per-listing category comes from the parser, not the walk."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, Any]]:
        return [_CATALOGUE]

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        # One mixed walk spanning every category, so no single (cm, ct) label.
        return (None, None)

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        # Single-row ingest (ingest_scraped_listing), not batched prepared writes,
        # so the transaction pooler is fine — no session pooler needed.
        return db.connect()

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        client = MaximaClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        total: int | None = None
        pages = 0
        page = 1
        while True:
            html, status = client.fetch_index(page)
            parsed = parse_index(html)
            pages += 1
            total = parsed.total if parsed.total is not None else total
            LOG.info("INDEX page=%d items=%d total=%s", page, len(parsed.items), total)
            if conn is not None:
                db.upsert_portal_raw_page(
                    conn, source=SOURCE,
                    source_id_native=f"index/{page}",
                    source_url=index_url(page),
                    page_kind="index", html=html, http_status=status,
                )
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
            # Stop on an empty page (the catalogue runs out — page N>last returns
            # no cards) or one that added nothing new.
            if not parsed.items or new_on_page == 0:
                break
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
            if price_map.get(nid) is not None and prev["price_czk"] == price_map[nid]:
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
            "ENQUEUE source=maxima new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        # Pilot: never drives mark_inactive (supports_complete_walk=false), so the
        # completeness flag is informational only — report False to be explicit.
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, total, pages, False

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        return 0  # pilot: index-absence sweep is off (supports_complete_walk=false)

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        return None

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> MaximaClient:
        return MaximaClient(limiter=limiter)

    def fetch_detail(
        self, client: MaximaClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or f"/nemovitosti/{native_id}/")
        try:
            html, status = client.fetch_detail(detail_ref or f"/nemovitosti/{native_id}/")
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
        # A gone detail flips that one listing inactive immediately (per-listing
        # signal, independent of the index-absence sweep this pilot keeps off).
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'maxima' AND claimed_at IS NULL AND given_up = false"
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


def _run_phase(
    portal: MaximaPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
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
        rc, agg = runner(portal, dry_run=dry_run, **kw)
    finally:
        if not dry_run:
            _finalize(run_id, agg)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    portal = MaximaPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # The catalogue is small, so a combined run (index walk + full drain) fits one
    # job comfortably. --index-only / --drain-only keep the same cadence-split
    # escape hatch as the other portals; omitting both runs both phases.
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
    p = argparse.ArgumentParser(description="nemovitosti.maxima.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap catalogue pages walked (ad-hoc partial run). Omit for the full walk.",
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
        help="walk the catalogue + enqueue only (no detail drain)",
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
