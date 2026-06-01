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
from collections.abc import Iterable, Sequence
from typing import Any, Literal, Protocol

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
    "street",
    "house_number",
    "zip",
    "street_id",
)

# Postgres type for each LISTING_COLUMN, used to build the jsonb_to_recordset
# column spec for the batched detail-drain write (write_detail_batch). Kept in
# lockstep with LISTING_COLUMNS by the assertion below so a new scraper column
# can't silently break the batch path.
_LISTING_COLUMN_PGTYPE: dict[str, str] = {
    "category_main": "text",
    "category_type": "text",
    "price_czk": "integer",
    "price_unit": "text",
    "area_m2": "numeric",
    "disposition": "text",
    "locality": "text",
    "district": "text",
    "locality_district_id": "integer",
    "locality_region_id": "integer",
    "locality_municipality_id": "integer",
    "locality_quarter_id": "integer",
    "locality_ward_id": "integer",
    "floor": "integer",
    "total_floors": "integer",
    "has_balcony": "boolean",
    "has_parking": "boolean",
    "has_lift": "boolean",
    "building_type": "text",
    "condition": "text",
    "energy_rating": "text",
    "estate_area": "numeric",
    "usable_area": "numeric",
    "garden_area": "numeric",
    "category_sub_cb": "integer",
    "furnished": "text",
    "terrace": "boolean",
    "cellar": "boolean",
    "garage": "boolean",
    "parking_lots": "integer",
    "ownership": "text",
    "description": "text",
    "street": "text",
    "house_number": "text",
    "zip": "text",
    "street_id": "integer",
}
assert set(_LISTING_COLUMN_PGTYPE) == set(LISTING_COLUMNS), (
    "_LISTING_COLUMN_PGTYPE drifted from LISTING_COLUMNS"
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


def connect_session(url: str | None = None) -> psycopg.Connection:
    """Open a connection that lets psycopg3 auto-prepare statements.

    For the scraper's hot detail-write loop only. Points at SUPABASE_DB_SESSION_URL
    (Supabase's Session-mode pooler, port 5432), where each client gets a dedicated
    backend, so leaving prepare_threshold at psycopg3's default is safe: the repeated
    upsert + spatial SQL gets server-side prepared (plan cached once, not re-derived
    on every listing) without risking DuplicatePreparedStatement the way the rebinding
    Transaction-mode pooler would.

    Falls back to connect() when SUPABASE_DB_SESSION_URL is unset, so environments
    without the secret keep working on the Transaction-mode pooler.
    """
    session_url = url or os.environ.get("SUPABASE_DB_SESSION_URL")
    if not session_url:
        return connect()
    return psycopg.connect(session_url, autocommit=True)


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
              -- Cast to double precision so a NULL lon/lat (common for bazos and
              -- other portals; rare for sreality) carries a concrete type. Without
              -- the cast psycopg sends untyped NULL and Postgres can't resolve the
              -- parameter type inside IS NOT NULL / ST_MakePoint, failing the whole
              -- insert ("could not determine data type of parameter").
              WHEN %(lon)s::double precision IS NOT NULL
               AND %(lat)s::double precision IS NOT NULL
              THEN ST_SetSRID(
                     ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision),
                     4326
                   )::geography
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
    partial failure can't leave a listing unlinked. New listings become their
    own singleton property (`_create_singleton_property`); cross-listing
    grouping is the out-of-band street+disposition dedup engine's job, not the
    insert path.
    """
    sreality_id = row["sreality_id"]
    with conn.transaction():
        result = upsert_listing(conn, row, raw_json, content_hash)
        _ensure_property(conn, sreality_id, "sreality")
    return result


def ingest_scraped_listing(
    conn: psycopg.Connection, listing: ScrapedListing,
) -> tuple[int, UpsertResult]:
    """Write a non-sreality ScrapedListing through the same matcher path.

    Returns `(pk, result)` — the assigned listing PK (synthetic negative for
    non-sreality rows) so the caller can attribute images / further writes to
    the right row, plus the upsert result.

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
    return pk, result


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
        _create_singleton_property(conn, listing_pk, source)
    else:
        _cheap_property_rollup(conn, listing_pk)


