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

from scraper.scraped_listing import ScrapedListing

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


def upsert_listing_with_property(
    conn: psycopg.Connection,
    row: dict[str, Any],
    raw_json: dict[str, Any],
    content_hash: str,
) -> UpsertResult:
    """upsert_listing + maintain the listing's canonical `properties` parent.

    The listing write and its property linkage commit in one transaction so a
    partial failure can't leave a listing unlinked. New listings run through
    the Tier-1 spatial matcher (`_match_or_create_property`); for sreality the
    matcher is inert (every existing property already has a sreality child,
    which the same-source exclusion skips), so this preserves the historical
    singleton-per-listing behaviour until a second source lands.
    """
    sreality_id = row["sreality_id"]
    with conn.transaction():
        result = upsert_listing(conn, row, raw_json, content_hash)
        _ensure_property(conn, sreality_id, "sreality")
    return result


def ingest_scraped_listing(
    conn: psycopg.Connection, listing: ScrapedListing,
) -> UpsertResult:
    """Write a non-sreality ScrapedListing through the same matcher path.

    Tier 0: (source, source_id_native) is the idempotency key — a re-fetch
    reuses the existing synthetic PK and updates in place; first sight draws a
    fresh negative PK from `synthetic_listing_id_seq`. The listing write +
    source identity + Tier-1 property matching then commit in one transaction.
    `upsert_listing` doesn't manage the source columns, so they're stamped
    right after the write, before the matcher reads `source`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id FROM listings "
            "WHERE source = %s AND source_id_native = %s",
            (listing.source, listing.source_id_native),
        )
        found = cur.fetchone()
    if found is not None:
        pk = int(found[0])
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('synthetic_listing_id_seq')")
            pk = int(cur.fetchone()[0])

    row = listing.to_row(pk)
    with conn.transaction():
        result = upsert_listing(conn, row, listing.raw or {}, listing.content_hash())
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET source = %s, source_url = %s, "
                "source_id_native = %s WHERE sreality_id = %s",
                (listing.source, listing.source_url, listing.source_id_native, pk),
            )
        _ensure_property(conn, pk, listing.source)
    return result


def _ensure_property(conn: psycopg.Connection, listing_pk: int, source: str) -> None:
    """Attach the listing to its canonical property, or refresh it if linked.

    Runs inside the caller's transaction (no own transaction block). A new
    (unlinked) listing goes through the Tier-1 matcher; an already-linked one
    gets a cheap rollup of its property.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings SET source_id_native = sreality_id::text
            WHERE sreality_id = %s AND source_id_native IS NULL
            """,
            (listing_pk,),
        )
        cur.execute(
            "SELECT property_id FROM listings WHERE sreality_id = %s",
            (listing_pk,),
        )
        found = cur.fetchone()
        property_id = found[0] if found else None

    if property_id is None:
        _match_or_create_property(conn, listing_pk, source)
    else:
        _cheap_property_rollup(conn, listing_pk)


def _match_or_create_property(
    conn: psycopg.Connection, listing_pk: int, source: str,
) -> None:
    """Tier-1 insert-time matcher (multi-portal dedup design).

    Probe `properties` for a spatial+price+area near-match that doesn't already
    have a child from this `source`:
      * exactly one hit  -> attach (this property gains a second source);
      * zero hits        -> new singleton property (the historical behaviour);
      * two or more hits  -> new singleton + enqueue each ambiguous pair into
        `property_identity_candidates` for operator review. Never guess.

    The same-source exclusion is what keeps this inert for sreality-only data:
    a new sreality listing's neighbours all already carry a sreality child, so
    nothing matches and it falls through to a fresh singleton.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price_czk, area_m2 FROM listings WHERE sreality_id = %s",
            (listing_pk,),
        )
        keyrow = cur.fetchone()
    price = keyrow[0] if keyrow else None
    area = keyrow[1] if keyrow else None

    hits: list[int] = []
    if price is not None and area is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id FROM properties p
                WHERE p.geom IS NOT NULL
                  AND p.current_price_czk IS NOT NULL
                  AND p.area_m2 IS NOT NULL
                  AND ST_DWithin(
                        p.geom,
                        (SELECT geom FROM listings WHERE sreality_id = %(pk)s),
                        20)
                  AND p.current_price_czk BETWEEN %(price)s * 0.98 AND %(price)s * 1.02
                  AND p.area_m2 BETWEEN %(area)s - 1 AND %(area)s + 1
                  AND NOT EXISTS (
                        SELECT 1 FROM listings c
                        WHERE c.property_id = p.id AND c.source = %(source)s)
                LIMIT 2
                """,
                {"pk": listing_pk, "price": price, "area": area, "source": source},
            )
            hits = [int(r[0]) for r in cur.fetchall()]

    if len(hits) == 1:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET property_id = %s WHERE sreality_id = %s",
                (hits[0], listing_pk),
            )
        _cheap_property_rollup(conn, listing_pk)
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO properties (
                repr_listing_id, category_main, category_type, disposition,
                area_m2, district, geom, current_price_czk,
                is_active, first_seen_at, last_seen_at,
                source_count, distinct_site_count
            )
            SELECT
                sreality_id, category_main, category_type, disposition,
                area_m2, district, geom, price_czk,
                is_active, first_seen_at, last_seen_at, 1, 1
            FROM listings WHERE sreality_id = %s
            RETURNING id
            """,
            (listing_pk,),
        )
        new_pid = int(cur.fetchone()[0])
        cur.execute(
            "UPDATE listings SET property_id = %s WHERE sreality_id = %s",
            (new_pid, listing_pk),
        )

    if len(hits) >= 2:
        markers = Jsonb({
            "price_czk": price,
            "area_m2": float(area) if area is not None else None,
            "radius_m": 20,
        })
        with conn.cursor() as cur:
            for h in hits:
                lo, hi = (h, new_pid) if h < new_pid else (new_pid, h)
                cur.execute(
                    """
                    INSERT INTO property_identity_candidates
                        (left_property_id, right_property_id, tier, markers_matched)
                    VALUES (%s, %s, 'tier1', %s)
                    ON CONFLICT (left_property_id, right_property_id) DO NOTHING
                    """,
                    (lo, hi, markers),
                )


