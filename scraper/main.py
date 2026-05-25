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
from dataclasses import dataclass
from typing import Any

import requests

from scraper import db, hashing, image_storage, parser
from scraper.rate_limit import RateLimiter
from scraper.sreality_client import (
    DISTRICT_IDS,
    GONE_STATUSES,
    SPLIT_THRESHOLD,
    ListingGoneError,
    SrealityClient,
)

DEFAULT_IMAGE_WORKERS = 32
# Detail fetches run on a small thread pool paced by a shared RateLimiter.
# Conservative defaults: the win is hiding per-request latency, not hammering
# the (single-egress-IP) API. Tunable per workflow / dialable down on blocks.
DEFAULT_DETAIL_WORKERS = 4
DEFAULT_DETAIL_RATE = 2.0  # requests/sec, global across all workers

# A full index walk that collected at least this fraction of the total the
# API reported (result_size) is treated as complete enough to drive
# mark_inactive. Below it, the walk likely truncated and flipping unseen
# listings to inactive would falsely delist live ones, so we skip. When
# result_size is unavailable we fall back to trusting the walk (see
# _walk_complete) rather than silently disabling delisting detection.
INDEX_MIN_COMPLETENESS = 0.9

# How many of the most recent download outcomes the suspicious-stop
# heuristic considers. 100 is small enough to react within a minute or
# two at 32-worker throughput, large enough that a few transient
# timeouts don't fire it.
SUSPICIOUS_STOP_WINDOW = 100
# Transient-failure ratio above which we assume sreality is rate-limiting
# or blocking us and bail. Confirmed `listing_taken_down` outcomes do
# NOT count — those are expected on backfill.
SUSPICIOUS_STOP_THRESHOLD = 0.30

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

    # Open a scrape_runs row for any non-dry-run invocation that actually
    # scrapes the index. The image-only backfill (images.yml) is NOT a
    # scrape run — recording it here polluted "last scrape" / the liveness
    # check / reconciliation with index_pages=0 rows, so it's excluded.
    run_id: int | None = None
    if not args.dry_run and not args.images_only:
        run_type = args.run_type or ("delta" if (
            args.limit is not None
            or args.detail_only is not None
        ) else "full")
        try:
            with db.connect() as conn:
                run_id = db.scrape_run_start(conn, run_type)
        except Exception as exc:
            LOG.warning("scrape_run_start failed: %s", exc)

    scrape_agg: dict[str, Any] = {}
    image_agg: dict[str, Any] = {"images_stored": 0, "by_category": {}}
    rc = 0

    # Finalize in a `finally` so a crash mid-run still records a terminal
    # state on the scrape_runs row instead of leaving it orphaned ("stuck"),
    # which freezes the Health liveness + reconciliation.
    try:
        if args.images_only:
            rc = 0
        elif args.detail_only is not None:
            rc, scrape_agg = _run_detail_only(
                _build_client(CATEGORIES[0][0], CATEGORIES[0][1]),
                args.detail_only,
                dry_run=args.dry_run,
            )
        else:
            rc, scrape_agg = _run_full(
                limit=args.limit,
                dry_run=args.dry_run,
                max_refetches=args.max_detail_refetches,
                max_refetches_per_category=args.max_detail_refetches_per_category,
                detail_workers=args.detail_workers,
                detail_rate=args.detail_rate,
                split=not args.no_split,
            )

        if (
            rc == 0
            and not args.dry_run
            and not args.no_image_downloads
        ):
            image_agg = _run_image_downloads(
                max_downloads=args.max_image_downloads,
                workers=args.image_workers,
                active_only=args.images_active_only,
            )
            if image_agg.get("stopped_suspicious"):
                # Exit non-zero so the cron-scheduled backfill workflow
                # re-schedules immediately on its next tick (every 2h).
                # Don't crash the nightly scrape itself — the scrape's
                # other side effects already landed.
                rc = max(rc, 75)

        if (
            rc == 0
            and not args.dry_run
            and not args.no_condition_scoring
            and not args.images_only
        ):
            _run_condition_scoring(max_scores=args.max_condition_scores)
    finally:
        if run_id is not None:
            try:
                with db.connect() as conn:
                    db.scrape_run_finalize(
                        conn, run_id,
                        **_combine_aggregates(scrape_agg, image_agg),
                    )
            except Exception as exc:
                LOG.warning("scrape_run_finalize failed: %s", exc)

    return rc


