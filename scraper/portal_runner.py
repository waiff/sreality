"""The one generic portal runner (Phase 4 portal framework).

A single index-walk loop and a single detail-drain loop that work for EVERY
portal. All portal-specific behavior comes from a `Portal` object (the seams
below); there are no per-portal branches in this module. A genuine per-portal
need is an explicit method on the `Portal` protocol — e.g. sreality's
district-split lives inside its `walk_category`, not here — justified in review.

- run_index_walk: per category, walk the index + enqueue new/changed ids into the
  shared listing_detail_queue (source-generic, migration 108); run mark_inactive
  under the completeness guard ONLY for portals that can prove a near-complete
  walk (`supports_complete_walk`, architectural rule #3). Records run_type='index'.
- run_detail_drain: claim a bounded slice of the queue for this source, fetch on a
  rate-limited pool, write in batches via the portal's writer, route gone→inactive
  and error→failure. Records run_type='detail'.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Protocol

from scraper import db
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger("scraper.portal_runner")

# How many claimed listings the batched writer flushes per transaction, and how
# many queue rows a single claim grabs.
DETAIL_BATCH_SIZE = 100
DRAIN_CLAIM_CHUNK = 500


@dataclass
class DrainItem:
    """One claimed listing after its network+parse stage (no DB I/O yet).

    `payload` is portal-specific (sreality: a FetchResult; bazos: a parsed
    ScrapedListing + image rows) and is consumed by `Portal.write_details`.
    """

    native_id: str
    kind: str  # "ok" | "gone" | "error"
    payload: Any = None
    error: str | None = None


class Portal(Protocol):
    """The seams the runner needs. Concrete portals (SrealityPortal, BazosPortal)
    implement this; everything else is shared in this module."""

    source: str
    supports_complete_walk: bool
    index_rate: float

    # --- index-walk seams ---
    def categories(self) -> list[Any]: ...
    def category_labels(self, category: Any) -> tuple[str, str]: ...
    def connect_index(self) -> Any: ...
    def walk_category(
        self, category: Any, conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[Any], dict[str, int], int | None, int, bool]: ...
    def mark_inactive(self, conn: Any, category: Any, seen: set[Any]) -> int: ...
    def active_count(self, conn: Any, category: Any) -> int | None: ...

    # --- detail-drain seams ---
    def connect_drain(self) -> Any: ...
    def make_client(self, limiter: RateLimiter) -> Any: ...
    def fetch_detail(self, client: Any, native_id: str, detail_ref: str | None) -> DrainItem: ...
    def write_details(self, conn: Any, items: list[DrainItem]) -> dict[str, int]: ...
    def mark_gone(self, conn: Any, native_id: str) -> None: ...
    def record_failure(self, conn: Any, native_id: str, message: str) -> None: ...
    def claimable_count(self, conn: Any) -> int: ...


def run_index_walk(
    portal: Portal,
    dry_run: bool,
    run_id: int | None = None,
    max_seconds: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Walk every category, touch + (optionally) mark_inactive, and enqueue
    new/price-changed ids. No detail fetch — the drain consumes the queue.

    When run_id is supplied, index_pages is committed per category (bump) so
    Health liveness survives a SIGKILL before finalize. When max_seconds is
    supplied, the walk stops starting new categories past that wall-clock budget
    and finalizes cleanly — so a slow or grown walk is never SIGKILLed by the
    job timeout (no more 'stuck' runs). Already-walked categories are complete,
    so mark_inactive stays safe (rule #3); the un-walked ones just aren't
    refreshed this run and the next walk picks them up."""
    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    total_pages = 0
    total_index = 0
    total_enqueued = 0
    category_aggregates: list[dict[str, Any]] = []
    limiter = RateLimiter(portal.index_rate)
    conn = None if dry_run else portal.connect_index()

    try:
        for category in portal.categories():
            if deadline is not None and time.monotonic() >= deadline:
                LOG.info(
                    "INDEX time budget %.0fs reached; stopping cleanly before the "
                    "next category (%d walked so far)",
                    max_seconds, len(category_aggregates),
                )
                break
            cm_text, ct_text = portal.category_labels(category)
            LOG.info("CATEGORY start cm=%s ct=%s", cm_text, ct_text)
            try:
                seen_ids, cat_counts, cat_result_size, cat_pages, complete = (
                    portal.walk_category(category, conn, dry_run, limiter)
                )
            except Exception as exc:
                LOG.exception(
                    "CATEGORY walk failed cm=%s ct=%s: %s — skipping sweep",
                    cm_text, ct_text, exc,
                )
                seen_ids, cat_counts, cat_result_size, cat_pages, complete = (
                    set(), {}, None, 0, False,
                )
            total_pages += cat_pages
            if conn is not None and run_id is not None:
                db.bump_index_pages(conn, run_id, cat_pages)
            total_index += len(seen_ids)
            total_enqueued += cat_counts.get("enqueued", 0)

            inactive = 0
            if conn is not None:
                if portal.supports_complete_walk and complete:
                    inactive = portal.mark_inactive(conn, category, seen_ids)
                    LOG.info(
                        "INACTIVE cm=%s ct=%s marked=%d collected=%d result_size=%s",
                        cm_text, ct_text, inactive, len(seen_ids), cat_result_size,
                    )
                elif portal.supports_complete_walk:
                    LOG.warning(
                        "INACTIVE skipped cm=%s ct=%s: walk looks incomplete "
                        "(collected=%d result_size=%s); not flipping to avoid "
                        "false delisting",
                        cm_text, ct_text, len(seen_ids), cat_result_size,
                    )

            active_db: int | None = None
            if conn is not None:
                try:
                    active_db = portal.active_count(conn, category)
                except Exception as exc:
                    LOG.warning(
                        "active_count failed cm=%s ct=%s: %s", cm_text, ct_text, exc
                    )
            LOG.info(
                "RECONCILE cm=%s ct=%s sreality=%s collected=%d active=%s",
                cm_text, ct_text, cat_result_size, len(seen_ids), active_db,
            )

            category_aggregates.append({
                "category_main": cm_text,
                "category_type": ct_text,
                "listings_found_new":   cat_counts.get("found_new", 0),
                "listings_scraped_new": 0,
                "listings_inactive":    inactive,
                "listings_enqueued":    cat_counts.get("enqueued", 0),
                "images_discovered":    0,
                "images_stored":        0,
                "sreality_result_size": cat_result_size,
                "collected":            len(seen_ids),
                "active_db":            active_db,
            })

        LOG.info(
            "INDEX total=%d pages=%d enqueued=%d",
            total_index, total_pages, total_enqueued,
        )
    finally:
        if conn is not None:
            conn.close()

    LOG.info(
        "RUN done pages=%d enqueued=%d inactive=%d",
        total_pages, total_enqueued,
        sum(c["listings_inactive"] for c in category_aggregates),
    )
    scrape_agg: dict[str, Any] = {
        "index_pages":          total_pages,
        "listings_found_new":   sum(c["listings_found_new"] for c in category_aggregates),
        "listings_scraped_new": 0,
        "listings_updated":     0,
        "listings_inactive":    sum(c["listings_inactive"] for c in category_aggregates),
        "images_discovered":    0,
        "errors":               0,
        "by_category":          category_aggregates,
    }
    return (0, scrape_agg)