def _cheap_property_rollup(conn: psycopg.Connection, listing_pk: int) -> None:
    """Insert-time rollup for one property: counts + lifecycle always; the
    display columns are mirrored from this child only while the property is a
    singleton. For multi-source properties the representative + price-history +
    denormalised filter columns are owned by the async recompute job
    (decision #2), so we leave them untouched here.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE properties p SET
                source_count        = agg.cnt,
                distinct_site_count = agg.dcnt,
                last_seen_at        = agg.last_seen,
                is_active           = agg.active,
                current_price_czk   = CASE WHEN agg.cnt = 1 THEN l.price_czk     ELSE p.current_price_czk END,
                area_m2             = CASE WHEN agg.cnt = 1 THEN l.area_m2        ELSE p.area_m2 END,
                district            = CASE WHEN agg.cnt = 1 THEN l.district       ELSE p.district END,
                disposition         = CASE WHEN agg.cnt = 1 THEN l.disposition    ELSE p.disposition END,
                geom                = CASE WHEN agg.cnt = 1 THEN l.geom           ELSE p.geom END,
                category_main       = CASE WHEN agg.cnt = 1 THEN l.category_main  ELSE p.category_main END,
                category_type       = CASE WHEN agg.cnt = 1 THEN l.category_type  ELSE p.category_type END
            FROM listings l
            JOIN LATERAL (
                SELECT count(*) AS cnt, count(DISTINCT source) AS dcnt,
                       max(last_seen_at) AS last_seen, bool_or(is_active) AS active
                FROM listings WHERE property_id = l.property_id
            ) agg ON true
            WHERE p.id = l.property_id AND l.sreality_id = %s
            """,
            (listing_pk,),
        )


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
    # Refresh the URL on conflict so a re-detail-fetch repoints a stale/rotated
    # CDN path on a not-yet-downloaded image (and clears its stale error state).
    # The storage_path IS NULL guard is load-bearing: an already-downloaded image
    # is never disturbed, so we never re-download what we have. xmax = 0 is true
    # only for genuine inserts, keeping the "newly inserted" count honest.
    sql = f"""
        INSERT INTO images (sreality_id, sreality_url, sequence)
        VALUES {values_sql}
        ON CONFLICT (sreality_id, sequence) DO UPDATE SET
            sreality_url = EXCLUDED.sreality_url,
            download_attempts = 0,
            last_error = NULL,
            unavailable_reason = NULL
        WHERE images.storage_path IS NULL
        RETURNING (xmax = 0) AS inserted
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, flat)
        return sum(1 for (inserted,) in cur.fetchall() if inserted)


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


def mark_image_unavailable(
    conn: psycopg.Connection,
    image_id: int,
    reason: str,
    error: str | None = None,
) -> None:
    """Terminally mark ONE image unavailable so it drops out of the
    pending-downloads queue.

    Used when the image's sreality CDN URL returns 404/410 — an expired,
    permanently-dead URL, not a transient failure worth retrying. Distinct
    from mark_image_listing_taken_down (which marks every image of a gone
    listing); here only this one URL is dead while the listing lives on.
    """
    truncated = (error or "")[:500] if error is not None else None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET unavailable_reason = %s,
                last_error = COALESCE(%s, last_error),
                last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """,
            (reason, truncated, image_id),
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


