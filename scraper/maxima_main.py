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

maxima is `supports_complete_walk=true` via AGENDA-GRAIN nomination: it reports a
per-AGENDA total (not per-category), so the runner can't gate a per-(cm,ct) sweep —
instead the whole agenda (af ≡ category_type) is nominated once its walk reaches
MAXIMA's end, scoped by category_type against the full walk's id
set (`db.presence_candidates` with category_main=None), never the title-derived per-category slice (which
could false-flip a listing whose index-time title category ≠ its detail-time one).
"Reached the end" is STRUCTURAL (rule #3, 2026-09-08): the page loop exited on a
terminator of maxima's own — its pager, its declared count, a corroborated empty
page, a clamped repeat — and no stop of ours (page cap, deadline, an
uncorroborated empty page) fired. The count is still computed and logged, and it
never vetoes: an agenda a row short of its declared total has still finished.
A gone detail fetch (404/410) still flips that one listing inactive immediately.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.location import CoordResolver
from scraper.maxima_client import MaximaClient, detail_url
from scraper.maxima_parser import category_of, index_price, parse_detail, parse_index
from scraper.portal import (
    PortalConfig,
    StopReason,
    WalkVerdict,
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

LOG = logging.getLogger(__name__)
SOURCE = "maxima"


class _AgendaWalk:
    """One agenda's (sale or rent) collected index, walked once and shared across
    that agenda's per-category descriptors."""

    def __init__(
        self, native_ids: list[str], ref_map: dict[str, str],
        price_map: dict[str, int | None], cat_map: dict[str, str | None],
        total: int | None, pages: int, reached_end: bool,
        stop: StopReason, coverage: WalkVerdict,
    ) -> None:
        self.native_ids = native_ids
        self.ref_map = ref_map
        self.price_map = price_map
        self.cat_map = cat_map  # id -> category_main (title-first, prefix fallback)
        self.total = total
        self.pages = pages
        # Did the walk reach MAXIMA's end (rule #3, structural since 2026-09-08)?
        # Gates agenda-grain nomination. False whenever the loop stopped for a
        # reason of OURS — a page cap, the deadline, an uncorroborated empty page.
        self.reached_end = reached_end
        self.stop = stop            # the one StopReason the page loop exited on
        self.coverage = coverage    # the numeric verdict: an alarm, never a gate


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
        # page > carry-forward > geocode (maxima's OpenLayers map config is often
        # absent, and until now those rows had NO coords path at all).
        self._coords = CoordResolver(SOURCE)

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
        conn = db.connect()
        self._coords.preload(conn)
        return conn

    @staticmethod
    def _confirm_empty_page(
        total: int | None, collected: int, *,
        pager_advanced: bool, pager_end_prev: bool,
    ) -> StopReason:
        """Classify an items-less page a re-fetch found still items-less: maxima's
        end, or ours? (the BARREN rule — a confirmation that cannot be obtained is
        not a confirmation).

        Never a ratio: a walk that came up a row short still ends here, on the
        portal's own pager, which is the whole point of the structural gate."""
        if total is not None and collected >= total:
            # We hold MAXIMA's own count (a declared zero with nothing collected
            # included) — this is the page past the last one.
            return "empty_confirmed"
        if pager_advanced and pager_end_prev:
            # Short of the declared count (live churn, a row the index moved under
            # us) or unmeasurable, but the pager — which has demonstrably rendered
            # forward links on this agenda — said the previous page was the last.
            # The portal ended the walk; the gap gets nominated and the page
            # decides (rule #3).
            return "empty_confirmed"
        if total is None and collected and not pager_advanced:
            # No total was ever readable AND no pager ever rendered: nothing can
            # contradict an empty page that follows pages which carried items.
            # (An agenda barren from page 1 proves nothing at all.)
            return "empty_confirmed"
        # Either the pager is actively saying there is more, or we are short of
        # maxima's count with nothing to corroborate the emptiness: a soft block,
        # a consent shell, a throttled render. Ours.
        return "barren"

    def _walk_agenda(
        self, af: int, conn: Any, limiter: RateLimiter, deadline: float | None = None,
    ) -> tuple[_AgendaWalk, int]:
        """Walk one agenda's full mixed index once; cache it for the agenda's
        other category descriptors. Returns (walk, pages_fetched_this_call) so the
        runner counts each agenda's pages exactly once (0 on a cache hit).

        A walk stopped for a reason of OURS — the page cap, the deadline, an
        uncorroborated empty page — is cached with reached_end=False, so every one
        of that agenda's category descriptors inherits the verdict and nominates
        nothing (rule #3)."""
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
        stop: StopReason = "barren"  # fail closed; every exit below re-stamps it
        barren_retried = False
        # The pager is EVIDENCE here, not the driver: the loop still pages until
        # the index runs out, so a pager that fails to render (unverified on the
        # rent agenda) can never truncate the walk — it only decides, afterwards,
        # whether the page the walk ran out on was maxima's end or ours.
        pager_advanced = False  # a forward pager link rendered at least once
        pager_end_prev = False  # the last item-carrying page exposed no next page
        while True:
            html, status = client.fetch_index(page, af=af)
            parsed = parse_index(html)
            pages += 1
            # ONLY a page that carried items may move the denominator. `total` is
            # what _confirm_empty_page measures POSITION against, so letting an
            # items-less page write it let a shell/mis-filtered page rendering a
            # small (or zero) counter corroborate itself as maxima's end.
            if parsed.items and parsed.total is not None:
                total = parsed.total
            LOG.info("INDEX af=%d page=%d items=%d total=%s", af, page, len(parsed.items), total)
            capped = bool(self._max_pages and pages >= self._max_pages)
            if not parsed.items:
                if capped:
                    # Ours either way: a capped walk (--max-pages, the probe's
                    # set_index_page_cap) has no budget left to corroborate
                    # anything, so don't spend a re-fetch on it.
                    stop = "page_cap"
                    break
                # ITEMS-FIRST. A blocked/consent/degraded 200 parses to zero items
                # exactly like the page past the last one, and the old compound
                # `not parsed.items or new_on_page == 0` break could not tell them
                # apart — the numeric gate was the only thing that separated them.
                # Re-fetch the same page once before believing it (the #637 lesson,
                # realitymix_main's one-shot retry), then classify.
                if not barren_retried:
                    barren_retried = True
                    continue
                stop = self._confirm_empty_page(
                    total, len(native_ids),
                    pager_advanced=pager_advanced, pager_end_prev=pager_end_prev,
                )
                LOG.info(
                    "INDEX empty page af=%d page=%d collected=%d total=%s -> %s",
                    af, page, len(native_ids), total, stop,
                )
                break
            barren_retried = False
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
            if new_on_page == 0:
                # A page of ids we already hold — page >= 2 by construction, since
                # page 1 has nothing to repeat. That is the clamped out-of-range
                # page a WordPress index serves past its last one, but ONLY the
                # pager makes it maxima's end: the same shape is a cached/repeated
                # page off a degraded edge, and that stop is ours.
                stop = (
                    "clamp_repeat" if pager_advanced and pager_end_prev
                    else "pager_stalled"
                )
                break
            if (
                total is not None and len(native_ids) >= total
                and parsed.next_offset is None
            ):
                # MAXIMA's own count, never our ratio: a walk that comes up a row
                # short simply never trips this and ends on the pager instead. And
                # only where the PAGER agrees there is nothing after this page: the
                # counter is loose (the live tail reads "Zobrazuji 29-42 z celkem
                # 29"), and breaking on the number alone stopped fetching rows that
                # exist past it — rows that were then never enqueued at all, while
                # the walk still claimed to have reached the end.
                stop = "declared_total_reached"
                break
            if capped:
                # Ours — and tested after the natural stops (the deadline's order,
                # for the same reason): a walk that finished the whole agenda on
                # its last permitted page did reach the end.
                stop = "page_cap"
                break
            # ABSENCE of pager markup is not maxima saying "last page": a page whose
            # cards render but whose pager fragment does not returns next_offset None
            # too, and it would then corroborate the barren page after it. Only a
            # pager that RENDERED can testify.
            pager_end_prev = parsed.pager_present and parsed.next_offset is None
            pager_advanced = pager_advanced or parsed.next_offset is not None
            # Checked after the natural stops so a walk that finished on this very
            # page isn't falsely called truncated; before fetching the next one so
            # the budget is still honoured.
            if deadline_reached(deadline):
                stop = "deadline"
                LOG.info(
                    "INDEX time budget reached af=%d page=%d collected=%d total=%s; "
                    "stopping this agenda walk (our stop -> no nomination)",
                    af, page, len(native_ids), total,
                )
                break
            page += 1

        # One unit per agenda (a single mixed index), so the page loop's stop
        # reason IS the agenda's verdict — no AND across slices to compute.
        portal_end = stop_is_portal_end(stop)
        reached_end = walk_reached_end(portal_end=portal_end, our_stop=not portal_end)
        # The numeric verdict stays, at AGENDA grain (the runner's per-category one
        # is 100% by construction — see walk_category), and it is an alarm only.
        coverage = walk_coverage(len(native_ids), total)
        LOG.info(
            "INDEX af=%d done pages=%d collected=%d total=%s stop=%s reached_end=%s "
            "coverage=%s",
            af, pages, len(native_ids), total, stop, reached_end, coverage,
        )
        walk = _AgendaWalk(
            native_ids, ref_map, price_map, cat_map, total, pages,
            reached_end, stop, coverage,
        )
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
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        cm = category.get("category_main")
        af = int(category.get("af") or 1)
        walk, pages = self._walk_agenda(af, conn, limiter, deadline)

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
            if classify_index_sighting(
                prev, walk.price_map.get(nid), self._price_change_min_pct,
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
            "ENQUEUE source=maxima cm=%s ct=%s new=%d changed=%d unchanged=%d enqueued=%d",
            cm, category.get("category_type"), len(new_ids), len(changed),
            len(unchanged_pks), enqueued,
        )
        # maxima reports a per-AGENDA total (220/34), not per-category, so the
        # per-category "portal expected" is what this category collected — index%
        # is then 100% by construction. The 5th element is the agenda's structural
        # verdict (not the slice's): nomination is agenda-grain, so the slice never
        # needs a proof of its own. The runner nominates only when the agenda walk
        # reached maxima's end.
        return (
            seen, {"found_new": len(new_ids), "enqueued": enqueued}, len(seen), pages,
            walk.reached_end,
        )

    def presence_candidates(
        self, conn: Any, category: dict[str, Any], seen: set[str],
    ) -> tuple[list[tuple[str, str | None, int | None]], int, dict[str, Any]] | None:
        """Agenda-grain nomination (rule #3, 2026-09-07). The runner calls this
        once per (cm, ct) descriptor, but maxima's end is reached per AGENDA
        (af == category_type), so the whole agenda is nominated once --
        scoped by category_type against the FULL agenda walk's id set, never the
        title-derived per-category slice (a listing whose index-time title
        category differs from its detail-time category would otherwise be
        nominated by a walk that did see it). The passed `seen` (this
        category's slice) is intentionally ignored. An agenda whose walk stopped
        for a reason of OURS nominates nothing: its unseen set is not evidence,
        it is the part of the index the walk never reached. A count that came up
        short is not such a reason (rule #3, structural since 2026-09-08)."""
        cm, ct = self.category_labels(category)
        af = int(category.get("af") or 1)
        if ct is None or af in self._swept_agendas:
            return None
        walk = self._agenda_cache.get(af)
        if walk is None or not walk.reached_end:
            return None
        self._swept_agendas.add(af)
        if walk.coverage != "complete":
            # The gap is nominated anyway and the page decides — but a walk that
            # reached maxima's end while short of maxima's own count is worth
            # saying out loud (the runner's per-category COVERAGE warning cannot
            # see this: maxima's per-category "expected" is the slice itself).
            LOG.warning(
                "COVERAGE af=%d ct=%s: reached maxima's end (%s) with %d of %s "
                "collected (coverage=%s) -- nominating the gap anyway (rule #3)",
                af, ct, walk.stop, len(walk.native_ids), walk.total, walk.coverage,
            )
        LOG.info(
            "VERIFY agenda af=%d ct=%s collected=%d total=%s stop=%s",
            af, ct, len(walk.native_ids), walk.total, walk.stop,
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
        help="wall-clock budget for EITHER phase (the index walk consumes it as "
             "its per-category deadline); the phase stops claiming + finalizes "
             "cleanly before the job timeout (no 'stuck' run)",
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
             "nominates unseen rows for a page check, no detail drain, no "
             "scrape_runs row",
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
