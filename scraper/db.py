"""Database I/O for the Sreality tracker.

Reads SUPABASE_DB_URL, upserts into listings, appends a row to
listing_snapshots only when the content hash changes, inserts new
image URLs, and at end of run marks unseen listings inactive.

Each listing's writes happen in one transaction so a partial failure
cannot leave the listings / listing_snapshots / images tables out of
sync for that listing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

LOG = logging.getLogger(__name__)

UpsertResult = Literal["new", "updated", "unchanged"]

LISTING_COLUMNS: tuple[str, ...] = (
    "category_main",
    "category_type",
    "price_czk",
    "price_unit",
    "area_m2",
    "disposition",
    "locality",
    "district",
    "locality_district_id",
    "locality_region_id",
    "locality_municipality_id",
    "locality_quarter_id",
    "locality_ward_id",
    "floor",
    "total_floors",
    "has_balcony",
    "has_parking",
    "has_lift",
    "building_type",
    "condition",
    "energy_rating",
    "estate_area",
    "usable_area",
    "garden_area",
    "category_sub_cb",
    "furnished",
    "terrace",
    "cellar",
    "garage",
    "parking_lots",
    "ownership",
    "description",
)


def database_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL environment variable is not set")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection. Callers manage transactions explicitly.

    prepare_threshold=None disables psycopg3's automatic prepared-statement
    caching. Required for Supabase's Transaction-mode pooler (PgBouncer),
    which rebinds connections between queries and trips
    DuplicatePreparedStatement otherwise.
    """
    return psycopg.connect(
        url or database_url(),
        autocommit=True,
        prepare_threshold=None,
    )


def upsert_listing(
    conn: psycopg.Connection,
    row: dict[str, Any],
    raw_json: dict[str, Any],
    content_hash: str,
) -> UpsertResult:
    """Upsert listings, append snapshot if content_hash differs from last.

    Returns 'new' for first insert, 'updated' if a snapshot was appended,
    'unchanged' if the listing already exists with this content_hash.
    """
    sreality_id = row["sreality_id"]
    raw_jsonb = Jsonb(raw_json)
    column_list = ", ".join(LISTING_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in LISTING_COLUMNS)
    update_set = ",\n          ".join(
        f"{c} = EXCLUDED.{c}" for c in LISTING_COLUMNS
    )

    upsert_sql = f"""
        INSERT INTO listings (
            sreality_id, last_seen_at, is_active,
            {column_list},
            geom, raw_json
        )
        VALUES (
            %(sreality_id)s, now(), true,
            {placeholders},
            CASE
              WHEN %(lon)s IS NOT NULL AND %(lat)s IS NOT NULL
              THEN ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
              ELSE NULL
            END,
            %(raw_json)s
        )
        ON CONFLICT (sreality_id) DO UPDATE SET
          last_seen_at = now(),
          is_active = true,
          {update_set},
          geom = EXCLUDED.geom,
          raw_json = EXCLUDED.raw_json
        RETURNING xmax = 0 AS inserted
    """

    params: dict[str, Any] = {
        "sreality_id": sreality_id,
        "raw_json": raw_jsonb,
        "lon": row.get("lon"),
        "lat": row.get("lat"),
    }
    for col in LISTING_COLUMNS:
        params[col] = row.get(col)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(upsert_sql, params)
        result = cur.fetchone()
        inserted = bool(result[0]) if result else False

        cur.execute(
            """
            SELECT content_hash FROM listing_snapshots
            WHERE sreality_id = %s
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (sreality_id,),
        )
        prev = cur.fetchone()
        unchanged = prev is not None and prev[0] == content_hash

        if not unchanged:
            cur.execute(
                """
                INSERT INTO listing_snapshots
                    (sreality_id, price_czk, content_hash, raw_json)
                VALUES (%s, %s, %s, %s)
                """,
                (sreality_id, row.get("price_czk"), content_hash, raw_jsonb),
            )

    if inserted:
        return "new"
    return "unchanged" if unchanged else "updated"


def record_images(
    conn: psycopg.Connection,
    sreality_id: int,
    images: Iterable[dict[str, Any]],
) -> int:
    """Insert any image rows that don't already exist. Returns newly inserted count."""
    rows = [
        (sreality_id, img["url"], img.get("sequence"))
        for img in images
        if img.get("url")
    ]
    if not rows:
        return 0

    values_sql = ", ".join("(%s, %s, %s)" for _ in rows)
    flat: list[Any] = [v for triple in rows for v in triple]
    sql = f"""
        INSERT INTO images (sreality_id, sreality_url, sequence)
        VALUES {values_sql}
        ON CONFLICT (sreality_id, sequence) DO NOTHING
        RETURNING id
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, flat)
        return cur.rowcount or 0


TOUCH_CHUNK_SIZE = 250


def touch_listings(
    conn: psycopg.Connection,
    sreality_ids: Iterable[int],
) -> int:
    """Bump last_seen_at and is_active for listings whose detail we skipped.

    Used when an index entry's price matches what is already stored, so we
    have evidence the listing is still on the market without paying for
    another detail fetch.

    Chunked because Supabase's transaction pooler enforces a statement
    timeout (~2 min) and a single UPDATE over the full id list blows past
    it. The UPDATE uses unnest+JOIN rather than `sreality_id = ANY(%s)`
    so the planner always drives off the PK index — large ANY() arrays
    can fall to a seqscan when stats are off, which is what tipped the
    20k-listing `dum prodej` category over the timeout.
    """
    ids = list(sreality_ids)
    if not ids:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(ids), TOUCH_CHUNK_SIZE):
            chunk = ids[start : start + TOUCH_CHUNK_SIZE]
            cur.execute(
                """
                UPDATE listings
                SET last_seen_at = now(),
                    is_active = true
                FROM unnest(%s::bigint[]) AS u(sreality_id)
                WHERE listings.sreality_id = u.sreality_id
                """,
                (chunk,),
            )
            total += cur.rowcount or 0
    return total


def mark_inactive(
    conn: psycopg.Connection,
    category_main: str,
    category_type: str,
    seen_ids: set[int],
) -> int:
    """Mark listings of this category not in seen_ids as is_active=false.

    Scoped to (category_main, category_type) so a per-category index walk
    only flips its own slice. Without scoping, scraping rentals would
    clobber sales `is_active`, and vice versa.
    """
    if not seen_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings
            SET is_active = false
            WHERE is_active = true
              AND category_main = %s
              AND category_type = %s
              AND sreality_id <> ALL(%s)
            """,
            (category_main, category_type, list(seen_ids)),
        )
        return cur.rowcount or 0


