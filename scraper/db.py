"""Database I/O for the Sreality tracker.

Reads SUPABASE_DB_URL, upserts into listings, appends a row to
listing_snapshots only when the content hash changes, inserts new
image URLs, and at end of run marks unseen listings inactive.

Each listing's writes happen in one transaction so a partial failure
cannot leave the listings / listing_snapshots / images tables out of
sync for that listing.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeVar

import psycopg
from psycopg.types.json import Jsonb, set_json_dumps

from scraper import media
from scraper.scraped_listing import ScrapedListing
from scraper.street import street_name_key
from toolkit.broker_sources import BROKER_FINGERPRINT_KEYS, BROKER_SOURCE_NAMES

LOG = logging.getLogger(__name__)


def _jsonb_default(obj: Any) -> Any:
    """Coerce the DB-native types JSON can't represent so no jsonb write can
    crash. `numeric` columns come back as Decimal and timestamps as datetime;
    a payload that mixes a DB-read value into a jsonb column (an estimation
    subject spec, a trace step) would otherwise raise 'not JSON serializable'.
    JSON has no Decimal type — float matches what every other producer emits."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _jsonb_dumps(obj: Any) -> str:
    return json.dumps(obj, default=_jsonb_default)


# Process-wide JSON serialization policy for every psycopg Jsonb/Json write.
# Registered once so any code path that wraps a payload in Jsonb() — an
# estimation subject spec, a trace step, a building proposal — survives a
# DB-native Decimal/datetime sneaking in, instead of raising at write time.
set_json_dumps(_jsonb_dumps)

UpsertResult = Literal["new", "updated", "unchanged"]

LISTING_COLUMNS: tuple[str, ...] = (
    "category_main",
    "category_type",
    "price_czk",
    "price_unit",
    "area_m2",
    # Which physical area area_m2 holds (migration 423). A provenance stamp, not
    # a value — out of every content hash, so backfilling it churns no snapshot.
    "area_basis",
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
    "subtype",
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
    # Derived (NOT parsed): the dedup street-group name key, a pure function of
    # `street` (scraper.street.street_name_key). Stamped from `street` at every
    # write path by _set_street_name_key — never read from the parsed row. Out of
    # the content hash (the hash covers raw_json, not derived columns), so
    # populating it never churns a snapshot.
    "street_name_key",
    # Portal-declared publication/last-bump timestamp (migration 266). Out of
    # every content hash (bazos re-stamps it per bump; backfills stay free).
    "published_at",
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
    "area_basis": "text",
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
    "subtype": "text",
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
    "street_name_key": "text",
    "published_at": "timestamptz",
}
assert set(_LISTING_COLUMN_PGTYPE) == set(LISTING_COLUMNS), (
    "_LISTING_COLUMN_PGTYPE drifted from LISTING_COLUMNS"
)

# The RÚIAN coord→street resolver (resolve_coord_streets.yml) fills these on rows whose
# PORTAL page carries no street — so the row's next detail refetch (correctly) re-parses
# NULL, and a plain `street = EXCLUDED.street` CLOBBERS the resolver's fill back to NULL
# (measured: 40% of a resolver cohort lost in 2.5 days). These columns carry forward when
# the incoming value is NULL: a parser that DOES produce a street still wins (fresher,
# page-sourced), but an incoming NULL never erases a stored value. Safe precisely because
# the trio is OUT of the content hash (no snapshot churn), a wrong-street risk is guarded
# upstream (street.reject_as_town) and downstream (the admin-geo trigger NULLs a
# resolver-sourced street when the listing's coordinates change — migration 262).
# published_at joins the set (migration 266): the signal is intermittent at the source
# (sreality's `edited` exists on ~40% of rows; a portal can stop rendering its date), so a
# fetch that yields no date must not erase what an earlier fetch or a raw_json backfill
# recorded — a fresher portal date still wins. Same rails: out of the content hash,
# informational only.
_PRESERVE_IF_NULL_COLUMNS = frozenset({"street", "house_number", "published_at"})

# street_name_key is NOT independently preserve-if-null: it is a pure function of
# street, so it must follow the STREET's preserve decision — preserved exactly when
# the street is preserved, else written as stamped (even when that stamp is NULL: a
# non-NULL street can legitimately fold to a NULL key, and keeping the OLD key under
# a NEW street would store a pair the weekly parity job rightly flags as drift).
_STREET_NAME_KEY_UPDATE_SQL = (
    "street_name_key = CASE WHEN EXCLUDED.street IS NULL "
    "THEN listings.street_name_key ELSE EXCLUDED.street_name_key END"
)

# street_source provenance ('parser' | 'resolver', migration 262): a page-parsed street
# marks 'parser'; a preserved (incoming-NULL) value keeps whatever provenance it had; the
# resolver stamps 'resolver' in its own UPDATE. The geom-change guard keys off it.
_STREET_SOURCE_UPDATE_SQL = (
    "street_source = CASE WHEN EXCLUDED.street IS NOT NULL THEN 'parser' "
    "ELSE listings.street_source END"
)


def _listing_update_set_sql() -> str:
    """The ONE ON CONFLICT SET builder shared by upsert_listing and the batched drain
    upsert, so preserve-if-null semantics can never drift between the two write paths."""
    return ",\n          ".join(
        (_STREET_NAME_KEY_UPDATE_SQL if c == "street_name_key"
         else f"{c} = COALESCE(EXCLUDED.{c}, listings.{c})" if c in _PRESERVE_IF_NULL_COLUMNS
         else f"{c} = EXCLUDED.{c}")
        for c in LISTING_COLUMNS
    )


def _set_street_name_key(d: dict[str, Any]) -> None:
    """Derive `street_name_key` from the row's `street`, in place. The single
    write-time derivation, called by every street-writing chokepoint
    (upsert_listing, write_detail_batch) so the stored key is always consistent
    with the stored street — the load-scoping invariant the dedup --dirty drain
    relies on. Pure function of `street` (scraper.street.street_name_key); a
    parsed value the row may already carry under this key is ignored."""
    d["street_name_key"] = street_name_key(d.get("street"))


# No real Czech property is priced anywhere near a billion crowns; a value this
# large is a data-entry placeholder (e.g. a seller typing 2147483647) or a parse
# artifact. It also overflows the int4 price columns (listings.price_czk,
# listing_snapshots.price_czk, listing_detail_queue.index_price_czk) — and in the
# batched write a single oversized value fails the whole jsonb_to_recordset cast,
# losing the entire batch. Clamp such values to NULL at every write boundary.
# The low end is a placeholder too: "1 Kč" / "0 Kč" is the seller's "dohodou"
# (price on request), not a price — NULL is the price-unknown representation.
MAX_PRICE_CZK = 2_000_000_000
MIN_PRICE_CZK = 2


def sane_price_czk(price: int | None) -> int | None:
    if price is None:
        return None
    if price > MAX_PRICE_CZK:
        LOG.warning("PRICE dropped implausible value=%s (> %s)", price, MAX_PRICE_CZK)
        return None
    if price < MIN_PRICE_CZK:
        LOG.warning("PRICE dropped placeholder value=%s (< %s)", price, MIN_PRICE_CZK)
        return None
    return price


# A foreign listing's synthetic ids (sreality assigns Spain/Bali/etc. localities
# municipality_ids in a 1.28-billion-and-rising space) can exceed int4 on the
# locality_*_id / street_id columns; an unbounded numeric column can likewise
# overflow numeric(p,s). Either fails the jsonb_to_recordset cast and aborts the
# whole ~100-listing detail batch. Like sane_price_czk, clamp out-of-range values
# to NULL at the write boundary — driven off _LISTING_COLUMN_PGTYPE so a future
# int4/numeric column is covered automatically with no hand-list to drift.
INT4_MIN, INT4_MAX = -2_147_483_648, 2_147_483_647
# Max abs value a numeric(p,s) column accepts is 10^(p-s). Every 'numeric'
# LISTING_COLUMN must have an entry here (asserted below).
_NUMERIC_ABS_MAX: dict[str, int] = {
    "area_m2": 10**6,  # numeric(7,1)
    "estate_area": 10**8,  # numeric(9,1)
    "usable_area": 10**8,  # numeric(9,1)
    "garden_area": 10**8,  # numeric(9,1)
}
assert set(_NUMERIC_ABS_MAX) == {
    c for c, t in _LISTING_COLUMN_PGTYPE.items() if t == "numeric"
}, "_NUMERIC_ABS_MAX drifted from the numeric LISTING_COLUMNS"


def sane_listing_numerics(obj: dict[str, Any]) -> None:
    """Clamp out-of-range int4/numeric LISTING_COLUMN values to NULL, in place.

    price_czk keeps its stricter business cap (sane_price_czk, applied first);
    this is the column-range backstop for every other numeric column. Every
    numeric LISTING_COLUMN is an area (asserted via _NUMERIC_ABS_MAX above),
    and a 0 m² area is a form placeholder, never a measurement — NULL it so
    area filters and Kč/m² math don't trip over it.
    """
    for col, pgtype in _LISTING_COLUMN_PGTYPE.items():
        v = obj.get(col)
        if v is None:
            continue
        if pgtype == "integer" and not (INT4_MIN <= v <= INT4_MAX):
            LOG.warning("NUMERIC dropped col=%s value=%s (int4 range)", col, v)
            obj[col] = None
        elif pgtype == "numeric" and v == 0:
            LOG.warning("NUMERIC dropped col=%s value=0 (area placeholder)", col)
            obj[col] = None
        elif pgtype == "numeric" and abs(v) >= _NUMERIC_ABS_MAX[col]:
            LOG.warning("NUMERIC dropped col=%s value=%s (numeric range)", col, v)
            obj[col] = None


def database_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL environment variable is not set")
    return url


# libpq TCP keepalives. The detail drain holds one connection for the whole
# --max-seconds budget (up to 40 min), idle during the rate-limited fetch waits;
# the Supabase pooler silently drops such a connection and the next op (often the
# teardown conn.close()) raises OperationalError. Keepalives keep the socket warm
# and surface a dead peer fast instead of on a late write. Applied to every
# connection for parity — harmless on short-lived ones.
_KEEPALIVES: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
    "tcp_user_timeout": 30000,
}


# Bounded retry for the CONNECT HANDSHAKE itself. run_resilient (below) retries a
# mid-flight drop on an already-OPEN connection; this covers the distinct case
# where the Supabase pooler drops the handshake ("server closed the connection
# unexpectedly") so psycopg.connect raises before any connection exists — the
# batch entrypoints' single startup connect was a SPOF. Only OperationalError is
# retried (via is_transient_db_error, single-sourcing the classifier with
# run_resilient), so a missing/wrong SUPABASE_DB_URL still RuntimeErrors fast out
# of database_url() and a real bug fails loud instead of spinning ~30s.
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_DELAY = 10.0