def sweep_stuck_scrape_runs(
    conn: psycopg.Connection,
    *,
    older_than_minutes: int = 90,
) -> int:
    """Finalize scrape_runs hard-killed before scrape_run_finalize ran.

    A GitHub-Actions job killed at the job timeout (SIGKILL) can't write
    ended_at, so the row stays orphaned and the Health 'runs finishing
    cleanly' check counts it 'stuck'. Stamp ended_at so a hard-kill self-heals
    on the next API boot. Counters stay as-is (zeros) — the row correctly
    reflects that it never reported aggregates. The cutoff must stay above the
    scrape job timeout so a still-running walk is never finalized.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scrape_runs SET ended_at = now()
            WHERE ended_at IS NULL
              AND started_at < now() - make_interval(mins => %s)
            RETURNING id
            """,
            (older_than_minutes,),
        )
        return len(cur.fetchall())


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


def upsert_portal_raw_page(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    source_url: str,
    page_kind: str,
    html: str,
    http_status: int | None,
) -> int:
    """Latest-wins upsert of one fetched HTML page into portal_raw_pages.

    Decouples fetch from parse so a page can be re-parsed without re-fetching.
    Returns the staging row id; a re-fetch overwrites the HTML and clears the
    previous parse state.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO portal_raw_pages
                (source, source_id_native, source_url, page_kind,
                 html, http_status, fetched_at, parsed_at, parse_error)
            VALUES (%s, %s, %s, %s, %s, %s, now(), NULL, NULL)
            ON CONFLICT (source, source_id_native, page_kind) DO UPDATE SET
                source_url  = EXCLUDED.source_url,
                html        = EXCLUDED.html,
                http_status = EXCLUDED.http_status,
                fetched_at  = now(),
                parsed_at   = NULL,
                parse_error = NULL
            RETURNING id
            """,
            (source, source_id_native, source_url, page_kind, html, http_status),
        )
        return int(cur.fetchone()[0])


def mark_portal_page_parsed(
    conn: psycopg.Connection, page_id: int, *, parse_error: str | None = None
) -> None:
    """Stamp a portal_raw_pages row parsed (or record why parsing failed)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE portal_raw_pages SET parsed_at = now(), parse_error = %s "
            "WHERE id = %s",
            (parse_error, page_id),
        )