def _create_singleton_property(
    conn: psycopg.Connection, listing_pk: int, source: str,
) -> None:
    """Give a newly-seen listing its own singleton `properties` parent.

    No matching at insert time: the street+disposition dedup engine
    (`toolkit.dedup_engine` + `scripts.dedup_engine`) owns ALL grouping and runs
    out-of-band. The old geo Tier-1 spatial probe (20m/price/area) was removed
    when matching moved to street+disposition — insert-time geo proximity is no
    longer how properties are linked. Every new listing starts as a singleton;
    the engine merges it onto a sibling later if street+disposition (+ visual)
    agree.
    """
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


def mark_properties_dirty(
    conn: psycopg.Connection,
    property_ids: Iterable[int],
) -> int:
    """Enqueue property ids for the incremental maintenance job (Phase 3).

    Idempotent set-based insert; nests in the caller's transaction so the dirty
    mark is atomic with the child-listing change that caused it. NULL ids are
    dropped. Returns rows newly enqueued.
    """
    ids = [int(p) for p in property_ids if p is not None]
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dirty_properties (property_id)
            SELECT DISTINCT u FROM unnest(%s::bigint[]) AS u
            ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
            """,
            (ids,),
        )
        return cur.rowcount or 0


def record_images(
    conn: psycopg.Connection,
    sreality_id: int,
    images: Iterable[dict[str, Any]],
) -> int:
    """Insert any image rows that don't already exist. Returns newly inserted count."""
    # De-dupe non-null sequences within this batch: sreality occasionally
    # returns two images sharing one `order`, and ON CONFLICT DO UPDATE raises
    # CardinalityViolation ("cannot affect row a second time") if a single
    # statement proposes the same conflict key twice. (DO NOTHING tolerated it;
    # the URL-refresh DO UPDATE does not.) NULL sequences are kept as-is — they
    # don't conflict (NULLs are distinct in the unique index).
    rows: list[tuple[int, str, Any]] = []
    seen_seqs: set[int] = set()
    for img in images:
        url = img.get("url")
        if not url:
            continue
        seq = img.get("sequence")
        if seq is not None:
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)
        rows.append((sreality_id, url, seq))
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
            # Phase 3: a re-sighting that flips a listing back to active changes
            # its property's lifecycle rollup with NO snapshot, so it would not
            # be caught by the snapshot-driven dirty mark. Capture exactly the
            # reactivated subset (was inactive) and enqueue their properties.
            # The bulk last_seen bump below covers the active majority.
            cur.execute(
                """
                WITH react AS (
                    UPDATE listings
                    SET is_active = true, last_seen_at = now()
                    FROM unnest(%s::bigint[]) AS u(sreality_id)
                    WHERE listings.sreality_id = u.sreality_id
                      AND listings.is_active = false
                    RETURNING listings.property_id
                )
                INSERT INTO dirty_properties (property_id)
                SELECT DISTINCT property_id FROM react WHERE property_id IS NOT NULL
                ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
                """,
                (chunk,),
            )
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
    *,
    source: str = "sreality",
) -> int:
    """Mark listings of this category not in seen_ids as is_active=false.

    Scoped to (source, category_main, category_type) so a per-category index
    walk only flips its own slice. Without the category scope, scraping rentals
    would clobber sales `is_active`; without the source scope, a sreality walk
    would sweep other portals' rows (which carry the same canon categories but
    are never in sreality's seen_ids) — see architectural rule #15.
    """
    if not seen_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings
            SET is_active = false
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s
              AND sreality_id <> ALL(%s)
            RETURNING property_id
            """,
            (source, category_main, category_type, list(seen_ids)),
        )
        rows = cur.fetchall()
        pids = {int(r[0]) for r in rows if r[0] is not None}
        if pids:
            cur.execute(
                """
                INSERT INTO dirty_properties (property_id)
                SELECT DISTINCT u FROM unnest(%s::bigint[]) AS u
                ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
                """,
                (list(pids),),
            )
        return len(rows)


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
            "UPDATE listings SET is_active = false WHERE sreality_id = %s "
            "RETURNING property_id",
            (sreality_id,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute(
                "INSERT INTO dirty_properties (property_id) VALUES (%s) "
                "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()",
                (int(row[0]),),
            )


def mark_inactive_native(
    conn: psycopg.Connection,
    source: str,
    category_main: str,
    category_type: str,
    seen_natives: set[str],
) -> int:
    """Native-id analogue of `mark_inactive` for portals whose index knows only
    a portal-native string id (bazos), not the bigint PK.

    Flips active listings of this (source, category_main, category_type) whose
    `source_id_native` is absent from the walk to is_active=false. Scoped the
    same way as `mark_inactive` (rule #15). A brand-new listing seen in the index
    but not yet drained has no row, so it cannot be wrongly swept.
    """
    if not seen_natives:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listings
            SET is_active = false
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s
              AND source_id_native <> ALL(%s)
            RETURNING property_id
            """,
            (source, category_main, category_type, list(seen_natives)),
        )
        rows = cur.fetchall()
        pids = {int(r[0]) for r in rows if r[0] is not None}
        if pids:
            cur.execute(
                """
                INSERT INTO dirty_properties (property_id)
                SELECT DISTINCT u FROM unnest(%s::bigint[]) AS u
                ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
                """,
                (list(pids),),
            )
        return len(rows)