def _connect_with_retry(
    opener: Callable[[], psycopg.Connection],
    *,
    attempts: int,
    delay: float,
) -> psycopg.Connection:
    for attempt in range(1, attempts + 1):
        try:
            return opener()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless transient
            if not is_transient_db_error(exc) or attempt >= attempts:
                raise
            LOG.warning(
                "CONNECT: transient error (attempt %d/%d, retry in %.0fs): %r",
                attempt, attempts, delay, exc,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def connect(
    url: str | None = None,
    *,
    attempts: int = _CONNECT_ATTEMPTS,
    retry_delay: float = _CONNECT_RETRY_DELAY,
) -> psycopg.Connection:
    """Open an autocommit connection. Callers manage transactions explicitly.

    prepare_threshold=None disables psycopg3's automatic prepared-statement
    caching. Required for Supabase's Transaction-mode pooler (PgBouncer),
    which rebinds connections between queries and trips
    DuplicatePreparedStatement otherwise.

    A pooler handshake drop is retried `attempts` times spaced `retry_delay`s
    apart (see _connect_with_retry). Callers that can't afford the full budget —
    the synchronous API per-request path — pass a smaller one.
    """
    return _connect_with_retry(
        lambda: psycopg.connect(
            url or database_url(),
            autocommit=True,
            prepare_threshold=None,
            **_KEEPALIVES,
        ),
        attempts=attempts,
        delay=retry_delay,
    )


def connect_session(
    url: str | None = None,
    *,
    attempts: int = _CONNECT_ATTEMPTS,
    retry_delay: float = _CONNECT_RETRY_DELAY,
) -> psycopg.Connection:
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
        return connect(attempts=attempts, retry_delay=retry_delay)
    return _connect_with_retry(
        lambda: psycopg.connect(session_url, autocommit=True, **_KEEPALIVES),
        attempts=attempts,
        delay=retry_delay,
    )


_T = TypeVar("_T")

# Bounded retry budget for a transient DB error on the detail drain's long-held
# connection (held for the whole --max-seconds budget, idle during the
# rate-limited fetch waits). Four attempts with exponential backoff (0.5/1/2 s,
# +jitter) ride out a pooler recycle, a deadlock victim, or a brief network blip;
# a genuine outage still reds the run after the budget, exactly as before this
# guard existed. (The index walk holds a connection too but is NOT wired through
# here — its per-category autocommit work self-recovers on the next cron tick.)
_RESILIENT_ATTEMPTS = 4
_RESILIENT_BASE_DELAY = 0.5


def is_transient_db_error(exc: BaseException) -> bool:
    """True for a DB error worth retrying. We treat EVERY psycopg.OperationalError
    as transient — connection drops (SSL EOF, pooler recycle, admin shutdown,
    idle-session timeout), deadlock / serialization rollbacks, and even the
    bounded resource/timeout classes (a statement-timeout or pool saturation is
    usually a passing lock/pooler condition, since statement_timeout is wall-clock
    from statement start and so includes lock waits). A real bug (IntegrityError,
    ProgrammingError, DataError) is NOT an OperationalError, so it fails loud
    immediately rather than spinning.

    Deliberately SQLSTATE-BLIND, which makes bounding the retry the CALL SITE's
    job. `QueryCanceled` (57014) is an OperationalError, so a statement killed by
    its own timeout is replayed in full: cheap on the drain's small per-batch ops
    that this budget was sized for, but a whole `attempts` x ceiling on anything
    long. Any caller whose statements can run for minutes MUST pass its own
    `attempts` — recompute_property_stats' `sweep.batch`/`sweep.attach` and
    resolve_brokers' chunk loops + timeout-lifted tail all cap at 2 for exactly
    this reason, and their workflow `timeout-minutes` are sized off that product.
    """
    return isinstance(exc, psycopg.OperationalError)


def run_resilient(
    conn: psycopg.Connection,
    op: Callable[[psycopg.Connection], _T],
    *,
    reconnect: Callable[[], psycopg.Connection],
    attempts: int = _RESILIENT_ATTEMPTS,
    base_delay: float = _RESILIENT_BASE_DELAY,
    label: str = "db op",
) -> tuple[_T, psycopg.Connection]:
    """Run op(conn), retrying transient DB errors and reconnecting when the
    pooler drops the connection mid-flight.

    Returns (result, live_conn). live_conn may be a FRESH connection (the
    original was reset), so every caller MUST rebind its handle:

        result, conn = db.run_resilient(conn, op, reconnect=portal.connect_drain)

    A deadlock / serialization rollback leaves the connection usable, so it is
    retried on the same conn; a connection drop (conn.broken / closed) gets a
    fresh one from `reconnect`. Re-raises immediately on a non-transient error (a
    bug, not an outage) and after `attempts` are exhausted (a real outage -> the
    run reds, same as before). The caller's op() MUST be idempotent — it is
    re-run from the top on every retry (the drain's batch writes are: latest-wins
    upserts + snapshot-on-change + Tier-0 ids, so a replay re-commits identically;
    the one non-idempotent pair, the failure-counter bumps, is wrapped in a single
    transaction by its caller so a replay re-applies it exactly once).
    """
    original = conn

    def _discard_created() -> None:
        # On a raise, close a connection run_resilient itself opened — the caller
        # never received it (we only hand it back via the success return), so
        # nobody else will. Never touch the caller's `original`: its own teardown
        # owns that one.
        if conn is not None and conn is not original:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort
                pass

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            if conn is None or getattr(conn, "closed", False):
                conn = reconnect()
            return op(conn), conn
        except Exception as exc:  # noqa: BLE001 - re-raised below unless transient
            if not is_transient_db_error(exc):
                _discard_created()
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            broken = (
                conn is None
                or getattr(conn, "broken", False)
                or getattr(conn, "closed", False)
            )
            LOG.warning(
                "%s: transient DB error (attempt %d/%d, reconnect=%s): %r",
                label, attempt, attempts, broken, exc,
            )
            if broken:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001 - already broken; close is best-effort
                        pass
                conn = None
            else:
                # Deadlock / serialization victim: the failed statement already
                # rolled back (autocommit + the transaction CM), but clear any
                # lingering aborted txn before reusing the same connection.
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(min(base_delay * 2 ** (attempt - 1), 8.0) + random.random() * base_delay)
    _discard_created()
    assert last_exc is not None
    raise last_exc


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
    update_set = _listing_update_set_sql()

    upsert_sql = f"""
        INSERT INTO listings (
            sreality_id, last_seen_at, is_active,
            {column_list},
            street_source, source, source_id_native, geom, raw_json, discovery_seq
        )
        VALUES (
            %(sreality_id)s, now(), true,
            {placeholders},
            CASE WHEN %(street)s::text IS NOT NULL THEN 'parser' END,
            -- The FULL natural key (migration 091) is stamped inline at INSERT, not
            -- healed afterward: non-sreality callers pass source + the portal's native
            -- id in the row; sreality (no row values) falls back to 'sreality' +
            -- sreality_id::text. `source` MUST be set here, not only by the post-insert
            -- UPDATE — its column default is 'sreality', so an insert that set only
            -- source_id_native would transiently be ('sreality', <native_id>) and could
            -- collide with a real sreality row on the UNIQUE(source, source_id_native)
            -- index, which ON CONFLICT (sreality_id) does not arbitrate (unique_violation
            -- → the whole ingest aborts and the portal drain wedges).
            %(source)s,
            %(source_id_native)s,
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
            %(raw_json)s,
            %(discovery_seq)s
        )
        -- Arbiter is the natural key, not sreality_id (R2 Phase D). Safe because
        -- (a) listings_source_native_uidx is a full (non-partial) unique index, so
        -- arbiter inference always succeeds, and (b) neither sreality_id nor source
        -- appears in the SET clause below (_listing_update_set_sql excludes both,
        -- source_id_native is COALESCE-healed separately) — a conflict on this
        -- arbiter can never rewrite the frozen surrogate-adjacent identity columns.
        ON CONFLICT (source, source_id_native) DO UPDATE SET
          last_seen_at = now(),
          is_active = true,
          inactive_at = NULL,
          {update_set},
          {_STREET_SOURCE_UPDATE_SQL},
          -- Heal a legacy NULL natural key on any refetch, but never overwrite a
          -- set one (preserve-if-null, same rail as geom below).
          source_id_native = COALESCE(listings.source_id_native, EXCLUDED.source_id_native),
          -- Preserve-if-null (mig-263 rail, extended to geom): an incoming NULL
          -- means "the page carried no coords", never "coords removed" — a bare
          -- EXCLUDED.geom silently wiped geocoded/backfilled coordinates on the
          -- next coords-less refetch (and with them the admin hierarchy's
          -- freshness and the geo_cell_key). Real moves still win: a non-NULL
          -- incoming geom replaces the stored one.
          geom = COALESCE(EXCLUDED.geom, listings.geom),
          raw_json = EXCLUDED.raw_json,
          -- Set-once (migration 368): a listing's discovery_seq is a first-discovery
          -- fact, never regenerated by a later refetch — same COALESCE-preserve shape
          -- as source_id_native above, just favoring the STORED value over the
          -- incoming one (a refetch's discovery_seq is a fresh, unrelated queue draw,
          -- not a correction).
          discovery_seq = COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)
        RETURNING xmax = 0 AS inserted, id
    """

    params: dict[str, Any] = {
        "sreality_id": sreality_id,
        "raw_json": raw_jsonb,
        "lon": row.get("lon"),
        "lat": row.get("lat"),
        # The natural-key pair, stamped inline (see the INSERT comment). sreality's
        # native id IS its sreality_id; non-sreality callers (ingest) put their
        # source + portal id in the row so the pair is written atomically.
        "source": row.get("source") or "sreality",
        "source_id_native": row.get("source_id_native") or str(sreality_id),
        # The queue-assigned discovery-order value (migration 368), threaded in by
        # ingest_scraped_listing / the sreality batch path; None for callers outside
        # the queue-driven drain (e.g. a manual --detail-only fetch) — NULL is correct
        # there, it just means "no discovery-order signal for this row".
        "discovery_seq": row.get("discovery_seq"),
    }
    for col in LISTING_COLUMNS:
        params[col] = row.get(col)
    params["price_czk"] = sane_price_czk(params["price_czk"])
    sane_listing_numerics(params)
    _set_street_name_key(params)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(upsert_sql, params)
        result = cur.fetchone()
        inserted = bool(result[0]) if result else False
        # The surrogate of the row we just wrote, read back in-transaction so the
        # snapshot below can carry it (R2 dual-write). On the ON CONFLICT arm the
        # INSERT's sequence default is evaluated and discarded, and `id` is not in
        # LISTING_COLUMNS so the DO UPDATE never rewrites it — RETURNING always
        # yields the persisted row's stable id, new or existing.
        listing_id = result[1] if result else None

        # Rekeyed onto listing_id (R2 Phase C): listing_id is already resolved
        # above, and listing_snapshots_listing_id_scraped_at_idx (mig 333) mirrors
        # the legacy (sreality_id, scraped_at) composite so this stays a single
        # index lookup on the hot per-write path.
        cur.execute(
            """
            SELECT content_hash FROM listing_snapshots
            WHERE listing_id = %s
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (listing_id,),
        )
        prev = cur.fetchone()
        unchanged = prev is not None and prev[0] == content_hash

        if not unchanged:
            cur.execute(
                """
                INSERT INTO listing_snapshots
                    (sreality_id, listing_id, price_czk, content_hash, raw_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (sreality_id, listing_id, params["price_czk"], content_hash, raw_jsonb),
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
    grouping is out-of-band and operator-ordered, never the insert path.
    """
    sreality_id = row["sreality_id"]
    with conn.transaction():
        result = upsert_listing(conn, row, raw_json, content_hash)
        # Property linkage keys on the surrogate id (like the portal path), not on
        # sreality_id. For sreality the natural sreality_id is always present and
        # uniquely identifies the row, so it's the safe lookup back to the surrogate.
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM listings WHERE sreality_id = %s", (sreality_id,))
            listing_id = int(cur.fetchone()[0])
        _ensure_property(conn, listing_id, "sreality")
    return result


# Sources whose listings carry a broker block that scripts.resolve_brokers
# attributes. A content-changed listing of one of these is enqueued into
# dirty_broker_listings so the incremental resolver re-attributes it within its
# cadence (it has no full-table straggler scan); a missed source degrades
# gracefully to daily-sweep-only attribution rather than breaking. Both this set
# and the fingerprint allowlist below are DERIVED from the one registry that also
# generates the resolver's SQL, so onboarding a portal can no longer half-land.
# sreality flows through write_detail_batch (which enqueues directly), not this
# path, but is in the set so it reads as the full broker-attributed source list.
BROKER_ATTRIBUTED_SOURCES = frozenset(BROKER_SOURCE_NAMES)

# Every attribution-relevant key the HTML portals put in raw["broker"]: the
# per-broker key (account_oid on idnes, broker_id elsewhere), the contacts, and
# the firm key (agency_name on idnes, agency_slug on ceskereality/remax,
# agency_id on realitymix). An allowlist, not a hash of the whole block, so a
# parser adding an incidental field can't churn the queue.
_BROKER_FINGERPRINT_KEYS: tuple[str, ...] = BROKER_FINGERPRINT_KEYS

_STORED_BROKER_BLOCK_SQL = "SELECT raw_json->'broker' FROM listings WHERE id = %s"


def _stored_broker_block(conn: psycopg.Connection, listing_id: int) -> Any:
    with conn.cursor() as cur:
        cur.execute(_STORED_BROKER_BLOCK_SQL, (listing_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _broker_fingerprint(block: Any) -> tuple[str | None, ...]:
    """The attribution-relevant identity of a raw["broker"] block.

    Total by construction — a portal that emits a list, a string or a missing block
    yields the empty fingerprint rather than raising, because this runs inside the
    live ingest transaction and must never abort an otherwise-valid listing."""
    if not isinstance(block, dict):
        return ()
    return tuple(
        None if block.get(k) is None else str(block[k]).strip()
        for k in _BROKER_FINGERPRINT_KEYS
    )


# The Gate-2 flip-writer scaffold (wave-5 item 7): OFF by default, so the
# nextval draw below is unconditional until an operator explicitly opts in.
# app_settings-backed (not env/process-cached) so the always-on realtime
# worker and cron drains pick up a flip on their very next batch, not after a
# restart.
GATE2_NULL_SREALITY_ID_SETTING = "gate2_null_sreality_id_enabled"


def _app_settings_flag(conn: psycopg.Connection, key: str) -> bool:
    """Live read of one boolean app_settings flag. Missing row or NULL -> False
    (fresh-deploy-safe: a flag that isn't seeded yet reads as OFF)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return False
    value = row[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _gate2_null_sreality_id_enabled(conn: psycopg.Connection) -> bool:
    """Live read of the flip-writer flag (default: keep minting synthetic
    negative sreality_ids)."""
    return _app_settings_flag(conn, GATE2_NULL_SREALITY_ID_SETTING)


def ingest_scraped_listing(
    conn: psycopg.Connection, listing: ScrapedListing,
    discovery_seq: int | None = None,
) -> tuple[int, UpsertResult]:
    """Write a non-sreality ScrapedListing through the same matcher path.

    `discovery_seq` is the claimed queue row's enqueue-time sequence value
    (migration 368; see claim_detail_batch / DrainItem.discovery_seq) — passed
    through so upsert_listing can stamp listings.discovery_seq on first insert.
    None for callers outside the queue-driven drain (e.g. a manual re-ingest).

    Returns `(listing_id, result)` — the row's SURROGATE `listings.id` (never the
    legacy sreality_id, which is a synthetic negative today and NULL for new rows
    once Gate 2 flips) so the caller can attribute images / further writes to the
    right row, plus the upsert result.

    Tier 0: (source, source_id_native) is the idempotency key — a re-fetch reuses
    the existing row and updates in place; first sight draws a fresh negative
    sreality_id from `synthetic_listing_id_seq` for the legacy column only (the
    sign-check rail), UNLESS the `gate2_null_sreality_id_enabled` app_settings
    flag is on, in which case it writes NULL instead (the actual Gate-2 flip,
    still off by default — see `_gate2_null_sreality_id_enabled`). Identity is
    carried on the surrogate `id`, resolved back out of the natural key
    (validated present + unique, migration 314) — every follow-up write keys on
    it, so nothing depends on a sreality_id that may be NULL. The listing write +
    source identity + Tier-1 property matching commit in one transaction.

    A write of a broker-attributed source enqueues the row into
    dirty_broker_listings (the incremental resolver's sole feed — same role
    write_detail_batch plays for sreality) when its CONTENT changed or when only its
    broker block did, so e.g. new idnes listings are attributed within the
    resolver's cadence, not only by the daily full sweep.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, sreality_id FROM listings "
            "WHERE source = %s AND source_id_native = %s",
            (listing.source, listing.source_id_native),
        )
        found = cur.fetchone()
    if found is not None:
        # Re-fetch: reuse the persisted surrogate. Its legacy sreality_id (whatever
        # it is now — synthetic negative pre-flip, NULL after) is fed back to to_row
        # only to fill the INSERT's sreality_id column; upsert's ON CONFLICT never
        # rewrites it, so the value is inert on this path.
        listing_id: int | None = int(found[0])
        legacy_sreality_id: int | None = found[1]
        # Read BEFORE the upsert overwrites raw_json, and only for the sources whose
        # brokers are attributed — elsewhere it would buy a raw_json detoast per
        # ingest for nothing. First sight needs no read: it enqueues on result anyway.
        stored_broker = (
            _stored_broker_block(conn, listing_id)
            if listing.source in BROKER_ATTRIBUTED_SOURCES else None
        )
    else:
        stored_broker = None
        listing_id = None  # sequence-generated in the INSERT; resolved post-upsert
        if _gate2_null_sreality_id_enabled(conn):
            legacy_sreality_id = None
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT nextval('synthetic_listing_id_seq')")
                legacy_sreality_id = int(cur.fetchone()[0])

    row = listing.to_row(legacy_sreality_id)
    # Carry the FULL natural key (source + native id) into the INSERT so it is stamped
    # atomically. Both matter: source_id_native for the NOT NULL invariant, and source
    # because its column default is 'sreality' — inserting only source_id_native would
    # transiently write ('sreality', <native_id>) and could collide with a real sreality
    # row on the UNIQUE(source, source_id_native) index (ON CONFLICT (sreality_id) does
    # not arbitrate it → unique_violation → drain wedge). source_url is not part of the
    # key, so it stays on the post-insert UPDATE.
    row["source"] = listing.source
    row["source_id_native"] = listing.source_id_native
    row["discovery_seq"] = discovery_seq
    with conn.transaction():
        result = upsert_listing(conn, row, listing.raw or {}, listing.content_hash())
        if listing_id is None:
            # First sight: the surrogate was just minted by the INSERT's sequence
            # default. Read it back on the natural key (never on sreality_id).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM listings "
                    "WHERE source = %s AND source_id_native = %s",
                    (listing.source, listing.source_id_native),
                )
                listing_id = int(cur.fetchone()[0])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET source_url = %s WHERE id = %s",
                (listing.source_url, listing_id),
            )
        _ensure_property(conn, listing_id, listing.source)
        if result != "unchanged":
            # Enqueue for the incremental property-stats recompute — the exact
            # counterpart of write_detail_batch's _BATCH_DIRTY_FROM_SIDS_SQL,
            # which only ever covered sreality. Without this the OTHER EIGHT
            # portals never enqueued on a content change, so their price-history
            # columns (price_change_count*, total_price_change_pct) were
            # refreshed only by the 04:15 full sweep — while _cheap_property_rollup
            # updates current_price_czk INLINE for singletons. A price could
            # therefore be up to 24h out of step with its own change history.
            # Set-based off the surrogate so a listing not yet attached to a
            # property (property_id NULL) is skipped, not crashed on.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dirty_properties (property_id) "
                    "SELECT property_id FROM listings "
                    "WHERE id = %s AND property_id IS NOT NULL "
                    "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()",
                    (listing_id,),
                )
        # The second arm is INDEPENDENT of the content hash on purpose. The four
        # HTML portals hash a fixed field allowlist (ScrapedListing._HASH_FIELDS)
        # that excludes raw_json, so a page whose ONLY change is its broker block
        # computes result == 'unchanged' and used to never enqueue — those portals
        # could not re-attribute a broker change at all. Folding the broker fields
        # into the content hash instead would append a listing_snapshots row for a
        # pure attribution change, which rule 2 reserves for CONTENT changes.
        if listing.source in BROKER_ATTRIBUTED_SOURCES and (
            result != "unchanged"
            or _broker_fingerprint(stored_broker) != _broker_fingerprint(
                listing.raw.get("broker") if isinstance(listing.raw, dict) else None)
        ):
            with conn.cursor() as cur:
                # Keyed on the surrogate (dirty_broker_listings_pkey is listing_id;
                # its sreality_id column is legacy/nullable). The upsert above ran
                # first in this same transaction, so the row is already present.
                cur.execute(
                    "INSERT INTO dirty_broker_listings (listing_id) VALUES (%s) "
                    "ON CONFLICT (listing_id) DO UPDATE SET marked_at = now()",
                    (listing_id,),
                )
    return listing_id, result


def _ensure_property(conn: psycopg.Connection, listing_id: int, source: str) -> None:
    """Attach the listing to its canonical property, or refresh it if linked.

    Keyed on the surrogate `listings.id` — `listing_id` is a real PK, never a
    sreality_id (which is NULL for post-Gate-2 portal rows). Runs inside the
    caller's transaction (no own transaction block). A new (unlinked) listing goes
    through the Tier-1 matcher; an already-linked one gets a cheap rollup of its
    property. (The legacy source_id_native heal is gone: migration 314 enforces
    source_id_native NOT NULL and upsert_listing stamps it inline, so the old
    heal-if-NULL was provably dead — and keyed on the wrong id besides.)
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT property_id FROM listings WHERE id = %s",
            (listing_id,),
        )
        found = cur.fetchone()
        property_id = found[0] if found else None

    if property_id is None:
        _create_singleton_property(conn, listing_id, source)
    else:
        _cheap_property_rollup(conn, listing_id)


def _create_singleton_property(
    conn: psycopg.Connection, listing_id: int, source: str,
) -> None:
    """Give a newly-seen listing its own singleton `properties` parent.

    No matching at insert time, ever. The old geo Tier-1 spatial probe
    (20m/price/area) was removed long ago, and the automatic decision engine
    that replaced it was itself removed in the 2026-08 "NEW DEDUP" cutoff
    (CLAUDE.md rule 15). Every new listing starts as a singleton and stays one
    until an operator orders a merge through `toolkit.property_identity`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO properties (
                repr_listing_id, repr_listing_ref_id, category_main, category_type, disposition,
                area_m2, district, locality, geom, current_price_czk,
                has_balcony, has_parking, has_lift, building_type, condition,
                ownership, furnished, terrace, cellar, garage, category_sub_cb, subtype,
                estate_area, usable_area, garden_area, parking_lots,
                ku_id, obec_id, okres_id, region_id, obec, okres, region,
                locality_district_id, locality_region_id, source, energy_rating,
                building_condition_level, apartment_condition_level,
                is_active, first_seen_at, last_seen_at,
                source_count, distinct_site_count, price_per_m2_source_listing_id
            )
            SELECT
                sreality_id, id, category_main, category_type, disposition,
                area_m2, district, locality, geom, price_czk,
                has_balcony, has_parking, has_lift, building_type, condition,
                ownership, furnished, terrace, cellar, garage, category_sub_cb, subtype,
                estate_area, usable_area, garden_area, parking_lots,
                ku_id, obec_id, okres_id, region_id, obec, okres, region,
                locality_district_id, locality_region_id, source, energy_rating,
                building_condition_level, apartment_condition_level,
                is_active, first_seen_at, last_seen_at, 1, 1,
                -- One child, so price and area trivially come from one row:
                -- stamp the per-m2 basis now rather than leave it NULL until the
                -- next maintenance pass (migration 424).
                price_per_m2_source_id(price_czk, area_m2, id)
            FROM listings WHERE id = %s
            RETURNING id
            """,
            (listing_id,),
        )
        new_pid = int(cur.fetchone()[0])
        cur.execute(
            "UPDATE listings SET property_id = %s WHERE id = %s",
            (new_pid, listing_id),
        )


def _cheap_property_rollup(conn: psycopg.Connection, listing_id: int) -> None:
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
                current_price_czk   = CASE WHEN agg.cnt = 1 THEN l.price_czk      ELSE p.current_price_czk END,
                area_m2             = CASE WHEN agg.cnt = 1 THEN l.area_m2         ELSE p.area_m2 END,
                price_per_m2_source_listing_id = CASE WHEN agg.cnt = 1
                    THEN price_per_m2_source_id(l.price_czk, l.area_m2, l.id)
                    ELSE p.price_per_m2_source_listing_id END,
                district            = CASE WHEN agg.cnt = 1 THEN l.district        ELSE p.district END,
                locality            = CASE WHEN agg.cnt = 1 THEN l.locality        ELSE p.locality END,
                disposition         = CASE WHEN agg.cnt = 1 THEN l.disposition     ELSE p.disposition END,
                geom                = CASE WHEN agg.cnt = 1 THEN l.geom            ELSE p.geom END,
                category_main       = CASE WHEN agg.cnt = 1 THEN l.category_main   ELSE p.category_main END,
                category_type       = CASE WHEN agg.cnt = 1 THEN l.category_type   ELSE p.category_type END,
                has_balcony         = CASE WHEN agg.cnt = 1 THEN l.has_balcony     ELSE p.has_balcony END,
                has_parking         = CASE WHEN agg.cnt = 1 THEN l.has_parking     ELSE p.has_parking END,
                has_lift            = CASE WHEN agg.cnt = 1 THEN l.has_lift        ELSE p.has_lift END,
                building_type       = CASE WHEN agg.cnt = 1 THEN l.building_type   ELSE p.building_type END,
                condition           = CASE WHEN agg.cnt = 1 THEN l.condition       ELSE p.condition END,
                ownership           = CASE WHEN agg.cnt = 1 THEN l.ownership       ELSE p.ownership END,
                furnished           = CASE WHEN agg.cnt = 1 THEN l.furnished       ELSE p.furnished END,
                terrace             = CASE WHEN agg.cnt = 1 THEN l.terrace         ELSE p.terrace END,
                cellar              = CASE WHEN agg.cnt = 1 THEN l.cellar          ELSE p.cellar END,
                garage              = CASE WHEN agg.cnt = 1 THEN l.garage          ELSE p.garage END,
                category_sub_cb     = CASE WHEN agg.cnt = 1 THEN l.category_sub_cb ELSE p.category_sub_cb END,
                subtype             = CASE WHEN agg.cnt = 1 THEN l.subtype         ELSE p.subtype END,
                estate_area         = CASE WHEN agg.cnt = 1 THEN l.estate_area     ELSE p.estate_area END,
                usable_area         = CASE WHEN agg.cnt = 1 THEN l.usable_area     ELSE p.usable_area END,
                garden_area         = CASE WHEN agg.cnt = 1 THEN l.garden_area     ELSE p.garden_area END,
                parking_lots        = CASE WHEN agg.cnt = 1 THEN l.parking_lots    ELSE p.parking_lots END,
                ku_id               = CASE WHEN agg.cnt = 1 THEN l.ku_id           ELSE p.ku_id END,
                obec_id             = CASE WHEN agg.cnt = 1 THEN l.obec_id         ELSE p.obec_id END,
                okres_id            = CASE WHEN agg.cnt = 1 THEN l.okres_id        ELSE p.okres_id END,
                region_id           = CASE WHEN agg.cnt = 1 THEN l.region_id       ELSE p.region_id END,
                obec                = CASE WHEN agg.cnt = 1 THEN l.obec            ELSE p.obec END,
                okres               = CASE WHEN agg.cnt = 1 THEN l.okres           ELSE p.okres END,
                region              = CASE WHEN agg.cnt = 1 THEN l.region          ELSE p.region END,
                locality_district_id = CASE WHEN agg.cnt = 1 THEN l.locality_district_id ELSE p.locality_district_id END,
                locality_region_id  = CASE WHEN agg.cnt = 1 THEN l.locality_region_id    ELSE p.locality_region_id END,
                source              = CASE WHEN agg.cnt = 1 THEN l.source          ELSE p.source END,
                energy_rating       = CASE WHEN agg.cnt = 1 THEN l.energy_rating   ELSE p.energy_rating END,
                building_condition_level  = CASE WHEN agg.cnt = 1 THEN l.building_condition_level  ELSE p.building_condition_level END,
                apartment_condition_level = CASE WHEN agg.cnt = 1 THEN l.apartment_condition_level ELSE p.apartment_condition_level END
            FROM listings l
            JOIN LATERAL (
                SELECT count(*) AS cnt, count(DISTINCT source) AS dcnt,
                       max(last_seen_at) AS last_seen, bool_or(is_active) AS active
                FROM listings WHERE property_id = l.property_id
            ) agg ON true
            WHERE p.id = l.property_id AND l.id = %s
            """,
            (listing_id,),
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
    sreality_id: int | None,
    images: Iterable[dict[str, Any]],
    *,
    listing_id: int | None = None,
) -> int:
    """Insert any image rows that don't already exist. Returns newly inserted count.

    Two call shapes resolve the surrogate FK (`images.listing_id`) two ways:
    sreality's own callers pass their always-present `sreality_id` and the row is
    looked up from it; the portal chokepoint (`record_media`) passes the resolved
    surrogate `listing_id` directly (post-Gate-2 a portal row's sreality_id is
    NULL, so it can never be the FK). Either way `images.listing_id` is non-NULL,
    which is what the ON CONFLICT (listing_id, sequence) arbiter needs to dedupe.
    """
    # De-dupe non-null sequences within this batch: sreality occasionally
    # returns two images sharing one `order`, and ON CONFLICT DO UPDATE raises
    # CardinalityViolation ("cannot affect row a second time") if a single
    # statement proposes the same conflict key twice. (DO NOTHING tolerated it;
    # the URL-refresh DO UPDATE does not.) NULL sequences are kept as-is — they
    # don't conflict (NULLs are distinct in the unique index).
    kept: list[tuple[str, Any]] = []
    seen_seqs: set[int] = set()
    for img in images:
        url = img.get("url")
        if not url:
            continue
        # Backstop: `images` is strictly photographic. Video URLs are routed to
        # listing_videos by record_media; this guard keeps a stray non-image URL
        # (a caller that bypassed the split) out of the photo pipeline regardless.
        if not media.is_image_url(url):
            continue
        seq = img.get("sequence")
        if seq is not None:
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)
        kept.append((url, seq))
    if not kept:
        return 0
    # Refresh the URL on conflict so a re-detail-fetch repoints a stale/rotated
    # CDN path on a not-yet-downloaded image (and clears its stale error state).
    # The storage_path IS NULL guard is load-bearing: an already-downloaded image
    # is never disturbed, so we never re-download what we have. xmax = 0 is true
    # only for genuine inserts, keeping the "newly inserted" count honest.
    #
    # The arbiter is listing_id (R2 Phase C, images_listing_id_sequence_key), so it
    # MUST be non-NULL — a NULL listing_id never conflicts, so it would spawn an
    # unbounded duplicate row on every refetch. The FK is therefore always carried
    # explicitly: the caller either hands us the resolved surrogate (`listing_id=`,
    # the portal path) or its sreality_id, from which we look the surrogate up
    # inline. images.sreality_id mirrors the listing's own (legacy negative today,
    # NULL after the Gate-2 flip) so the two never disagree. The DB backstop for the
    # non-NULL invariant is images_listing_id_present_check (migration 350).
    if listing_id is not None:
        values_sql = ", ".join(
            "((SELECT sreality_id FROM listings WHERE id = %s), %s, %s, %s)" for _ in kept
        )
        flat: list[Any] = [
            v for url, seq in kept for v in (listing_id, listing_id, url, seq)
        ]
    else:
        values_sql = ", ".join(
            "(%s, (SELECT id FROM listings WHERE sreality_id = %s), %s, %s)" for _ in kept
        )
        flat = [
            v for url, seq in kept for v in (sreality_id, sreality_id, url, seq)
        ]
    with conn.transaction(), conn.cursor() as cur:
        sql = f"""
            INSERT INTO images (sreality_id, listing_id, sreality_url, sequence)
            VALUES {values_sql}
            ON CONFLICT (listing_id, sequence) DO UPDATE SET
                sreality_url = EXCLUDED.sreality_url,
                download_attempts = 0,
                last_error = NULL,
                unavailable_reason = NULL
            WHERE images.storage_path IS NULL
            RETURNING (xmax = 0) AS inserted
        """
        cur.execute(sql, flat)
        return sum(1 for (inserted,) in cur.fetchall() if inserted)


def record_videos(
    conn: psycopg.Connection,
    sreality_id: int | None,
    videos: Iterable[dict[str, Any]],
    *,
    listing_id: int | None = None,
) -> int:
    """Insert video-media rows into listing_videos. Returns newly inserted count.

    Mirrors record_images (same de-dupe + URL-refresh-where-not-downloaded upsert,
    same two FK-resolution shapes — `listing_id=` for the portal chokepoint, else
    looked up from sreality_id) but writes the non-image sibling table. We capture
    the URL only — bytes are NOT downloaded today (storage_path stays NULL), keeping
    the image pool free of large video fetches; a future isolated video drain can
    fill them in.
    """
    kept: list[tuple[str, Any]] = []
    seen_seqs: set[int] = set()
    for vid in videos:
        url = vid.get("url")
        if not url:
            continue
        seq = vid.get("sequence")
        if seq is not None:
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)
        kept.append((url, seq))
    if not kept:
        return 0

    if listing_id is not None:
        values_sql = ", ".join(
            "((SELECT sreality_id FROM listings WHERE id = %s), %s, %s, %s)" for _ in kept
        )
        flat: list[Any] = [
            v for url, seq in kept for v in (listing_id, listing_id, url, seq)
        ]
    else:
        values_sql = ", ".join(
            "(%s, (SELECT id FROM listings WHERE sreality_id = %s), %s, %s)" for _ in kept
        )
        flat = [
            v for url, seq in kept for v in (sreality_id, sreality_id, url, seq)
        ]
    with conn.transaction(), conn.cursor() as cur:
        sql = f"""
            INSERT INTO listing_videos (sreality_id, listing_id, source_url, sequence)
            VALUES {values_sql}
            ON CONFLICT (listing_id, sequence) DO UPDATE SET
                source_url = EXCLUDED.source_url
            WHERE listing_videos.storage_path IS NULL
            RETURNING (xmax = 0) AS inserted
        """
        cur.execute(sql, flat)
        return sum(1 for (inserted,) in cur.fetchall() if inserted)


def record_media(
    conn: psycopg.Connection,
    listing_id: int,
    media_urls: Iterable[str],
) -> int:
    """Split a portal's ordered media URLs into images + videos and record each.

    The single ingest chokepoint every portal calls instead of hand-rolling the
    enumerate-then-record incantation: images land in `images`, videos in
    `listing_videos`, with each item's sequence = its original gallery position
    (so a leading video leaves a sequence gap, never renumbering the photos).
    Returns the number of newly inserted image rows (what the portals log).

    `listing_id` is the SURROGATE `listings.id` (as returned by
    ingest_scraped_listing), carried straight into the child rows' FK — never a
    sreality_id, which is NULL for a post-Gate-2 portal row.
    """
    image_rows, video_rows = media.split_media_rows(media_urls)
    new_images = record_images(conn, None, image_rows, listing_id=listing_id)
    record_videos(conn, None, video_rows, listing_id=listing_id)
    return new_images


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
                    SET is_active = true, inactive_at = NULL, last_seen_at = now()
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
                    is_active = true,
                    inactive_at = NULL
                FROM unnest(%s::bigint[]) AS u(sreality_id)
                WHERE listings.sreality_id = u.sreality_id
                """,
                (chunk,),
            )
            total += cur.rowcount or 0
    return total


def touch_listings_by_id(
    conn: psycopg.Connection,
    listing_ids: Iterable[int],
) -> int:
    """Surrogate-id analogue of `touch_listings` for the non-sreality portals.

    Same last_seen_at bump + reactivation dirty-mark, but keyed on the surrogate
    `listings.id`. A portal index walk resolves the surrogate (its sreality_id is
    a synthetic negative today and NULL once Gate 2 flips), so a sreality_id-keyed
    touch would match nothing — starving rule #4's last_seen_at signal for every
    unchanged portal row. Separate function (not a parametrized key column) to
    mirror the mark_inactive / mark_inactive_native split and stay discoverable by
    the SQL-correctness gate.
    """
    ids = list(listing_ids)
    if not ids:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(ids), TOUCH_CHUNK_SIZE):
            chunk = ids[start : start + TOUCH_CHUNK_SIZE]
            cur.execute(
                """
                WITH react AS (
                    UPDATE listings
                    SET is_active = true, inactive_at = NULL, last_seen_at = now()
                    FROM unnest(%s::bigint[]) AS u(id)
                    WHERE listings.id = u.id
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
                    is_active = true,
                    inactive_at = NULL
                FROM unnest(%s::bigint[]) AS u(id)
                WHERE listings.id = u.id
                """,
                (chunk,),
            )
            total += cur.rowcount or 0
    return total


def _seen_without_nulls(seen: Collection[Any], label: str) -> list[Any] | None:
    """Drop NULL ids from a delisting sweep's seen-set; None if that empties it.

    Two SQL three-valued-logic traps guard this family's `<> ALL(%s)` predicate:
    ONE NULL element makes the comparison NULL for EVERY row, silently turning
    the sweep into a permanent no-op, while an EMPTY array makes it true for
    every row, delisting the whole scope. A NULL can't identify a row, so
    dropping it is right — but the caller must then bail out rather than sweep
    with what is left of an all-NULL set (hence the None return).
    """
    kept = [i for i in seen if i is not None]
    if not kept:
        return None
    if len(kept) != len(seen):
        LOG.warning("INACTIVE %s: dropped %d NULL id(s) from the seen-set",
                    label, len(seen) - len(kept))
    return kept


def mark_inactive(
    conn: psycopg.Connection,
    category_main: str,
    category_type: str,
    seen_ids: set[int],
    *,
    source: str = "sreality",
    min_unseen_hours: int | None = None,
) -> int:
    """Mark listings of this category not in seen_ids as is_active=false.

    Scoped to (source, category_main, category_type) so a per-category index
    walk only flips its own slice. Without the category scope, scraping rentals
    would clobber sales `is_active`; without the source scope, a sreality walk
    would sweep other portals' rows (which carry the same canon categories but
    are never in sreality's seen_ids) — see architectural rule #15.

    `min_unseen_hours` additionally restricts the flip to rows whose
    last_seen_at is older than that many hours — the staleness rail that keeps
    a single walk's index hiccup from delisting a row touched by a recent walk.
    """
    if not seen_ids:
        return 0
    ids = _seen_without_nulls(seen_ids, f"{source}/{category_main}/{category_type}")
    if ids is None:
        return 0
    stale_clause = (
        "\n              AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    params: list[Any] = [source, category_main, category_type]
    if min_unseen_hours is not None:
        params.append(min_unseen_hours)
    params.append(ids)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE listings
            SET is_active = false, inactive_at = now()
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s{stale_clause}
              AND sreality_id <> ALL(%s)
            RETURNING property_id
            """,
            tuple(params),
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
            "UPDATE listings SET is_active = false, inactive_at = now() "
            "WHERE sreality_id = %s RETURNING property_id",
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
    *,
    subtype: str | None = None,
    scope_subtype: bool = False,
    min_unseen_hours: int | None = None,
) -> int:
    """Native-id analogue of `mark_inactive` for portals whose index knows only
    a portal-native string id (bazos), not the bigint PK.

    Flips active listings of this (source, category_main, category_type) whose
    `source_id_native` is absent from the walk to is_active=false. Scoped the
    same way as `mark_inactive` (rule #15). A brand-new listing seen in the index
    but not yet drained has no row, so it cannot be wrongly swept.

    `scope_subtype=True` ALSO scopes the sweep to `subtype` (NULL-safe). bazos
    walks fine sections that collapse onto one category_main (chata + dum -> dum;
    kancelar/sklad/... -> komercni), so without this each section's per-scope
    sweep would flip the other sections' rows inactive. The clause only NARROWS
    the sweep, so the failure direction is over-retention, never over-deletion.

    `min_unseen_hours` additionally restricts the flip to rows whose
    last_seen_at is older than that many hours — the staleness rail that keeps
    a single walk's index hiccup from delisting a row touched by a recent walk.
    """
    if not seen_natives:
        return 0
    natives = _seen_without_nulls(seen_natives, f"{source}/{category_main}/{category_type}")
    if natives is None:
        return 0
    sub_clause = "\n              AND subtype IS NOT DISTINCT FROM %s" if scope_subtype else ""
    stale_clause = (
        "\n              AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    params: list[Any] = [source, category_main, category_type]
    if scope_subtype:
        params.append(subtype)
    if min_unseen_hours is not None:
        params.append(min_unseen_hours)
    params.append(natives)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE listings
            SET is_active = false, inactive_at = now()
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s{sub_clause}{stale_clause}
              AND source_id_native <> ALL(%s)
            RETURNING property_id
            """,
            tuple(params),
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


def mark_inactive_agenda(
    conn: psycopg.Connection,
    source: str,
    category_type: str,
    seen_natives: set[str],
    *,
    min_unseen_hours: int | None = None,
) -> int:
    """Agenda-grain native-id sweep: flip active (source, category_type) listings
    whose `source_id_native` is absent from `seen_natives` to is_active=false.

    For portals (maxima/remax) whose index is TWO mixed agendas — sale / rent ≡
    category_type — that report a per-AGENDA total but only a TITLE-DERIVED
    per-category slice. A per-(category_main, category_type) sweep would risk
    false-flipping a listing whose index-time title category disagrees with its
    detail-time stored category (the same ad in two different `category_main`
    buckets). Scoping by category_type with the FULL agenda walk's id set removes
    that risk: a still-listed ad is in `seen_natives` regardless of which
    category_main it maps to, so only ads genuinely gone from the whole agenda
    flip. Source-scoped (rule #15) so a portal's walk only touches its own rows.

    `min_unseen_hours` is the same staleness rail as `mark_inactive_native`. Only
    call with the full agenda's id set AFTER a completeness-proven agenda walk.
    """
    if not seen_natives:
        return 0
    natives = _seen_without_nulls(seen_natives, f"{source}/{category_type}")
    if natives is None:
        return 0
    stale_clause = (
        "\n              AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    params: list[Any] = [source, category_type]
    if min_unseen_hours is not None:
        params.append(min_unseen_hours)
    params.append(natives)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE listings
            SET is_active = false, inactive_at = now()
            WHERE is_active = true
              AND source = %s
              AND category_type = %s{stale_clause}
              AND source_id_native <> ALL(%s)
            RETURNING property_id
            """,
            tuple(params),
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
            "UPDATE listings SET is_active = false, inactive_at = now() "
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
    """Fetch (surrogate id, sreality_id, price_czk, last_seen_at) keyed by
    source_id_native for one portal.

    The native-id analogue of `index_summary` (which keys on the bigint PK that
    sreality's index already carries). A non-sreality portal's index walk only
    knows the portal-native string id, so it looks rows up by
    (source, source_id_native) to decide price-change refetch — and to resolve the
    surrogate `id` set for touch_listings_by_id. The `"id"` value is the identity
    to carry forward; `"sreality_id"` is legacy (NULL for post-Gate-2 rows).
    """
    ids = [str(n) for n in native_ids]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id_native, id, sreality_id, price_czk, last_seen_at
            FROM listings
            WHERE source = %s AND source_id_native = ANY(%s)
            """,
            (source, ids),
        )
        return {
            native: {"id": lid, "sreality_id": pk, "price_czk": price, "last_seen_at": ls}
            for native, lid, pk, price, ls in cur.fetchall()
        }


def native_ids_with_geom(
    conn: psycopg.Connection, source: str,
) -> dict[str, tuple[float, float]]:
    """Stored (lat, lon) per source_id_native of `source` rows that already
    carry coordinates.

    Lets a detail drain carry a stored coordinate forward onto a refetched
    listing whose page gave none — geom is never wiped by the upsert and a
    geocode credit is only ever spent once per listing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id_native, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM listings "
            "WHERE source = %s AND geom IS NOT NULL AND source_id_native IS NOT NULL",
            (source,),
        )
        return {native: (lat, lon) for native, lat, lon in cur.fetchall()}


def active_count(
    conn: psycopg.Connection,
    category_main: str,
    category_type: str,
    *,
    source: str = "sreality",
    subtype: str | None = None,
    scope_subtype: bool = False,
) -> int:
    """Current active-listing count for one (source, category_main, category_type).

    `scope_subtype=True` narrows to `subtype` (NULL-safe) so the count matches a
    subtype-scoped `mark_inactive_native` sweep (bazos fine sections)."""
    sub_clause = "\n              AND subtype IS NOT DISTINCT FROM %s" if scope_subtype else ""
    params: list[Any] = [source, category_main, category_type]
    if scope_subtype:
        params.append(subtype)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) FROM listings
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s{sub_clause}
            """,
            tuple(params),
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
) -> list[tuple[int, int, int | None, str, str | None, str | None, int | None]]:
    """Return (image_id, listing_id, sequence, sreality_url, category_main,
    category_type, sreality_id) rows that still need download.

    BOTH ids are returned because the drain needs each for a different job:
    `listing_id` keys the R2 object and the shard (it survives Gate 2), while
    `sreality_id` is what the taken-down classification path needs — a
    freshness check is a sreality portal fetch, so it is inherently legacy-keyed
    and simply does not apply to a listing without one.

    Filters out images already stored (storage_path IS NOT NULL),
    images we have given up on (download_attempts >= max_attempts), and
    images terminally classified as unavailable (unavailable_reason IS
    NOT NULL — e.g. the parent listing was taken down).

    With `active_only=True`, restrict to images whose parent listing is
    `is_active = true` — the backfill workflow's prioritisation knob,
    so the cap-bounded slice goes to listings users can still browse.

    `shard=(k, n)` partitions the pending queue by the PARENT LISTING —
    `hash(listing_id) mod n == k` — so N parallel drainer jobs each own a
    disjoint slice (horizontal scale-out) AND a single listing's photos all
    fall in ONE shard. Sharding on `image_id` instead would stripe a
    listing's photos across shards that drain at slightly different rates,
    so a recent listing renders half its photos until the slowest shard
    catches up; keying on the listing makes a listing flip to complete in
    one burst. The id is HASHED (not raw modulo) because sreality ids are
    multiples of 4, so `sreality_id % n` would pile everything into one
    shard. `sources` restricts to specific `listings.source` values
    (per-CDN scoping). Both are pure selection predicates; the download
    path stays source-agnostic.

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
        # Hash the listing id (not the image id) so a listing's photos all land
        # in one shard, and hash it (not raw modulo) because sreality ids are
        # multiples of 4 — raw `sreality_id % n` collapses everything into one
        # shard. `& 2147483647` clears the sign bit (no abs() overflow risk).
        # Shards on the SURROGATE (R2): post-Gate-2 images.sreality_id is NULL
        # for a non-sreality listing, hashint8(NULL) is NULL, and `NULL % n = k`
        # is NULL — so those images would match NO shard and never drain at all.
        extra += " AND (hashint8(i.listing_id) & 2147483647) %% %s = %s"
        params.extend([n, k])
    params.append(limit)
    sql = f"""
        SELECT i.id, i.listing_id, i.sequence, i.sreality_url,
               l.category_main, l.category_type, i.sreality_id
        FROM images i
        LEFT JOIN listings l ON l.id = i.listing_id
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
    phash: int | None = None,
) -> None:
    """`phash` rides the same statement as `storage_path` (computed inline on
    the bytes already in hand — Wave C-4); None preserves any existing hash so
    the hourly compute_image_phash backfill stays the backstop."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE images
            SET storage_path = %s,
                phash = COALESCE(%s, phash),
                last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """,
            (storage_path, phash, image_id),
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
    on the next API boot. Counters are left as-is on purpose: the index walk
    (bump_index_pages) and the detail-drain (bump_scrape_run_counts) persist their
    counts incrementally as they go, so a swept row already carries the real
    totals of whatever committed before the kill. The cutoff must stay above the
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
    bump_already_applied: bool = False,
) -> None:
    """Close out the scrape_runs row with aggregate counters.

    bump_already_applied: the detail-drain persists its five row counters
    incrementally via bump_scrape_run_counts (crash/SIGKILL-survivable), so for a
    drain run finalize must NOT re-write them — overwriting would double-count on
    the happy path, or (on a crash, where the aggregate is empty) zero the counts
    that were already committed. It then only stamps ended_at + index_pages +
    images_stored + by_category. Index/full/delta runs (default False) keep writing
    their aggregate exactly as before.
    """
    with conn.transaction(), conn.cursor() as cur:
        if bump_already_applied:
            cur.execute(
                """
                UPDATE scrape_runs
                SET ended_at      = now(),
                    index_pages   = GREATEST(index_pages, %s),
                    images_stored = %s,
                    by_category   = %s
                WHERE id = %s
                """,
                (index_pages, images_stored, Jsonb(by_category or []), run_id),
            )
            return
        cur.execute(
            """
            UPDATE scrape_runs
            SET ended_at             = now(),
                index_pages          = GREATEST(index_pages, %s),
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


def bump_index_pages(conn: psycopg.Connection, run_id: int, n: int) -> None:
    """Add n to a scrape_runs row's index_pages immediately, best-effort.

    The index walk calls this after each category commits so Health liveness
    (which keys off scrape_runs.index_pages > 0) reflects real progress even
    when a long walk is SIGKILLed by its job timeout before it can finalize.
    The walk connection is autocommit, so each bump persists on its own.
    Finalize uses GREATEST(index_pages, ...), so the final reconcile never
    clobbers an accumulated total. Audit bookkeeping must never break a walk.
    """
    if n <= 0:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scrape_runs SET index_pages = index_pages + %s WHERE id = %s",
                (int(n), run_id),
            )
    except Exception:  # noqa: BLE001 - never let bookkeeping abort a walk
        LOG.warning("bump_index_pages failed for run %s", run_id, exc_info=True)


def bump_scrape_run_counts(
    conn: psycopg.Connection,
    run_id: int | None,
    *,
    found_new: int = 0,
    scraped_new: int = 0,
    updated: int = 0,
    inactive: int = 0,
    errors: int = 0,
    images_discovered: int = 0,
) -> None:
    """Additively persist drain counters as batches flush, best-effort.

    The detail-drain commits each batch in its own transaction, but its in-memory
    counts used to surface only via the runner's terminal return — so a mid-run
    crash (or a SIGKILL) left the scrape_runs row reading 0 despite committed
    writes. Bumping per chunk (like bump_index_pages does for index_pages) keeps
    the counts on the row regardless of how the run ends. The drain connection is
    autocommit, so each bump persists on its own; finalize for a drain run is told
    NOT to re-write these columns (bump_already_applied), so the happy path counts
    exactly once. Audit bookkeeping must never abort a drain.
    """
    if run_id is None:
        return
    if not (found_new or scraped_new or updated or inactive or errors or images_discovered):
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scrape_runs SET
                  listings_found_new   = listings_found_new   + %s,
                  listings_scraped_new = listings_scraped_new + %s,
                  listings_updated     = listings_updated     + %s,
                  listings_inactive    = listings_inactive    + %s,
                  errors               = errors               + %s,
                  images_discovered    = images_discovered    + %s
                WHERE id = %s
                """,
                (found_new, scraped_new, updated, inactive, errors,
                 images_discovered, run_id),
            )
    except Exception:  # noqa: BLE001 - never let bookkeeping abort a drain
        LOG.warning("bump_scrape_run_counts failed for run %s", run_id, exc_info=True)


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
    # The claimed queue row's enqueue-time sequence value (migration 368),
    # attached by the runner after claim (see portal_runner.DrainItem), read here
    # by write_detail_batch. Not set by fetch_detail itself — it has nothing to
    # do with fetch/parse, only with when the id was originally discovered.
    discovery_seq: int | None


# jsonb_to_recordset keeps the SQL text fixed-shape (the column-type spec is a
# literal; only the single jsonb param varies), so psycopg3 can prepare the plan
# once on the session pooler — the same prepared-statement win as Phase 1, now
# for the whole batch in one round-trip.
_BATCH_RECORD_SPEC = ", ".join(
    f"{c} {_LISTING_COLUMN_PGTYPE[c]}" for c in LISTING_COLUMNS
)
_BATCH_SELECT_COLS = ", ".join(f"j.{c}" for c in LISTING_COLUMNS)
# One shared builder with upsert_listing — preserve-if-null for the resolver street trio
# (see _PRESERVE_IF_NULL_COLUMNS) applies identically to both write paths.
_BATCH_UPDATE_SET = _listing_update_set_sql()

_BATCH_UPSERT_SQL = f"""
    INSERT INTO listings (
        sreality_id, last_seen_at, is_active,
        {", ".join(LISTING_COLUMNS)},
        street_source, source_id_native, geom, raw_json, discovery_seq
    )
    SELECT
        j.sreality_id, now(), true,
        {_BATCH_SELECT_COLS},
        CASE WHEN j.street IS NOT NULL THEN 'parser' END,
        -- sreality-only path: its native id IS sreality_id. Stamped inline so the
        -- drain (the primary sreality write path since the cadence split) no longer
        -- leaves the (source, source_id_native) natural key NULL — the hole that
        -- accumulated 396 NULL sreality rows before this fix.
        j.sreality_id::text,
        CASE
          WHEN j.lon IS NOT NULL AND j.lat IS NOT NULL
          THEN ST_SetSRID(ST_MakePoint(j.lon, j.lat), 4326)::geography
          ELSE NULL
        END,
        j.raw_json,
        j.discovery_seq
    FROM jsonb_to_recordset(%s::jsonb) AS j(
        sreality_id bigint, {_BATCH_RECORD_SPEC},
        lon double precision, lat double precision, raw_json jsonb,
        discovery_seq bigint
    )
    -- Arbiter is the natural key, not sreality_id (R2 Phase D) — see upsert_listing's
    -- identical retarget for the full safety argument. `source` isn't in this
    -- INSERT's column list (this path is sreality-only, so it always takes the
    -- 'sreality' column DEFAULT) — Postgres materializes defaults before evaluating
    -- the arbiter, so the conflict check still sees the right value.
    ON CONFLICT (source, source_id_native) DO UPDATE SET
      last_seen_at = now(),
      is_active = true,
      inactive_at = NULL,
      {_BATCH_UPDATE_SET},
      {_STREET_SOURCE_UPDATE_SQL},
      source_id_native = COALESCE(listings.source_id_native, EXCLUDED.source_id_native),
      geom = COALESCE(EXCLUDED.geom, listings.geom),
      raw_json = EXCLUDED.raw_json,
      -- Set-once (migration 368) — same shape as upsert_listing's identical clause.
      discovery_seq = COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)
    RETURNING (xmax = 0) AS inserted
"""

# Snapshot-on-change, set-based: insert a snapshot for exactly the listings
# whose content_hash differs from their latest (or that have none yet). raw_json
# is read back from the listings row just upserted in the same txn, so the large
# raw payload isn't sent twice. IS DISTINCT FROM handles the no-prior-snapshot
# case (latest NULL → distinct → one snapshot for a brand-new listing).
_BATCH_SNAPSHOT_SQL = """
    INSERT INTO listing_snapshots (sreality_id, listing_id, price_czk, content_hash, raw_json)
    SELECT j.sreality_id, l.id, j.price_czk, j.content_hash, l.raw_json
    FROM jsonb_to_recordset(%s::jsonb)
        AS j(sreality_id bigint, price_czk integer, content_hash text)
    JOIN listings l ON l.sreality_id = j.sreality_id
    LEFT JOIN LATERAL (
        -- Rekeyed onto listing_id (R2 Phase C, same rule-2 guard as upsert_listing):
        -- l.id is already joined, and listing_snapshots_listing_id_scraped_at_idx
        -- (mig 333) mirrors the legacy composite.
        SELECT content_hash FROM listing_snapshots s
        WHERE s.listing_id = l.id
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

# Broker intelligence (phase 1): a content change can alter a listing's broker
# block (it is part of the content hash), so enqueue the changed listings for
# re-attribution by scripts.resolve_brokers --incremental. This is the sreality
# feed of the incremental resolver (idnes feeds the same queue via
# ingest_scraped_listing); the resolver has no full-table straggler scan. A
# brand-new listing is a content change (no prior snapshot), so it lands here
# too. Anything missed is reconciled by the daily full sweep.
# The JOIN onto listings is the R2 dual-write handle for listing_id, same reasoning
# as _BATCH_IMAGES_SQL below: the batch upsert ran first in this same transaction,
# so every sid already has its row (and surrogate id) visible here. Arbiter is
# listing_id (R2 Phase D, dirty_broker_listings_pkey) — see ingest_scraped_listing's
# identical retarget above.
_BATCH_DIRTY_BROKERS_FROM_SIDS_SQL = """
    INSERT INTO dirty_broker_listings (sreality_id, listing_id)
    SELECT s.sid, l.id
    FROM unnest(%s::bigint[]) AS s(sid)
    JOIN listings l ON l.sreality_id = s.sid
    ON CONFLICT (listing_id) DO UPDATE SET marked_at = now()
"""

# The JOIN onto listings is the R2 dual-write handle: the batch upsert above ran
# first in this same transaction, so every j.sreality_id already has its row (and
# therefore its surrogate id) visible here. Resolving the id in SQL — rather than
# zipping a RETURNING back to Python — is deliberate: INSERT ... SELECT RETURNING
# order is unspecified, so a positional zip could silently misalign ids to rows.
_BATCH_IMAGES_SQL = """
    INSERT INTO images (sreality_id, listing_id, sreality_url, sequence)
    SELECT j.sreality_id, l.id, j.sreality_url, j.sequence
    FROM jsonb_to_recordset(%s::jsonb)
        AS j(sreality_id bigint, sreality_url text, sequence integer)
    JOIN listings l ON l.sreality_id = j.sreality_id
    -- Arbiter is listing_id (R2 Phase C, images_listing_id_sequence_key) — see
    -- record_images for why sreality_id was never safe here.
    ON CONFLICT (listing_id, sequence) DO UPDATE SET
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
        price_czk = sane_price_czk(row.get("price_czk"))
        obj: dict[str, Any] = {c: row.get(c) for c in LISTING_COLUMNS}
        obj["price_czk"] = price_czk
        sane_listing_numerics(obj)
        _set_street_name_key(obj)
        obj["sreality_id"] = sid
        obj["lon"] = row.get("lon")
        obj["lat"] = row.get("lat")
        obj["raw_json"] = r.raw or {}
        obj["discovery_seq"] = r.discovery_seq
        listing_objs.append(obj)
        snapshot_objs.append({
            "sreality_id": sid,
            "price_czk": price_czk,
            "content_hash": r.content_hash,
        })
        for img in r.images or []:
            url = img.get("url")
            if not url:
                continue
            # Backstop (mirrors record_images): keep non-image URLs out of the
            # photo pipeline. sreality has no video media today, so this never
            # fires for the drain — it's defense-in-depth for a future schema shift.
            if not media.is_image_url(url):
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
            cur.execute(_BATCH_DIRTY_BROKERS_FROM_SIDS_SQL, (changed_sids,))

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
    row a drain has already claimed. The original enqueued_at is deliberately
    KEPT on re-enqueue: the claim order is (priority DESC, enqueued_at ASC), so
    re-stamping now() pushed every still-queued row behind the walk's fresh
    inserts each run — a backlog bigger than one drain's budget then starved its
    tail forever (remax rent listings cycled unfetched for weeks) and the Health
    queue-age metrics under-reported the wait. Chunked to stay under the pooler
    timeout.
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
            prices = [sane_price_czk(p) for _, _, p, _ in chunk]
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
                    priority = GREATEST(listing_detail_queue.priority, EXCLUDED.priority)
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
) -> list[tuple[str, str | None, int | None, int]]:
    """Atomically claim up to `limit` available rows for `source`, highest
    priority + oldest first. Returns (native_id, detail_ref, index_price_czk,
    discovery_seq) — discovery_seq is the row's enqueue-time sequence value
    (see migration 368), carried through so the drain can stamp it onto
    listings.discovery_seq at write time, independent of claim/fetch/write order.

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
            RETURNING q.native_id, q.detail_ref, q.index_price_czk, q.discovery_seq
            """,
            (source, limit),
        )
        return [(nid, ref, price, dseq) for nid, ref, price, dseq in cur.fetchall()]


