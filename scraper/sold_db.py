"""Database I/O for the sold-transactions store (sold-comps W2). SQL only.

Everything here is keyed on the municipality CELL, which is the unit the source is
fetched in: the obec's expanded envelope box, the work-list of cells the deal
pipeline makes worth fetching, the batched row upsert, and the one append-only
ledger row per cell attempt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scraper.db import connect  # noqa: F401 (re-export; also installs the jsonb serializer)
from scraper.reas_parser import SoldTransaction

# The box margin, and the largest radius the read surface offers. ONE constant:
# expanding every cell by the widest read means any subject inside the obec is
# covered out to its full radius by construction, with no second fetch.
MAX_READ_RADIUS_M = 5000

# A cell's rows are re-fetched at most this often once a walk succeeded — the
# source republishes a transfer ~30 days after the sale, so a month-and-change is
# the cadence at which a cell can actually have changed.
CELL_OK_TTL_SECONDS = 35 * 24 * 3600
# A failure is retried sooner, but not immediately: the failure modes are the
# source being unreachable and its contract having moved, and neither heals in
# minutes.
CELL_FAILED_TTL_SECONDS = 6 * 3600

# Both relations, in one probe: the lane must be able to tell "nothing to do" from
# "this database has not been migrated yet" (a branch DB, or main before apply).
_SOLD_STORE_PRESENT_SQL = """
    SELECT to_regclass('public.sold_transactions') IS NOT NULL
       AND to_regclass('public.sold_transaction_fetches') IS NOT NULL
"""

# The cell box. `admin_boundaries.id` IS the ČÚZK/RÚIAN code of the unit (migration
# 017's header: `sreality_id` is the separate bridge into sreality's own id space),
# so it joins directly to `listing_location.obec_kod` and to reas's `municipalityId`.
# The envelope is taken FIRST and the margin applied to that rectangle: buffering a
# 50 m-simplified obec multipolygon would cost thousands of vertices for the same
# four numbers. `geom` is geography, so the margin is metres without a projection.
_OBEC_CELL_BOX_SQL = """
    SELECT ST_YMin(cell.box), ST_XMin(cell.box),
           ST_YMax(cell.box), ST_XMax(cell.box),
           ST_AsEWKT(cell.box)
    FROM (
        SELECT ST_Envelope(
                   ST_Buffer(
                       ST_Envelope(ab.geom::geometry)::geography,
                       %(margin_m)s::double precision
                   )::geometry
               ) AS box
        FROM admin_boundaries ab
        WHERE ab.level = 'obec' AND ab.id = %(obec_kod)s
    ) cell
"""

# The work-list. A cell earns a fetch when the deal pipeline holds a live card on a
# property in it — ANY account's card (DISTINCT over accounts: two accounts carding
# one property is one cell, and one of them removing their card must not stop the
# other's fetch). Terminal and archived stages are excluded from the REFRESH only;
# rows already stored stay readable for ever, because a closed deal is exactly where
# the comps must survive.
#
# Freshness is subtracted from the NEWEST ledger row per (source, obec_kod) — the
# ledger is append-only, so "the cell's state" is its last row and nothing else. A
# cell that has never been looked at sorts first.
#
# The `admin_boundaries` join is not decoration. A cell with no polygon has no box,
# so a fetch can only skip it — and a skip writes no ledger row, which leaves
# `fetched_at` NULL, which sorts that cell FIRST again on the very next pass. One
# obec missing from the boundary ingest would hold a slot of the cap for ever. A
# cell that cannot be boxed is not work, so it is never offered.
_SOLD_COMP_CELLS_SQL = """
    WITH cells AS (
        SELECT DISTINCT ll.obec_kod AS obec_kod
        FROM property_pipeline pp
        JOIN pipeline_stages ps
          ON ps.account_id = pp.account_id AND ps.id = pp.stage_id
        JOIN properties p ON p.id = pp.property_id
        JOIN listing_location ll ON ll.listing_id = p.repr_listing_ref_id
        JOIN admin_boundaries ab
          ON ab.level = 'obec' AND ab.id = ll.obec_kod
        WHERE p.status = 'active'
          AND p.is_active
          AND p.category_main IN ('byt', 'dum')
          AND ps.archived_at IS NULL
          AND NOT ps.is_terminal
          AND ll.geom IS NOT NULL
          AND ll.obec_kod IS NOT NULL
    )
    SELECT c.obec_kod
    FROM cells c
    LEFT JOIN LATERAL (
        SELECT f.status, f.fetched_at
        FROM sold_transaction_fetches f
        WHERE f.source = %(source)s AND f.obec_kod = c.obec_kod
        ORDER BY f.fetched_at DESC, f.id DESC
        LIMIT 1
    ) newest ON true
    WHERE newest.fetched_at IS NULL
       OR (newest.status = 'ok'
           AND newest.fetched_at < now() - make_interval(
               secs => %(ok_ttl)s::double precision))
       OR (newest.status = 'failed'
           AND newest.fetched_at < now() - make_interval(
               secs => %(failed_ttl)s::double precision))
    ORDER BY newest.fetched_at NULLS FIRST, c.obec_kod
    LIMIT %(cap)s