def mark_listing_inactive_native(
    conn: psycopg.Connection,
    source: str,
    native_id: str,
) -> None:
    """Flip a single (source, source_id_native) listing inactive — used when a
    portal detail fetch reports the ad gone (404/410 / gone-marker body). A
    definitive per-listing signal, independent of the index-absence sweep."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE listings SET is_active = false "
            "WHERE source = %s AND source_id_native = %s RETURNING property_id",
            (source, native_id),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute(
                "INSERT INTO dirty_properties (property_id) VALUES (%s) "
                "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()",
                (int(row[0]),),
            )


def portal_inactive_sweep_due(
    conn: psycopg.Connection,
    source: str,
    default_interval_hours: int = 12,
) -> bool:
    """Whether a portal's index-absence delisting sweep is allowed to run now.

    Throttled via `portals.inactive_sweep_min_interval_hours` (NULL → the code
    default): the frequent index walk touches last_seen + enqueues new ads every
    run, but the riskier delisting sweep runs at most once per window so a single
    flaky/rate-limited walk can never mass-delist. Unknown source → allowed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT last_inactive_sweep_at IS NULL
                   OR now() - last_inactive_sweep_at
                      >= make_interval(hours => coalesce(inactive_sweep_min_interval_hours, %s))
            FROM portals WHERE source = %s
            """,
            (default_interval_hours, source),
        )
        row = cur.fetchone()
    return True if row is None else bool(row[0])


def record_portal_inactive_sweep(conn: psycopg.Connection, source: str) -> None:
    """Stamp the moment a portal's delisting sweep actually ran (throttle clock)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE portals SET last_inactive_sweep_at = now() WHERE source = %s",
            (source,),
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