def complete_detail(
    conn: psycopg.Connection,
    source: str,
    native_ids: Iterable[str],
    outcome: str = "written",
) -> int:
    """Remove drained rows from the queue (success or confirmed-gone), logging
    each into detail_queue_completions (migration 265) in the same transaction
    so the enqueue->detail-write latency survives the row's deletion."""
    ids = [str(n) for n in native_ids]
    if not ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            WITH del AS (
                DELETE FROM listing_detail_queue
                WHERE source = %(source)s AND native_id = ANY(%(ids)s)
                RETURNING source, native_id, priority, attempts,
                          enqueued_at, claimed_at
            )
            INSERT INTO detail_queue_completions
                (source, native_id, priority, attempts, enqueued_at,
                 claimed_at, outcome)
            SELECT source, native_id, priority, attempts, enqueued_at,
                   claimed_at, %(outcome)s
            FROM del
            """,
            {"source": source, "ids": ids, "outcome": outcome},
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
    max_attempts so a permanently-broken listing stops re-claiming.

    A row crossing the give-up threshold is a terminal outcome, so it is logged
    to detail_queue_completions in the same transaction. The `old` self-join
    captures the pre-update claimed_at (nulled by the SET) and the give-up
    transition edge (was_given_up), so a resilient-retry replay that bumps an
    already-given-up row never logs a duplicate."""
    ids = [str(n) for n in native_ids]
    if not ids:
        return
    truncated = (error_message or "")[:500]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            WITH upd AS (
                UPDATE listing_detail_queue q SET
                    attempts   = q.attempts + 1,
                    given_up   = (q.attempts + 1) >= %(max)s,
                    claimed_at = NULL,
                    last_error = %(err)s
                FROM listing_detail_queue old
                WHERE q.source = %(source)s AND q.native_id = ANY(%(ids)s)
                  AND old.source = q.source AND old.native_id = q.native_id
                RETURNING q.source, q.native_id, q.priority, q.attempts,
                          q.enqueued_at, old.claimed_at AS old_claimed_at,
                          q.given_up, old.given_up AS was_given_up
            )
            INSERT INTO detail_queue_completions
                (source, native_id, priority, attempts, enqueued_at,
                 claimed_at, outcome)
            SELECT source, native_id, priority, attempts, enqueued_at,
                   old_claimed_at, 'given_up'
            FROM upd WHERE given_up AND NOT was_given_up
            """,
            {"max": max_attempts, "err": truncated, "source": source, "ids": ids},
        )


COMPLETION_RETENTION_DAYS = 7


def reclaim_stale_claims(
    conn: psycopg.Connection,
    source: str,
    older_than_minutes: int = 30,
) -> int:
    """Release `source` claims older than the cutoff (a drain SIGKILLed
    mid-flight), so its rows become claimable again. Mirrors
    sweep_stuck_scrape_runs. Also prunes this source's expired
    detail_queue_completions rows (7-day ephemeral ledger, the rule-#9
    posture) — running it here, at every drain start, keeps the ledger
    bounded without pg_cron."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM detail_queue_completions "
            "WHERE source = %s AND completed_at < now() - make_interval(days => %s)",
            (source, COMPLETION_RETENTION_DAYS),
        )
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


