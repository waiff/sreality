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
    "floor",
    "total_floors",
    "has_balcony",
    "has_parking",
    "has_lift",
    "building_type",
    "condition",
    "energy_rating",
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


def touch_listings(
    conn: psycopg.Connection,
    sreality_ids: Iterable[int],
) -> int:
    """Bump last_seen_at and is_active for listings whose detail we skipped.

    Used when an index entry's price matches what is already stored, so we
    have evidence the listing is still on the market without paying for
    another detail fetch.
    """
    ids = list(sreality_ids)
    if not ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings
            SET last_seen_at = now(),
                is_active = true
            WHERE sreality_id = ANY(%s)
            """,
            (ids,),
        )
        return cur.rowcount or 0


def mark_inactive(
    conn: psycopg.Connection,
    seen_ids: set[int],
) -> int:
    """Mark listings not in seen_ids as is_active=false. Returns affected row count."""
    if not seen_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings
            SET is_active = false
            WHERE is_active = true
              AND sreality_id <> ALL(%s)
            """,
            (list(seen_ids),),
        )
        return cur.rowcount or 0


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


def pending_image_downloads(
    conn: psycopg.Connection,
    max_attempts: int = 5,
    limit: int = 1000,
) -> list[tuple[int, int, int | None, str]]:
    """Return (image_id, sreality_id, sequence, sreality_url) rows that still need download.

    Filters out images already stored (storage_path IS NOT NULL) and ones
    we have given up on (download_attempts >= max_attempts).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, sreality_id, sequence, sreality_url
            FROM images
            WHERE storage_path IS NULL
              AND download_attempts < %s
            ORDER BY id
            LIMIT %s
            """,
            (max_attempts, limit),
        )
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
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """,
            (image_id,),
        )


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