def index_summary_native(
    conn: psycopg.Connection,
    source: str,
    native_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Fetch (sreality_id PK, price_czk, last_seen_at) keyed by source_id_native
    for one portal.

    The native-id analogue of `index_summary` (which keys on the bigint PK that
    sreality's index already carries). A non-sreality portal's index walk only
    knows the portal-native string id, so it looks rows up by
    (source, source_id_native) to decide price-change refetch — and to resolve
    the PK set for a source-scoped `mark_inactive`.
    """
    ids = [str(n) for n in native_ids]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id_native, sreality_id, price_czk, last_seen_at
            FROM listings
            WHERE source = %s AND source_id_native = ANY(%s)
            """,
            (source, ids),
        )
        return {
            native: {"sreality_id": pk, "price_czk": price, "last_seen_at": ls}
            for native, pk, price, ls in cur.fetchall()
        }


def active_count(
    conn: psycopg.Connection,
    category_main: str,
    category_type: str,
    *,
    source: str = "sreality",
) -> int:
    """Current active-listing count for one (source, category_main, category_type)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM listings
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s
            """,
            (source, category_main, category_type),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def pending_image_downloads(
    conn: psycopg.Connection,
    max_attempts: int = 5,
    limit: int = 1000,
    active_only: bool = False,
    *,
    shard: tuple[int, int] | None = None,
    sources: tuple[str, ...] | None = None,
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

    `shard=(k, n)` partitions the pending queue by `image_id mod n == k`,
    so N parallel drainer jobs each own a disjoint slice — the horizontal
    scale-out knob. `sources` restricts to specific `listings.source`
    values (per-CDN scoping). Both are pure selection predicates; the
    download path stays source-agnostic.

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
    # Append predicates conditionally so the default call's params stay
    # exactly (max_attempts, limit) — tests assert that shape.
    extra = ""
    params: list[Any] = [max_attempts]
    if sources:
        extra += " AND l.source = ANY(%s)"
        params.append(list(sources))
    if shard is not None:
        k, n = shard
        extra += " AND (i.id %% %s) = %s"
        params.extend([n, k])
    params.append(limit)
    sql = f"""
        SELECT i.id, i.sreality_id, i.sequence, i.sreality_url,
               l.category_main, l.category_type
        FROM images i
        LEFT JOIN listings l ON l.sreality_id = i.sreality_id
        WHERE i.storage_path IS NULL
          AND i.unavailable_reason IS NULL
          AND i.download_attempts < %s
          {where_active}{extra}
        {order_clause}
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
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
    source: str = "sreality",
) -> int:
    """Open a new scrape_runs row. Returns the id."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scrape_runs (run_type, source)
            VALUES (%s, %s)
            RETURNING id
            """,
            (run_type, source),
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


# --- Phase 2: needs-detail queue + batched detail-drain writes --------------
#
# The index-walk enqueues new / price-changed ids into listing_detail_queue;
# the detail-drain claims a bounded slice, fetches, and writes them in batches
# via write_detail_batch. The queue is the "what to fetch" signal;
# listing_fetch_failures stays the Health-visible give-up ledger.

# Priorities (higher drains first): failure-retry > price-changed > new.
QUEUE_PRIORITY_NEW = 0
QUEUE_PRIORITY_CHANGED = 1
QUEUE_PRIORITY_FAILURE = 2

_QUEUE_ENQUEUE_CHUNK = 1000


class DetailResult(Protocol):
    """The subset of scraper.main.FetchResult that write_detail_batch reads.

    Duck-typed so db.py needn't import main (which imports db). Only 'ok'
    results are passed to write_detail_batch.
    """

    row: dict[str, Any] | None
    raw: dict[str, Any] | None
    images: list[dict[str, Any]] | None
    content_hash: str | None


# jsonb_to_recordset keeps the SQL text fixed-shape (the column-type spec is a
# literal; only the single jsonb param varies), so psycopg3 can prepare the plan
# once on the session pooler — the same prepared-statement win as Phase 1, now
# for the whole batch in one round-trip.
_BATCH_RECORD_SPEC = ", ".join(
    f"{c} {_LISTING_COLUMN_PGTYPE[c]}" for c in LISTING_COLUMNS
)
_BATCH_SELECT_COLS = ", ".join(f"j.{c}" for c in LISTING_COLUMNS)
_BATCH_UPDATE_SET = ",\n          ".join(
    f"{c} = EXCLUDED.{c}" for c in LISTING_COLUMNS
)

_BATCH_UPSERT_SQL = f"""
    INSERT INTO listings (
        sreality_id, last_seen_at, is_active,
        {", ".join(LISTING_COLUMNS)},
        geom, raw_json
    )
    SELECT
        j.sreality_id, now(), true,
        {_BATCH_SELECT_COLS},
        CASE
          WHEN j.lon IS NOT NULL AND j.lat IS NOT NULL
          THEN ST_SetSRID(ST_MakePoint(j.lon, j.lat), 4326)::geography
          ELSE NULL
        END,
        j.raw_json
    FROM jsonb_to_recordset(%s::jsonb) AS j(
        sreality_id bigint, {_BATCH_RECORD_SPEC},
        lon double precision, lat double precision, raw_json jsonb
    )
    ON CONFLICT (sreality_id) DO UPDATE SET
      last_seen_at = now(),
      is_active = true,
      {_BATCH_UPDATE_SET},
      geom = EXCLUDED.geom,
      raw_json = EXCLUDED.raw_json
    RETURNING (xmax = 0) AS inserted
"""

# Snapshot-on-change, set-based: insert a snapshot for exactly the listings
# whose content_hash differs from their latest (or that have none yet). raw_json
# is read back from the listings row just upserted in the same txn, so the large
# raw payload isn't sent twice. IS DISTINCT FROM handles the no-prior-snapshot
# case (latest NULL → distinct → one snapshot for a brand-new listing).
_BATCH_SNAPSHOT_SQL = """
    INSERT INTO listing_snapshots (sreality_id, price_czk, content_hash, raw_json)
    SELECT j.sreality_id, j.price_czk, j.content_hash, l.raw_json
    FROM jsonb_to_recordset(%s::jsonb)
        AS j(sreality_id bigint, price_czk integer, content_hash text)
    JOIN listings l ON l.sreality_id = j.sreality_id
    LEFT JOIN LATERAL (
        SELECT content_hash FROM listing_snapshots s
        WHERE s.sreality_id = j.sreality_id
        ORDER BY s.scraped_at DESC, s.id DESC
        LIMIT 1
    ) latest ON true
    WHERE latest.content_hash IS DISTINCT FROM j.content_hash
    RETURNING sreality_id
"""

# Phase 3: enqueue the changed listings' properties as dirty so the incremental
# maintenance job recomputes only them. New listings (property_id NULL) are
# skipped here -- the job's straggler-attach phase resolves them instead.
_BATCH_DIRTY_FROM_SIDS_SQL = """
    INSERT INTO dirty_properties (property_id)
    SELECT DISTINCT property_id FROM listings
    WHERE sreality_id = ANY(%s) AND property_id IS NOT NULL
    ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""

_BATCH_IMAGES_SQL = """
    INSERT INTO images (sreality_id, sreality_url, sequence)
    SELECT j.sreality_id, j.sreality_url, j.sequence
    FROM jsonb_to_recordset(%s::jsonb)
        AS j(sreality_id bigint, sreality_url text, sequence integer)
    ON CONFLICT (sreality_id, sequence) DO UPDATE SET
        sreality_url = EXCLUDED.sreality_url,
        download_attempts = 0,
        last_error = NULL,
        unavailable_reason = NULL
    WHERE images.storage_path IS NULL
    RETURNING (xmax = 0) AS inserted
"""


def write_detail_batch(
    conn: psycopg.Connection,
    results: Sequence[DetailResult],
) -> dict[str, int]:
    """Write a batch of successful detail fetches in ONE transaction.

    Set-based: one multi-row listings upsert, one snapshot-on-change insert
    (changed -> exactly one snapshot, unchanged -> none), one images upsert,
    one failure-clear. Collapses the per-listing round-trips into ~4 per batch.

    Does NOT run the Tier-1 property matcher — new listings land with
    property_id NULL and are matched asynchronously by recompute_property_stats
    (Phase 2 deferral). Returns counts {new, updated, unchanged, images_discovered}.
    """
    n = len(results)
    if n == 0:
        return {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}

    listing_objs: list[dict[str, Any]] = []
    snapshot_objs: list[dict[str, Any]] = []
    image_objs: list[dict[str, Any]] = []
    ok_ids: list[int] = []
    seen_img: set[tuple[int, int]] = set()

    for r in results:
        row = r.row or {}
        sid = int(row["sreality_id"])
        ok_ids.append(sid)
        obj: dict[str, Any] = {c: row.get(c) for c in LISTING_COLUMNS}
        obj["sreality_id"] = sid
        obj["lon"] = row.get("lon")
        obj["lat"] = row.get("lat")
        obj["raw_json"] = r.raw or {}
        listing_objs.append(obj)
        snapshot_objs.append({
            "sreality_id": sid,
            "price_czk": row.get("price_czk"),
            "content_hash": r.content_hash,
        })
        for img in r.images or []:
            url = img.get("url")
            if not url:
                continue
            seq = img.get("sequence")
            if seq is not None:
                key = (sid, seq)
                if key in seen_img:
                    continue
                seen_img.add(key)
            image_objs.append(
                {"sreality_id": sid, "sreality_url": url, "sequence": seq}
            )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_BATCH_UPSERT_SQL, (Jsonb(listing_objs),))
        new = sum(1 for (inserted,) in cur.fetchall() if inserted)

        cur.execute(_BATCH_SNAPSHOT_SQL, (Jsonb(snapshot_objs),))
        changed_sids = [int(r[0]) for r in cur.fetchall()]
        snapshots = len(changed_sids)

        images_discovered = 0
        if image_objs:
            cur.execute(_BATCH_IMAGES_SQL, (Jsonb(image_objs),))
            images_discovered = sum(1 for (ins,) in cur.fetchall() if ins)

        cur.execute(
            "DELETE FROM listing_fetch_failures WHERE sreality_id = ANY(%s)",
            (ok_ids,),
        )

        if changed_sids:
            cur.execute(_BATCH_DIRTY_FROM_SIDS_SQL, (changed_sids,))

    # snapshots == new + updated (a brand-new listing always gets one snapshot);
    # the rest were content-identical touches.
    updated = max(0, snapshots - new)
    unchanged = n - new - updated
    return {
        "new": new,
        "updated": updated,
        "unchanged": unchanged,
        "images_discovered": images_discovered,
    }


def enqueue_detail(
    conn: psycopg.Connection,
    source: str,
    entries: Sequence[tuple[str, str | None, int | None, int]],
) -> int:
    """Enqueue (native_id, detail_ref, index_price_czk, priority) tuples for
    detail fetch under `source` (Phase 4 source-generic queue).

    native_id is the portal-native id (sreality: sreality_id as text; bazos:
    source_id_native); detail_ref is what the drain needs to FETCH the detail
    (None for sreality — the URL is derived from the id; the detail path/URL for
    crawler portals). For sreality the bigint sreality_id column is set too (from
    the numeric native_id) so the write path + the legacy unique still work.

    Idempotent on (source, native_id): re-seeing an id refreshes its observed
    price + detail_ref and raises its priority (GREATEST), but never disturbs a
    row a drain has already claimed. Chunked to stay under the pooler timeout.
    """
    rows = list(entries)
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), _QUEUE_ENQUEUE_CHUNK):
            chunk = rows[start : start + _QUEUE_ENQUEUE_CHUNK]
            native_ids = [str(nid) for nid, _, _, _ in chunk]
            refs = [r for _, r, _, _ in chunk]
            prices = [p for _, _, p, _ in chunk]
            prios = [int(pr) for _, _, _, pr in chunk]
            cur.execute(
                """
                INSERT INTO listing_detail_queue
                    (source, native_id, detail_ref, index_price_czk, priority,
                     sreality_id)
                SELECT %(source)s, u.nid, u.ref, u.price, u.prio,
                       CASE WHEN %(source)s = 'sreality'
                            THEN u.nid::bigint ELSE NULL END
                FROM unnest(
                    %(nids)s::text[], %(refs)s::text[],
                    %(prices)s::int[], %(prios)s::smallint[]
                ) AS u(nid, ref, price, prio)
                ON CONFLICT (source, native_id) DO UPDATE SET
                    detail_ref      = EXCLUDED.detail_ref,
                    index_price_czk = EXCLUDED.index_price_czk,
                    priority = GREATEST(listing_detail_queue.priority, EXCLUDED.priority),
                    enqueued_at     = now()
                WHERE listing_detail_queue.claimed_at IS NULL
                """,
                {"source": source, "nids": native_ids, "refs": refs,
                 "prices": prices, "prios": prios},
            )
            total += cur.rowcount or 0
    return total