# Index pages are re-fetched on every walk, but the archive only needs ~daily
# refresh grain — the per-page TOAST write was why index archiving was removed
# in June 2026, so the re-enable (location-data W0 item 0n) refreshes an
# existing index row at most once per ~day instead of once per walk. 22h
# leaves headroom for walk-start jitter on the hourly/6h cadences.
INDEX_ARCHIVE_REFRESH_HOURS: float = 22.0

# Both raw-page upsert forms are module-level plain literals ON PURPOSE: the
# schema-and-sql CI gate (tests/sql_corpus.py) only discovers SQL that appears
# as an ast.Constant, so a concatenated/f-string variant would silently drop
# this statement from the PREPARE corpus (a confirmed review finding on the
# first cut of W0 0n).
_RAW_PAGE_UPSERT_SQL = """
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
"""

_RAW_PAGE_UPSERT_GUARDED_SQL = """
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
    WHERE portal_raw_pages.fetched_at < now() - make_interval(secs => %s)
    RETURNING id
"""


def index_archive_week(now: datetime | None = None) -> str:
    """ISO-week suffix for index-archive keys ('2026w33').

    Index pages have no stable per-page identity — their content shifts with
    pagination — so a position-only key would make the archive a rolling
    snapshot of the currently-live index, preserving nothing for delisted
    listings (the critical review finding on W0 0n's first cut). Week-stamped
    keys make the archive ACCUMULATE (one row per page position per week)
    while bounding growth; the W2a payload store supersedes this scheme.
    """
    dt = now or datetime.now(timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}w{week:02d}"


