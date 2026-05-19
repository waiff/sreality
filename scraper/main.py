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
    python -m scraper.main --images-only         # only run image phase
    python -m scraper.main --max-detail-refetches 2000   # global cap
    python -m scraper.main --max-detail-refetches-per-category 500  # per-cat cap
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

# All sreality category pairs we collect, as (category_main_cb,
# category_type_cb). Rentals first (the established slice), then sales,
# then commercial. Adding/removing a pair is the only knob needed to
# expand or contract scrape coverage; everything downstream
# (parser, db schema, snapshot history) is already category-agnostic.
CATEGORIES: tuple[tuple[int, int], ...] = (
    (1, 2),  # byt / pronajem
    (1, 1),  # byt / prodej
    (2, 2),  # dum / pronajem
    (2, 1),  # dum / prodej
    (4, 2),  # komercni / pronajem
    (4, 1),  # komercni / prodej
)

LOG = logging.getLogger("scraper")

_HREF_ID_RE = re.compile(r"/estates/(\d+)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.images_only:
        if args.dry_run or args.detail_only is not None or args.no_image_downloads:
            LOG.error(
                "--images-only is incompatible with --dry-run, "
                "--detail-only, and --no-image-downloads"
            )
            return 2
        rc = 0
    elif args.detail_only is not None:
        rc = _run_detail_only(
            _build_client(CATEGORIES[0][0], CATEGORIES[0][1]),
            args.detail_only,
            dry_run=args.dry_run,
        )
    else:
        rc = _run_full(
            limit=args.limit,
            dry_run=args.dry_run,
            max_refetches=args.max_detail_refetches,
            max_refetches_per_category=args.max_detail_refetches_per_category,
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

    if (
        rc == 0
        and not args.dry_run
        and not args.no_condition_scoring
        and not args.images_only
    ):
        _run_condition_scoring(max_scores=args.max_condition_scores)

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
        "--images-only",
        action="store_true",
        help=(
            "run only the image-download phase; skip the scrape and "
            "condition-scoring phases. Useful for backfill workflows "
            "that drain the R2 backlog without re-walking the index."
        ),
    )
    p.add_argument(
        "--max-detail-refetches",
        type=int,
        default=None,
        help=(
            "global cap on listing detail fetches this run, shared "
            "across all categories in CATEGORIES order "
            "(default: unlimited; workflow passes 10000)"
        ),
    )
    p.add_argument(
        "--max-detail-refetches-per-category",
        type=int,
        default=None,
        help=(
            "cap on listing detail fetches per category. Combines with "
            "--max-detail-refetches: the effective cap for any single "
            "category is min(per-category, remaining-global). Without "
            "this flag, an early high-volume category (e.g. byt/prodej) "
            "can starve later categories of the shared global budget."
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
    p.add_argument(
        "--no-condition-scoring",
        action="store_true",
        help=(
            "skip the condition-scoring phase even if ANTHROPIC_API_KEY "
            "is configured"
        ),
    )
    p.add_argument(
        "--max-condition-scores",
        type=int,
        default=200,
        help=(
            "cap number of condition scores written this run (default: "
            "200; ~$3/run at the cached rate). Set to 0 to disable."
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_client(category_main: int, category_type: int) -> SrealityClient:
    return SrealityClient(
        category_main=category_main,
        category_type=category_type,
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
    limit: int | None,
    dry_run: bool,
    max_refetches: int | None = None,
    max_refetches_per_category: int | None = None,
) -> int:
    """Walk every category in CATEGORIES sequentially.

    Sharing one DB connection and one mutable refetch-budget across
    categories so the global per-run cap behaves the same as before —
    listings deferred under the cap drain via the existing
    failure-priority path on subsequent runs. The per-category cap is
    layered on top: each category sees `min(remaining-global,
    per-category)` as its effective cap. `--limit` is interpreted as
    a global cap on index entries collected across the whole run, not
    per category.
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0}
    total_pages = 0
    total_index = 0
    refetch_budget = [max_refetches] if max_refetches is not None else [None]

    conn = None if dry_run else db.connect()

    try:
        global_collected = 0
        for category_main, category_type in CATEGORIES:
            cm_text = parser.CATEGORY_MAIN[category_main]
            ct_text = parser.CATEGORY_TYPE[category_type]
            LOG.info("CATEGORY start cm=%s ct=%s", cm_text, ct_text)

            client = _build_client(category_main, category_type)

            remaining_for_limit = (
                None if limit is None else max(0, limit - global_collected)
            )
            seen_ids, cat_counts = _walk_category(
                client,
                conn,
                cat_limit=remaining_for_limit,
                dry_run=dry_run,
                refetch_budget=refetch_budget,
                cat_refetch_cap=max_refetches_per_category,
            )
            global_collected += len(seen_ids)
            total_pages += client.pages_fetched
            total_index += len(seen_ids)
            for k, v in cat_counts.items():
                counts[k] = counts.get(k, 0) + v
            LOG.info(
                "CATEGORY done cm=%s ct=%s seen=%d new=%d updated=%d "
                "unchanged=%d errors=%d",
                cm_text, ct_text, len(seen_ids),
                cat_counts["new"], cat_counts["updated"],
                cat_counts["unchanged"], cat_counts["errors"],
            )

            # Commit inactive-marking per category, immediately after
            # the walk that produced its seen_ids. If a later category
            # crashes mid-walk, this category's marking still survives.
            if conn is not None and limit is None:
                inactive = db.mark_inactive(conn, cm_text, ct_text, seen_ids)
                LOG.info(
                    "INACTIVE cm=%s ct=%s marked=%d",
                    cm_text, ct_text, inactive,
                )

            if limit is not None and global_collected >= limit:
                LOG.info(
                    "INDEX limit=%d reached after category cm=%s ct=%s; "
                    "skipping remaining categories",
                    limit, cm_text, ct_text,
                )
                break

        LOG.info("INDEX total=%d pages=%d", total_index, total_pages)

        if conn is not None and limit is not None:
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
        total_pages,
        counts["new"],
        counts["updated"],
        counts["unchanged"],
        counts["errors"],
    )
    return 0


def _walk_category(
    client: SrealityClient,
    conn: Any,
    cat_limit: int | None,
    dry_run: bool,
    refetch_budget: list[int | None],
    cat_refetch_cap: int | None = None,
) -> tuple[set[int], dict[str, int]]:
    """Walk one category's index + refetch loop.

    `refetch_budget` is a single-element mutable list so the global
    cap decrements as each category consumes refetches. `cat_refetch_cap`
    is the per-category ceiling (None = no per-category limit).
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0}
    index_entries: list[tuple[int, int | None]] = []
    for estate in client.iter_index():
        if cat_limit is not None and len(index_entries) >= cat_limit:
            break
        sid = _extract_id(estate)
        if sid is None:
            LOG.warning("INDEX skipped entry without id")
            continue
        index_entries.append((sid, _extract_price(estate)))

    seen_ids = {sid for sid, _ in index_entries}
    existing = (
        db.index_summary(conn, seen_ids) if conn is not None else {}
    )

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

    if conn is not None and to_refetch:
        failed_ids = db.active_failure_ids(conn, set(to_refetch))
        if failed_ids:
            priority = [s for s in to_refetch if s in failed_ids]
            rest = [s for s in to_refetch if s not in failed_ids]
            to_refetch = priority + rest
            LOG.info("PLAN priority_retry=%d", len(priority))

    caps = [c for c in (refetch_budget[0], cat_refetch_cap) if c is not None]
    if caps:
        cap = min(caps)
        if len(to_refetch) > cap:
            deferred = len(to_refetch) - cap
            to_refetch = to_refetch[:cap]
            LOG.info(
                "PLAN cap=%d deferred=%d (remaining listings will be picked up next run)",
                cap, deferred,
            )

    total_refetch = len(to_refetch)
    if total_refetch:
        LOG.info("DETAIL starting refetch=%d", total_refetch)
    for i, sid in enumerate(to_refetch, start=1):
        outcome = _process_one(client, conn, sid, dry_run=dry_run)
        counts[outcome] = counts.get(outcome, 0) + 1
        if refetch_budget[0] is not None:
            refetch_budget[0] = max(0, refetch_budget[0] - 1)
        if i % 50 == 0:
            LOG.info(
                "DETAIL progress=%d/%d new=%d updated=%d errors=%d",
                i, total_refetch,
                counts["new"], counts["updated"], counts["errors"],
            )

    return seen_ids, counts


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


def _run_condition_scoring(max_scores: int) -> None:
    """Score listings whose latest snapshot has no condition-score row.

    No-op when ANTHROPIC_API_KEY is unset (mirrors the image-download
    phase's gate on R2 env vars) — a misconfigured deploy can't break
    the scrape. No region filter: every newly-changed snapshot gets
    scored as it lands, matching architectural rule #14. Per-listing
    cost at the cached rate is ~$0.014 (after the first ~17.8k-token
    cache write), so the 200-default cap holds nightly spend ~$3.
    """
    if max_scores <= 0:
        LOG.info("SCORE skipped (--max-condition-scores=0)")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        LOG.info("SCORE skipped (ANTHROPIC_API_KEY not set)")
        return

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from toolkit.condition_scoring import ScoringError, score_listing_condition

    with db.connect() as conn:
        pending = _pending_condition_scores(conn, limit=max_scores)
        total = len(pending)
        LOG.info("SCORE pending=%d cap=%d", total, max_scores)
        if total == 0:
            LOG.info("SCORE done scored=0 errors=0 cost=$0.0000")
            return

        providers = {"anthropic": AnthropicProvider()}
        llm_client = LLMClient(conn, providers=providers)

        scored = 0
        errors = 0
        cost_so_far = 0.0
        for i, sid in enumerate(pending, start=1):
            try:
                result = score_listing_condition(
                    conn, llm_client, sreality_id=sid, n_images=0,
                )
            except ScoringError as exc:
                errors += 1
                LOG.warning("SCORE id=%d skipped error=%s", sid, exc)
                continue
            except Exception as exc:
                errors += 1
                LOG.exception("SCORE id=%d crashed: %s", sid, exc)
                continue

            scored += 1
            c = result["data"].get("cost_usd") or 0.0
            if not result["data"].get("cache_hit"):
                cost_so_far += float(c)
            if i % 50 == 0 or i == total:
                LOG.info(
                    "SCORE progress=%d/%d scored=%d errors=%d cost_so_far=$%.4f",
                    i, total, scored, errors, cost_so_far,
                )

    LOG.info(
        "SCORE done scored=%d errors=%d cost=$%.4f",
        scored, errors, cost_so_far,
    )


def _pending_condition_scores(conn: Any, *, limit: int) -> list[int]:
    """Active listings whose latest snapshot has no row in
    `listing_condition_scores`. Mirrors the backfill selection (no
    region filter) so the same idempotent / resumable semantics apply:
    score rows drop out of subsequent runs once written.
    """
    sql = (
        "WITH latest_snapshot AS ( "
        "  SELECT sreality_id, MAX(id) AS snapshot_id "
        "  FROM listing_snapshots GROUP BY sreality_id "
        ") "
        "SELECT l.sreality_id "
        "FROM listings l "
        "JOIN latest_snapshot ls ON ls.sreality_id = l.sreality_id "
        "LEFT JOIN listing_condition_scores cs "
        "  ON cs.sreality_id = ls.sreality_id "
        " AND cs.snapshot_id = ls.snapshot_id "
        "WHERE l.is_active = true "
        "  AND l.last_seen_at > now() - interval '30 days' "
        "  AND cs.id IS NULL "
        "ORDER BY l.last_seen_at DESC "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [int(r[0]) for r in cur.fetchall()]


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
