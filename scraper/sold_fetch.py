"""One municipality cell, fetched (sold-comps W2).

Composes the three single-purpose modules — `sold_db` for the box and the writes,
`reas_client` for the HTTP, `reas_parser` for the payload — and owns exactly one
decision of its own: what happens when a cell goes wrong.

A CELL ATTEMPT ALWAYS ENDS IN THE LEDGER, never in an exception. `fetch_cell`
returns a status; the one thing it cannot do is raise into the worker lane, because
a cell that failed silently is indistinguishable from a cell that holds no sales.
The one attempt that writes nothing — an obec with no boundary polygon, which has no
box to key the row on — is unreachable from the lane by construction: the work-list
joins `admin_boundaries`, so an unboxable cell is never offered as work. It stays
reachable from the CLI, where the operator names the obec.

AND AN `ok` ROW SAYS WHAT IT COVERED. A walk the page cap or a non-advancing
`nextPage` cut short is still true about every row it took and false about being all
of them, so it carries the shortfall in the ledger's `error` (`_truncation`).

MALFORMED RECORD = THE WHOLE PAGE FAILS (W1's open issue, decided here). The
parser's refusals are contract failures, not bad rows: the active catalogue served
under a 200, an identity grammar that moved, a fifth `type`. Storing the good 90%
of such a page would bake a half-truth into a table of facts and leave nothing able
to say which rows were lost — so the page is dropped, the cell gets a `failed`
ledger row carrying the parser's own message, and the next tick retries it in six
hours. Broker-reported records stay a counted DROP, because they are a different
kind of claim rather than a defect (`reas_parser`'s module docstring).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import psycopg

from scraper import sold_db
# `build_client` and `SOURCE` are re-exported: the worker lane opens its client and
# names its ledger through this module, so shared code never imports a source's own.
from scraper.reas_client import ReasClient, build_client  # noqa: F401
from scraper.reas_parser import SOURCE, SoldTransaction, parse_sold_html

LOG = logging.getLogger(__name__)

# 25 pages of 100 = 2,500 records. Praha — ONE obec, and the only cell measured to
# paginate at all — holds ~1,100 sales in the source's 24-month window, 11 pages of
# ~1.9 MB. That measurement is on Praha's BARE envelope, while the box a cell sends
# is that envelope widened by MAX_READ_RADIUS_M on every side (~1.8x the area, and
# the ring added is Praha-západ/-východ), so the cap's headroom is real but not
# proven. Which is why a walk the cap cut short says so in the ledger: `truncated`
# below, never a clean `ok`.
MAX_PAGES = 25

# The ledger's `error` is a diagnosis, not a payload: a stack-shaped message from a
# thousand-page HTML body must not become the widest column in the table.
_ERROR_MAX = 500


@dataclass(frozen=True)
class SoldWalk:
    """One box walked: the rows, and everything the ledger needs to be honest."""

    rows: list[SoldTransaction]
    pages: int
    # The source's own total for this box INSIDE the sold window, against
    # `source_total` (its total without the window). `count` is the truncation
    # denominator: it says how much of the cell a cut-short walk did not take.
    count: int | None
    source_total: int | None
    dropped: int
    truncated: bool


def walk_bounds(
    client: ReasClient,
    bounds: tuple[float, float, float, float],
    *,
    max_pages: int = MAX_PAGES,
) -> SoldWalk:
    """Pages 1..max_pages of one box.

    Stops when the source says there is no next page — `nextPage` is the only walk
    terminator, and outside Praha it is null on page 1. Two other exits, and both
    are TRUNCATION, not completion: the page cap, and a `nextPage` that does not
    advance (a pinned edge cache or a source-side pagination bug would otherwise
    re-fetch the same page until the cap, recording it as a full cell).
    """
    rows: list[SoldTransaction] = []
    count: int | None = None
    source_total: int | None = None
    dropped = 0
    pages = 0
    next_page: int | None = 1
    truncated = False
    while next_page is not None:
        if pages >= max_pages:
            truncated = True
            break
        requested = next_page
        page = parse_sold_html(client.fetch_sold_page(bounds, page=requested))
        pages += 1
        rows.extend(page.rows)
        dropped += page.dropped_broker_reported
        if count is None:
            count = page.count
        if source_total is None:
            source_total = page.possible_count
        next_page = page.next_page
        if next_page is not None and next_page <= requested:
            truncated = True
            break
    return SoldWalk(rows, pages, count, source_total, dropped, truncated)


def fetch_cell(
    conn: psycopg.Connection,
    client: ReasClient,
    obec_kod: int,
    *,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Fetch, store and record one obec cell. Never raises."""
    started = time.monotonic()
    box: sold_db.CellBox | None = None
    try:
        box = sold_db.obec_cell_box(conn, obec_kod)
        if box is None:
            # No polygon, so no cell and no box to key a ledger row on. The lane
            # cannot get here (its work-list joins `admin_boundaries`); the CLI can,
            # because there the operator names the obec.
            return _result(obec_kod, "skipped", started,
                           error="obec has no boundary polygon")
        walk = walk_bounds(
            client,
            (box.sw_lat, box.sw_lng, box.ne_lat, box.ne_lng),
            max_pages=max_pages,
        )
        stored, new = sold_db.upsert_sold_transactions(conn, walk.rows)
        truncated = _truncation(walk, stored)
        sold_db.record_fetch(
            conn, source=SOURCE, obec_kod=obec_kod, bbox=box.ewkt, status="ok",
            record_count=stored, source_total=walk.source_total, pages=walk.pages,
            error=truncated,
        )
    except Exception as exc:  # noqa: BLE001 - a cell failure is a ledger row
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX]
        LOG.warning("SOLD cell obec=%s failed: %s", obec_kod, error)
        if box is not None:
            try:
                sold_db.record_fetch(
                    conn, source=SOURCE, obec_kod=obec_kod, bbox=box.ewkt,
                    status="failed", error=error,
                )
            except Exception as ledger_exc:  # noqa: BLE001
                # The cell was attempted and the ledger does not know: it will be
                # re-walked in full on the next pass instead of in six hours, and
                # only this line says why.
                LOG.error("SOLD cell obec=%s left no ledger row: %s",
                          obec_kod, ledger_exc)
        return _result(obec_kod, "failed", started, error=error)
    return _result(
        obec_kod, "ok", started,
        records=stored, new=new, pages=walk.pages,
        source_total=walk.source_total, dropped=walk.dropped, error=truncated,
    )


def _truncation(walk: SoldWalk, stored: int) -> str | None:
    """What an `ok` row must still admit: the walk did not reach the end.

    Written into the ledger's `error` rather than a new status, because the cell DID
    yield facts and they are all true — what is not true is that they are all of
    them. The TTL is deliberately the full 35 days even so: re-asking tomorrow would
    walk the same first 25 pages and stop in the same place.
    """
    if not walk.truncated:
        return None
    return f"truncated: took {stored} of {walk.count} in {walk.pages} pages"


def _result(
    obec_kod: int,
    status: str,
    started: float,
    *,
    records: int = 0,
    new: int = 0,
    pages: int = 0,
    source_total: int | None = None,
    dropped: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "obec_kod": obec_kod,
        "status": status,
        "records": records,
        "new": new,
        "pages": pages,
        "source_total": source_total,
        "dropped": dropped,
        "error": error,
        "seconds": round(time.monotonic() - started, 1),
    }
