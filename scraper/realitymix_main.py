"""Orchestrator for the realitymix.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.realitymix_main`. realitymix is a `Portal`
(RealitymixPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages the HTML search results and enqueues new/price-changed ids
into the shared `listing_detail_queue` (source='realitymix', migration 108), then
a detail-drain that fetches each listing page, parses it to a `ScrapedListing`,
and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1
matching). No bespoke pipeline — only the per-portal fetcher (RealitymixClient) +
parser (realitymix_parser) + config differ from sreality/idnes/ceskereality (the
modularity rule in CLAUDE.md).

Two deliberate differences from the ceskereality template:
- The detail URL (`/detail/{obec}/{slug}-{id}.html`) does NOT encode the
  category, so `parse_detail` derives it from the page's BreadcrumbList JSON-LD
  instead of from the URL — self-contained, one config walks all 12 (cm × ct).
- `walk_category` drives `?stranka` straight to `ceil(total / PER_PAGE)` (the
  page-reported total) rather than trusting a pager "next" arrow, and treats a
  barren page as transient (one retry). This is the lesson from ceskereality's
  reverted #637: an arrow-trusting walk stops early on a throttled/degraded page.
  realitymix is nginx (not Cloudflare) and paginates reliably to the exact total
  with no deep-pagination cap, so a per-category walk can reach the portal's own
  end → `supports_complete_walk` lets the runner nominate the rows the walk did
  not see for a page check (rule #3), source-scoped (rule #15). Coordinates come
  straight from the page's `data-gps-lat/-lon`, so there is no geocoding step.
"""

from __future__ import annotations

import argparse
import logging
from math import ceil
from typing import Any

from scraper import db, portal_runner
from scraper.portal import (
    PortalConfig,
    StopReason,
    default_config,
    deadline_reached,
    load_portal_config,
    classify_index_sighting,
    stop_is_portal_end,
    walk_coverage,
    walk_reached_end,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter
from scraper.realitymix_client import RealitymixClient, detail_url
from scraper.realitymix_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    index_price,
    parse_detail,
    parse_index,
)

LOG = logging.getLogger(__name__)
SOURCE = "realitymix"
PER_PAGE = 20  # realitymix renders 20 results per ?stranka page

# No staleness rail any more (rule #3, 2026-09-07): a walk that reached the
# portal's end nominates the rows it did not see for a page check and the drain's
# fetch decides, so a single walk-miss costs one fetch, never a live listing.
# What licenses that nomination is STRUCTURAL (2026-09-08) -- the page loop
# stopped because realitymix said there is nothing after the page we just read,
# not because our row count reconciled with the declared total. A category one
# row short of its own "z celkem N" is a finished walk over a live index; the
# count is now a report (walk_coverage, logged below and by the runner), never a
# veto.