def _flush_drain_batch(
    portal: Portal,
    conn: Any,
    buffer: list[DrainItem],
    counts: dict[str, int],
    dry_run: bool,
) -> None:
    if not buffer:
        return
    if dry_run:
        for it in buffer:
            LOG.info("DRY-RUN id=%s", it.native_id)
        return
    res = portal.write_details(conn, buffer)
    for k in ("new", "updated", "unchanged", "images_discovered"):
        counts[k] = counts.get(k, 0) + res.get(k, 0)
    db.complete_detail(conn, portal.source, [it.native_id for it in buffer])
    LOG.info(
        "DRAIN flush size=%d new=%d updated=%d unchanged=%d images=%d",
        len(buffer), res.get("new", 0), res.get("updated", 0),
        res.get("unchanged", 0), res.get("images_discovered", 0),
    )


def run_detail_drain(
    portal: Portal,
    max_claims: int | None,
    dry_run: bool,
    detail_workers: int,
    detail_rate: float,
    max_seconds: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Claim queue rows for this source, fetch on a worker pool, write batched.

    `max_seconds` is an optional wall-clock budget: the drain stops claiming new
    chunks once it is exceeded and finalizes cleanly (records ended_at), so a
    write-bound portal can never overrun its GitHub-Actions timeout and leave a
    'stuck' scrape_run. When set, claims use a smaller chunk so the budget is
    checked often enough; the queue persists, so deferred work drains next run.
    """
    counts: dict[str, int] = {
        "new": 0, "updated": 0, "unchanged": 0, "gone": 0, "errors": 0,
        "images_discovered": 0,
    }
    limiter = RateLimiter(detail_rate)
    client = portal.make_client(limiter)

    if dry_run:
        with portal.connect_index() as conn:
            claimable = portal.claimable_count(conn)
        LOG.info("DRAIN dry-run claimable=%d max_claims=%s; exit", claimable, max_claims)
        return (0, {})

    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    claim_chunk = min(DRAIN_CLAIM_CHUNK, 100) if max_seconds else DRAIN_CLAIM_CHUNK
    conn = portal.connect_drain()
    total_claimed = 0
    buffer: list[DrainItem] = []
    try:
        reclaimed = db.reclaim_stale_claims(conn, portal.source)
        if reclaimed:
            LOG.info("DRAIN reclaimed stale claims=%d", reclaimed)
        LOG.info(
            "DRAIN starting source=%s max_claims=%s workers=%d batch=%d budget=%ss",
            portal.source, max_claims, detail_workers, DETAIL_BATCH_SIZE, max_seconds,
        )
        while max_claims is None or total_claimed < max_claims:
            if deadline is not None and time.monotonic() >= deadline:
                LOG.info(
                    "DRAIN time budget %ss reached at claimed=%d; finalizing cleanly",
                    max_seconds, total_claimed,
                )
                break
            chunk = claim_chunk
            if max_claims is not None:
                chunk = min(chunk, max_claims - total_claimed)
            claimed = db.claim_detail_batch(conn, portal.source, chunk)
            if not claimed:
                break
            total_claimed += len(claimed)
            with ThreadPoolExecutor(max_workers=max(1, detail_workers)) as pool:
                futures = {
                    pool.submit(portal.fetch_detail, client, nid, ref): nid
                    for nid, ref, _price in claimed
                }
                for future in as_completed(futures):
                    item = future.result()  # never raises
                    if item.kind == "ok":
                        buffer.append(item)
                        if len(buffer) >= DETAIL_BATCH_SIZE:
                            _flush_drain_batch(portal, conn, buffer, counts, dry_run)
                            buffer = []
                    elif item.kind == "gone":
                        LOG.info("DETAIL id=%s gone (is_active=false)", item.native_id)
                        try:
                            portal.mark_gone(conn, item.native_id)
                        except Exception as exc:
                            LOG.warning(
                                "could not mark id=%s inactive: %s", item.native_id, exc
                            )
                        db.complete_detail(conn, portal.source, [item.native_id])
                        counts["gone"] += 1
                    else:  # error: keep the queue row, bump attempts, log failure
                        LOG.error("DETAIL id=%s error: %s", item.native_id, item.error)
                        portal.record_failure(conn, item.native_id, item.error or "error")
                        db.fail_detail(conn, portal.source, [item.native_id], item.error or "error")
                        counts["errors"] += 1
            LOG.info(
                "DRAIN progress claimed=%d new=%d updated=%d unchanged=%d "
                "gone=%d errors=%d buffered=%d",
                total_claimed, counts["new"], counts["updated"],
                counts["unchanged"], counts["gone"], counts["errors"], len(buffer),
            )
        _flush_drain_batch(portal, conn, buffer, counts, dry_run)
    finally:
        conn.close()

    LOG.info(
        "RUN done pages=0 new=%d updated=%d unchanged=%d gone=%d errors=%d claimed=%d",
        counts["new"], counts["updated"], counts["unchanged"],
        counts["gone"], counts["errors"], total_claimed,
    )
    scrape_agg: dict[str, Any] = {
        "index_pages":          0,
        "listings_found_new":   counts["new"],
        "listings_scraped_new": counts["new"],
        "listings_updated":     counts["updated"],
        "listings_inactive":    counts["gone"],
        "images_discovered":    counts["images_discovered"],
        "errors":               counts["errors"],
        "by_category":          [],
    }
    return (0, scrape_agg)