def mark_listing_inactive(
    conn: psycopg.Connection,
    sreality_id: int,
) -> None:
    """Flip a single listing to is_active=false.

    Used when a detail fetch reports the listing is gone (404/410 or
    sreality's 'page does not exist' body) — a delisting detected mid-run,
    independent of the end-of-walk index-absence sweep in mark_inactive.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE listings SET is_active = false WHERE sreality_id = %s",
            (sreality_id,),
        )


def index_summary(
    conn: psycopg.Connection,
    sreality_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    """Fetch (price_czk, last_seen_at) for the given ids.

    Used by main.py to decide whether to refetch the detail endpoint
    based on price changes seen in the index, without burning a detail
    request when nothing has changed.
    """
    ids = list(sreality_ids)
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sreality_id, price_czk, last_seen_at
            FROM listings
            WHERE sreality_id = ANY(%s)
            """,
            (ids,),
        )
        return {
            sreality_id: {"price_czk": price_czk, "last_seen_at": last_seen_at}
            for sreality_id, price_czk, last_seen_at in cur.fetchall()
        }


def active_count(
    conn: psycopg.Connection,
    category_main: str,
    category_type: str,
) -> int:
    """Current active-listing count for one (category_main, category_type)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM listings
            WHERE is_active = true
              AND category_main = %s
              AND category_type = %s
            """,
            (category_main, category_type),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def pending_image_downloads(
    conn: psycopg.Connection,
    max_attempts: int = 5,
    limit: int = 1000,
    active_only: bool = False,
) -> list[tuple[int, int, int | None, str, str | None, str | None]]:
    """Return (image_id, sreality_id, sequence, sreality_url, category_main, category_type)
    rows that still need download.

    Filters out images already stored (storage_path IS NOT NULL),
    images we have given up on (download_attempts >= max_attempts), and
    images terminally classified as unavailable (unavailable_reason IS
    NOT NULL — e.g. the parent listing was taken down).

    With `active_only=True`, restrict to images whose parent listing is
    `is_active = true` — the backfill workflow's prioritisation knob,
    so the cap-bounded slice goes to listings users can still browse.

    Ordering puts active listings first (when both kinds are in scope)
    and newest within each tier so freshly-discovered active images
    drain before old inactive ones. The category columns come from the
    parent listing so the image-download phase can attribute its
    results per (category_main, category_type) on the scrape_runs row.
    """
    where_active = "AND l.is_active = true" if active_only else ""
    order_clause = (
        "ORDER BY i.id DESC"
        if active_only
        else "ORDER BY (l.is_active IS TRUE) DESC NULLS LAST, i.id DESC"
    )
    sql = f"""
        SELECT i.id, i.sreality_id, i.sequence, i.sreality_url,
               l.category_main, l.category_type
        FROM images i
        LEFT JOIN listings l ON l.sreality_id = i.sreality_id
        WHERE i.storage_path IS NULL
          AND i.unavailable_reason IS NULL
          AND i.download_attempts < %s
          {where_active}
        {order_clause}
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (max_attempts, limit))
        return list(cur.fetchall())


def mark_image_stored(
    conn: psycopg.Connection,
    image_id: int,
    storage_path: str,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET storage_path = %s,
                last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """,
            (storage_path, image_id),
        )