def fresh_index_page_keys(conn: psycopg.Connection, source: str, *, hours: float) -> set[str]:
    """source_id_native of this source's index pages archived in the last
    `hours` — the client-side skip set, so a walk doesn't upload multi-MB
    payloads the ON CONFLICT guard would discard server-side anyway (the
    guard stays as the racing-writer backstop)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id_native FROM portal_raw_pages"
            " WHERE source = %s AND page_kind = 'index'"
            "   AND fetched_at >= now() - make_interval(secs => %s)",
            (source, hours * 3600.0),
        )
        return {r[0] for r in cur.fetchall()}


# The W2a-0 shadow-hash churn instrument (design 02 section 2.3.2 P1, 06 section
# 6.9 OQ9): OFF by default, app_settings-backed so a flip reaches the always-on
# worker without a redeploy.
PAYLOAD_SHADOW_HASH_SETTING = "location_payload_shadow_hash"

# The flag is read at most once per process per TTL, NOT once per item. The
# Gate-2 precedent reads live, but it sits in ingest_scraped_listing — a per-item
# write path that already round-trips several times. This instrument hangs off
# write_detail_batch, whose whole design is ~4 round-trips per 100-listing batch;
# a live read per item would make that ~104, i.e. 1-3 s of pure Frankfurt-pooler
# RTT added to every flush inside the realtime worker's time-budgeted drain lane,
# to read a flag that is off. The bound this buys back: a flip is picked up
# within _FLAG_CACHE_TTL seconds by the always-on worker, and immediately by
# every cron run (a fresh process starts with an empty cache).
_FLAG_CACHE_TTL = 60.0
_FLAG_CACHE: dict[str, tuple[float, bool]] = {}

# Same TTL, keyed by SOURCE: the W2a-2 dual-write is a per-portal limit
# (PortalLimits.payload_dual_write), not one global flag, because enabling the
# archive is a per-portal storage decision. The whole resolved PortalLimits is
# cached because both payload limits come from ONE load_portal_config read —
# caching them separately would double a read that sits on a per-page path, and
# caching them as a bare pair would make the two booleans swappable at the
# unpack site with nothing to catch it.
_LIMIT_CACHE: dict[str, tuple[float, Any]] = {}

# `detail` is the ONLY page_kind the dual-write limit governs on its own, and the
# set is spelled that way round on purpose. The invariant is about GRAIN, not
# about the word "index": a detail body is one listing fetched when that listing
# is enqueued, while every other `location_page_kind` label — index, map,
# gazetteer, snapshot, archive, none — is a whole-SURFACE artefact refetched on a
# walk cadence, so its churn is the storage question `payload_index_archive`
# exists to gate. Two of those are already live and declare `archive: true`
# (ceskereality's map, bezrealitky's gazetteer); an allowlist naming only 'index'
# would have let both archive on every walk with the second gate off.
DETAIL_PAGE_KIND = "detail"
INDEX_PAGE_KIND = "index"


def clear_app_settings_flag_cache() -> None:
    """Drop every cached gate value — app_settings flags AND the per-portal
    payload-archive limit (tests; a process that must re-read at once)."""
    _FLAG_CACHE.clear()
    _LIMIT_CACHE.clear()


def _app_settings_flag_cached(
    conn: psycopg.Connection, key: str, *, ttl: float = _FLAG_CACHE_TTL,
) -> bool:
    now = time.monotonic()
    cached = _FLAG_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    value = _app_settings_flag(conn, key)
    _FLAG_CACHE[key] = (now + ttl, value)
    return value


_PAYLOAD_CHURN_UPSERT_SQL = """
    INSERT INTO portal_payload_churn
        (source, source_id_native, page_kind, normalizer_version,
         first_seen_at, last_seen_at,
         fetches, raw_changes, norm_changes,
         last_raw_sha256, last_norm_sha256, last_byte_size, last_norm_byte_size,
         last_observation)
    VALUES (%s, %s, %s::location_page_kind, %s,
            coalesce(%s::timestamptz, now()), coalesce(%s::timestamptz, now()),
            1, 0, 0, %s, %s, %s, %s, %s)
    ON CONFLICT (source, source_id_native, page_kind, normalizer_version)
    DO UPDATE SET
        last_seen_at        = greatest(portal_payload_churn.last_seen_at,
                                       EXCLUDED.last_seen_at),
        fetches             = portal_payload_churn.fetches + 1,
        raw_changes         = portal_payload_churn.raw_changes
                              + (portal_payload_churn.last_raw_sha256
                                 IS DISTINCT FROM EXCLUDED.last_raw_sha256)::int,
        norm_changes        = portal_payload_churn.norm_changes
                              + (portal_payload_churn.last_norm_sha256
                                 IS DISTINCT FROM EXCLUDED.last_norm_sha256)::int,
        last_raw_sha256     = EXCLUDED.last_raw_sha256,
        last_norm_sha256    = EXCLUDED.last_norm_sha256,
        last_byte_size      = EXCLUDED.last_byte_size,
        last_norm_byte_size = EXCLUDED.last_norm_byte_size,
        last_observation    = EXCLUDED.last_observation
    WHERE portal_payload_churn.last_observation
          IS DISTINCT FROM EXCLUDED.last_observation