def _combine_aggregates(
    scrape_agg: dict[str, Any],
    image_agg: dict[str, Any],
) -> dict[str, Any]:
    """Merge scrape-phase + image-phase aggregates into kwargs for finalize."""
    images_by_cat: dict[tuple[str | None, str | None], int] = image_agg.get(
        "by_category", {}
    )
    by_category: list[dict[str, Any]] = list(scrape_agg.get("by_category", []))

    # Apply images_stored counts to matching entries; create rows for
    # categories that only appear in the image phase (--images-only).
    seen_keys = {(c["category_main"], c["category_type"]) for c in by_category}
    for cat_key, stored in images_by_cat.items():
        if cat_key in seen_keys:
            for c in by_category:
                if (c["category_main"], c["category_type"]) == cat_key:
                    c["images_stored"] = stored
                    break
        else:
            cm, ct = cat_key
            by_category.append({
                "category_main": cm,
                "category_type": ct,
                "listings_found_new":   0,
                "listings_scraped_new": 0,
                "listings_inactive":    0,
                "images_discovered":    0,
                "images_stored":        stored,
            })

    return {
        "index_pages":          scrape_agg.get("index_pages", 0),
        "listings_found_new":   scrape_agg.get("listings_found_new", 0),
        "listings_scraped_new": scrape_agg.get("listings_scraped_new", 0),
        "listings_updated":     scrape_agg.get("listings_updated", 0),
        "listings_inactive":    scrape_agg.get("listings_inactive", 0),
        "images_discovered":    scrape_agg.get("images_discovered", 0),
        "images_stored":        image_agg.get("images_stored", 0),
        "errors":               scrape_agg.get("errors", 0),
        "by_category":          by_category,
    }


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
        help=(
            "cap number of images downloaded this run (default: 1000). "
            "Set to 0 for no cap — the phase drains the queue until "
            "empty or the suspicious-stop heuristic fires."
        ),
    )
    p.add_argument(
        "--images-active-only",
        action="store_true",
        help=(
            "restrict the image-download phase to images attached to "
            "currently-active listings, ordered newest-first. Used by "
            "the backfill workflow to prioritise the user-visible "
            "coverage gap; the nightly scrape leaves this off so "
            "inactive listings' photos keep filling in as bandwidth "
            "allows."
        ),
    )
    p.add_argument(
        "--image-workers",
        type=int,
        default=DEFAULT_IMAGE_WORKERS,
        help=f"concurrent download/upload workers (default: {DEFAULT_IMAGE_WORKERS})",
    )
    p.add_argument(
        "--detail-workers",
        type=int,
        default=DEFAULT_DETAIL_WORKERS,
        help=(
            "concurrent detail-fetch workers (default: "
            f"{DEFAULT_DETAIL_WORKERS}). Network I/O runs in parallel; DB "
            "writes stay serial on the main thread."
        ),
    )
    p.add_argument(
        "--detail-rate",
        type=float,
        default=DEFAULT_DETAIL_RATE,
        help=(
            "global detail-fetch rate cap in requests/sec across ALL "
            f"workers (default: {DEFAULT_DETAIL_RATE}). Auto-backs-off on "
            "HTTP 429/403. Dial down if sreality starts blocking."
        ),
    )
    p.add_argument(
        "--no-split",
        action="store_true",
        help=(
            "disable district-splitting of large categories: walk each "
            "category in a single pass even when it exceeds SPLIT_THRESHOLD, "
            "skipping the per-category result-size probe. Cuts request "
            "volume dramatically (a big category goes from ~99 district "
            "walks to one) at the cost of leaving the delisting sweep "
            "disabled for categories too large to walk whole — the "
            "completeness guard simply skips mark_inactive for them. Use to "
            "back off when sreality is rate-limiting or blocking the runner."
        ),
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
    p.add_argument(
        "--run-type",
        choices=("full", "delta"),
        default=None,
        help=(
            "explicit scrape_runs.run_type label. Overrides the "
            "limit-based heuristic — the unified 15-min job does a full "
            "index walk (no --limit) but should still record as 'delta' "
            "so the Health page separates it from the nightly 'full' run."
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_client(
    category_main: int,
    category_type: int,
    limiter: RateLimiter | None = None,
    locality_region_id: int | None = None,
    locality_district_id: int | None = None,
) -> SrealityClient:
    return SrealityClient(
        category_main=category_main,
        category_type=category_type,
        country_id=int(os.environ.get("SREALITY_COUNTRY_ID", 10001)),
        limiter=limiter,
        locality_region_id=locality_region_id,
        locality_district_id=locality_district_id,
    )


def _run_detail_only(
    client: SrealityClient,
    sreality_id: int,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    raw = client.get_detail(sreality_id)
    row = parser.parse_listing(raw)
    images = parser.parse_images(raw)
    h = hashing.content_hash(raw)

    cm_text = row.get("category_main") or "?"
    ct_text = row.get("category_type") or "?"

    if dry_run:
        LOG.info(
            "DRY-RUN id=%d hash=%s images=%d price=%s area=%s",
            sreality_id, h[:8], len(images),
            row.get("price_czk"), row.get("area_m2"),
        )
        LOG.info("RUN done pages=0 new=0 updated=0 unchanged=0 errors=0")
        return (0, {})

    counts = {"new": 0, "updated": 0, "unchanged": 0}
    new_imgs = 0
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
    scrape_agg: dict[str, Any] = {
        "index_pages":          0,
        "listings_found_new":   counts["new"],
        "listings_scraped_new": counts["new"],
        "listings_updated":     counts["updated"],
        "listings_inactive":    0,
        "images_discovered":    new_imgs,
        "errors":               0,
        "by_category": [{
            "category_main": cm_text,
            "category_type": ct_text,
            "listings_found_new":   counts["new"],
            "listings_scraped_new": counts["new"],
            "listings_inactive":    0,
            "images_discovered":    new_imgs,
            "images_stored":        0,
        }],
    }
    return (0, scrape_agg)


def _run_full(
    limit: int | None,
    dry_run: bool,
    max_refetches: int | None = None,
    max_refetches_per_category: int | None = None,
    detail_workers: int = DEFAULT_DETAIL_WORKERS,
    detail_rate: float = DEFAULT_DETAIL_RATE,
    split: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Walk every category in CATEGORIES sequentially.

    Sharing one DB connection and one mutable refetch-budget across
    categories so the global per-run cap behaves the same as before —
    listings deferred under the cap drain via the existing
    failure-priority path on subsequent runs. The per-category cap is
    layered on top: each category sees `min(remaining-global,
    per-category)` as its effective cap. `--limit` is interpreted as
    a global cap on index entries collected across the whole run, not
    per category.

    Returns (rc, scrape_aggregates) where scrape_aggregates carries the
    scrape-phase counters destined for the scrape_runs row. The image-
    download phase counters are added by main() after this returns.
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0}
    total_pages = 0
    total_index = 0
    blocked_categories = 0
    refetch_budget = [max_refetches] if max_refetches is not None else [None]
    category_aggregates: list[dict[str, Any]] = []

    # One shared limiter across all categories' clients + workers, so the
    # request-rate cap is global. Adapts down on 429/403.
    limiter = RateLimiter(detail_rate)

    conn = None if dry_run else db.connect()

    try:
        global_collected = 0
        for category_main, category_type in CATEGORIES:
            cm_text = parser.CATEGORY_MAIN[category_main]
            ct_text = parser.CATEGORY_TYPE[category_type]
            LOG.info("CATEGORY start cm=%s ct=%s", cm_text, ct_text)

            remaining_for_limit = (
                None if limit is None else max(0, limit - global_collected)
            )
            try:
                seen_ids, cat_counts, cat_result_size, cat_pages, complete = (
                    _walk_category_split(
                        category_main,
                        category_type,
                        limiter=limiter,
                        conn=conn,
                        cat_limit=remaining_for_limit,
                        dry_run=dry_run,
                        refetch_budget=refetch_budget,
                        cat_refetch_cap=max_refetches_per_category,
                        detail_workers=detail_workers,
                        split=split,
                    )
                )
            except Exception as exc:
                # A category's walk failing (e.g. sreality throttling that
                # outlasts the retries) must not kill the whole run. Record it
                # as incomplete — the sweep is skipped (no false delisting) and
                # the remaining categories still walk and finalize.
                #
                # An HTTP error here is almost always sreality refusing the
                # request (404/403 anti-bot block on the runner IP), not a code
                # fault, so log it as a one-line warning rather than a stack
                # trace — a blocked run otherwise spams six tracebacks. Genuine
                # unexpected errors still get a full LOG.exception.
                if isinstance(exc, (requests.HTTPError, ListingGoneError)):
                    blocked_categories += 1
                    LOG.warning(
                        "CATEGORY walk blocked cm=%s ct=%s: %s — skipping sweep "
                        "(sreality refused the request; data left untouched)",
                        cm_text, ct_text, exc,
                    )
                else:
                    LOG.exception(
                        "CATEGORY walk failed cm=%s ct=%s: %s — skipping sweep",
                        cm_text, ct_text, exc,
                    )
                seen_ids, cat_counts, cat_result_size, cat_pages, complete = (
                    set(), {}, None, 0, False,
                )
            global_collected += len(seen_ids)
            total_pages += cat_pages
            total_index += len(seen_ids)
            for k, v in cat_counts.items():
                counts[k] = counts.get(k, 0) + v
            LOG.info(
                "CATEGORY done cm=%s ct=%s seen=%d new=%d updated=%d "
                "unchanged=%d errors=%d",
                cm_text, ct_text, len(seen_ids),
                cat_counts.get("new", 0), cat_counts.get("updated", 0),
                cat_counts.get("unchanged", 0), cat_counts.get("errors", 0),
            )

            # Commit inactive-marking per category immediately after its
            # walk. The `complete` flag already folds in walk-completeness
            # (and, for region-split categories, every region being complete),
            # so a truncated walk never flips live listings to inactive.
            inactive = 0
            if conn is not None and limit is None:
                if complete:
                    inactive = db.mark_inactive(conn, cm_text, ct_text, seen_ids)
                    LOG.info(
                        "INACTIVE cm=%s ct=%s marked=%d collected=%d result_size=%s",
                        cm_text, ct_text, inactive, len(seen_ids), cat_result_size,
                    )
                else:
                    LOG.warning(
                        "INACTIVE skipped cm=%s ct=%s: walk looks incomplete "
                        "(collected=%d result_size=%s); not flipping to avoid "
                        "false delisting",
                        cm_text, ct_text, len(seen_ids), cat_result_size,
                    )

            # Reconciliation: record sreality's reported total vs what we
            # collected vs our active DB count so the Health page can surface
            # drift. active_db reflects the post-inactivation state.
            active_db: int | None = None
            if conn is not None:
                try:
                    active_db = db.active_count(conn, cm_text, ct_text)
                except Exception as exc:
                    LOG.warning(
                        "active_count failed cm=%s ct=%s: %s", cm_text, ct_text, exc
                    )
            drift_txt = "n/a"
            if cat_result_size and active_db is not None:
                drift_txt = (
                    f"{100.0 * (active_db - cat_result_size) / cat_result_size:.1f}%"
                )
            LOG.info(
                "RECONCILE cm=%s ct=%s sreality=%s collected=%d active=%s drift=%s",
                cm_text, ct_text, cat_result_size, len(seen_ids), active_db, drift_txt,
            )

            # Delistings detected mid-walk via a gone detail fetch are
            # disjoint from mark_inactive's index-absence sweep (that sweep
            # only flips rows still is_active=true), so summing is safe.
            category_aggregates.append({
                "category_main": cm_text,
                "category_type": ct_text,
                "listings_found_new":   cat_counts.get("found_new", 0),
                "listings_scraped_new": cat_counts.get("new", 0),
                "listings_inactive":    inactive + cat_counts.get("gone", 0),
                "images_discovered":    cat_counts.get("images_discovered", 0),
                "images_stored":        0,
                "sreality_result_size": cat_result_size,
                "collected":            len(seen_ids),
                "active_db":            active_db,
            })

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

    if blocked_categories:
        LOG.warning(
            "RUN blocked categories=%d/%d: sreality refused the index walk "
            "(likely anti-bot IP block on the runner); nothing collected for "
            "those categories and nothing marked inactive. Recovery shows as "
            "'INDEX page=1 estates=...' lines reappearing on a later run.",
            blocked_categories, len(CATEGORIES),
        )

    LOG.info(
        "RUN done pages=%d new=%d updated=%d unchanged=%d gone=%d errors=%d",
        total_pages,
        counts["new"],
        counts["updated"],
        counts["unchanged"],
        counts.get("gone", 0),
        counts["errors"],
    )

    scrape_agg: dict[str, Any] = {
        "index_pages":          total_pages,
        "listings_found_new":   counts.get("found_new", 0),
        "listings_scraped_new": counts["new"],
        "listings_updated":     counts["updated"],
        "listings_inactive":    sum(c["listings_inactive"] for c in category_aggregates),
        "images_discovered":    counts.get("images_discovered", 0),
        "errors":               counts["errors"],
        "by_category":          category_aggregates,
    }
    return (0, scrape_agg)


def _walk_complete(collected: int, result_size: int | None) -> bool:
    """Whether an index walk covered enough of the API-reported total to
    safely drive mark_inactive.

    Only a *positive* signal of incompleteness suppresses the flip: if the
    API didn't report result_size (or reported <= 0) we fall back to
    trusting the walk, matching the pre-existing nightly behaviour, rather
    than silently disabling delisting detection.
    """
    if result_size is None or result_size <= 0:
        return True
    return collected >= result_size * INDEX_MIN_COMPLETENESS


_REFETCH_OUTCOMES = ("new", "updated", "unchanged", "gone", "errors")


def _walk_category_split(
    category_main: int,
    category_type: int,
    *,
    limiter: RateLimiter | None,
    conn: Any,
    cat_limit: int | None,
    dry_run: bool,
    refetch_budget: list[int | None],
    cat_refetch_cap: int | None,
    detail_workers: int,
    split: bool = True,
) -> tuple[set[int], dict[str, int], int | None, int, bool]:
    """Walk one category, splitting large ones by district (okres).

    Sreality caps deep pagination per filter, so a category bigger than
    SPLIT_THRESHOLD can't be retrieved whole in one pass (a coarser region
    split only reached ~86%, below the completeness bar). We probe the
    national total and, if over the threshold, walk each district separately
    and union — every district is well under the cap, so the union is complete
    and mark_inactive can run.

    Returns (seen_ids, counts, result_size, pages_fetched, complete). For a
    split walk `complete` requires EVERY district's own walk to be complete
    AND the union to cover the national probe (so a district missing from
    DISTRICT_IDS can't masquerade as complete — architectural rule #3). A
    single district that errors out is isolated: it marks the category
    incomplete (sweep skipped) but never crashes the run.
    """
    cm_text = parser.CATEGORY_MAIN[category_main]
    ct_text = parser.CATEGORY_TYPE[category_type]

    # --limit runs are partial by definition and never mark_inactive, so
    # there's no reason to split them. --no-split (split=False) forces a
    # single walk too, and skips the probe entirely — with no split decision
    # to make, probing is just an extra request against a possibly-blocked
    # host (client.result_size still latches from page 1 during the walk, so
    # completeness checking is unaffected).
    result_size: int | None = None
    if cat_limit is None and split:
        probe = _build_client(category_main, category_type, limiter=limiter)
        try:
            result_size = probe.probe_result_size()
        except Exception as exc:
            LOG.warning("PROBE failed cm=%s ct=%s: %s", cm_text, ct_text, exc)

    if not split or cat_limit is not None or result_size is None or result_size <= SPLIT_THRESHOLD:
        client = _build_client(category_main, category_type, limiter=limiter)
        seen, counts = _walk_category(
            client, conn, cat_limit, dry_run, refetch_budget,
            cat_refetch_cap, detail_workers,
        )
        rs = client.result_size if client.result_size is not None else result_size
        complete = cat_limit is None and _walk_complete(len(seen), rs)
        return seen, counts, rs, client.pages_fetched, complete

    LOG.info(
        "SPLIT cm=%s ct=%s result_size=%d > %d: walking %d districts",
        cm_text, ct_text, result_size, SPLIT_THRESHOLD, len(DISTRICT_IDS),
    )
    union: set[int] = set()
    counts: dict[str, int] = {}
    summed_drs = 0
    pages = 0
    all_districts_complete = True
    cat_refetched = 0
    for district in DISTRICT_IDS:
        district_cap = (
            None if cat_refetch_cap is None
            else max(0, cat_refetch_cap - cat_refetched)
        )
        dclient = _build_client(
            category_main, category_type, limiter=limiter,
            locality_district_id=district,
        )
        try:
            dseen, dcounts = _walk_category(
                dclient, conn, None, dry_run, refetch_budget,
                district_cap, detail_workers,
            )
        except Exception as exc:
            all_districts_complete = False
            LOG.warning(
                "SPLIT district failed cm=%s ct=%s district=%d: %s",
                cm_text, ct_text, district, exc,
            )
            continue
        union |= dseen
        for k, v in dcounts.items():
            counts[k] = counts.get(k, 0) + v
        drs = dclient.result_size
        if drs is not None:
            summed_drs += drs
        pages += dclient.pages_fetched
        cat_refetched += sum(dcounts.get(o, 0) for o in _REFETCH_OUTCOMES)
        # Empty district (drs 0) is trivially complete; a populated district
        # whose own walk fell short is a truncation.
        if drs is None or len(dseen) < drs * INDEX_MIN_COMPLETENESS:
            all_districts_complete = False
            LOG.warning(
                "SPLIT district incomplete cm=%s ct=%s district=%d collected=%d result_size=%s",
                cm_text, ct_text, district, len(dseen), drs,
            )
    # Complete only if every district fully walked AND the union covers the
    # national total — the latter catches a district missing from DISTRICT_IDS
    # (both union and summed sizes would otherwise drop together).
    complete = all_districts_complete and _walk_complete(len(union), result_size)
    return union, counts, (summed_drs or result_size), pages, complete


def _walk_category(
    client: SrealityClient,
    conn: Any,
    cat_limit: int | None,
    dry_run: bool,
    refetch_budget: list[int | None],
    cat_refetch_cap: int | None = None,
    detail_workers: int = DEFAULT_DETAIL_WORKERS,
) -> tuple[set[int], dict[str, int]]:
    """Walk one category's index + refetch loop.

    `refetch_budget` is a single-element mutable list so the global
    cap decrements as each category consumes refetches. `cat_refetch_cap`
    is the per-category ceiling (None = no per-category limit).
    """
    counts = {
        "new": 0, "updated": 0, "unchanged": 0, "errors": 0, "gone": 0,
        "found_new": 0, "images_discovered": 0,
    }
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

    counts["found_new"] = sum(1 for sid in seen_ids if sid not in existing)

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
    if not total_refetch:
        return seen_ids, counts

    LOG.info(
        "DETAIL starting refetch=%d workers=%d", total_refetch, detail_workers
    )
    # Worker threads do the network I/O (client.get_detail + parse + hash);
    # the main thread serialises DB writes via _write_result against the
    # single, not-thread-safe psycopg connection. Same pattern as the
    # image-download phase. The per-category + global caps were already
    # applied to to_refetch above, so concurrency only changes completion
    # order, never which listings run.
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, detail_workers)) as pool:
        futures = {pool.submit(_fetch_detail, client, sid): sid for sid in to_refetch}
        for future in as_completed(futures):
            fr = future.result()  # _fetch_detail never raises
            outcome, new_imgs = _write_result(conn, fr, dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
            counts["images_discovered"] += new_imgs
            if refetch_budget[0] is not None:
                refetch_budget[0] = max(0, refetch_budget[0] - 1)
            done += 1
            if done % 50 == 0:
                LOG.info(
                    "DETAIL progress=%d/%d new=%d updated=%d gone=%d errors=%d",
                    done, total_refetch,
                    counts["new"], counts["updated"],
                    counts.get("gone", 0), counts["errors"],
                )

    return seen_ids, counts


@dataclass
class FetchResult:
    """Outcome of the network+parse stage of one detail fetch (no DB I/O).

    Produced by worker threads; consumed on the main thread by
    _write_result, which is the only place DB writes happen (psycopg
    connections are not thread-safe).
    """
    sid: int
    kind: str  # "ok" | "gone" | "error"
    row: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    content_hash: str | None = None
    error: BaseException | None = None
    source: str | None = None  # "fetch" | "parse" for kind == "error"


def _fetch_detail(client: SrealityClient, sid: int) -> FetchResult:
    """Worker: fetch + parse + hash one listing. No DB I/O. Never raises.

    Runs on a pool thread, so it must touch neither the psycopg connection
    nor any shared mutable state. Returns everything _write_result needs.
    """
    try:
        raw = client.get_detail(sid)
    except ListingGoneError:
        return FetchResult(sid, "gone")
    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if getattr(exc, "response", None) is not None
            else None
        )
        if status in GONE_STATUSES:
            return FetchResult(sid, "gone")
        return FetchResult(sid, "error", error=exc, source="fetch")
    except Exception as exc:
        return FetchResult(sid, "error", error=exc, source="fetch")

    try:
        row = parser.parse_listing(raw)
        images = parser.parse_images(raw)
        h = hashing.content_hash(raw)
    except Exception as exc:
        return FetchResult(sid, "error", error=exc, source="parse")

    return FetchResult(
        sid, "ok", row=row, raw=raw, images=images, content_hash=h
    )


def _write_result(conn: Any, fr: FetchResult, dry_run: bool) -> tuple[str, int]:
    """Main-thread: apply a FetchResult's DB writes. Returns (outcome, new_imgs).

    The outcome (new/updated/unchanged) is only known after the upsert, so
    it is determined here rather than in the worker.
    """
    if fr.kind == "gone":
        return _handle_gone(conn, fr.sid)
    if fr.kind == "error":
        LOG.error("DETAIL id=%d %s error: %s", fr.sid, fr.source, fr.error)
        _record_failure(conn, fr.sid, fr.source or "fetch", fr.error)
        return ("errors", 0)

    if dry_run:
        LOG.info(
            "DRY-RUN id=%d hash=%s images=%d price=%s",
            fr.sid, (fr.content_hash or "")[:8],
            len(fr.images or []), (fr.row or {}).get("price_czk"),
        )
        return ("unchanged", 0)

    try:
        result = db.upsert_listing(conn, fr.row, fr.raw, fr.content_hash)
        LOG.info("DETAIL id=%d %s", fr.sid, result)
        new_imgs = db.record_images(conn, fr.sid, fr.images)
        if new_imgs:
            LOG.info("IMAGE id=%d inserted=%d", fr.sid, new_imgs)
        _clear_failure(conn, fr.sid)
        return (result, new_imgs)
    except Exception as exc:
        LOG.exception("DETAIL id=%d db error: %s", fr.sid, exc)
        _record_failure(conn, fr.sid, "db", exc)
        return ("errors", 0)


def _process_one(
    client: SrealityClient,
    conn: Any,
    sid: int,
    dry_run: bool,
) -> tuple[str, int]:
    """Serial fetch+write for one listing. Used by the single-listing paths
    and tests; the pooled walk calls _fetch_detail / _write_result directly."""
    return _write_result(conn, _fetch_detail(client, sid), dry_run)


def _handle_gone(conn: Any, sid: int) -> tuple[str, int]:
    """A delisted listing: flip is_active=false and clear any failure row.

    A gone detail fetch is evidence of delisting, not a transient failure,
    so it must not accumulate in listing_fetch_failures (which would burn
    the 5-attempt budget and then strand the listing as given_up). Returns
    the 'gone' outcome so the walk counts it separately from errors.
    """
    LOG.info("DETAIL id=%d gone (is_active=false)", sid)
    if conn is None:
        return ("gone", 0)
    try:
        db.mark_listing_inactive(conn, sid)
    except Exception as exc:
        LOG.warning("could not mark id=%d inactive: %s", sid, exc)
    _clear_failure(conn, sid)
    return ("gone", 0)


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


def _run_image_downloads(
    max_downloads: int,
    workers: int,
    active_only: bool = False,
) -> dict[str, Any]:
    """Drain pending image downloads. Returns aggregates for scrape_runs.

    Loops in batches until either (a) the pending queue is empty,
    (b) the per-run cap is reached (if max_downloads > 0), or (c) the
    transient-failure rate over the last SUSPICIOUS_STOP_WINDOW
    outcomes exceeds SUSPICIOUS_STOP_THRESHOLD — the operator's
    "if other failures get suspicious, stop and try again in 2h" rule.

    `max_downloads=0` means "no cap": drain the queue. The nightly
    `scrape.yml` uses this; the backfill workflow uses a finite cap as
    a runtime guardrail.

    On a 404 from sreality's CDN, we call `freshness_check` on the
    parent listing (one HTTP per gone listing, cached within this run)
    so listings that have actually been taken down get flipped to
    `is_active = false` and ALL of their remaining pending images get
    marked `unavailable_reason = 'listing_taken_down'` in one shot.
    These outcomes don't count toward the suspicious-stop heuristic.

    Per-category buckets are derived from each image's parent listing
    so the phase populates scrape_runs.by_category even when run
    standalone via --images-only.
    """
    if not image_storage.is_configured():
        LOG.info("IMAGES skipped (R2 env vars not set)")
        return {"images_stored": 0, "by_category": {}, "stopped_suspicious": False}

    from collections import deque

    from scraper.sreality_client import SrealityClient

    r2 = image_storage.R2Client.from_env(max_pool_connections=workers)
    counts = {
        "downloaded": 0, "errors": 0, "attempted": 0,
        "taken_down": 0, "source_unavailable": 0,
    }
    by_cat: dict[tuple[str | None, str | None], int] = {}
    # Per-listing classification cache for THIS run, so we never call
    # freshness_check more than once per gone listing.
    gone_listings: set[int] = set()
    alive_listings: set[int] = set()
    outcome_window: deque[str] = deque(maxlen=SUSPICIOUS_STOP_WINDOW)
    stopped_suspicious = False
    # One reusable client; SrealityClient is stateless beyond its
    # category settings, and freshness_check ignores category.
    freshness_client = SrealityClient(category_main=1, category_type=2)
    # Batch size — large enough that worker pool stays saturated,
    # small enough that we re-query often and pick up freshly-discovered
    # active images during continuous runs.
    batch_size = 1000

    def _remaining_cap() -> int | None:
        if max_downloads <= 0:
            return None
        return max(0, max_downloads - counts["attempted"])

    with db.connect() as conn:
        LOG.info(
            "IMAGES start cap=%s workers=%d active_only=%s",
            "unlimited" if max_downloads <= 0 else max_downloads,
            workers, active_only,
        )

        # Loop draining one batch at a time. Re-querying between
        # batches picks up rows whose freshness check just classified
        # them away (we exclude unavailable_reason IS NOT NULL).
        while True:
            remaining = _remaining_cap()
            if remaining == 0:
                LOG.info("IMAGES cap reached at attempted=%d", counts["attempted"])
                break
            this_batch = min(batch_size, remaining or batch_size)
            pending = db.pending_image_downloads(
                conn, limit=this_batch, active_only=active_only,
            )
            if not pending:
                break

            cat_lookup: dict[int, tuple[str | None, str | None]] = {}
            sid_by_image: dict[int, int] = {}

            # Skip images whose parent listing is already known gone
            # — we'd just re-mark them and waste an HTTP call.
            filtered_pending: list[tuple[Any, ...]] = []
            for image_id, sid, seq, url, cm, ct in pending:
                if sid in gone_listings:
                    continue
                cat_lookup[image_id] = (cm, ct)
                sid_by_image[image_id] = sid
                filtered_pending.append((image_id, sid, seq, url, cm, ct))

            if not filtered_pending:
                continue

            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_id = {
                    pool.submit(
                        _fetch_one_image, sid, seq, url, r2
                    ): image_id
                    for image_id, sid, seq, url, _cm, _ct in filtered_pending
                }
                for future in as_completed(future_to_id):
                    image_id = future_to_id[future]
                    sid = sid_by_image[image_id]
                    key, error = future.result()
                    counts["attempted"] += 1

                    if error is None:
                        db.mark_image_stored(conn, image_id, key)
                        counts["downloaded"] += 1
                        outcome_window.append("ok")
                        cat_key = cat_lookup.get(image_id, (None, None))
                        by_cat[cat_key] = by_cat.get(cat_key, 0) + 1
                    else:
                        kind = _classify_image_failure(
                            conn, freshness_client, sid, error,
                            gone_listings=gone_listings,
                            alive_listings=alive_listings,
                        )
                        if kind == "taken_down":
                            n = db.mark_image_listing_taken_down(conn, sid)
                            counts["taken_down"] += n
                            outcome_window.append("taken_down")
                            LOG.info(
                                "IMAGE listing_taken_down sid=%d marked=%d",
                                sid, n,
                            )
                        elif kind == "source_unavailable":
                            # Permanently-dead CDN URL on a live listing —
                            # mark this image so it leaves the queue; NOT a
                            # transient failure (doesn't trip suspicious-stop).
                            db.mark_image_unavailable(
                                conn, image_id, "source_unavailable",
                                error=str(error),
                            )
                            counts["source_unavailable"] += 1
                            outcome_window.append("source_unavailable")
                            LOG.info(
                                "IMAGE source_unavailable id=%d", image_id
                            )
                        else:
                            db.mark_image_attempt(conn, image_id, error=str(error))
                            counts["errors"] += 1
                            outcome_window.append("transient")
                            LOG.warning("IMAGE id=%d error: %s", image_id, error)

                    if counts["attempted"] % 50 == 0:
                        LOG.info(
                            "IMAGES progress=%d downloaded=%d errors=%d "
                            "taken_down=%d source_unavailable=%d",
                            counts["attempted"], counts["downloaded"],
                            counts["errors"], counts["taken_down"],
                            counts["source_unavailable"],
                        )

                    if _suspicious_stop(outcome_window):
                        stopped_suspicious = True
                        break

            if stopped_suspicious:
                LOG.warning(
                    "IMAGES STOP suspicious — transient-failure rate over "
                    "last %d outcomes exceeded %.0f%%. Cron will retry.",
                    SUSPICIOUS_STOP_WINDOW,
                    SUSPICIOUS_STOP_THRESHOLD * 100,
                )
                break

    LOG.info(
        "IMAGES done downloaded=%d errors=%d taken_down=%d "
        "source_unavailable=%d attempted=%d",
        counts["downloaded"], counts["errors"],
        counts["taken_down"], counts["source_unavailable"], counts["attempted"],
    )
    return {
        "images_stored": counts["downloaded"],
        "by_category": by_cat,
        "stopped_suspicious": stopped_suspicious,
    }


def _suspicious_stop(outcomes: "Any") -> bool:
    """True when transient-failure ratio over the window exceeds threshold.

    Requires a full window before firing so a handful of failures
    early in a run don't trip the wire.
    """
    if len(outcomes) < SUSPICIOUS_STOP_WINDOW:
        return False
    transient = sum(1 for o in outcomes if o == "transient")
    return transient / len(outcomes) > SUSPICIOUS_STOP_THRESHOLD


def _classify_image_failure(
    conn: Any,
    client: "Any",
    sreality_id: int,
    error: Exception,
    *,
    gone_listings: set[int],
    alive_listings: set[int],
) -> str:
    """Classify an image download failure as 'taken_down', 'source_unavailable',
    or 'transient'.

    A 404/410 on the image URL means that URL is permanently dead (sreality
    CDN URLs expire). If the parent listing is also gone we bulk-mark all its
    images 'taken_down'; if the listing is still alive it's just this one URL
    that expired → 'source_unavailable' (mark this image only). Either way it
    is NOT a transient failure: retrying never succeeds and it must not count
    toward the suspicious-stop ratio. Other errors (5xx, timeout, connection
    reset, R2 failures) are 'transient'. Per-run caches keep each liveness
    verdict to at most one freshness_check.
    """
    if not _is_gone_image_error(error):
        return "transient"
    if sreality_id in gone_listings:
        return "taken_down"
    if sreality_id in alive_listings:
        return "source_unavailable"
    try:
        result = client_freshness_check(conn, client, sreality_id)
    except Exception as exc:
        LOG.warning(
            "IMAGE freshness check failed sid=%d: %s — treating as transient",
            sreality_id, exc,
        )
        return "transient"
    if result == "gone":
        gone_listings.add(sreality_id)
        return "taken_down"
    alive_listings.add(sreality_id)
    return "source_unavailable"


def _is_gone_image_error(error: Exception) -> bool:
    """True iff the exception is an HTTP 404/410 from sreality's CDN."""
    import requests

    if isinstance(error, requests.HTTPError):
        resp = getattr(error, "response", None)
        if resp is not None and resp.status_code in (404, 410):
            return True
    return False


def client_freshness_check(conn: Any, client: "Any", sreality_id: int) -> str:
    """Thin wrapper so tests can monkeypatch one symbol."""
    from scraper.freshness import freshness_check

    result = freshness_check(conn, client, sreality_id)
    return result["outcome"]


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