"""

# A re-seen sale OVERWRITES: the source revises a transfer's attributes (and its
# photo set) after first publishing it, and this table holds the current fact, not
# its history. `fetched_at` is restamped so the row always says when we last looked.
_UPSERT_SOLD_SQL = """
    INSERT INTO sold_transactions (
        source, source_record_id, sold_at, price_czk, asking_last_czk,
        listed_at, published_at, category_main, category_type, subtype,
        disposition, area_m2, area_basis, usable_area, estate_area,
        geom, address_text, obec_kod, ku_kod, ulice_kod,
        photo_urls, source_url, fetched_at, raw)
    SELECT j.source, j.source_record_id, j.sold_at, j.price_czk, j.asking_last_czk,
           j.listed_at, j.published_at, j.category_main, j.category_type, j.subtype,
           j.disposition, j.area_m2, j.area_basis, j.usable_area, j.estate_area,
           ST_GeomFromEWKT(j.geom), j.address_text, j.obec_kod, j.ku_kod, j.ulice_kod,
           j.photo_urls, j.source_url, now(), j.raw
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS j(
        source text, source_record_id text, sold_at date, price_czk integer,
        asking_last_czk integer, listed_at timestamptz, published_at timestamptz,
        category_main text, category_type text, subtype text, disposition text,
        area_m2 numeric, area_basis text, usable_area numeric, estate_area numeric,
        geom text, address_text text, obec_kod bigint, ku_kod bigint, ulice_kod bigint,
        photo_urls text[], source_url text, raw jsonb)
    ON CONFLICT (source, source_record_id) DO UPDATE SET
        sold_at         = EXCLUDED.sold_at,
        price_czk       = EXCLUDED.price_czk,
        asking_last_czk = EXCLUDED.asking_last_czk,
        listed_at       = EXCLUDED.listed_at,
        published_at    = EXCLUDED.published_at,
        category_main   = EXCLUDED.category_main,
        category_type   = EXCLUDED.category_type,
        subtype         = EXCLUDED.subtype,
        disposition     = EXCLUDED.disposition,
        area_m2         = EXCLUDED.area_m2,
        area_basis      = EXCLUDED.area_basis,
        usable_area     = EXCLUDED.usable_area,
        estate_area     = EXCLUDED.estate_area,
        geom            = EXCLUDED.geom,
        address_text    = EXCLUDED.address_text,
        obec_kod        = EXCLUDED.obec_kod,
        ku_kod          = EXCLUDED.ku_kod,
        ulice_kod       = EXCLUDED.ulice_kod,
        photo_urls      = EXCLUDED.photo_urls,
        source_url      = EXCLUDED.source_url,
        fetched_at      = EXCLUDED.fetched_at,
        raw             = EXCLUDED.raw
    RETURNING (xmax = 0) AS inserted