def claim_detail_batch(
    conn: psycopg.Connection,
    source: str,
    limit: int,
) -> list[tuple[str, str | None, int | None]]:
    """Atomically claim up to `limit` available rows for `source`, highest
    priority + oldest first. Returns (native_id, detail_ref, index_price_czk).

    FOR UPDATE SKIP LOCKED makes concurrent drains safe. The claim is committed
    immediately (claimed_at set) so a crashed drain's rows are recovered by
    reclaim_stale_claims rather than lost.
    """
    if limit <= 0:
        return []
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            WITH c AS (
                SELECT source, native_id FROM listing_detail_queue
                WHERE source = %s AND claimed_at IS NULL AND given_up = false
                ORDER BY priority DESC, enqueued_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE listing_detail_queue q SET claimed_at = now()
            FROM c WHERE q.source = c.source AND q.native_id = c.native_id
            RETURNING q.native_id, q.detail_ref, q.index_price_czk
            """,
            (source, limit),
        )
        return [(nid, ref, price) for nid, ref, price in cur.fetchall()]


def complete_detail(
    conn: psycopg.Connection,
    source: str,
    native_ids: Iterable[str],
) -> int:
    """Remove drained rows from the queue (success or confirmed-gone)."""
    ids = [str(n) for n in native_ids]
    if not ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM listing_detail_queue "
            "WHERE source = %s AND native_id = ANY(%s)",
            (source, ids),
        )
        return cur.rowcount or 0


def fail_detail(
    conn: psycopg.Connection,
    source: str,
    native_ids: Iterable[str],
    error_message: str,
    max_attempts: int = FAILURE_GIVE_UP_THRESHOLD,
) -> None:
    """Release a failed claim back to the queue, bumping attempts; give up at
    max_attempts so a permanently-broken listing stops re-claiming."""
    ids = [str(n) for n in native_ids]
    if not ids:
        return
    truncated = (error_message or "")[:500]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listing_detail_queue SET
                attempts   = attempts + 1,
                given_up   = (attempts + 1) >= %s,
                claimed_at = NULL,
                last_error = %s
            WHERE source = %s AND native_id = ANY(%s)
            """,
            (max_attempts, truncated, source, ids),
        )


def reclaim_stale_claims(
    conn: psycopg.Connection,
    source: str,
    older_than_minutes: int = 30,
) -> int:
    """Release `source` claims older than the cutoff (a drain SIGKILLed
    mid-flight), so its rows become claimable again. Mirrors
    sweep_stuck_scrape_runs."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listing_detail_queue SET claimed_at = NULL
            WHERE source = %s AND claimed_at IS NOT NULL AND given_up = false
              AND claimed_at < now() - make_interval(mins => %s)
            RETURNING native_id
            """,
            (source, older_than_minutes),
        )
        return len(cur.fetchall())


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
