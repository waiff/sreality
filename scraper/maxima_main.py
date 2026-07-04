"""Orchestrator for the nemovitosti.maxima.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.maxima_main`. Maxima is a `Portal` (MaximaPortal)
driven by the one generic `scraper.portal_runner`: an index-walk that pages the
catalogue HTML and enqueues new/price-changed ids into the shared
`listing_detail_queue` (source='maxima', migration 108), then a detail-drain that
fetches each listing page, parses it to a `ScrapedListing`, and ingests via
`db.ingest_scraped_listing` (Tier-0 idempotency + Tier-1 matching). No bespoke
pipeline — only the per-portal fetcher (MaximaClient) + parser (maxima_parser) +
config differ from sreality/idnes (the modularity rule in CLAUDE.md).

Maxima is a small agency catalogue served as TWO mixed indexes — sale (the default
view, `af=1`) and rent (the buy/rent toggle, `af=2`) — each spanning every property
category with no per-category URL. The config descriptors are therefore per
(category_main, category_type, af): `walk_category` walks (or reuses, via an
agenda-level cache) that agenda's full index, then keeps the slice whose native-id
prefix (b=byt, d=dum, f=pozemek, g=komercni, o=ostatni) maps to the descriptor's
category. This gives the runner real (cm, ct) labels — the Health reconciliation
joins listings on those — while fetching each agenda's pages only once per run.
The drain still derives each listing's category from the detail page itself
(`maxima_parser.parse_detail`), so the queue stays category-agnostic.

maxima is `supports_complete_walk=true` via AGENDA-GRAIN delisting: it reports a
per-AGENDA total (not per-category), so the runner can't gate a per-(cm,ct) sweep —
instead `mark_inactive` flips the whole agenda (af ≡ category_type) once the agenda
walk reaches its reported total, scoped by category_type against the full walk's id
set (`db.mark_inactive_agenda`), never the title-derived per-category slice (which
could false-flip a listing whose index-time title category ≠ its detail-time one).
A gone detail fetch (404/410) still flips that one listing inactive immediately.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.maxima_client import MaximaClient, detail_url
from scraper.maxima_parser import category_of, index_price, parse_detail, parse_index
from scraper.portal import (
    PortalConfig,
    default_config,
    load_portal_config,
    price_changed,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "maxima"
INDEX_MIN_COMPLETENESS = 0.995  # agenda walk must reach ≥99.5% of the reported total to delist


class _AgendaWalk:
    """One agenda's (sale or rent) collected index, walked once and shared across
    that agenda's per-category descriptors."""

    def __init__(
        self, native_ids: list[str], ref_map: dict[str, str],
        price_map: dict[str, int | None], cat_map: dict[str, str | None],
        total: int | None, pages: int, complete: bool,
    ) -> None:
        self.native_ids = native_ids
        self.ref_map = ref_map
        self.price_map = price_map
        self.cat_map = cat_map  # id -> category_main (title-first, prefix fallback)
        self.total = total
        self.pages = pages
        # Did this walk reach the agenda's reported total? Gates agenda-grain
        # delisting (mark_inactive). False on a capped/short walk → no flip.
        self.complete = complete