def mark_image_attempt(
    conn: psycopg.Connection,
    image_id: int,
    error: str | None = None,
) -> None:
    """Record one failed image-download attempt.

    Persists the exception text on `images.last_error` (truncated to
    500 chars) so post-hoc diagnosis works without scraping CI logs.
    """
    truncated = (error or "")[:500] if error is not None else None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET last_download_attempt_at = now(),
                download_attempts = download_attempts + 1,
                last_error = COALESCE(%s, last_error)
            WHERE id = %s
            """,
            (truncated, image_id),
        )


def mark_image_listing_taken_down(
    conn: psycopg.Connection,
    sreality_id: int,
) -> int:
    """Mark every pending image of a gone listing as terminally unavailable.

    Called by the image-download phase after a freshness check confirms
    the parent listing returns 404/410 from sreality. The reason
    'listing_taken_down' is the operator's "image not downloaded in
    time" semantic — it's a state, not a download failure.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET unavailable_reason = 'listing_taken_down',
                last_download_attempt_at = now()
            WHERE sreality_id = %s
              AND storage_path IS NULL
              AND unavailable_reason IS NULL
            """,
            (sreality_id,),
        )
        return cur.rowcount or 0


FAILURE_GIVE_UP_THRESHOLD = 5


def record_fetch_failure(
    conn: psycopg.Connection,
    sreality_id: int,
    error_message: str,
    max_attempts: int = FAILURE_GIVE_UP_THRESHOLD,
) -> None:
    """Record a failed detail fetch. Marks given_up at max_attempts."""
    truncated = (error_message or "")[:500]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO listing_fetch_failures
                (sreality_id, attempts, first_failure_at, last_failure_at, last_error, given_up)
            VALUES (%s, 1, now(), now(), %s, false)
            ON CONFLICT (sreality_id) DO UPDATE SET
              attempts = listing_fetch_failures.attempts + 1,
              last_failure_at = now(),
              last_error = EXCLUDED.last_error,
              given_up = (listing_fetch_failures.attempts + 1) >= %s
            """,
            (sreality_id, truncated, max_attempts),
        )


def clear_fetch_failure(
    conn: psycopg.Connection,
    sreality_id: int,
) -> None:
    """Remove the failure row after a successful fetch."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM listing_fetch_failures WHERE sreality_id = %s",
            (sreality_id,),
        )


def scrape_run_start(
    conn: psycopg.Connection,
    run_type: str,
) -> int:
    """Open a new scrape_runs row. Returns the id."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scrape_runs (run_type)
            VALUES (%s)
            RETURNING id
            """,
            (run_type,),
        )
        result = cur.fetchone()
        return int(result[0])


def scrape_run_finalize(
    conn: psycopg.Connection,
    run_id: int,
    *,
    index_pages: int = 0,
    listings_found_new: int = 0,
    listings_scraped_new: int = 0,
    listings_updated: int = 0,
    listings_inactive: int = 0,
    images_discovered: int = 0,
    images_stored: int = 0,
    errors: int = 0,
    by_category: list[dict[str, Any]] | None = None,
) -> None:
    """Close out the scrape_runs row with aggregate counters."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scrape_runs
            SET ended_at             = now(),
                index_pages          = %s,
                listings_found_new   = %s,
                listings_scraped_new = %s,
                listings_updated     = %s,
                listings_inactive    = %s,
                images_discovered    = %s,
                images_stored        = %s,
                errors               = %s,
                by_category          = %s
            WHERE id = %s
            """,
            (
                index_pages,
                listings_found_new,
                listings_scraped_new,
                listings_updated,
                listings_inactive,
                images_discovered,
                images_stored,
                errors,
                Jsonb(by_category or []),
                run_id,
            ),
        )


def active_failure_ids(
    conn: psycopg.Connection,
    sreality_ids: Iterable[int],
) -> set[int]:
    """Return ids in this set that have an active (not given_up) failure row.

    Used by main.py to prioritise these in to_refetch so the per-run
    cap doesn't keep deferring listings that are consistently late in
    the index ordering.
    """
    ids = list(sreality_ids)
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sreality_id FROM listing_fetch_failures
            WHERE given_up = false
              AND sreality_id = ANY(%s)
            """,
            (ids,),
        )
        return {row[0] for row in cur.fetchall()}