class RealitymixPortal:
    """realitymix.cz as a Portal: the seams the generic runner needs, wrapping the
    realitymix client + parser. Operational scope (categories, complete-walk
    capability, rates) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct
        # per-(cm, ct) union of complete slices' seen ids + completed-slice
        # counts — the cross-slice nomination buffer (see presence_candidates).
        self._sweep_seen: dict[tuple[str, str], set[str]] = {}
        self._sweep_done: dict[tuple[str, str], int] = {}

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): realitymix's default index
        # order is newest-first, so a page-capped walk IS the delta probe.
        self._max_pages = pages

    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            CATEGORY_MAIN.get(category.get("category")),
            SALE_TYPE.get(category.get("sale_type")),
        )

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        conn = db.connect()
        return conn

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = RealitymixClient(limiter=limiter)

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        total: int | None = None
        pages = 0
        page = 1
        barren_retried = False
        # Every exit below assigns one. "error" is the fail-closed default, so a
        # path added later without a classification reads as OUR stop and the
        # category nominates nothing (same posture as stop_is_portal_end's
        # unknown reason) rather than silently claiming the portal's end.
        stop: StopReason = "error"

        def at_or_past_declared_end(page_no: int) -> bool:
            """The barren rule's POSITION test: is this ?stranka at or past the LAST
            page realitymix's own count implies, or -- with no total ever readable --
            after pages of this category that did carry items?

            The last page the count implies, not the page after it. The loop bounds
            itself at ceil(total/PER_PAGE), so the old `(page-1)*PER_PAGE >= total`
            could never be true for a page the walk actually fetched — which made
            this unsatisfiable in the portal's normal mode and classified its real
            production shape (a 404, or a blank, ON the last declared page, because
            "z celkem N" over-declares by a few rows) as a stop of OURS. That is the
            ceskereality pathology this change exists to remove.
            """
            if total is not None:
                return page_no >= max(1, ceil(total / PER_PAGE))
            return bool(native_ids)

        while True:
            if deadline_reached(deadline):
                # Rule #3: a budget-truncated walk must never authorise a
                # nomination sweep, so record the stop as ours.
                stop = "deadline"
                LOG.info(
                    "INDEX deadline reached sale_type=%s category=%s page=%d "
                    "collected=%d total=%s -> stopping early (walk incomplete)",
                    sale_type, cat, page, len(native_ids), total,
                )
                break
            try:
                html, _ = client.fetch_index(sale_type, cat, page)
            except ListingGoneError:
                # A 404/410 on this ?stranka. At or past what the declared total
                # implies (the "total off by one" case), or with no total ever
                # readable after pages that did carry items, it is realitymix
                # saying there is nothing here. Below the declared last page it
                # is a hole in OUR walk, not an end.
                stop = "empty_confirmed" if at_or_past_declared_end(page) else "error"
                break
            parsed = parse_index(html)
            pages += 1
            # ONLY a page that carried items may move the denominator. The position
            # test above measures against `total`, so an items-less "no results" /
            # degraded render carrying its own smaller (or zero) "z celkem N" would
            # otherwise corroborate ITSELF as the end, mid-walk, on the strength of
            # the page that is the anomaly.
            if parsed.items and parsed.total is not None:
                total = parsed.total
            LOG.info("INDEX page=%d items=%d total=%s", page, len(parsed.items), total)
            new_on_page = 0
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in ref_map:
                    native_ids.append(nid)
                    new_on_page += 1
                ref_map[nid] = detail_url(item.detail_path)
                price_map[nid] = index_price(item.price_text)

            if self._max_pages and pages >= self._max_pages:
                stop = "page_cap"
                break

            last_page = ceil(total / PER_PAGE) if total else None
            # ITEMS-FIRST: an items-less HTTP 200 is what a throttle, a soft block
            # and the page after the last one all look like, so it is OURS until
            # corroborated -- retry the same ?stranka once (the #637 lesson, incl.
            # a throttled FINAL page) and only a still-empty page at/past the
            # declared end counts as realitymix's end. A confirmation that cannot
            # be obtained is not a confirmation: a first page that stays barren
            # with no total is `barren`, and the category nominates nothing.
            if not parsed.items:
                if not barren_retried:
                    barren_retried = True
                    continue
                if page == 1 and not native_ids and parsed.total == 0:
                    # A MEASURED zero, read twice: the portal's own answer for a
                    # category it has nothing in. This is the one place an
                    # items-less page's count is allowed to speak, and only for
                    # the first page of a walk that has collected nothing — never
                    # mid-walk, where a degraded render's small count would put
                    # itself past the end and confirm itself.
                    total = 0
                    stop = "empty_confirmed"
                    break
                stop = (
                    "empty_confirmed" if at_or_past_declared_end(page) else "barren"
                )
                break
            barren_retried = False
            # The ROW-IDENTITY test runs whether or not a total was readable. It used
            # to sit in an `elif` under the declared-total arm, i.e. only in the mode
            # realitymix is never in (it renders "z celkem N" on every results page)
            # — so an edge cache serving page 1's body for every ?stranka paged all
            # the way to `last_page` on 20 ids and reported `declared_total_reached`.
            if page >= 2 and new_on_page == 0:
                if last_page is None or page >= last_page:
                    # Past (or at) the last page the count implies: realitymix
                    # clamping an out-of-range ?stranka back onto its last page. The
                    # weakest terminator -- subset-only, page >= 2.
                    stop = "clamp_repeat"
                else:
                    # BELOW the declared last page the offset simply did not advance
                    # — a cache, a session-pinned result set, a broken ?stranka. That
                    # is OURS: nothing here says we are at the end.
                    LOG.warning(
                        "INDEX sale_type=%s category=%s page=%d added no new id "
                        "while the count says %s rows: the offset did not advance",
                        sale_type, cat, page, total,
                    )
                    stop = "pager_stalled"
                break
            if last_page is not None and page >= last_page:
                # We fetched every page index realitymix's own count says
                # exists -- the portal's number, never our ratio, so a walk a
                # few rows short still ends here.
                stop = "declared_total_reached"
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
            if classify_index_sighting(
                prev, price_map.get(nid), self._price_change_min_pct,
            ) == "unchanged":
                unchanged_pks.append(prev["id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings_by_id(conn, unchanged_pks)

        entries = (
            [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=realitymix new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        # Rule #3 gate, structural since 2026-09-08. A realitymix category is a
        # flat national list (no region split, split_threshold=None), so it is ONE
        # unit and its verdict is this page loop's stop reason. A declared
        # --max-pages / --probe cap makes the run partial by declaration, even on
        # the walk where it ended before the cap fired.
        reasons: list[StopReason] = [stop]
        if self._max_pages:
            reasons.append("page_cap")
        reached_end = walk_reached_end(
            portal_end=all(stop_is_portal_end(r) for r in reasons),
            our_stop=any(not stop_is_portal_end(r) for r in reasons),
        )
        # The count still gets measured every walk -- it just reports now. The
        # runner warns when a reached-end walk is short; this line is the per-slice
        # record of WHICH stop ended it, which the count alone cannot show.
        LOG.info(
            "INDEX walk end sale_type=%s category=%s stop=%s reached_end=%s "
            "collected=%d total=%s pages=%d coverage=%s",
            sale_type, cat, stop, reached_end, len(seen), total, pages,
            walk_coverage(len(seen), total, stopped_early=not reached_end),
        )
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, total, pages, reached_end

    def note_empty_slice(self, category: dict[str, Any]) -> None:
        """A slice the runner refused to nominate from (it saw nothing) still counts
        as one of the group's slices — otherwise a nationwide-empty sibling slug
        (pronajem/chaty on an aggregator) would hold its whole (category_main,
        category_type) group below `expected` for ever, and no row of that group
        would ever be nominated again. It contributes no ids."""
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return
        key = (cm, ct)
        self._sweep_seen.setdefault(key, set())
        self._sweep_done[key] = self._sweep_done.get(key, 0) + 1

    def presence_candidates(
        self, conn: Any, category: dict[str, Any], seen: set[str],
    ) -> tuple[list[tuple[str, str | None, int | None]], int] | None:
        """Nominate this category's unseen rows for a page check (rule #3,
        2026-09-07) -- once per (category_main, category_type), with the UNION
        of its slices' seen ids. Several index slices collapse onto one
        canonical category ('domy' and 'chaty' both -> dum) and the runner calls
        this per slice; nominating from one slice's seen set would nominate the
        sibling's whole population every walk. Buffer, nominate on the group's
        LAST slice to reach the portal's end; a sibling that stopped on one of
        OUR stops never reaches this call, so nothing is nominated this walk and
        the next walk retries."""
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        key = (cm, ct)
        group = self._sweep_seen.setdefault(key, set())
        group.update(seen)
        self._sweep_done[key] = self._sweep_done.get(key, 0) + 1
        expected = sum(1 for c in self._categories if self.category_labels(c) == key)
        if self._sweep_done[key] < expected:
            return None
        return db.presence_candidates(conn, SOURCE, cm, ct, group)

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> RealitymixClient:
        return RealitymixClient(limiter=limiter)

    def fetch_detail(
        self, client: RealitymixClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
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
                # W2a-0 churn instrument: this whole write_details is replayed on
                # a transient pooler drop, so the counter bump inside needs the
                # item's per-fetch token to make the replay a no-op.
            )
            pk, result = db.ingest_scraped_listing(
                conn, p["listing"], discovery_seq=it.discovery_seq,
                discovered_at=it.discovered_at)
            image_urls = p["listing"].raw.get("image_urls") or []
            inserted = db.record_media(conn, pk, image_urls)
            db.mark_portal_page_parsed(conn, page_id)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        # Keyed on the native id directly (not a sreality_id round-trip): post-Gate-2
        # the row's sreality_id is NULL, so the legacy mark_listing_inactive no-ops.
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'realitymix' AND claimed_at IS NULL AND given_up = false"
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




def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    portal = RealitymixPortal(config, max_pages=args.max_pages)

    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No mark_inactive, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

    # realitymix is large (~48k), so production runs the cadence split via the
    # workflows (--index-only feeds --drain-only). A bare combined run (omit both
    # flags) still works for ad-hoc/local use; the split flags are the norm.
    rc = 0
    if not args.drain_only:
        rc = portal_runner.run_phase(
            portal, "index", portal_runner.run_index_walk, args.dry_run,
            max_seconds=args.max_seconds,
        )
    if rc == 0 and not args.index_only:
        rc = portal_runner.run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="realitymix.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; nominates "
             "nothing). Omit for a full walk to the portal's own end.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = per-portal config / drain the queue)",
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
        help="wall-clock budget for EITHER phase (the index walk consumes it as "
             "its per-category deadline); the phase stops claiming + finalizes "
             "cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue + nominate unseen rows for a page check "
             "only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages index page(s) per category, then exit — never "
             "nominates unseen rows for a page check, no detail drain, no "
             "scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="index pages per category for --probe (default 1)",
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
