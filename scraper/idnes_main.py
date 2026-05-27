"""Orchestrator for the reality.idnes.cz crawler (multi-portal).

Runnable as `python -m scraper.idnes_main`. Walks one idnes category's index
(?page=N paging), fetches each listing detail, stages the raw HTML in
`portal_raw_pages`, parses it to a `ScrapedListing`, and feeds it through
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 property matching).

Kept separate from `scraper.main` (the sreality JSON scraper) on purpose, and
modelled on `scraper.bazos_main`. A one-category crawl is a partial walk, so
this NEVER runs mark_inactive (architectural rule #3) — it only upserts.
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from scraper import db
from scraper.idnes_client import IdnesClient, detail_url, index_url
from scraper.idnes_parser import CATEGORY_MAIN, SALE_TYPE, parse_detail, parse_index
from scraper.rate_limit import RateLimiter
from scraper.sreality_client import ListingGoneError

LOG = logging.getLogger(__name__)
SOURCE = "idnes"


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

    client = IdnesClient(limiter=RateLimiter(args.rate))
    conn = None if args.dry_run else db.connect()
    counts = {"new": 0, "updated": 0, "unchanged": 0, "gone": 0, "errors": 0, "images": 0}

    try:
        details = _walk_index(client, conn, args)
        LOG.info("PLAN details=%d", len(details))
        _refetch_details(client, conn, args, details, canon_main, canon_type, counts)
        LOG.info(
            "RUN done details=%d new=%d updated=%d unchanged=%d gone=%d "
            "errors=%d images=%d",
            len(details), counts["new"], counts["updated"],
            counts["unchanged"], counts["gone"], counts["errors"], counts["images"],
        )
    finally:
        if conn is not None:
            conn.close()
    return 0


def _walk_index(
    client: IdnesClient, conn: "psycopg.Connection | None", args: argparse.Namespace
) -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = []
    seen: set[str] = set()
    page = 1
    pages = 0
    while True:
        html, status = client.fetch_index(args.sale_type, args.category, page)
        parsed = parse_index(html)
        pages += 1
        LOG.info(
            "INDEX page=%d items=%d total=%s", page, len(parsed.items), parsed.total
        )
        if conn is not None:
            db.upsert_portal_raw_page(
                conn, source=SOURCE,
                source_id_native=f"{args.sale_type}/{args.category}/{page}",
                source_url=index_url(args.sale_type, args.category, page),
                page_kind="index", html=html, http_status=status,
            )
        for item in parsed.items:
            if item.source_id_native not in seen:
                seen.add(item.source_id_native)
                details.append((item.source_id_native, item.detail_path))
        if args.max_pages and pages >= args.max_pages:
            break
        if not parsed.items or parsed.next_page is None:
            break
        page = parsed.next_page
    return details


def _refetch_details(
    client: IdnesClient,
    conn: "db.psycopg.Connection | None",
    args: argparse.Namespace,
    details: list[tuple[str, str]],
    canon_main: str,
    canon_type: str,
    counts: dict[str, int],
) -> None:
    total = len(details)
    for i, (sid, path) in enumerate(details, 1):
        url = detail_url(path)
        try:
            html, status = client.fetch_detail(path)
        except ListingGoneError:
            counts["gone"] += 1
            LOG.info("DETAIL id=%s gone", sid)
            continue
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            counts["errors"] += 1
            LOG.warning("DETAIL id=%s fetch error: %s", sid, exc)
            continue

        page_id = None
        if conn is not None:
            page_id = db.upsert_portal_raw_page(
                conn, source=SOURCE, source_id_native=sid, source_url=url,
                page_kind="detail", html=html, http_status=status,
            )
        try:
            listing = parse_detail(
                html, source_url=url,
                category_main=canon_main, category_type=canon_type,
            )
            if conn is not None:
                pk, result = db.ingest_scraped_listing(conn, listing)
                image_urls = listing.raw.get("image_urls") or []
                images = [
                    {"url": img_url, "sequence": seq}
                    for seq, img_url in enumerate(image_urls)
                ]
                inserted = db.record_images(conn, pk, images)
                db.mark_portal_page_parsed(conn, page_id)
                counts[result] += 1
                counts["images"] += inserted
                LOG.info("DETAIL id=%s %s images=%d", sid, result, inserted)
            else:
                LOG.info("DETAIL id=%s parsed (dry-run)", sid)
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            LOG.warning("DETAIL id=%s parse/ingest error: %s", sid, exc)
            if conn is not None and page_id is not None:
                db.mark_portal_page_parsed(conn, page_id, parse_error=str(exc))

        if i % 50 == 0:
            LOG.info("DETAIL progress=%d/%d", i, total)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="reality.idnes.cz crawler (multi-portal)")
    p.add_argument("--sale-type", default="prodej", choices=sorted(SALE_TYPE))
    p.add_argument("--category", default="byty", choices=sorted(CATEGORY_MAIN))
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages walked (pilot safety); omit for a full walk",
    )
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
