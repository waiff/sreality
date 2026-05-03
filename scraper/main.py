"""CLI entrypoint for the daily Sreality scraper.

Two-phase scrape: walk the index endpoint to collect listing IDs and
their current prices, then fetch the detail endpoint only for listings
that are new or whose price has changed since the last run. Listings we
already have at the same price get a cheap last_seen_at bump.

After the scrape phase, an optional image-download phase reads pending
image rows and uploads their bytes to Cloudflare R2 (if R2_* env vars
are set; otherwise the phase is a no-op).

Run with:
    python -m scraper.main                       # full run
    python -m scraper.main --limit 10            # cap to 10 listings; mark-inactive skipped
    python -m scraper.main --dry-run             # log only, no DB writes
    python -m scraper.main --detail-only 28...   # one listing
    python -m scraper.main --no-image-downloads  # skip image phase
    python -m scraper.main --max-detail-refetches 2000   # cap details
    python -m scraper.main --max-image-downloads 500     # cap images
    python -m scraper.main --image-workers 16            # tune concurrency

`--limit` is production-safe: the limited scrape upserts what it sees,
but it does NOT mark unseen listings inactive — that inference is only
valid when the entire sreality index has been walked.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from scraper import db, hashing, image_storage, parser
from scraper.sreality_client import SrealityClient

DEFAULT_IMAGE_WORKERS = 8

LOG = logging.getLogger("scraper")

_HREF_ID_RE = re.compile(r"/estates/(\d+)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    client = _build_client()

    if args.detail_only is not None:
        rc = _run_detail_only(
            client, args.detail_only, dry_run=args.dry_run
        )
    else:
        rc = _run_full(
            client,
            limit=args.limit,
            dry_run=args.dry_run,
            max_refetches=args.max_detail_refetches,
        )

    if (
        rc == 0
        and not args.dry_run
        and not args.no_image_downloads
    ):
        _run_image_downloads(
            max_downloads=args.max_image_downloads,
            workers=args.image_workers,
        )

    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scraper", description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "cap number of index entries processed. With this flag the "
            "scrape skips mark-inactive: a partial index view cannot "
            "determine which listings have left sreality."
        ),
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
    p.add_argument(
        "--no-image-downloads",
        action="store_true",
        help="skip the image-download phase even if R2 is configured",
    )
    p.add_argument(
        "--max-detail-refetches",
        type=int,
        default=None,
        help=(
            "cap number of listing detail fetches this run "
            "(default: unlimited; workflow passes mode-specific cap)"
        ),
    )
    p.add_argument(
        "--max-image-downloads",
        type=int,
        default=1000,
        help="cap number of images downloaded this run (default: 1000)",
    )
    p.add_argument(
        "--image-workers",
        type=int,
        default=DEFAULT_IMAGE_WORKERS,
        help=f"concurrent download/upload workers (default: {DEFAULT_IMAGE_WORKERS})",
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
    max_refetches: int | None = None,
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

        # Bump last_seen_at for every existing listing that appeared in
        # this index, before any detail fetches. Index appearance is the
        # source of truth for "still on the market"; if a detail fetch
        # later fails, the timestamp is already correct.
        if conn is not None and existing:
            db.touch_listings(conn, list(existing))

        to_refetch: list[int] = []
        unchanged = 0
        for sid, idx_price in index_entries:
            prev = existing.get(sid)
            if (
                prev is not None
                and idx_price is not None
                and prev["price_czk"] == idx_price
            ):
                unchanged += 1
            else:
                to_refetch.append(sid)

        counts["unchanged"] = unchanged
        LOG.info("PLAN unchanged=%d refetch=%d", unchanged, len(to_refetch))

        # Bump previously-failed listings to the front so the cap doesn't
        # keep deferring them. Failed listings have a row in
        # listing_fetch_failures with given_up=false; they get retried
        # until either succeeding or hitting attempts >= 5.
        if conn is not None and to_refetch:
            failed_ids = db.active_failure_ids(conn, set(to_refetch))
            if failed_ids:
                priority = [s for s in to_refetch if s in failed_ids]
                rest = [s for s in to_refetch if s not in failed_ids]
                to_refetch = priority + rest
                LOG.info("PLAN priority_retry=%d", len(priority))

        if max_refetches is not None and len(to_refetch) > max_refetches:
            deferred = len(to_refetch) - max_refetches
            to_refetch = to_refetch[:max_refetches]
            LOG.info(
                "PLAN cap=%d deferred=%d (remaining listings will be picked up next run)",
                max_refetches, deferred,
            )

        total_refetch = len(to_refetch)
        if total_refetch:
            LOG.info("DETAIL starting refetch=%d", total_refetch)
        for i, sid in enumerate(to_refetch, start=1):
            outcome = _process_one(client, conn, sid, dry_run=dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
            if i % 50 == 0:
                LOG.info(
                    "DETAIL progress=%d/%d new=%d updated=%d errors=%d",
                    i, total_refetch,
                    counts["new"], counts["updated"], counts["errors"],
                )

        if conn is not None:
            if limit is None:
                inactive = db.mark_inactive(conn, seen_ids)
                LOG.info("INACTIVE marked=%d", inactive)
            else:
                LOG.info(
                    "INACTIVE skipped: --limit %d gives a partial index view "
                    "(is_active=false inference requires a full walk)",
                    limit,
                )
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
        _record_failure(conn, sid, "fetch", exc)
        return "errors"

    try:
        row = parser.parse_listing(raw)
        images = parser.parse_images(raw)
        h = hashing.content_hash(raw)
    except Exception as exc:
        LOG.error("DETAIL id=%d parse error: %s", sid, exc)
        _record_failure(conn, sid, "parse", exc)
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
        _clear_failure(conn, sid)
        return result
    except Exception as exc:
        LOG.exception("DETAIL id=%d db error: %s", sid, exc)
        _record_failure(conn, sid, "db", exc)
        return "errors"


def _record_failure(conn: Any, sid: int, source: str, exc: BaseException) -> None:
    """Best-effort: record a fetch failure. Never raises."""
    if conn is None:
        return
    try:
        db.record_fetch_failure(conn, sid, f"{source}: {exc}")
    except Exception as e:
        LOG.warning("could not record failure for id=%d: %s", sid, e)


def _clear_failure(conn: Any, sid: int) -> None:
    """Best-effort: clear an existing failure row. Never raises."""
    if conn is None:
        return
    try:
        db.clear_fetch_failure(conn, sid)
    except Exception as e:
        LOG.warning("could not clear failure for id=%d: %s", sid, e)


def _run_image_downloads(max_downloads: int, workers: int) -> None:
    if not image_storage.is_configured():
        LOG.info("IMAGES skipped (R2 env vars not set)")
        return

    r2 = image_storage.R2Client.from_env()
    counts = {"downloaded": 0, "errors": 0, "attempted": 0}

    with db.connect() as conn:
        pending = db.pending_image_downloads(conn, limit=max_downloads)
        total = len(pending)
        LOG.info(
            "IMAGES pending=%d cap=%d workers=%d",
            total, max_downloads, workers,
        )

        # Worker threads do the network I/O (download + upload) in
        # parallel; the main thread serialises DB writes against the
        # single psycopg connection (psycopg connections are not
        # thread-safe). boto3 S3 clients are thread-safe.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_id = {
                pool.submit(
                    _fetch_one_image, sid, seq, url, r2
                ): image_id
                for image_id, sid, seq, url in pending
            }
            for future in as_completed(future_to_id):
                image_id = future_to_id[future]
                key, error = future.result()
                counts["attempted"] += 1
                if error is None:
                    db.mark_image_stored(conn, image_id, key)
                    counts["downloaded"] += 1
                else:
                    db.mark_image_attempt(conn, image_id)
                    counts["errors"] += 1
                    LOG.warning("IMAGE id=%d error: %s", image_id, error)
                if counts["attempted"] % 50 == 0:
                    LOG.info(
                        "IMAGES progress=%d/%d downloaded=%d errors=%d",
                        counts["attempted"], total,
                        counts["downloaded"], counts["errors"],
                    )

    LOG.info(
        "IMAGES done downloaded=%d errors=%d attempted=%d",
        counts["downloaded"], counts["errors"], counts["attempted"],
    )


def _fetch_one_image(
    sreality_id: int,
    sequence: int | None,
    url: str,
    r2: image_storage.R2Client,
) -> tuple[str, Exception | None]:
    """Worker: download from sreality, upload to R2. Returns (key, error)."""
    key = image_storage.image_key(sreality_id, sequence)
    try:
        data = image_storage.download_image(url)
        r2.upload_bytes(key, data)
        return (key, None)
    except Exception as exc:
        return (key, exc)


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