"""


def record_payload_churn(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    body: bytes,
    content_type: str,
    observation: str,
    fetched_at: datetime | None = None,
    normalizer_version: str | None = None,
) -> None:
    """Bump this artefact's raw-vs-normalised churn counters. Stores NO body.

    The measurement 02 section 2.3.2 makes a gate on P2 (index archiving): one
    bounded row per (source, source_id_native, page_kind, normalizer_version)
    carrying both hashes, both sizes and three counters, so the change RATE is a
    number instead of the assumption the storage projection currently rests on.

    `observation` identifies the FETCH, not the call: replaying it is a no-op
    (migration 402's WHERE guard), which is what makes this safe to run inside
    the drain's run_resilient-retried batch write.

    `normalizer_version` overrides the cohort this fetch is counted in. The live
    ingest never passes one — it takes the resolver's answer for this surface, which
    is `payload_norm@N+profile@<digest>` where the portal's contract declares a
    volatile profile and `payload_norm@N+base` where it does not. The confirmation
    probe passes `payload_norm.probe_normalizer_version()` so its minutes-apart
    cadence lands in its own cohort instead of contaminating the passive counters.

    PRECONDITION: an autocommit connection (scraper.db.connect). The statement is
    self-contained, so a caller already inside `with conn.transaction():` gets it
    wrapped in a savepoint instead — a churn failure must never poison the
    caller's transaction and take the ingest write down with it.
    """
    # Deferred: location_data is not on the scraper's import path (and not in the
    # API image before this wave), so the flag-off scrape never pays for it.
    from location_data.payload_norm import normalise, resolve_normalisation

    # By (source, page_kind), never by source alone: every shipped profile was
    # derived by diffing DETAIL pages, and an index page is a LIST of properties,
    # not a property. The resolution falls back to the generic base for a surface
    # nobody has diffed and stamps that fallback into its own cohort, so the two
    # instruments never average together — profile and cohort as ONE answer, so a
    # counter can never be filed under a normaliser that was not the one applied.
    resolved = resolve_normalisation(source, page_kind)
    result = normalise(
        body,
        content_type=content_type,
        volatile=resolved.profile,
    )
    params = (
        source, source_id_native, page_kind,
        normalizer_version or resolved.normalizer_version,
        fetched_at, fetched_at,
        result.raw_sha256, result.norm_sha256,
        result.byte_size, result.norm_byte_size, observation,
    )
    if getattr(conn, "autocommit", True):
        with conn.cursor() as cur:
            cur.execute(_PAYLOAD_CHURN_UPSERT_SQL, params)
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_PAYLOAD_CHURN_UPSERT_SQL, params)


def record_payload_churn_if_enabled(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    body: bytes | Callable[[], bytes],
    content_type: str | None = None,
    observation: str | None = None,
    fetched_at: datetime | None = None,
) -> None:
    """Flag-gated, never-raising wrapper — the ONLY form callers should use.

    An instrument that can break the thing it measures is worthless, so every
    failure (flag read, normaliser, upsert) warns and returns. `content_type`
    None sniffs the body, for the archive path that is handed both HTML and JSON
    through one `html` parameter.

    `body` should be a THUNK wherever producing the bytes costs anything: the
    flag is read first and a disabled instrument must do no serialisation work at
    all (sreality's index payload is multi-MB and this sits in the hourly walk).
    Serialising inside the try/except is also what keeps a non-JSON-serialisable
    value in a raw dict from killing a 100-item flush.

    `observation` identifies the logical FETCH. Callers inside a retried op MUST
    pass one that survives the replay (DrainItem.observation_id); anything else
    gets a fresh token per call, which is the pre-existing count-every-call
    behaviour.
    """
    try:
        if not _app_settings_flag_cached(conn, PAYLOAD_SHADOW_HASH_SETTING):
            return
        payload = body() if callable(body) else body
        if content_type is None:
            from location_data.payload_norm import sniff_content_type
            content_type = sniff_content_type(payload)
        record_payload_churn(
            conn,
            source=source,
            source_id_native=source_id_native,
            page_kind=page_kind,
            body=payload,
            content_type=content_type,
            observation=observation or uuid.uuid4().hex,
            fetched_at=fetched_at,
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must not kill ingest
        LOG.warning(
            "payload churn record failed source=%s key=%s: %s",
            source, source_id_native, exc,
        )


def _payload_limits(
    conn: psycopg.Connection, source: str, *, ttl: float = _FLAG_CACHE_TTL,
) -> Any:
    """This portal's resolved `PortalLimits`, cached for the payload gates.

    Resolved through the standard limit precedence (baked default < global
    `app_settings.scraper_limits_global` < `portals.operational_limits`), so
    enabling either needs no migration and no deploy — the `shared_rate_limiter`
    precedent. Cached per source for the same reason the shadow-hash flag is:
    the read is two SELECTs and this sits on a per-page path.

    A failed read is cached as the BAKED defaults (every gate off) for the same
    TTL. Off is the safe direction (the archive is an addition, never a
    dependency of the scrape), and caching the failure is what keeps an
    unreadable registry row from re-asking twice per fetched page for a walk.
    """
    now = time.monotonic()
    cached = _LIMIT_CACHE.get(source)
    if cached is not None and cached[0] > now:
        return cached[1]
    # `autocommit` is a PROXY for "not inside a transaction", exact for every
    # caller today (the drain and the index archivers all hold an autocommit
    # connection). It would under-protect only an autocommit connection read from
    # inside an explicit `with conn.transaction():` — no such caller exists.
    # Deferred import: scraper.portal must stay free to import scraper.db.
    from scraper.portal import PortalLimits, load_portal_config

    value = PortalLimits()
    try:
        if getattr(conn, "autocommit", True):
            value = load_portal_config(conn, source).limits
        else:
            # A caller already inside a transaction gets the read wrapped in a
            # savepoint: a gate this optional must never poison the ingest
            # transaction it is riding in.
            with conn.transaction():
                value = load_portal_config(conn, source).limits
    except Exception as exc:  # noqa: BLE001 - the gate must not kill ingest
        LOG.warning("payload archive limit read failed source=%s: %s", source, exc)
        value = PortalLimits()
    _LIMIT_CACHE[source] = (now + ttl, value)
    return value


def _payload_archive_enabled(
    conn: psycopg.Connection, source: str, page_kind: str, *, ttl: float = _FLAG_CACHE_TTL,
) -> bool:
    """May this body be appended to the payload archive?

    One gate for a detail body, two in series for every other page_kind (W2a-6),
    and a third that is not a flag at all: the surface has to have been WEIGHED.
    The second gate is an AND on top of `payload_dual_write`, never an OR:
    surface-grain bodies re-order on every walk and are refetched on the walk
    cadence (sreality's index 24x/day), so a portal whose per-listing churn signs
    off cheaply may still have surface churn that does not — but "the archive is
    off for this portal" has to stay one switch that means it.

    The third gate closes a hole the flags cannot: `location_data.payload_budget`
    is the frozen corpus the storage ceiling is computed from, and it carries only
    the surfaces someone has measured (today, every portal's `detail`). Archiving
    an unmeasured surface would not break anything visibly — it would make the
    number the operator signed silently wrong, which is worse. So an unmeasured
    (source, page_kind) is refused here, which is also what forces whoever turns
    on `payload_index_archive` to profile the index surface first instead of
    discovering its cost on the storage bill.
    """
    limits = _payload_limits(conn, source, ttl=ttl)
    if not limits.payload_dual_write:
        return False
    if page_kind != DETAIL_PAGE_KIND and not limits.payload_index_archive:
        return False
    from location_data import payload_budget

    if not payload_budget.is_measured(source, page_kind):
        LOG.warning(
            "payload archive refuses unmeasured surface source=%s page_kind=%s — add it "
            "to location_data/payload_budget.PORTAL_STORAGE (re-derive with "
            "scripts/location_payload_storage_ceiling.py) before archiving it",
            source, page_kind,
        )
        return False
    return True


def append_payload_if_enabled(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    body: bytes | Callable[[], bytes],
    content_type: str | None = None,
    http_status: int | None = None,
) -> None:
    """Limit-gated, never-raising dual-write into the payload archive (W2a-2).

    A `detail` body passes `payload_dual_write` alone; EVERY other page_kind —
    index, map, gazetteer, snapshot, archive, none — passes `payload_index_archive`
    as well (W2a-6), because all of them are surface-grain artefacts refetched on
    a walk cadence rather than one listing's body.

    `portal_raw_pages` is latest-wins, so the body a claim's evidence span
    points into is gone the moment the page is refetched;
    `location_data.payloads.append_payload` is the append-on-change store that
    ends that, and this is the ONLY form scrapers should call it in. Every
    failure — limit read, normaliser, append — warns and returns: the archive
    is downstream of the scrape and must never be able to stop it.

    `body` should be a THUNK wherever producing the bytes costs anything (the
    churn hook's reasoning: sreality's index payload is multi-MB and this rides
    the hourly walk), and `content_type` None sniffs it, for the chokepoint that
    is handed both HTML and JSON through one `html` parameter.

    No idempotency token, unlike the churn counters: the append is
    content-addressed, so a `_flush_drain_batch` replay after a pooler drop
    collides on the same `payload_sha256` and bumps `last_observed_at` instead
    of writing a second row.
    """
    try:
        if not _payload_archive_enabled(conn, source, page_kind):
            return
        payload = body() if callable(body) else body
        if content_type is None:
            from location_data.payload_norm import sniff_content_type
            content_type = sniff_content_type(payload)
        # Deferred like the churn hook's: with the limit off (the default) the
        # scrape never imports the archive at all.
        from location_data.payloads import append_payload

        append_payload(
            conn,
            source=source,
            source_id_native=source_id_native,
            page_kind=page_kind,
            # The chokepoint knows the portal's own key, not our pk — the row is
            # reachable by (source, source_id_native, page_kind) either way, and
            # inventing a listing_id here would mean an extra lookup per page.
            listing_id=None,
            body=payload,
            content_type=content_type,
            http_status=http_status,
            # `contract_version` NULL is honest here: it is the version of the
            # EXTRACTION contract a mined payload was claimed under, and nothing has
            # mined this body — W2's claim intake is what stamps it. The portal's
            # contract does decide how this body normalises (its
            # `persistence.volatile_paths`), and `normalizer_version` says so with a
            # digest of the profile applied; that is a different axis on purpose,
            # which is why the archive can be reconfigured without re-versioning any
            # claim (migration 408).
            contract_version=None,
            observed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 - the archive must not kill ingest
        LOG.warning(
            "payload archive append failed source=%s key=%s: %s",
            source, source_id_native, exc,
        )


def upsert_portal_raw_page(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    source_url: str,
    page_kind: str,
    html: str,
    http_status: int | None,
    refresh_after_hours: float | None = None,
    record_churn: bool = True,
    churn_observation: str | None = None,
) -> int | None:
    """Latest-wins upsert of one fetched HTML page into portal_raw_pages.

    Decouples fetch from parse so a page can be re-parsed without re-fetching.
    Returns the staging row id; a re-fetch overwrites the HTML and clears the
    previous parse state. With `refresh_after_hours` set, an existing row
    younger than that is left untouched and None is returned — the write-cost
    guard for index-page archiving.

    `record_churn=False` is for the callers that already recorded the fetch
    themselves (the index archivers, which measure pages their client-side
    freshness skip never hands to this function) — without it those surfaces
    would double-count exactly the fetches that do get archived.
    `churn_observation` is the caller's per-fetch idempotency token; the drain's
    write_details MUST pass DrainItem.observation_id, because that whole call is
    replayed on a transient pooler drop.

    W2a-2's payload dual-write hangs off the END of this function, so ONE edit
    covers every HTML writer that stages through here — the seven detail writers
    and the three index archivers — with no per-portal branch (rule #21). It does
    NOT cover the two portals that stage no body (sreality's estate JSON,
    bezrealitky's advert), which call `append_payload_if_enabled` directly, nor
    does it decide the gate: W2a-6 gives every non-`detail` page_kind a second
    limit, resolved inside that function. It is deliberately NOT gated on the
    staging row: a
    `refresh_after_hours` skip means portal_raw_pages already holds a body young
    enough, which says nothing about whether the CONTENT moved — and an
    append-on-change archive that drops a genuinely changed body is the one
    failure it cannot recover from. An unchanged one costs a no-op DO UPDATE.
    """
    encoded: bytes | None = None

    def _body() -> bytes:
        # Memoised: with both gates on, a multi-MB index payload is encoded once
        # for the churn hook and the archive, not twice.
        nonlocal encoded
        if encoded is None:
            encoded = html.encode("utf-8")
        return encoded

    if record_churn:
        record_payload_churn_if_enabled(
            conn,
            source=source,
            source_id_native=source_id_native,
            page_kind=page_kind,
            body=_body,
            observation=churn_observation,
        )
    with conn.cursor() as cur:
        if refresh_after_hours is None:
            cur.execute(
                _RAW_PAGE_UPSERT_SQL,
                (source, source_id_native, source_url, page_kind, html, http_status),
            )
        else:
            cur.execute(
                _RAW_PAGE_UPSERT_GUARDED_SQL,
                (source, source_id_native, source_url, page_kind, html,
                 http_status, refresh_after_hours * 3600.0),
            )
        row = cur.fetchone()
        page_id = int(row[0]) if row is not None else None
    append_payload_if_enabled(
        conn,
        source=source,
        source_id_native=source_id_native,
        page_kind=page_kind,
        body=_body,
        http_status=http_status,
    )
    return page_id


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
