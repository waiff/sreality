"""Orchestrator for the remax-czech.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.remax_main`. RE/MAX is a `Portal` (RemaxPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that pages the
search results and enqueues new/price-changed ids into the shared
`listing_detail_queue` (source='remax', migration 108), then a detail-drain that
fetches each listing page, parses it to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + singleton property). No bespoke
pipeline — only the per-portal fetcher (RemaxClient) + parser (remax_parser) +
config differ (the modularity rule in CLAUDE.md).

remax exposes its catalogue as TWO mixed indexes — sale (`sale=1`) and rent
(`sale=2`) — each spanning every property category with no per-category URL. The
config descriptors are therefore per (category_main, category_type): each
`walk_category` walks (or reuses, via an agenda-level cache) that offer-type's
full index, then keeps the slice whose card-title category maps to the
descriptor. This gives the runner real (cm, ct) labels — the Health
reconciliation joins listings on those — while fetching each agenda's pages only
once per run. The drain re-derives each listing's category from the detail page
("Typ nemovitosti" + title), so the queue stays category-agnostic.

remax is `supports_complete_walk=true` via AGENDA-GRAIN nomination: it reports a
per-AGENDA total (the per-category slice is title-derived, not a portal-reported
per-(cm,ct) total), so the runner can't gate a per-category sweep — instead the
whole agenda (sale ≡ category_type) nominates every active row it did not see for
a page check once the walk REACHED THE PORTAL'S END (rule #3, structural: remax
said there is nothing after this page and no stop of OURS fired; the collected /
declared ratio is logged, never a gate), scoped by category_type against the full
walk's id set (`db.presence_candidates` with category_main=None), never the
title-derived slice. A gone detail fetch still flips that one listing at once.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.location import CoordResolver
from scraper.portal import (
    PortalConfig,
    StopReason,
    default_config,
    load_portal_config,
    classify_index_sighting,
    deadline_reached, stop_is_portal_end, walk_coverage, walk_reached_end,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter
from scraper.remax_client import RemaxClient, detail_url, index_url
from scraper.remax_parser import category_of, index_price, parse_detail, parse_index

LOG = logging.getLogger(__name__)
SOURCE = "remax"
_PAGE_SIZE = 21  # remax serves 21 cards per search page


class _AgendaWalk:
    """One offer-type's (sale or rent) collected index, walked once and shared
    across that agenda's per-category descriptors."""

    def __init__(
        self, native_ids: list[str], ref_map: dict[str, str],
        price_map: dict[str, int | None], cat_map: dict[str, str | None],
        total: int | None, pages: int, stop_reason: StopReason, reached_end: bool,
    ) -> None:
        self.native_ids = native_ids
        self.ref_map = ref_map
        self.price_map = price_map
        self.cat_map = cat_map  # id -> category_main (from the card title)
        self.total = total
        self.pages = pages
        # Why the page loop stopped (StopReason) and the structural verdict read
        # off it (rule #3): the gate for agenda-grain nomination, never a count.
        self.stop_reason = stop_reason
        self.reached_end = reached_end


