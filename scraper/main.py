"""CLI entrypoint for the daily Sreality scraper.

Two-phase scrape: walk the index endpoint to collect listing IDs and
their current prices, then fetch the detail endpoint only for listings
that are new or whose price has changed since the last run. Listings we
already have at the same price get a cheap last_seen_at bump.

Run with:
    python -m scraper.main                 # full run
    python -m scraper.main --limit 10      # cap to 10 listings
    python -m scraper.main --dry-run       # log only, no DB writes
    python -m scraper.main --detail-only 2836292428
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any

from scraper import db, hashing, parser
from scraper.sreality_client import SrealityClient

LOG = logging.getLogger("scraper")

_HREF_ID_RE = re.compile(r"/estates/(\d+)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    client = _build_client()

    if args.detail_only is not None:
        return _run_detail_only(client, args.detail_only, dry_run=args.dry_run)
    return _run_full(client, limit=args.limit, dry_run=args.dry_run)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scraper", description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap number of index entries processed (testing)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would be written but make no DB writes",
    )
    p.add_argument(
        "--detail-only",
        type=int,
        default=None,
        metavar="SREALITY_ID",
        help="fetch and write a single listing by id; skip the index phase",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_client() -> SrealityClient:
    return SrealityClient(
        category_main=int(os.environ.get("SREALITY_CATEGORY_MAIN", 1)),
        category_type=int(os.environ.get("SREALITY_CATEGORY_TYPE", 2)),
        country_id=int(os.environ.get("SREALITY_COUNTRY_ID", 10001)),
    )


def _run_detail_only(
    client: SrealityClient,
    sreality_id: int,
    dry_run: bool,
) -> int:
    raw = client.get_detail(sreality_id)
    row = parser.parse_listing(raw)
    images = parser.parse_images(raw)
    h = hashing.content_hash(raw)

    if dry_run:
        LOG.info(
            "DRY-RUN id=%d hash=%s images=%d price=%s area=%s",
            sreality_id, h[:8], len(images),
            row.get("price_czk"), row.get("area_m2"),
        )
        LOG.info("RUN done pages=0 new=0 updated=0 unchanged=0 errors=0")
        return 0

    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with db.connect() as conn:
        result = db.upsert_listing(conn, row, raw, h)
        counts[result] = 1
        LOG.info("DETAIL id=%d %s", sreality_id, result)
        new_imgs = db.record_images(conn, sreality_id, images)
        if new_imgs:
            LOG.info("IMAGE id=%d inserted=%d", sreality_id, new_imgs)
    LOG.info(
        "RUN done pages=0 new=%d updated=%d unchanged=%d errors=0",
        counts["new"], counts["updated"], counts["unchanged"],
    )
    return 0


def _run_full(
    client: SrealityClient,
    limit: int | None,
    dry_run: bool,
) -> int:
    counts = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0}

    index_entries: list[tuple[int, int | None]] = []
    for estate in client.iter_index():
        if limit is not None and len(index_entries) >= limit:
            break
        sid = _extract_id(estate)
        if sid is None:
            LOG.warning("INDEX skipped entry without id")
            continue
        index_entries.append((sid, _extract_price(estate)))

    LOG.info(
        "INDEX total=%d pages=%d", len(index_entries), client.pages_fetched
    )

    seen_ids = {sid for sid, _ in index_entries}
    conn = None if dry_run else db.connect()

    try:
        existing = (
            db.index_summary(conn, seen_ids) if conn is not None else {}
        )

        to_touch: list[int] = []
        to_refetch: list[int] = []
        for sid, idx_price in index_entries:
            prev = existing.get(sid)
            if (
                prev is not None
                and idx_price is not None
                and prev["price_czk"] == idx_price
            ):
                to_touch.append(sid)
            else:
                to_refetch.append(sid)

        LOG.info(
            "PLAN touch=%d refetch=%d", len(to_touch), len(to_refetch)
        )

        if conn is not None and to_touch:
            touched = db.touch_listings(conn, to_touch)
            counts["unchanged"] = touched

        for sid in to_refetch:
            outcome = _process_one(client, conn, sid, dry_run=dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1

        if conn is not None:
            inactive = db.mark_inactive(conn, seen_ids)
            LOG.info("INACTIVE marked=%d", inactive)
    finally:
        if conn is not None:
            conn.close()

    LOG.info(
        "RUN done pages=%d new=%d updated=%d unchanged=%d errors=%d",
        client.pages_fetched,
        counts["new"],
        counts["updated"],
        counts["unchanged"],
        counts["errors"],
    )
    return 0


def _process_one(
    client: SrealityClient,
    conn: Any,
    sid: int,
    dry_run: bool,
) -> str:
    try:
        raw = client.get_detail(sid)
    except Exception as exc:
        LOG.error("DETAIL id=%d fetch error: %s", sid, exc)
        return "errors"

    try:
        row = parser.parse_listing(raw)
        images = parser.parse_images(raw)
        h = hashing.content_hash(raw)
    except Exception as exc:
        LOG.error("DETAIL id=%d parse error: %s", sid, exc)
        return "errors"

    if dry_run:
        LOG.info(
            "DRY-RUN id=%d hash=%s images=%d price=%s",
            sid, h[:8], len(images), row.get("price_czk"),
        )
        return "unchanged"

    try:
        result = db.upsert_listing(conn, row, raw, h)
        LOG.info("DETAIL id=%d %s", sid, result)
        new_imgs = db.record_images(conn, sid, images)
        if new_imgs:
            LOG.info("IMAGE id=%d inserted=%d", sid, new_imgs)
        return result
    except Exception as exc:
        LOG.exception("DETAIL id=%d db error: %s", sid, exc)
        return "errors"


def _extract_id(estate: dict[str, Any]) -> int | None:
    hid = estate.get("hash_id")
    if isinstance(hid, int):
        return hid
    if isinstance(hid, str) and hid.isdigit():
        return int(hid)
    href = ((estate.get("_links") or {}).get("self") or {}).get("href", "")
    match = _HREF_ID_RE.search(href)
    return int(match.group(1)) if match else None


def _extract_price(estate: dict[str, Any]) -> int | None:
    pc = estate.get("price_czk")
    if isinstance(pc, dict):
        v = pc.get("value_raw")
        if isinstance(v, (int, float)):
            return int(v)
    p = estate.get("price")
    if isinstance(p, (int, float)):
        return int(p)
    return None


if __name__ == "__main__":
    sys.exit(main())
