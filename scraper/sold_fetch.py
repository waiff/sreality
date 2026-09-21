"""One municipality cell, fetched (sold-comps W2).

Composes the three single-purpose modules — `sold_db` for the box and the writes,
`reas_client` for the HTTP, `reas_parser` for the payload — and owns exactly one
decision of its own: what happens when a cell goes wrong.

A CELL ATTEMPT ALWAYS ENDS IN THE LEDGER, never in an exception. `fetch_cell`
returns a status; the one thing it cannot do is raise into the worker lane, because
a cell that failed silently is indistinguishable from a cell that holds no sales.

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

import contextlib
import logging
import time
from typing import Any

import psycopg

from scraper import sold_db
from scraper.reas_client import ReasClient
from scraper.reas_parser import SOURCE, SoldTransaction, parse_sold_html

LOG = logging.getLogger(__name__)

# 25 pages of 100 = 2,500 records. The only cell that paginates at all is Praha,
# which is ONE obec holding ~1,100 sales in the source's 24-month window = 11 pages
# of ~1.9 MB; the cap is therefore a runaway guard, not a coverage limit, and
# `pages == MAX_PAGES` in the ledger is the tell that it ever bound.
MAX_PAGES = 25

# The ledger's `error` is a diagnosis, not a payload: a stack-shaped message from a
# thousand-page HTML body must not become the widest column in the table.
_ERROR_MAX = 500


def walk_bounds(
    client: ReasClient,
    bounds: tuple[float, float, float, float],
    *,
    max_pages: int = MAX_PAGES,
) -> tuple[list[SoldTransaction], int, int | None, int]:
    """Pages 1..max_pages of one box -> (rows, pages, source_total, dropped).

    Stops as soon as the source says there is no next page — `nextPage` is the
    only walk terminator, and outside Praha it is null on page 1.
    """
    rows: list[SoldTransaction] = []
    source_total: int | None = None
    dropped = 0
    pages = 0
    next_page: int | None = 1
    while next_page is not None and pages < max_pages:
        page = parse_sold_html(client.fetch_sold_page(bounds, page=next_page))
        pages += 1
        rows.extend(page.rows)
        dropped += page.dropped_broker_reported
        if source_total is None:
            source_total = page.possible_count
        next_page = page.next_page
    return rows, pages, source_total, dropped


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
            # No polygon, so no cell and no box to key a ledger row on.
            return _result(obec_kod, "skipped", started,
                           error="obec has no boundary polygon")
        rows, pages, source_total, dropped = walk_bounds(
            client,
            (box.sw_lat, box.sw_lng, box.ne_lat, box.ne_lng),
            max_pages=max_pages,
        )
        new = sold_db.upsert_sold_transactions(conn, rows)
        sold_db.record_fetch(
            conn, source=SOURCE, obec_kod=obec_kod, bbox=box.ewkt, status="ok",
            record_count=len(rows), source_total=source_total, pages=pages,
        )
    except Exception as exc:  # noqa: BLE001 - a cell failure is a ledger row
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX]
        LOG.warning("SOLD cell obec=%s failed: %s", obec_kod, error)
        if box is not None:
            with contextlib.suppress(Exception):
                sold_db.record_fetch(
                    conn, source=SOURCE, obec_kod=obec_kod, bbox=box.ewkt,
                    status="failed", error=error,
                )
        return _result(obec_kod, "failed", started, error=error)
    return _result(
        obec_kod, "ok", started,
        records=len(rows), new=new, pages=pages,
        source_total=source_total, dropped=dropped,
    )


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