class RemaxPortal:
    """remax-czech.cz as a Portal: the seams the generic runner needs, wrapping
    the remax client + parser. Two mixed indexes (sale=1 / sale=2); each config
    descriptor pairs a category with its offer-type flag and keeps the
    title-derived slice for its category."""

    source = SOURCE
    index_rate = 1.0

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct
        self._agenda_cache: dict[int, _AgendaWalk] = {}
        self._swept_agendas: set[int] = set()  # delist each agenda once per run
        # page > carry-forward > geocode (remax pages without a data-gps pair had
        # NO coords path until now).
        self._coords = CoordResolver(SOURCE)

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): remax's default index
        # order is newest-first, so a page-capped agenda walk IS the delta
        # probe. The agenda cache holds a walk taken at the OLD cap, so a cap
        # change must drop it — otherwise a deepened probe would replay the
        # shallower cached agenda.
        if pages != self._max_pages:
            self._agenda_cache.clear()
        self._max_pages = pages

    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (category.get("category_main"), category.get("category_type"))

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        conn = db.connect()
        self._coords.preload(conn)
        return conn

    def _walk_agenda(
        self, sale: int, conn: Any, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[_AgendaWalk, int]:
        """Walk one offer-type's full mixed index once; cache it for the agenda's
        other category descriptors. Returns (walk, pages_fetched_this_call) so the
        runner counts each agenda's pages exactly once (0 on a cache hit)."""
        cached = self._agenda_cache.get(sale)
        if cached is not None:
            return cached, 0

        client = RemaxClient(limiter=limiter)
        # Location-data W0 item 0n: the index card's data-display-address is
        # the ONLY house-number-bearing remax surface — archive it. Keys are
        # week-stamped (accumulate, don't roll over); the preloaded fresh set
        # skips re-uploading bodies the server-side guard would discard; and a
        # page-capped walk (self._max_pages — the realtime delta probe every
        # ~3 min) never archives, so a transient probe fetch can't claim a
        # page's daily slot ahead of the full 6h walk.
        archive_ok = conn is not None and not self._max_pages
        week = db.index_archive_week()
        fresh: set[str] = set()
        if archive_ok:
            try:
                fresh = db.fresh_index_page_keys(
                    conn, SOURCE, hours=db.INDEX_ARCHIVE_REFRESH_HOURS
                )
            except Exception as exc:  # noqa: BLE001 - optimisation only
                LOG.warning("INDEX archive preload failed: %s", exc)
        native_ids: list[str] = []
        ref_map: dict[str, str] = {}
        price_map: dict[str, int | None] = {}
        cat_map: dict[str, str | None] = {}
        total: int | None = None
        pages = 0
        page = 1
        stop_reason: StopReason | None = None
        while True:
            if deadline_reached(deadline):
                stop_reason = "deadline"
                LOG.info(
                    "INDEX deadline sale=%d stopping before page=%d pages=%d collected=%d total=%s",
                    sale, page, pages, len(native_ids), total,
                )
                break
            html, status = client.fetch_index(sale=sale, stranka=page)
            key = f"{sale}/{page}/{week}"
            if archive_ok and key not in fresh:
                try:
                    db.upsert_portal_raw_page(
                        conn,
                        source=SOURCE,
                        source_id_native=key,
                        source_url=index_url(sale, page),
                        page_kind="index",
                        html=html,
                        http_status=status,
                        refresh_after_hours=db.INDEX_ARCHIVE_REFRESH_HOURS,
                    )
                    fresh.add(key)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning(
                        "INDEX archive failed sale=%d page=%d: %s", sale, page, exc
                    )
            parsed = parse_index(html)
            pages += 1
            # ONLY a page that carried cards may move the denominator. remax's
            # "zobrazeno N z celkem M" line renders on a lost-filter / backend-error
            # page too (and `_TOTAL_RE` happily reads "z celkem 0"), so letting an
            # items-less page update `total` let the suspect page manufacture its own
            # position corroboration in _classify_empty_page.
            if parsed.items and parsed.total is not None:
                total = parsed.total
            LOG.info("INDEX sale=%d page=%d items=%d total=%s", sale, page, len(parsed.items), total)
            new_on_page = 0
            for item in parsed.items:
                nid = item.source_id_native
                if nid not in ref_map:
                    native_ids.append(nid)
                    new_on_page += 1
                ref_map[nid] = detail_url(item.detail_path)
                # Same clamps as the stored price so the unchanged-compare can't
                # see a value the write boundary would have nulled.
                price_map[nid] = db.sane_price_czk(index_price(item.price_text))
                cat_map[nid] = category_of(None, item.title)
            if self._max_pages and pages >= self._max_pages:
                stop_reason = "page_cap"
                break
            # Items-first: a page with zero cards is a soft block / blank-shell
            # 200 until it is corroborated, and it looks exactly like the page
            # after the last one — so it can no longer share one break with the
            # portal's real end signals (that conflation is what the numeric gate
            # was silently defending against).
            if not parsed.items:
                stop_reason = self._classify_empty_page(
                    client, sale, page, total=total, saw_items=bool(native_ids),
                )
                break
            if new_on_page == 0:
                # It was read as remax clamping an out-of-range ?stranka=N back onto
                # a valid page. The live probe (2026-09-08) refutes that: page 81 of
                # an 80-page agenda answers HTTP 200 with zero cards and no redirect
                # — remax does not clamp. So a page of cards that adds no id is an
                # edge-cached body, a session reset or an index that reshuffled by a
                # full page, none of which says we are at the tail. The loop still
                # has to break; it must not nominate.
                LOG.warning(
                    "INDEX sale=%d page=%d added no new card; the list did not "
                    "advance (collected=%d total=%s)",
                    sale, page, len(native_ids), total,
                )
                stop_reason = "pager_stalled" if pages >= 2 else "barren"
                break
            if total is not None and len(native_ids) >= total:
                stop_reason = "declared_total_reached"
                break
            page += 1

        # The gate is STRUCTURAL (rule #3): did remax end this page loop, and did
        # no stop of ours fire? remax's index is ONE national list per agenda, so
        # the agenda has a single unit and its stop reason IS the category's — a
        # walk that ends one row short of the reported total has still reached the
        # portal's last page and may nominate. A reasonless exit (unreachable
        # today: every break classifies itself) reads as a unit we never walked.
        # The walk is cached, so the whole agenda — every one of its category
        # descriptors — inherits this verdict.
        reason: StopReason = stop_reason or "slice_unreached"
        portal_end = stop_is_portal_end(reason)
        reached_end = walk_reached_end(portal_end=portal_end, our_stop=not portal_end)
        LOG.info(
            "INDEX done sale=%d pages=%d collected=%d total=%s stop=%s reached_end=%s",
            sale, pages, len(native_ids), total, reason, reached_end,
        )
        walk = _AgendaWalk(
            native_ids, ref_map, price_map, cat_map, total, pages, reason, reached_end)
        self._agenda_cache[sale] = walk
        return walk, pages

    def _classify_empty_page(
        self, client: RemaxClient, sale: int, page: int, *,
        total: int | None, saw_items: bool,
    ) -> StopReason:
        """Is this items-less page remax's end, or a soft block? (the barren rule)

        Re-read the SAME url once, through the limiter: only a page that is still
        empty AND sits past the last page the reported total implies — or, with no
        total ever readable, past a page of this agenda that DID carry cards — is
        the portal's end. Everything else is `barren`, a stop of OURS: a
        confirmation that cannot be obtained is not a confirmation, and a
        WAF/consent/blank-shell 200 parses to exactly zero cards.
        """
        try:
            html, _status = client.fetch_index(sale=sale, stranka=page)
            reread = parse_index(html)
        except Exception as exc:  # noqa: BLE001 - an unreadable re-read proves nothing
            LOG.warning(
                "INDEX barren re-read failed sale=%d page=%d: %s", sale, page, exc)
            return "barren"
        if reread.items:
            LOG.warning(
                "INDEX barren page carried %d cards on re-read sale=%d page=%d — "
                "a transient blank, not the end of the index",
                len(reread.items), sale, page,
            )
            return "barren"
        # Where _PAGE_SIZE finally earns its keep: the declared total is what says
        # whether this empty page sits past the end or short of it.
        last_page = None if total is None else (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        # `>=`, not `>`: the count says which page the tail is ON, so an empty page
        # AT that page is the tail a churned-down agenda leaves behind (a strict `>`
        # filed those finished walks as barren and nominated nothing).
        confirmed = (page >= last_page) if last_page is not None else saw_items
        if not confirmed:
            LOG.warning(
                "INDEX barren page unconfirmed sale=%d page=%d total=%s last_page=%s "
                "— treating it as our own stop", sale, page, total, last_page,
            )
        return "empty_confirmed" if confirmed else "barren"

    @staticmethod
    def _belongs(mapped: str | None, cm: str | None) -> bool:
        """Whether an id's title-derived category `mapped` belongs to descriptor
        category `cm`. 'ostatni' is the catch-all, so an un-derivable category (a
        new remax type) is never silently dropped."""
        if mapped == cm:
            return True
        return cm == "ostatni" and mapped is None

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        cm = category.get("category_main")
        sale = int(category.get("sale") or 1)
        walk, pages = self._walk_agenda(sale, conn, limiter, deadline)

        native_ids = [n for n in walk.native_ids if self._belongs(walk.cat_map.get(n), cm)]
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
            idx_price = walk.price_map.get(nid)
            # A price-less card ("Dohodou" / "Info o ceně v RK") carries NO
            # change signal — classifying it as changed put ~1,100 such
            # listings on a permanent CHANGED-priority refetch treadmill every
            # walk, eating the whole drain budget ahead of the NEW rows (rent
            # listings never got claimed). Touch it and move on.
            if classify_index_sighting(
                prev, idx_price, self._price_change_min_pct,
            ) == "unchanged":
                unchanged_pks.append(prev["id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings_by_id(conn, unchanged_pks)

        entries = (
            [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=remax cm=%s ct=%s new=%d changed=%d unchanged=%d enqueued=%d",
            cm, category.get("category_type"), len(new_ids), len(changed),
            len(unchanged_pks), enqueued,
        )
        # remax reports a per-AGENDA total, not per-category, so the per-category
        # "portal expected" is what this category collected — index% is then 100%
        # by construction. The 5th element is the AGENDA's structural verdict, not
        # the slice's: nomination is agenda-grain, so the title-derived slice never
        # needs an end of its own.
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, len(seen), pages, walk.reached_end

    def presence_candidates(
        self, conn: Any, category: dict[str, Any], seen: set[str],
    ) -> tuple[list[tuple[str, str | None, int | None]], int, dict[str, Any]] | None:
        """Agenda-grain nomination (rule #3, 2026-09-07). The runner calls this
        once per (cm, ct) descriptor, but remax's completeness is per AGENDA
        (sale == category_type), so the whole agenda is nominated once --
        scoped by category_type against the FULL agenda walk's id set, never the
        title-derived per-category slice (a listing whose index-time title
        category differs from its detail-time category would otherwise be
        nominated by a walk that did see it). The passed `seen` (this
        category's slice) is intentionally ignored. An agenda that did not reach
        remax's end nominates nothing: its unseen set is then the part of the
        index we never visited, not evidence of absence."""
        cm, ct = self.category_labels(category)
        sale = int(category.get("sale") or 1)
        if ct is None or sale in self._swept_agendas:
            return None
        walk = self._agenda_cache.get(sale)
        if walk is None or not walk.reached_end:
            return None
        self._swept_agendas.add(sale)
        coverage = walk_coverage(len(walk.native_ids), walk.total)
        LOG.info(
            "VERIFY agenda sale=%d ct=%s collected=%d total=%s stop=%s coverage=%s",
            sale, ct, len(walk.native_ids), walk.total, walk.stop_reason, coverage,
        )
        if coverage != "complete":
            # An alarm, never a veto (rule #3): the walk reached remax's end, so it
            # nominates — but ending short of remax's own count means the portal's
            # pagination dropped rows, and nothing else can see it here (the
            # runner's coverage check compares len(seen) with len(seen) for this
            # portal, since the per-category "declared" total IS what we collected).
            LOG.warning(
                "COVERAGE agenda sale=%d ct=%s collected=%d declared=%s verdict=%s stop=%s",
                sale, ct, len(walk.native_ids), walk.total, coverage, walk.stop_reason,
            )
        candidates, active_rows = db.presence_candidates(
            conn, SOURCE, None, ct, set(walk.native_ids),
        )
        return candidates, active_rows, {"category_main": None}

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> RemaxClient:
        return RemaxClient(limiter=limiter)

    def fetch_detail(
        self, client: RemaxClient, native_id: str, detail_ref: str | None,
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
        # Page coords win -> carry a stored geom forward -> geocode the locality
        # (never fails the fetch; scraper.location).
        listing = self._coords.fill(native_id, listing)
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
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = %s AND claimed_at IS NULL AND given_up = false",
                (SOURCE,),
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
    portal = RemaxPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
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

    # Omitting both flags runs the index walk then the detail drain in one job,
    # bounded by --max-pages / --max-detail (+ --max-seconds). The split flags
    # exist so a large backfill can be cadence-split like sreality/idnes.
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
    p = argparse.ArgumentParser(description="remax-czech.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap search pages walked per agenda (ad-hoc partial run). Omit for the full walk.",
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
        help="wall-clock budget for EITHER phase (the index walk consumes it as "
             "its per-category deadline); the phase stops claiming + finalizes "
             "cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the search index + enqueue only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages index page(s) per agenda, then exit — never "
             "nominates unseen rows for a page check, no detail drain, no "
             "scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="index pages per agenda for --probe (default 1)",
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
