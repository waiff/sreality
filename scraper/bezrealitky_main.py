"""Orchestrator for the bezrealitky.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.bezrealitky_main`. Bezrealitky is a `Portal`
(BezrealitkyPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages bezrealitky's GraphQL `listAdverts` and enqueues
new/price-changed ids into the shared `listing_detail_queue` (source='bezrealitky',
migration 108), then a detail-drain that fetches `advert(id)`, parses it to a
ScrapedListing, and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency +
Tier-1 matching). No bespoke pipeline — only the per-portal fetcher
(BezrealitkyClient) + parser (bezrealitky_parser) + config differ from sreality.

Unlike bazos (a partial-walk HTML crawler), bezrealitky's GraphQL exposes a
`totalCount` and has no deep-pagination cap, so a full per-category walk is
provable-complete: `supports_complete_walk` (config-driven) lets the runner
mark delisted listings inactive under the completeness guard (architectural rule
#3), source-scoped so it only ever touches bezrealitky rows (rule #15). Because
the detail JSON carries offerType/estateType, the drain derives each listing's
category from the response — so bezrealitky walks MANY categories from one config
without the queue-encodes-category limitation that constrains bazos.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.bezrealitky_client import BezrealitkyClient
from scraper.bezrealitky_parser import (
    ESTATE_TYPE,
    OFFER_TYPE,
    SOURCE,
    parse_advert,
)
from scraper.portal import PortalConfig, default_config, load_portal_config
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

INDEX_PAGE_SIZE = 100

# An index walk that collected at least this fraction of the API-reported
# totalCount is treated as complete enough to drive mark_inactive; below it the
# walk likely truncated and flipping unseen listings inactive would falsely
# delist live ones. Mirrors scraper.main.INDEX_MIN_COMPLETENESS.
INDEX_MIN_COMPLETENESS = 0.9


def _walk_complete(collected: int, total: int | None) -> bool:
    if not total or total <= 0:
        return True
    return collected >= total * INDEX_MIN_COMPLETENESS


class BezrealitkyPortal:
    """Bezrealitky as a Portal: the seams the generic runner needs, wrapping the
    bezrealitky GraphQL client + parser. Operational scope (categories,
    complete-walk capability) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            ESTATE_TYPE.get(category.get("estate_type")),
            OFFER_TYPE.get(category.get("offer_type")),
        )

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        # Single-row ingest (ingest_scraped_listing), not batched prepared
        # writes, so the transaction pooler is fine — no session pooler needed.
        return db.connect()

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        offer = category["offer_type"]
        estate = category["estate_type"]
        client = BezrealitkyClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        offset = 0
        total = 0
        pages = 0
        while True:
            adverts, total = client.search(
                offer, estate, limit=INDEX_PAGE_SIZE, offset=offset
            )
            pages += 1
            LOG.info("INDEX offset=%d items=%d total=%d", offset, len(adverts), total)
            for adv in adverts:
                nid = str(adv["id"])
                if nid not in price_map:
                    native_ids.append(nid)
                price_map[nid] = adv.get("price")
            offset += len(adverts)
            if self._max_pages and pages >= self._max_pages:
                break
            if not adverts or offset >= total:
                break

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
            [(n, None, price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, None, price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=bezrealitky new=%d changed=%d unchanged=%d enqueued=%d",
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
    def make_client(self, limiter: RateLimiter) -> BezrealitkyClient:
        return BezrealitkyClient(limiter=limiter)

    def fetch_detail(
        self, client: BezrealitkyClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        try:
            advert = client.get_detail(native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        try:
            listing = parse_advert(advert)
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        return DrainItem(native_id=native_id, kind="ok", payload={"listing": listing})

    def write_details(self, conn: Any, items: list[DrainItem]) -> dict[str, int]:
        counts = {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}
        for it in items:
            listing = it.payload["listing"]
            pk, result = db.ingest_scraped_listing(conn, listing)
            image_urls = listing.raw.get("image_urls") or []
            images = [{"url": u, "sequence": seq} for seq, u in enumerate(image_urls)]
            inserted = db.record_images(conn, pk, images)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        # Complete-walk portal: a gone detail flips that one listing inactive
        # immediately (mirrors sreality), then the runner dequeues it.
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
                "WHERE source = 'bezrealitky' AND claimed_at IS NULL AND given_up = false"
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
    portal: BezrealitkyPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
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
    portal = BezrealitkyPortal(config, max_pages=args.max_pages)

    # Index-walk (enqueue) then detail-drain (fetch + ingest), through the one
    # shared runner. Two scrape_runs rows ('index' + 'detail'), like sreality.
    rc = _run_phase(portal, "index", portal_runner.run_index_walk, args.dry_run)
    if rc == 0:
        rc = _run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=args.max_detail, detail_workers=args.workers,
            detail_rate=args.rate,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="bezrealitky.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; suppresses "
             "mark_inactive). Omit for a full, complete walk.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = drain the queue)",
    )
    p.add_argument("--workers", type=int, default=2, help="detail-fetch workers")
    p.add_argument(
        "--rate", type=float, default=1.0,
        help="detail-fetch requests/second ceiling (default 1.0)",
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