class MaximaPortal:
    """nemovitosti.maxima.cz as a Portal: the seams the generic runner needs,
    wrapping the maxima client + parser.

    maxima exposes TWO mixed indexes — sale (the default view, af=1) and rent
    (the buy/rent toggle, af=2) — each spanning every property category with no
    per-category URL. So the config descriptors are per (category_main,
    category_type, af): each walk_category walks (or reuses, via the agenda cache)
    that agenda's full index, then keeps the slice whose native-id prefix maps to
    the descriptor's category. This gives the runner real (cm, ct) labels — the
    Health reconciliation joins listings on those — while fetching each agenda's
    pages only once per run."""

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

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): maxima's whole catalogue
        # is ~22 pages, so even a shallow capped walk covers the fresh head.
        # The agenda cache holds a walk taken at the OLD cap, so a cap change
        # must drop it — otherwise a deepened probe would replay the shallower
        # cached agenda.
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
        # Single-row ingest (ingest_scraped_listing), not batched prepared writes,
        # so the transaction pooler is fine — no session pooler needed.
        return db.connect()

    def _walk_agenda(
        self, af: int, conn: Any, limiter: RateLimiter,
    ) -> tuple[_AgendaWalk, int]:
        """Walk one agenda's full mixed index once; cache it for the agenda's
        other category descriptors. Returns (walk, pages_fetched_this_call) so the
        runner counts each agenda's pages exactly once (0 on a cache hit)."""
        cached = self._agenda_cache.get(af)
        if cached is not None:
            return cached, 0

        client = MaximaClient(limiter=limiter)
        native_ids: list[str] = []
        ref_map: dict[str, str] = {}
        price_map: dict[str, int | None] = {}
        cat_map: dict[str, str | None] = {}
        total: int | None = None
        pages = 0
        page = 1
        while True:
            html, status = client.fetch_index(page, af=af)
            parsed = parse_index(html)
            pages += 1
            total = parsed.total if parsed.total is not None else total
            LOG.info("INDEX af=%d page=%d items=%d total=%s", af, page, len(parsed.items), total)
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
                # Title-first so the rent agenda (whose ids carry prefixes the sale
                # taxonomy doesn't cover) is categorised the same way parse_detail
                # will categorise it — no Health-reconciliation fragmentation.
                cat_map[nid] = category_of(nid, item.title)
            if self._max_pages and pages >= self._max_pages:
                break
            # Stop on an empty page (catalogue exhausted) or one adding nothing new.
            if not parsed.items or new_on_page == 0:
                break
            page += 1

        # Complete only if we walked the whole agenda (not page-capped) AND reached
        # the portal-reported total — the gate for agenda-grain delisting.
        capped = bool(self._max_pages and pages >= self._max_pages)
        complete = (
            not capped and total is not None and total > 0
            and len(native_ids) >= total * INDEX_MIN_COMPLETENESS
        )
        walk = _AgendaWalk(native_ids, ref_map, price_map, cat_map, total, pages, complete)
        self._agenda_cache[af] = walk
        return walk, pages

    @staticmethod
    def _belongs(mapped: str | None, cm: str | None) -> bool:
        """Whether an id's derived category `mapped` belongs to descriptor category
        `cm`. 'ostatni' is the catch-all, so an unmapped category (a new maxima
        type) is never silently dropped."""
        if mapped == cm:
            return True
        return cm == "ostatni" and mapped is None

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        cm = category.get("category_main")
        af = int(category.get("af") or 1)
        walk, pages = self._walk_agenda(af, conn, limiter)

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
            if walk.price_map.get(nid) is not None and not price_changed(
                prev["price_czk"], walk.price_map[nid], self._price_change_min_pct,
            ):
                unchanged_pks.append(prev["sreality_id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings(conn, unchanged_pks)

        entries = (
            [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, walk.ref_map[n], walk.price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=maxima cm=%s ct=%s new=%d changed=%d unchanged=%d enqueued=%d",
            cm, category.get("category_type"), len(new_ids), len(changed),
            len(unchanged_pks), enqueued,
        )
        # maxima reports a per-AGENDA total (220/34), not per-category, so the
        # per-category "portal expected" is what this category collected — index%
        # is then 100% by construction. The COMPLETE flag is the agenda's (not the
        # slice's): delisting is agenda-grain (mark_inactive), so the slice never
        # needs its own completeness proof. The runner only delists when the agenda
        # walk reached its reported total.
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, len(seen), pages, walk.complete

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        # Agenda-grain: the runner calls this once per (cm, ct) descriptor, but
        # maxima's completeness is per-AGENDA (af ≡ category_type), so we sweep the
        # whole agenda once — scoped by category_type against the FULL agenda walk's
        # id set, NOT the title-derived per-category slice (which could false-flip a
        # listing whose index-time title category ≠ its detail-time category). The
        # passed `seen` (this category's slice) is intentionally ignored.
        cm, ct = self.category_labels(category)
        af = int(category.get("af") or 1)
        if ct is None or af in self._swept_agendas:
            return 0
        walk = self._agenda_cache.get(af)
        if walk is None or not walk.complete:
            return 0
        self._swept_agendas.add(af)
        # 12h staleness rail (~2 walk cadences at 6h): tightened 24->12h for the
        # real-time delisting SLO; 2 walk-misses is still robust to single-walk jitter.
        flipped = db.mark_inactive_agenda(
            conn, SOURCE, ct, set(walk.native_ids), min_unseen_hours=12,
        )
        LOG.info(
            "INACTIVE agenda af=%d ct=%s marked=%d collected=%d total=%s",
            af, ct, flipped, len(walk.native_ids), walk.total,
        )
        return flipped

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

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
            inserted = db.record_media(conn, pk, image_urls)
            db.mark_portal_page_parsed(conn, page_id)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        # A gone detail flips that one listing inactive immediately (a definitive
        # per-listing signal, independent of the agenda-grain index-absence sweep).
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


def _finalize(run_id: int | None, agg: dict[str, Any], *, drain: bool = False) -> None:
    if run_id is None or (not agg and not drain):
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
                images_stored=0,  # crawl records image-URL rows only; bytes uploaded async by images.yml
                errors=agg.get("errors", 0),
                by_category=agg.get("by_category", []),
                bump_already_applied=drain,
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
        kw = {**kw, "run_id": run_id}
        rc, agg = runner(portal, dry_run=dry_run, **kw)
    finally:
        if not dry_run:
            _finalize(run_id, agg, drain=runner is portal_runner.run_detail_drain)
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

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No mark_inactive, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

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
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages catalogue page(s) per agenda, then exit — never "
             "mark_inactive, no detail drain, no scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="catalogue pages per agenda for --probe (default 1)",
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