"""

_RECORD_FETCH_SQL = """
    INSERT INTO sold_transaction_fetches (
        source, obec_kod, bbox, status, record_count, source_total, pages, error)
    VALUES (%(source)s, %(obec_kod)s, ST_GeomFromEWKT(%(bbox)s), %(status)s,
            %(record_count)s, %(source_total)s, %(pages)s, %(error)s)
"""


@dataclass(frozen=True)
class CellBox:
    """One obec's fetch box: the four bounds the source wants, and the polygon the
    ledger records so a stored row can always be traced to the box that found it."""

    sw_lat: float
    sw_lng: float
    ne_lat: float
    ne_lng: float
    ewkt: str


def obec_cell_box(conn: psycopg.Connection, obec_kod: int) -> CellBox | None:
    """The obec's envelope expanded by `MAX_READ_RADIUS_M`, or None if it has no
    polygon (an obec absent from the boundary ingest has no cell)."""
    with conn.cursor() as cur:
        cur.execute(_OBEC_CELL_BOX_SQL, {
            "margin_m": float(MAX_READ_RADIUS_M), "obec_kod": int(obec_kod),
        })
        row = cur.fetchone()
    if row is None:
        return None
    return CellBox(
        sw_lat=float(row[0]), sw_lng=float(row[1]),
        ne_lat=float(row[2]), ne_lng=float(row[3]), ewkt=str(row[4]),
    )


def sold_comp_cells(
    conn: psycopg.Connection, source: str, *, cap: int
) -> list[int] | None:
    """The obec cells worth fetching now, never-looked-at first, at most `cap`.

    None (not an empty list) when the store is absent: a caller must be able to
    tell "nothing to do" from "this database cannot answer", so the lane skips on
    an unmigrated database instead of raising every tick.
    """
    with conn.cursor() as cur:
        cur.execute(_SOLD_STORE_PRESENT_SQL)
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        cur.execute(_SOLD_COMP_CELLS_SQL, {
            "source": source,
            "ok_ttl": float(CELL_OK_TTL_SECONDS),
            "failed_ttl": float(CELL_FAILED_TTL_SECONDS),
            "cap": int(cap),
        })
        return [int(r[0]) for r in cur.fetchall()]


def upsert_sold_transactions(
    conn: psycopg.Connection, rows: Sequence[SoldTransaction]
) -> tuple[int, int]:
    """Write a cell's rows in one statement -> (sales stored, of which new).

    Deduped on the natural key first: `ON CONFLICT` cannot touch one row twice in
    a single statement, and a page that carried the same transfer twice would
    otherwise abort the whole write. STORED is what the statement actually wrote,
    which is what the ledger must count — a walk whose page boundary shifted
    mid-flight parses one sale twice and would otherwise be recorded as two.
    """
    if not rows:
        return 0, 0
    deduped: dict[str, SoldTransaction] = {r.source_record_id: r for r in rows}
    payload: list[dict[str, Any]] = [asdict(r) for r in deduped.values()]
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SOLD_SQL, {"rows": Jsonb(payload)})
        written = cur.fetchall()
    return len(written), sum(1 for (inserted,) in written if inserted)


def record_fetch(
    conn: psycopg.Connection,
    *,
    source: str,
    obec_kod: int,
    bbox: str,
    status: str,
    record_count: int = 0,
    source_total: int | None = None,
    pages: int | None = None,
    error: str | None = None,
) -> None:
    """Append one ledger row for one cell attempt. A zero-yield fetch writes one
    too: "we asked this cell and it held nothing" is the fact that separates an
    empty answer from a lane that never ran."""
    with conn.cursor() as cur:
        cur.execute(_RECORD_FETCH_SQL, {
            "source": source,
            "obec_kod": int(obec_kod),
            "bbox": bbox,
            "status": status,
            "record_count": int(record_count),
            "source_total": source_total,
            "pages": pages,
            "error": error,
        })
