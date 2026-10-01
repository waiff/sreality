"""Orchestrator for the bazos.cz crawler — on the shared portal framework (Phase 4).

Runnable as `python -m scraper.bazos_main`. Bazos is a `Portal` (BazosPortal)
driven by the generic `scraper.portal_runner`. Its own `walk_category` pages the
index and hands its sightings to `portal_runner.reconcile_sightings`, which touches
and enqueues into the shared `listing_detail_queue` (source='bazos', migration 108); the shared detail-drain fetches (BazosClient) + parses
(bazos_parser) + writes via `listing_write.write_listings` (the one listing write;
a first-seen row lands `property_id` NULL and the straggler-attach births its
singleton, rule #15).

Every scope pages to bazos's own last page, so a walk of it is provable-finished:
`supports_complete_walk=True`, and a walk that REACHED THE END nominates the rows
it did not see for a page check (rule #3, structural gate 2026-09-08) — the
drain's fetch decides, so a frequent walk surfaces new ads + freshness every run
while a wrong nomination costs one fetch, never a live listing. The index's total
("z N inzerátů") is still measured, but as a coverage record, never as the gate.

Scope: every category in the portal registry (14 nationwide sale+rent sections —
byt/dum/chata/restaurace/kancelar/prostory/sklad). The source-generic queue carries
no category, so the drain reads each ad's category off its detail-page breadcrumb
(`parse_detail`) — the same "detail self-identifies" pattern idnes/bezrealitky use.
A `--sale-type`/`--category` dispatch override narrows to a single scope.

Cadence split, like sreality/idnes (rule #19): the full 14-scope index walk is
~1500 pages (≈ 50 min), so it cannot share one job with the detail drain without
starving it. `bazos_index_walk.yml` runs `--index-only` (every 6h); the bounded
`bazos_detail_drain.yml` runs `--drain-only` (hourly, `--max-seconds` budget).
Omitting both flags runs both phases back-to-back — an ad-hoc combined run for
local debugging / narrow scopes; no workflow uses it.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from scraper import db, portal_runner
from scraper.bazos_client import BazosClient, detail_url
from scraper.bazos_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    SUBTYPE,
    _parse_price,
    parse_detail,
    parse_index,
)
from scraper.portal import (
    PortalConfig,
    StopReason,
    deadline_reached,
    stop_is_portal_end,
    walk_coverage,
    walk_reached_end,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "bazos"

# No staleness rail any more (rule #3, 2026-09-07): a complete walk nominates
# the rows it did not see for a page check and the drain's fetch decides, so a
# single walk-miss costs one fetch, never a live listing.


class BazosPortal(portal_runner.PortalDefaults):
    """Bazos as a Portal: the seams the generic runner needs, wrapping the
    bazos client + parser. Single-category, one locality scope per run.

    Complete-walk capable: bazos 404s the offset past a section's last page and
    reports its own total, so a walk that ends on one of those signals has
    reached the end and nominates the rows it did not see for a page check
    (rule #3); the nomination is scoped to the section's subtype so fine
    sections that share a category_main never nominate each other's rows.

    The 22 scopes are walked one per `walk_category` call and judged
    independently — there is no conjunction across them, so one truncated
    section never suppresses another's nomination."""

    source = SOURCE
    price_change_min_pct = 0.0
    index_rate = 0.5

    def __init__(
        self,
        *,
        categories: list[dict[str, str]],
        locality: str | None = None,
        radius_km: int | None = None,
        max_pages: int | None = None,
    ) -> None:
        # Each scope is a bazos URL pair {"sale_type", "category"} (e.g.
        # prodam/byt + pronajmu/byt). The drain reads the category off each
        # detail's breadcrumb, so one portal covers all scopes via one queue.
        self._scopes = list(categories)
        self._locality = locality
        self._radius_km = radius_km
        self._max_pages = max_pages

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): bazos's default index
        # order is newest-first, so a page-capped walk IS the delta probe.
        self._max_pages = pages

    def categories(self) -> list[dict[str, str]]:
        return list(self._scopes)

    def category_labels(self, category: dict[str, str]) -> tuple[str | None, str | None]:
        return (
            CATEGORY_MAIN.get(category.get("category")),
            SALE_TYPE.get(category.get("sale_type")),
        )

    def walk_category(
        self, category: dict[str, str], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        _, canon_type = self.category_labels(category)
        client = BazosClient(limiter=limiter)
        seen: set[str] = set()
        items: list[tuple[str, str, int | None]] = []  # (native, detail_path, idx_price)
        total: int | None = None
        pages = 0
        page_size = 0  # largest page bazos served this scope; places a 404 at page grain
        offset = 0
        # The largest page this scope has served. bazos publishes no page size, so
        # the walk measures it: it is what tells a SHORT tail page (bazos ran out
        # of ads) from a FULL page that claims no next page — the shape a renamed
        # pager selector produces on page 1 of every scope.
        page_size = 0
        # A walk cut short by the wall-clock budget is NOT a finished walk: this
        # flag poisons the numeric coverage record below (the structural verdict
        # reads the stop reason instead).
        stopped_early = False
        # Every break below stamps the reason this page loop stopped. The seed is
        # a stop of OURS, so a loop that somehow left without stamping one can
        # never nominate (rule #3 fails closed).
        stop: StopReason = "error"
        while True:
            if deadline_reached(deadline):
                stopped_early = True
                stop = "deadline"
                LOG.info(
                    "INDEX deadline reached sale_type=%s category=%s "
                    "pages=%d offset=%d seen=%d; stopping walk (incomplete)",
                    sale_type, cat, pages, offset, len(seen),
                )
                break
            try:
                html, status = client.fetch_index(
                    sale_type, cat, offset,
                    locality=self._locality, radius_km=self._radius_km,
                )
            except ListingGoneError:
                # bazos 404s an offset past the last result page, and its pager's
                # "Další" link points one page beyond the end — so keep what we
                # collected rather than letting it abort (and discard) the whole
                # walk. Whether that 404 is the END or a soft block wearing a 404
                # is decided by _classify_gone, not by this break.
                stop = self._classify_gone(
                    total=total, collected=len(seen), pages=pages, offset=offset,
                    page_size=page_size,
                )
                LOG.info(
                    "INDEX end-of-results at offset=%d (gone) stop=%s", offset, stop
                )
                break
            page = parse_index(html)
            pages += 1
            page_size = max(page_size, len(page.items))
            if page.total is not None:
                total = page.total
            page_size = max(page_size, len(page.items))
            LOG.info(
                "INDEX offset=%d items=%d total=%s", offset, len(page.items), page.total
            )
            for item in page.items:
                if item.source_id_native not in seen:
                    seen.add(item.source_id_native)
                    idx_price, _ = _parse_price(item.price_text, canon_type)
                    # Same clamps as the stored price so the changed-compare can't
                    # see a value the write boundary would have nulled.
                    items.append((
                        item.source_id_native,
                        item.detail_path,
                        db.sane_price_czk(idx_price),
                    ))
            if self._max_pages and pages >= self._max_pages:
                stop = "page_cap"
                break
            # ITEMS-FIRST (rule #3): an items-less HTTP 200 is what a soft block,
            # a consent shell, a proxy blank and a renamed listing selector all
            # look like — and it is NOT how bazos ends a section (it 404s the
            # offset past the end). It must be corroborated before it can pass as
            # the portal's end, so it cannot share the pager's break any more.
            if not page.items:
                stop = self._confirm_barren(
                    client, sale_type, cat, offset,
                    total=total, collected=len(seen), pages=pages,
                )
                break
            if page.next_offset is None:
                # `_next_offset` returns None for bazos's real last page AND for
                # any page whose pager markup we failed to parse (renamed
                # container, relabelled anchor, truncated body) — two states one
                # value, and the second one would end every scope on page 1. A
                # page SHORTER than the largest this scope served is bazos running
                # out of ads; a FULL one that claims no next page has to be
                # corroborated before it may pass as the portal's end.
                if total is not None and len(seen) >= total:
                    stop = "declared_total_reached"
                elif len(page.items) < page_size:
                    stop = "pager_end"
                else:
                    stop = self._confirm_pager_end(
                        client, sale_type, cat, offset + len(page.items), seen=seen,
                    )
                break
            # Stop once we've collected the portal-reported total; the pager
            # often advertises one offset past the end (which 404s).
            if total is not None and len(seen) >= total:
                stop = "declared_total_reached"
                break
            if page.next_offset <= offset:
                # A pager that does not advance would walk this offset forever.
                stop = "pager_stalled"
                LOG.warning(
                    "INDEX pager stalled sale_type=%s category=%s offset=%d next=%d",
                    sale_type, cat, offset, page.next_offset,
                )
                break
            offset = page.next_offset

        # The gate is STRUCTURAL: did bazos say there is nothing after this page?
        # The count is still measured — as a record and an alarm (the runner logs
        # the gap and writes both facts into scrape_runs.by_category) — but it
        # never vetoes any more: a scope one row short of a total that jitters
        # mid-walk has still reached bazos's last page.
        coverage = walk_coverage(len(seen), total, stopped_early=stopped_early)
        portal_end = stop_is_portal_end(stop)
        # A configured page cap is a stop of OURS even when the loop ended before
        # reaching it: it says we asked for a slice of the index, not all of it.
        # A --locality/--radius-km run walks one geographic slice of the section
        # while nomination is section-wide (presence_candidates carries no geo
        # predicate), so it is a subset of the index however it ends.
        narrowed = bool(self._locality) or self._radius_km is not None
        if narrowed:
            LOG.info(
                "INDEX sale_type=%s category=%s walked a locality slice "
                "(locality=%s radius_km=%s); it nominates nothing",
                sale_type, cat, self._locality, self._radius_km,
            )
        reached_end = walk_reached_end(
            portal_end=portal_end,
            our_stop=not portal_end or bool(self._max_pages) or narrowed,
        )
        LOG.info(
            "INDEX walk end sale_type=%s category=%s stop=%s reached_end=%s "
            "collected=%d total=%s pages=%d coverage=%s",
            sale_type, cat, stop, reached_end, len(seen), total, pages, coverage,
        )
        counts = portal_runner.reconcile_sightings(
            conn, SOURCE, {n: (path, price) for n, path, price in items},
            min_change_pct=self.price_change_min_pct,
        )
        return seen, counts, total, pages, reached_end

    def _classify_gone(
        self, *, total: int | None, collected: int, pages: int, offset: int,
        page_size: int = 0,
    ) -> StopReason:
        """Is a 404/410 on an index page bazos's past-the-end marker, or ours?

        A soft block served as a 404 is byte-for-byte the same signal, so the
        404 alone proves nothing: it is the portal's end only once the walk has
        actually read a page of this scope AND bazos's own counter agrees that
        this offset is on or past the LAST page it implies. Page, not row: the
        counter over-counts by a handful (ads removed between the count and the
        page render), and the pager does sometimes advertise one page past the
        end -- 2026-09-09 03:54, prodam/pozemek: 13,451 collected of a declared
        13,461, the last page held 11, the pager still offered offset 13,460,
        bazos 404'd it, and a row-exact rule (offset >= total) called that a
        stop of ours, so a 13k-row scope nominated nothing. A count one row
        long must never veto a walk the portal itself ended (rule #3).

        A 404 while the declared total is still pages away -- or while no
        total was ever readable, so nothing can place the 404 at all -- is a
        stop of ours. That an unreadable counter costs us the 404 terminator is
        fine: a healthy walk usually ends on `pager_end`."""
        if pages == 0:
            return "error"
        if total is None:
            return "error"
        if collected >= total:
            return "declared_total_reached"
        if offset + max(page_size, 0) >= total:
            return "empty_confirmed"
        return "error"

    def _confirm_pager_end(
        self, client: BazosClient, sale_type: str, cat: str, next_offset: int, *,
        seen: set[str],
    ) -> StopReason:
        """A FULL page that advertised no next page: bazos's tail, or a pager we
        could not parse?

        One extra fetch of the offset that page would have pointed at settles it,
        and settles it structurally — no count is consulted, so an over-declared
        total can never veto a finished walk. bazos 404s an offset past the last
        result page and runs dry at one just short of it, so gone-or-empty IS the
        corroboration; ads we have not already collected prove the pager, not the
        portal, ended the walk, and ads we HAVE all collected mean the offset did
        not advance."""
        try:
            html, _status = client.fetch_index(
                sale_type, cat, next_offset,
                locality=self._locality, radius_km=self._radius_km,
            )
        except ListingGoneError:
            return "pager_end"
        items = parse_index(html).items
        if not items:
            return "pager_end"
        if {i.source_id_native for i in items} - seen:
            LOG.warning(
                "INDEX pager end unproven sale_type=%s category=%s offset=%d: the "
                "next offset still carries ads we never saw, so the pager was not read",
                sale_type, cat, next_offset,
            )
            return "barren"
        LOG.warning(
            "INDEX pager end unproven sale_type=%s category=%s offset=%d: the next "
            "offset re-served ads we already hold, so it did not advance",
            sale_type, cat, next_offset,
        )
        return "pager_stalled"

    def _confirm_barren(
        self, client: BazosClient, sale_type: str, cat: str, offset: int, *,
        total: int | None, collected: int, pages: int,
    ) -> StopReason:
        """Re-fetch an items-less page once and classify it (rule #3's barren rule).

        Still empty AND at or past what the declared total implies — or no total
        was ever readable and an earlier page of this scope carried items — is
        bazos's end; anything else stays `barren`, a stop of ours. A confirmation
        that cannot be obtained is not a confirmation, so a scope whose FIRST page
        is barren with no total is never confirmed."""
        try:
            html, _status = client.fetch_index(
                sale_type, cat, offset,
                locality=self._locality, radius_km=self._radius_km,
            )
        except ListingGoneError:
            # The re-read landing on bazos's past-the-end 404 corroborates the
            # blank only where the counter places that 404 past the end; a 404 the
            # walk cannot place (no readable total) is a soft block's shape too,
            # which is what keeps a barren FIRST page from confirming itself.
            return self._classify_gone(
                total=total, collected=collected, pages=pages, offset=offset,
            )
        if parse_index(html).items:
            # The blank was a fluke, so this page's ads were missed: the walk is
            # not one we can nominate from, however it ends.
            LOG.warning(
                "INDEX barren page re-read carried items sale_type=%s category=%s "
                "offset=%d; treating the walk as truncated",
                sale_type, cat, offset,
            )
            return "barren"
        if (total is not None and offset >= total) or (total is None and collected > 0):
            return "empty_confirmed"
        LOG.warning(
            "INDEX barren page uncorroborated sale_type=%s category=%s offset=%d "
            "total=%s seen=%d; not an end-of-results",
            sale_type, cat, offset, total, collected,
        )
        return "barren"

    def presence_candidates(
        self, conn: Any, category: dict[str, str], seen: set[str],
    ) -> tuple[list[tuple[str, str | None, int | None]], int, dict[str, Any]] | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        # Scope the nomination to this section's subtype: bazos walks fine
        # sections that collapse onto one category_main (chata + dum -> dum), so
        # an un-scoped per-section nomination would send every sibling section's
        # rows to the drain for a page check on every walk.
        sub = SUBTYPE.get(category.get("category"))
        candidates, active_rows = db.presence_candidates(
            conn, SOURCE, cm, ct, seen, subtype=sub, scope_subtype=True,
        )
        return candidates, active_rows, {"subtype": sub}

    def active_count(self, conn: Any, category: dict[str, str]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        sub = SUBTYPE.get(category.get("category"))
        return db.active_count(
            conn, cm, ct, source=SOURCE, subtype=sub, scope_subtype=True
        )

    # --- detail-drain seams ---
    def fetch_detail(
        self, client: BazosClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        # parse_detail reads the real category off the page breadcrumb; the
        # primary scope's labels are only a fallback for a page that lacks it.
        fb_main, fb_type = self.category_labels(self._scopes[0])
        try:
            listing = parse_detail(
                html, source_url=url,
                category_main=fb_main, category_type=fb_type,
            )
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        return DrainItem(
            native_id=native_id, kind="ok",
            payload={"listing": listing, "html": html, "status": status, "url": url},
        )


def _resolve_scopes(
    args: argparse.Namespace, config: "PortalConfig"
) -> list[dict[str, str]] | None:
    """CLI sale_type/category (dispatch override) → that one scope; otherwise the
    portal registry's categories (scheduled run = every configured scope)."""
    if args.sale_type or args.category:
        st = args.sale_type or "prodam"
        cat = args.category or "byt"
        if SALE_TYPE.get(st) is None or CATEGORY_MAIN.get(cat) is None:
            LOG.error("unmapped scope sale_type=%s category=%s", st, cat)
            return None
        return [{"sale_type": st, "category": cat}]
    scopes = [
        c for c in config.categories
        if SALE_TYPE.get(c.get("sale_type")) and CATEGORY_MAIN.get(c.get("category"))
    ]
    return scopes or [{"sale_type": "prodam", "category": "byt"}]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    portal_runner.configure_logging(args.verbose)

    config = portal_runner.load_config(SOURCE, args.dry_run)
    limits = config.limits
    scopes = _resolve_scopes(args, config)
    if scopes is None:
        return 2
    LOG.info("SCOPES %s", scopes)

    portal = BazosPortal(
        categories=scopes,
        locality=args.locality, radius_km=args.radius_km, max_pages=args.max_pages,
    )
    portal.index_rate = limits.index_rate
    portal.shared_rate_limiter = limits.shared_rate_limiter
    portal.price_change_min_pct = limits.price_change_min_pct

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else limits.detail_workers
    rate = args.rate if args.rate is not None else limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None else limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No nomination, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

    # Cadence split, like sreality/idnes (rule #19): --index-only walks +
    # enqueues (and marks inactive under the completeness guard); --drain-only
    # fetches + ingests a bounded slice of the queue. Bazos walks every scope in
    # the registry (14 nationwide sale+rent sections, ~1500 index pages ≈ 50 min),
    # so a combined run can't do both inside one job — the full index eats the
    # window and starves the drain. Omitting both flags runs both phases (the
    # dispatch-only combined fallback). Two scrape_runs rows ('index' + 'detail').
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
    p = argparse.ArgumentParser(description="bazos.cz crawler (portal framework)")
    p.add_argument(
        "--sale-type", default=None, choices=sorted(SALE_TYPE),
        help="single-scope override (default: every scope in the portal config)",
    )
    p.add_argument(
        "--category", default=None, choices=sorted(CATEGORY_MAIN),
        help="single-scope override (paired with --sale-type)",
    )
    p.add_argument("--locality", default=None)
    p.add_argument("--radius-km", type=int, default=None)
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages walked (pilot safety); omit for a full walk",
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
        help="requests/second ceiling (default: per-portal config)",
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
             "--probe-pages index page(s) per scope, then exit — never "
             "nominates unseen rows for a page check, no detail drain, no "
             "scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="index pages per scope for --probe (default 1)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
