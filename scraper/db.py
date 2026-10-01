"""Database I/O for listings lifecycle, the detail queue, images and run bookkeeping.

The listing content write is scraper/listing_write.py.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb, set_json_dumps

from scraper import media
from scraper.attribute_contract import CONTRACT

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
    # Portal-declared publication/last-bump timestamp (migration 266). Out of
    # every content hash (bazos re-stamps it per bump; backfills stay free).
    "published_at",
    # The listing's page on its own portal, emitted by every portal's parser (sreality
    # assembles it in scraper.sreality_url) — a stored fact read everywhere and
    # reconstructed nowhere. Out of every content hash (identity, not content).
    "source_url",
)

# Postgres type for each LISTING_COLUMN: the jsonb_to_recordset column spec of
# scraper/listing_write.py. Kept in lockstep with LISTING_COLUMNS by the assertion
# below so a new scraper column can't silently break the write.
_LISTING_COLUMN_PGTYPE: dict[str, str] = {
    "category_main": "text",
    "category_type": "text",
    "price_czk": "integer",
    "price_unit": "text",
    "area_m2": "numeric",
    "area_basis": "text",
    "disposition": "text",
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
    "published_at": "timestamptz",
    "source_url": "text",
}
assert set(_LISTING_COLUMN_PGTYPE) == set(LISTING_COLUMNS), (
    "_LISTING_COLUMN_PGTYPE drifted from LISTING_COLUMNS"
)

# published_at (migration 266): the signal is intermittent at the source (sreality's
# `edited` exists on ~40% of rows; a portal can stop rendering its date), so a fetch that
# yields no date must not erase what an earlier fetch or a raw_json backfill recorded — a
# fresher portal date still wins. Out of the content hash, informational only.
# source_url (docs/design/portal-listing-url.md): an incoming NULL can only mean "the
# parser could not assemble the URL" (a sreality code outside its codebook, a missing
# locality), never "the listing left its page" — so it must not erase a stored URL, or
# one detail-drain cycle would wipe the backfilled history (the migration-262 street
# incident). Clearing a known-bad URL is a deliberate act, never the ingest path's.
_PRESERVE_IF_NULL_COLUMNS = frozenset({"published_at", "source_url"})

# R4: the same preserve-if-null shape, decided per (source, column) by the ATTRIBUTE
# CONTRACT instead of by a hand-kept list. A `structured` or `derived` cell IS the parse's
# verdict — the portal stopped stating the fact, or the URL/breadcrumb stopped yielding it
# — so its NULL still clears, and a portal that drops a fact can still drop it. A `text`
# or `none` cell is the opposite: the prose grammar only speaks when the prose does, and
# nothing in the parse ever looks at a `none` cell, so the NULL those two produce is
# SILENCE, not a correction. Writing it anyway is what erased 12-14% of every cell the
# description-enrichment lane filled (2,949 of 24,621 `condition` fills; 100% of them on
# rows refetched after the fill, 0 in the complement) — permanently, because an
# unchanged-hash refetch mints no snapshot for the lane's selector to re-attempt.
# Residual, accepted and with NO automated remedy today: the ingest grammar can no longer
# CLEAR a text cell it stops matching, the same trade already accepted for published_at /
# source_url. `scripts/reparse.py` (R9) can CORRECT such a cell but never blanks one by
# design, so removing a stale preserved value is a deliberate hand-written UPDATE until
# something is built for it.
_PARSE_SILENT_PRODUCERS = frozenset({"text", "none"})

# `area_basis` is `derived` on every portal and would therefore clear — but it is not an
# independent verdict: `scraper.area.derive_headline_area` stamps it on whichever measure
# it just picked for `area_m2` and returns (None, None) together. Letting the two decouple
# would leave a preserved bazos area with its basis blanked, i.e. a stored parcel figure
# (14,901 active rows read 'plot') silently re-reading as usable area to price-per-m2,
# best_area and the plot guards. So the pair moves together: basis follows the number.
_AREA_BASIS_FOLLOWS = ("area_m2", "area_basis")


@lru_cache(maxsize=None)
def _preserved_columns(source: str) -> frozenset[str]:
    """Columns a NULL from `source`'s parse must not clear.

    A source with no contract row keeps only the two identity preserves — the pre-R4
    behaviour; the rail that keeps the nine portals in the contract is
    `tests/scraper/test_attribute_contract.py`, which pins the contract's keys to
    `scraper.portal._DEFAULTS` (the per-portal config fleet, rule 21)."""
    preserved = _PRESERVE_IF_NULL_COLUMNS | frozenset(
        column for column, declared in CONTRACT.get(source, {}).items()
        if declared.producer in _PARSE_SILENT_PRODUCERS
    )
    number, basis = _AREA_BASIS_FOLLOWS
    return preserved | {basis} if number in preserved else preserved


def detail_ref(source: str, source_url: str | None) -> str | None:
    """The URL a detail fetch may use, or None to fetch by native id.

    sreality is ALWAYS None, and that is a safety boundary, not an optimisation: its
    listings.source_url is the human page, which 302s into a login.seznam.cz autologin
    chain and an infinite `cwtkn=` redirect loop (contracts/portals/sreality.yaml — "must
    not be" evaded). The scraper reaches sreality by id through /api/v1/estates. This is
    the one place that decision lives; every enqueue path that carries a stored URL into
    the queue routes through it.
    """
    return None if source == "sreality" else source_url

@lru_cache(maxsize=None)
def _listing_update_set_sql(source: str) -> str:
    """The ONE ON CONFLICT SET builder, rendered into scraper/listing_write.py's per-source
    upsert (and read by reparse), so preserve-if-null semantics live in one place."""
    preserved = _preserved_columns(source)
    return ",\n          ".join(
        (f"{c} = COALESCE(EXCLUDED.{c}, listings.{c})" if c in preserved
         else f"{c} = EXCLUDED.{c}")
        for c in LISTING_COLUMNS
    )


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


def refresh_matview(
    conn: psycopg.Connection,
    name: str,
    *,
    statement_timeout: str | None = None,
) -> int:
    """Publish one registered matview through public.refresh_matview (migration 578)
    — the ONE Python path to a refresh.

    The SQL side refuses unregistered names (loud, replacing the silent-no-op stamp
    for matview producers), refreshes CONCURRENTLY so readers never block (plain form
    only on an unpopulated first populate), returns -1 without refreshing when another
    refresh of the same matview is in flight, and stamps derived_artifacts with real
    rows + duration_ms inside the refresh's own transaction.

    `statement_timeout` re-budgets the refresh transaction ('0' = no limit); None
    inherits the connection's budget. Callers already inside a transaction get a
    savepoint; note a SET LOCAL made here then lasts to the END of the outer
    transaction, so only pass a timeout from top-level callers.
    """
    with conn.transaction(), conn.cursor() as cur:
        if statement_timeout is not None:
            cur.execute(
                "select set_config('statement_timeout', %s, true)",
                (statement_timeout,))
        cur.execute("select public.refresh_matview(%s)", (name,))
        return int(cur.fetchone()[0])


# The Gate-2 flip-writer scaffold (wave-5 item 7): OFF by default, so the listing
# writer's first-sight nextval draw (scraper/listing_write.py) stays on until an
# operator explicitly opts in.
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


# THE one way a property is born (rule 15): a bare row naming its one advert, linked in the same
# statement; `scripts.recompute_property_stats` fills every other column from that advert.
# `is_active` rides along only so the status-history trigger (migration 392) logs the advert's
# real state at birth. A listing linked meanwhile is skipped, never re-pointed.
NEW_SINGLETONS_SQL = """
    WITH born AS (
        INSERT INTO properties (repr_listing_ref_id, is_active)
        SELECT l.id, l.is_active FROM listings l
        WHERE l.id = ANY(%(ids)s::bigint[]) AND l.property_id IS NULL
        RETURNING id, repr_listing_ref_id
    )
    UPDATE listings l SET property_id = born.id
    FROM born
    WHERE l.id = born.repr_listing_ref_id AND l.property_id IS NULL
    RETURNING born.id
"""


def create_singleton_properties(
    conn: psycopg.Connection, listing_ids: Collection[int],
) -> list[int]:
    """Give each still-unlinked listing its own bare property; returns the new property ids."""
    with conn.cursor() as cur:
        cur.execute(NEW_SINGLETONS_SQL, {"ids": sorted({int(i) for i in listing_ids})})
        return [int(r[0]) for r in cur.fetchall()]


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
    listing_id: int,
    images: Iterable[dict[str, Any]],
) -> int:
    """Insert any image rows that don't already exist. Returns newly inserted count.

    Keyed on the surrogate `listings.id`, so `images.listing_id` is non-NULL, which is
    what the ON CONFLICT (listing_id, sequence) arbiter needs to dedupe. A fetched
    payload's media are written by scraper/listing_write.py; this serves the heals
    (scripts/reextract.py via record_media).
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
    # unbounded duplicate row on every refetch. images.sreality_id mirrors the
    # listing's own (legacy negative today, NULL after the Gate-2 flip) so the two
    # never disagree. The DB backstop for the non-NULL invariant is
    # images_listing_id_present_check (migration 350).
    values_sql = ", ".join(
        "((SELECT sreality_id FROM listings WHERE id = %s), %s, %s, %s)" for _ in kept
    )
    flat: list[Any] = [
        v for url, seq in kept for v in (listing_id, listing_id, url, seq)
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
    listing_id: int,
    videos: Iterable[dict[str, Any]],
) -> int:
    """Insert video-media rows into listing_videos. Returns newly inserted count.

    Mirrors record_images (same de-dupe + URL-refresh-where-not-downloaded upsert,
    keyed on the surrogate `listings.id`) but writes the non-image sibling table. We capture
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

    values_sql = ", ".join(
        "((SELECT sreality_id FROM listings WHERE id = %s), %s, %s, %s)" for _ in kept
    )
    flat: list[Any] = [
        v for url, seq in kept for v in (listing_id, listing_id, url, seq)
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

    The media split for a payload-free heal (scripts/reextract.py); a fetched payload's
    media go through scraper/listing_write.py. Images land in `images`, videos in
    `listing_videos`, with each item's sequence = its original gallery position
    (so a leading video leaves a sequence gap, never renumbering the photos).
    Returns the number of newly inserted image rows (what the portals log).

    `listing_id` is the SURROGATE `listings.id`, carried straight into the child
    rows' FK — never a sreality_id, which is NULL for a post-Gate-2 portal row.
    """
    image_rows, video_rows = media.split_media_rows(media_urls)
    new_images = record_images(conn, listing_id, image_rows)
    record_videos(conn, listing_id, video_rows)
    return new_images


TOUCH_CHUNK_SIZE = 250
# touch_listings deadlock retry: a lock race with a concurrent writer over the
# same rows. Postgres has already rolled back the victim, so the other side
# finishes and a short pause suffices; three attempts covers a double race.
_TOUCH_DEADLOCK_ATTEMPTS = 3
_TOUCH_DEADLOCK_DELAY = 0.5
# A lock-wait cancel means the statement sat the FULL statement_timeout (2 min)
# behind a long writer; by the time it fails the holder is usually committed,
# so pause long enough for it to finish releasing rather than re-queue at once.
_TOUCH_LOCKWAIT_DELAY = 5.0


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
            total += _touch_chunk_with_retry(cur, chunk, _touch_chunk)
    return total


def _touch_chunk_with_retry(
    cur: Any, chunk: list[int], touch: Callable[[Any, list[int]], int],
) -> int:
    """One touch chunk, retried when it loses a lock fight.

    Two ways a touch loses one, both seen in production, both fatal to the
    category walk that had already finished its real work:

    * DeadlockDetected -- a lock-order race with a concurrent writer (the drain,
      the realtime worker, a bulk job). 2026-09-05 10:38: a fully-paged sreality
      komercni/prodej walk recorded collected=0 and skipped its sweep because
      this LAST step lost the race. Three of 46 runs.
    * QueryCanceled -- "canceling statement due to statement timeout ... while
      locking tuple": the touch waited the whole 2-minute statement_timeout
      behind a long transaction holding the same rows. 2026-09-05 21:26 and
      21:27: idnes dum/prodej AND ceskereality komercni/prodej died within a
      minute of each other, both behind the hourly MF-yield recompute, whose
      single bulk UPDATE over listings held row locks for ~90 s twice. The touch
      statements themselves are indexed lookups over <=250 ids and never take
      two minutes on their own, so a cancel here is a lock wait, not slowness.

    Retrying is safe: the walk connection is autocommit, so either error aborts
    only this statement, and both statements in a chunk are idempotent (SET
    last_seen_at = now(); INSERT ... ON CONFLICT). After a deadlock Postgres has
    already rolled back the victim, so the other side completes and a short
    pause is enough; after a lock-wait cancel the holder is usually done, so a
    longer pause lets it finish releasing. Anything else raises unchanged.
    """
    for attempt in range(1, _TOUCH_DEADLOCK_ATTEMPTS + 1):
        try:
            return touch(cur, chunk)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.QueryCanceled) as exc:
            if attempt == _TOUCH_DEADLOCK_ATTEMPTS:
                raise
            lock_wait = isinstance(exc, psycopg.errors.QueryCanceled)
            LOG.warning(
                "touch: %s on chunk of %d (attempt %d/%d); retrying",
                "lock-wait timeout" if lock_wait else "deadlock",
                len(chunk), attempt, _TOUCH_DEADLOCK_ATTEMPTS,
            )
            base = _TOUCH_LOCKWAIT_DELAY if lock_wait else _TOUCH_DEADLOCK_DELAY
            time.sleep(base * attempt)
    raise AssertionError("unreachable")


def _touch_chunk(cur: Any, chunk: list[int]) -> int:
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
    return cur.rowcount or 0


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
            total += _touch_chunk_with_retry(cur, chunk, _touch_chunk_by_id)
    return total


def _touch_chunk_by_id(cur: Any, chunk: list[int]) -> int:
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
    return cur.rowcount or 0


def presence_candidates(
    conn: psycopg.Connection,
    source: str,
    category_main: str | None,
    category_type: str,
    seen: set[Any],
    *,
    seen_key: str = "native",
    subtype: str | None = None,
    scope_subtype: bool = False,
) -> tuple[list[tuple[str, str | None, int | None]], int]:
    """The active rows of this (source, category) that a COMPLETE walk did not
    see, oldest sighting first, plus the scope's active row count.

    Rule #3 since 2026-09-07: index absence NOMINATES, it no longer delists. A
    row the walk did not see is a candidate; the detail drain then visits its
    page, and the page decides -- a positive gone signal (404/410, a redirect
    off the listing, the portal's own "no longer active" text) flips it, a live
    page refreshes it, an error leaves it for the next pass. That is why this
    query carries none of the old sweep's rails (no min_unseen_hours, no refusal):
    a wrong nomination costs one fetch, not a live listing.

    `seen_key` names what the walk's seen set holds: portal-native string ids
    (`source_id_native`, every crawler portal) or sreality's integer ids
    (`sreality_id`). `scope_subtype` narrows to `subtype` for portals whose fine
    index sections collapse onto one category_main (bazos: chata + dum -> dum),
    so one section's walk cannot nominate its siblings' rows every run.

    `category_main=None` scopes by category_type alone: the agenda portals
    (remax, maxima) walk one mixed sale/rent index whose completeness is proved
    per AGENDA, and a listing's title-derived category can differ from its
    detail-derived one, so nominating per category would nominate rows the
    agenda walk did see.

    Returns (native_id, detail_ref, price) triples: detail_ref is the stored
    source_url for crawler portals and None for sreality (its fetch derives the
    URL from the id); price is carried so the re-enqueue does not blank the
    queue's observed price.
    """
    key_col = "sreality_id" if seen_key == "sreality_id" else "source_id_native"
    ids = [x for x in seen if x is not None]
    if seen_key == "sreality_id":
        ids = [int(x) for x in ids]
    else:
        ids = [str(x) for x in ids]
    sub_clause = "\n              AND subtype IS NOT DISTINCT FROM %s" if scope_subtype else ""
    cm_clause = "\n              AND category_main = %s" if category_main is not None else ""
    scope_params: list[Any] = [source]
    if category_main is not None:
        scope_params.append(category_main)
    scope_params.append(category_type)
    if scope_subtype:
        scope_params.append(subtype)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*)
            FROM listings
            WHERE is_active = true
              AND source = %s{cm_clause}
              AND category_type = %s{sub_clause}
            """,
            tuple(scope_params),
        )
        active_rows = int(cur.fetchone()[0])
        # Two exclusions keep the oldest-unseen order from jamming. A row that
        # already has a queue row (waiting, claimed, or given up after five
        # failures) is in the drain's hands, and a row the drain finished in
        # the last day was just checked; without these, a page that errors
        # keeps its old last_seen_at, sorts first forever and eats every
        # throttle slot while the rows behind it are never nominated.
        cur.execute(
            f"""
            SELECT {key_col}::text, source_url, price_czk
            FROM listings l
            WHERE l.is_active = true
              AND l.source = %s{cm_clause}
              AND l.category_type = %s{sub_clause}
              AND l.{key_col} <> ALL(%s)
              AND NOT EXISTS (
                    SELECT 1 FROM listing_detail_queue q
                    WHERE q.source = l.source AND q.native_id = l.{key_col}::text)
              AND NOT EXISTS (
                    SELECT 1 FROM detail_queue_completions c
                    WHERE c.source = l.source AND c.native_id = l.{key_col}::text
                      AND c.completed_at > now() - interval '24 hours')
            ORDER BY l.last_seen_at ASC NULLS FIRST, l.id
            """,
            tuple(scope_params) + (ids,),
        )
        rows = cur.fetchall()
    return (
        [(str(r[0]), detail_ref(source, r[1]), r[2]) for r in rows],
        active_rows,
    )


def enqueue_presence_checks(
    conn: psycopg.Connection,
    source: str,
    category_main: str | None,
    category_type: str,
    candidates: Sequence[tuple[str, str | None, int | None]],
    *,
    active_rows: int,
    subtype: str | None = None,
) -> tuple[int, int]:
    """Queue nominated rows for a page check, bounded per walk. Returns
    (queued, deferred).

    The bound is the old flip cap (`app_settings.delist_flip_cap`: fraction of
    the scope's active rows, only above min_rows), repurposed. It no longer
    refuses anything -- every closure is now page-verified -- it throttles: a
    walk that nominates more than its share queues the oldest-unseen share now
    and leaves the rest for the next walk, so a broken walk (240 of 4,771
    collected) cannot flood the drain with thousands of fetches, and a real
    backlog drains in a few walks without anyone typing an override. The
    operator's bounded, expiring overrides still lift the bound for one scope.
    A truncation is RECORDED in delist_flip_refusals (same columns, the row now
    means "deferred", not "refused") so the signal outlives the Actions log.
    """
    total = len(candidates)
    if total == 0:
        return 0, 0
    fraction, min_rows, overrides = _delist_cap(conn)
    limit = total
    if active_rows >= min_rows:
        cap = max(1, int(active_rows * fraction))
        if total > cap:
            permit = _delist_override_permits(
                overrides, source=source, category_main=category_main,
                category_type=category_type, subtype=subtype, candidates=total,
            )
            if permit is not None:
                LOG.warning(
                    "VERIFY OVERRIDE source=%s cm=%s ct=%s candidates=%d cap=%d "
                    "-- operator override in force until %s (%s)",
                    source, category_main, category_type, total, cap,
                    permit.get("until"), permit.get("reason", "no reason given"),
                )
            else:
                limit = cap
                LOG.warning(
                    "VERIFY DEFERRED source=%s cm=%s ct=%s candidates=%d active=%d cap=%d "
                    "-- queuing the %d oldest-unseen now, the rest next walk",
                    source, category_main, category_type, total, active_rows, cap, cap,
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO delist_flip_refusals
                                (source, category_main, category_type, subtype,
                                 candidates, active_rows, cap)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (source, category_main, category_type, subtype,
                             total, active_rows, cap),
                        )
                except Exception as exc:  # noqa: BLE001 - bookkeeping never blocks the queue
                    LOG.warning("presence checks: could not record the deferral: %s", exc)
    batch = list(candidates)[:limit]
    queued = enqueue_detail(
        conn, source,
        [(nid, ref, price, QUEUE_PRIORITY_VERIFY) for nid, ref, price in batch],
    )
    # A row the drain gave up on (5 failed fetches) is excluded from every claim
    # and never re-armed by anything else, so a listing whose page erred five
    # times once would stay active forever with no path to a check. Give a
    # bounded number of them another five tries per walk, oldest first, and
    # move them to the back of the line (enqueued_at = now()) so the same few
    # cannot monopolise the budget walk after walk. Rows in the queue are not
    # nominated above, so this is the only way a given-up row comes back
    # (rule #5: tracked, not dropped).
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE listing_detail_queue q
            SET given_up = false, attempts = 0, enqueued_at = now(),
                priority = LEAST(q.priority, %(verify)s)
            WHERE (q.source, q.native_id) IN (
                SELECT source, native_id FROM listing_detail_queue
                WHERE source = %(source)s AND given_up = true AND claimed_at IS NULL
                ORDER BY enqueued_at
                LIMIT %(rearm)s
            )
            """,
            {"source": source, "verify": QUEUE_PRIORITY_VERIFY, "rearm": PRESENCE_REARM_PER_WALK},
        )
    return queued, total - limit


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


# --- the delisting flip cap (migration 451) ---------------------------------
#
# mark_inactive never had a ceiling: it flips every unseen active row of a
# category in one statement, however many that is. That was survivable only
# because the completeness gate kept the dangerous cases from running -- a
# coincidence, not a safety property, and the coincidence ends every time we
# repair a portal's walk. Fixing coverage is the SAME EVENT as authorising the
# mass flip it unblocks.
#
# CALIBRATED AGAINST 60 DAYS OF REAL SWEEPS, not guessed. Over 11,763 sweeps
# that flipped at least one row, the per-sweep share of a category is p95=1.8%,
# p99=3.4%, and then the tail jumps straight to 86%: routine churn and genuine
# incidents are two separate populations, and the gap between them is where the
# ceiling belongs. At 2% the breaker would have tripped 446 times in 60 days --
# on ordinary sreality and idnes rental churn -- which is not a breaker, it is
# an outage generator. At 10% it trips on exactly the four real events in that
# window (realitymix dum/prodej at 86%, ceskereality komercni/prodej at 30% and
# 13.7%, sreality pozemek/podil at 18.7%).
#
# min_rows is a floor on CATEGORY SIZE, not on the ceiling. It is 2,000 because
# the small categories are the churny ones: sreality pozemek/drazba holds ~600
# live rows and legitimately turns over 6-39% of them in a sweep (auctions end
# on a date), as does idnes dum/pronajem at ~630. Policing those is noise.
_DELIST_CAP_DEFAULTS = {"fraction": 0.10, "min_rows": 2000}


def _delist_cap(conn: psycopg.Connection) -> tuple[float, int, list[dict[str, Any]]]:
    """Operator-tunable ceiling from app_settings, falling back to the baked
    defaults so a settings hiccup can never REMOVE the guard.

    Also returns the operator's release valve (`overrides`): see
    `_delist_override_permits`. The valve lives in the SAME setting as the cap
    so there is one knob to read, one to audit, and no second mechanism that
    can drift out of step with the first.
    """
    fraction = float(_DELIST_CAP_DEFAULTS["fraction"])
    min_rows = int(_DELIST_CAP_DEFAULTS["min_rows"])
    overrides: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'delist_flip_cap'")
            row = cur.fetchone()
        if row and isinstance(row[0], dict):
            fraction = float(row[0].get("fraction", fraction))
            min_rows = int(row[0].get("min_rows", min_rows))
            raw = row[0].get("overrides")
            if isinstance(raw, list):
                overrides = [o for o in raw if isinstance(o, dict)]
    except Exception as exc:  # noqa: BLE001 - a broken knob must not disarm the cap
        LOG.warning("delist cap: falling back to defaults (%s)", exc)
        return (float(_DELIST_CAP_DEFAULTS["fraction"]),
                int(_DELIST_CAP_DEFAULTS["min_rows"]), [])
    return fraction, min_rows, overrides


def _delist_override_permits(
    overrides: list[dict[str, Any]],
    *,
    source: str,
    category_main: str | None,
    category_type: str | None,
    subtype: str | None,
    candidates: int,
) -> dict[str, Any] | None:
    """The operator's release valve for a breaker that has latched.

    A refusal does not clear itself: the unswept rows keep aging, so the next
    sweep proposes MORE and is refused again. That is correct breaker
    behaviour -- an auto-reclosing breaker defeats the purpose -- but a breaker
    with no reset is a permanent stall, so the operator needs a way to say "I
    verified this one, let it through" that does not mean "raise the ceiling
    everywhere".

    An override is therefore SCOPED (it names the source, and may name the
    category), BOUNDED (`max_rows` is a hard row count, so even a wildcard
    override cannot authorise an unbounded flip), and EXPIRING (`until` is
    required and must still be in the future). Anything missing, unparseable or
    already expired is IGNORED -- the valve fails shut, like the cap it
    releases.
    """
    for o in overrides:
        try:
            if o.get("source") != source:
                continue
            for key, actual in (("category_main", category_main),
                                ("category_type", category_type),
                                ("subtype", subtype)):
                want = o.get(key)
                if want is not None and want != actual:
                    break
            else:
                until = datetime.fromisoformat(str(o["until"]).replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until <= datetime.now(timezone.utc):
                    continue
                if candidates <= int(o["max_rows"]):
                    return o
        except Exception as exc:  # noqa: BLE001 - a malformed valve stays shut
            LOG.warning("delist cap: ignoring malformed override %r (%s)", o, exc)
    return None


def _delist_flip_allowed(
    conn: psycopg.Connection,
    *,
    source: str,
    category_main: str | None,
    category_type: str | None,
    subtype: str | None,
    candidates: int,
    active_rows: int,
) -> bool:
    """May a sweep of this size proceed?

    Refusing is safe in the direction that matters: an unswept stale row is
    visible, queryable and self-heals the moment the listing is seen again
    (touch_listings), while a wrongly-delisted live listing is invisible to
    Browse, the watchdog and every estimate, and nothing re-surfaces it.

    The refusal is RECORDED, not just logged: an Actions log expires, and the
    lesson of this sprint is that a signal nothing can query is a signal nobody
    receives.
    """
    fraction, min_rows, overrides = _delist_cap(conn)
    if active_rows < min_rows:
        # The cap polices catastrophes, not small categories. The small ones are
        # the churny ones: sreality pozemek/drazba turns over 6-39% of its ~600
        # live rows per sweep because auctions end on a date.
        return True
    cap = max(1, int(active_rows * fraction))
    if candidates <= cap:
        return True
    permit = _delist_override_permits(
        overrides, source=source, category_main=category_main,
        category_type=category_type, subtype=subtype, candidates=candidates,
    )
    if permit is not None:
        LOG.warning(
            "DELIST OVERRIDE source=%s cm=%s ct=%s subtype=%s candidates=%d cap=%d "
            "-- operator override in force until %s (%s)",
            source, category_main, category_type, subtype, candidates, cap,
            permit.get("until"), permit.get("reason", "no reason given"),
        )
        return True
    LOG.error(
        "DELIST REFUSED source=%s cm=%s ct=%s subtype=%s candidates=%d active=%d cap=%d "
        "-- a sweep this large is a claim the market moved overnight. Verify by FETCHING the "
        "listings, then release THIS scope with a bounded, expiring entry in "
        "app_settings.delist_flip_cap.overrides; do not raise the ceiling for every portal",
        source, category_main, category_type, subtype, candidates, active_rows, cap,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delist_flip_refusals
                    (source, category_main, category_type, subtype,
                     candidates, active_rows, cap)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (source, category_main, category_type, subtype,
                 candidates, active_rows, cap),
            )
    except Exception as exc:  # noqa: BLE001 - never let bookkeeping undo the refusal
        LOG.warning("delist cap: could not record the refusal: %s", exc)
    return False


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

    RETIRED with `mark_inactive_native` / `mark_inactive_agenda`: no production
    caller since 2026-09-07 — index absence only nominates a page check
    (`portal_runner._queue_presence_checks`, rule #3); only tests call these.

    Scoped to (source, category_main, category_type) so a per-category index
    walk only flips its own slice. Without the category scope, scraping rentals
    would clobber sales `is_active`; without the source scope, a sreality walk
    would sweep other portals' rows (which carry the same canon categories but
    are never in sreality's seen_ids) — see architectural rule #15.

    `min_unseen_hours` additionally restricts the flip to rows whose
    last_seen_at is older than that many hours (the retired staleness rail).
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
    stale_filter = (
        "\n                  AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    count_params: list[Any] = [ids]
    if min_unseen_hours is not None:
        count_params.append(min_unseen_hours)
    count_params += [source, category_main, category_type]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (
                WHERE sreality_id <> ALL(%s){stale_filter}) AS candidates,
              count(*) AS active_rows
            FROM listings
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s
            """,
            tuple(count_params),
        )
        candidates, active_rows = cur.fetchone()
        if not _delist_flip_allowed(
            conn, source=source, category_main=category_main,
            category_type=category_type, subtype=None,
            candidates=int(candidates), active_rows=int(active_rows),
        ):
            return 0
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
    sreality's 'page does not exist' body) — the page-verified flip rule #3
    relies on (the index-absence sweep in `mark_inactive` is retired).
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
    # The cap's count runs the SAME predicate, but with the natives array inside
    # a FILTER, so its parameters bind in a different order.
    stale_filter = (
        "\n                  AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    count_params: list[Any] = [natives]
    if min_unseen_hours is not None:
        count_params.append(min_unseen_hours)
    count_params += [source, category_main, category_type]
    if scope_subtype:
        count_params.append(subtype)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (
                WHERE source_id_native <> ALL(%s){stale_filter}) AS candidates,
              count(*) AS active_rows
            FROM listings
            WHERE is_active = true
              AND source = %s
              AND category_main = %s
              AND category_type = %s{sub_clause}
            """,
            tuple(count_params),
        )
        candidates, active_rows = cur.fetchone()
        if not _delist_flip_allowed(
            conn, source=source, category_main=category_main,
            category_type=category_type, subtype=subtype if scope_subtype else None,
            candidates=int(candidates), active_rows=int(active_rows),
        ):
            return 0
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
    stale_filter = (
        "\n                  AND last_seen_at < now() - make_interval(hours => %s)"
        if min_unseen_hours is not None else ""
    )
    count_params: list[Any] = [natives]
    if min_unseen_hours is not None:
        count_params.append(min_unseen_hours)
    count_params += [source, category_type]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (
                WHERE source_id_native <> ALL(%s){stale_filter}) AS candidates,
              count(*) AS active_rows
            FROM listings
            WHERE is_active = true
              AND source = %s
              AND category_type = %s
            """,
            tuple(count_params),
        )
        candidates, active_rows = cur.fetchone()
        if not _delist_flip_allowed(
            conn, source=source, category_main=None,
            category_type=category_type, subtype=None,
            candidates=int(candidates), active_rows=int(active_rows),
        ):
            return 0
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


INDEX_SUMMARY_CHUNK = 5000


def index_summary_native(
    conn: psycopg.Connection,
    source: str,
    native_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Fetch (surrogate id, sreality_id, price_czk, last_seen_at) keyed by
    source_id_native for one portal.

    The native-id analogue of `index_summary` (which keys on the bigint PK that
    sreality's index already carries). The index walk's sighting diff
    (`portal_runner.reconcile_sightings`) looks rows up by (source,
    source_id_native) to decide price-change refetch — and to resolve the
    surrogate `id` set for touch_listings_by_id. The `"id"` value is the identity
    to carry forward; `"sreality_id"` is legacy (NULL for post-Gate-2 rows).
    Ids are deduped and looked up INDEX_SUMMARY_CHUNK at a time, so one huge
    category (ceskereality, ~21k) never becomes one statement.
    """
    ids = list(dict.fromkeys(str(n) for n in native_ids))
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for start in range(0, len(ids), INDEX_SUMMARY_CHUNK):
            cur.execute(
                """
                SELECT source_id_native, id, sreality_id, price_czk, last_seen_at
                FROM listings
                WHERE source = %s AND source_id_native = ANY(%s)
                """,
                (source, ids[start : start + INDEX_SUMMARY_CHUNK]),
            )
            out.update(
                (native, {"id": lid, "sreality_id": pk, "price_czk": price, "last_seen_at": ls})
                for native, lid, pk, price, ls in cur.fetchall()
            )
    return out


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


# Provenance columns ride the SAME write as the bytes (migration 496), but only
# when the caller measured them — an empty `{extra}` reproduces the pre-496
# statement byte-for-byte, so no existing caller's SQL changes.
_MARK_IMAGE_STORED_SQL = """
            UPDATE images
            SET storage_path = %s,
                phash = COALESCE(%s, phash),{extra}
                last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """


def mark_image_stored(
    conn: psycopg.Connection,
    image_id: int,
    storage_path: str,
    phash: int | None = None,
    *,
    rendition: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """`phash` rides the same statement as `storage_path` (computed inline on
    the bytes already in hand — Wave C-4); None preserves any existing hash so
    the hourly compute_image_phash backfill stays the backstop. `rendition` /
    `width` / `height` (migration 496) are written only when measured, so a
    caller that doesn't decode the bytes leaves them untouched."""
    extra = ""
    params: list[Any] = [storage_path, phash]
    if rendition is not None:
        extra += "\n                rendition = %s,"
        params.append(rendition)
    if width is not None:
        extra += "\n                stored_width = %s,"
        params.append(width)
    if height is not None:
        extra += "\n                stored_height = %s,"
        params.append(height)
    params.append(image_id)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_MARK_IMAGE_STORED_SQL.format(extra=extra), tuple(params))


_INVALIDATE_DERIVED_SIGNALS_SQL = """
    UPDATE images
    SET phash = NULL,
        clip_tagged_at = NULL
    WHERE id = ANY(%s::bigint[])
    """

# The DINOv3 table is created under a pg_available_extensions guard (migration
# 480), so it can legitimately be absent; probe rather than assume.
_DINOV3_TABLE_PROBE_SQL = "SELECT to_regclass('public.image_dinov3_embeddings') IS NOT NULL"

_DROP_DINOV3_EMBEDDINGS_SQL = """
    DELETE FROM image_dinov3_embeddings
    WHERE image_id = ANY(%s::bigint[])
    """


def invalidate_derived_signals(
    conn: psycopg.Connection,
    image_ids: Sequence[int],
    *,
    drop_dinov3: bool = False,
) -> int:
    """The ONE chokepoint every byte-changing job calls after replacing an R2 object.

    Nulling `images.phash` and `images.clip_tagged_at` is not a deletion of data
    but a re-arming of two producers: both columns ARE the selection predicates of
    `scripts/compute_image_phash.py` (`phash IS NULL`) and
    `scripts/clip_tag_backfill.py` (`clip_tagged_at IS NULL`), so the hourly lanes
    recompute the stale signals on their own with no separate backlog to manage.
    `drop_dinov3` is opt-in and default OFF for the mirror-image reason: DINOv3's
    refill lane (`scripts/dinov3_embed_backfill.py`) is an anti-join dispatched by
    hand on a GPU, so deleting vectors creates a backlog nothing scheduled will
    drain. NEVER touched, by anything, ever: `image_tag_labels` (and its
    `in_training` flag), `tag_exam_*`, `tag_review_samples`, `tag_label_notes`,
    `tag_candidates`, `image_tag_annotations`, `phash_pair_notes`,
    `image_training_examples`, `image_border_cases`, `dedup_sim.*`,
    `image_tag_scores`, `image_room_classifications`, `listing_image_comparisons`,
    `image_clip_tags` and `image_clip_embeddings` — the last two are UPSERTED in
    place by the CLIP lane (never deleted) and are joined by
    `toolkit/tag_candidates.py` to build its centroids, so a delete here would
    silently degrade recall instead of merely costing a recompute.
    """
    ids = list(image_ids)
    if not ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_INVALIDATE_DERIVED_SIGNALS_SQL, (ids,))
        updated = cur.rowcount or 0
        if drop_dinov3:
            cur.execute(_DINOV3_TABLE_PROBE_SQL)
            row = cur.fetchone()
            if row and row[0]:
                cur.execute(_DROP_DINOV3_EMBEDDINGS_SQL, (ids,))
    return updated


# `phash = %s` is a PLAIN assignment, never COALESCE: the old hash describes bytes
# that no longer exist, so a failed inline hash must leave NULL for the phash lane
# to pick up rather than resurrect a stale value. `storage_path` and
# `download_attempts` are deliberately absent — the key is reused, not recomputed,
# and a re-master is not a download attempt.
_MARK_IMAGE_REMASTERED_SQL = """
    UPDATE images
    SET rendition = %s,
        stored_width = %s,
        stored_height = %s,
        phash = %s,
        last_download_attempt_at = now(),
        sreality_url = COALESCE(%s, sreality_url)
    WHERE id = %s
    """


def mark_image_remastered(
    conn: psycopg.Connection,
    image_id: int,
    *,
    rendition: str,
    width: int | None,
    height: int | None,
    phash: int | None,
    sreality_url: str | None = None,
) -> None:
    """Stamp provenance on a row whose R2 object was overwritten in place (mig 496).

    Invalidation and the stamp commit TOGETHER: the outer `conn.transaction()`
    demotes `invalidate_derived_signals`'s own block to a savepoint, so a crash
    between them can't leave the row's signals nulled while `rendition` stays
    NULL (which would keep it in the pending index and re-download it).
    """
    with conn.transaction():
        invalidate_derived_signals(conn, [image_id])
        with conn.cursor() as cur:
            cur.execute(
                _MARK_IMAGE_REMASTERED_SQL,
                (rendition, width, height, phash, sreality_url, image_id),
            )


# The re-master lane's two non-success terminals. Both stamp ONLY the attempt
# clock — `storage_path`, `download_attempts` and `phash` describe bytes that are
# still in the bucket and still valid, and a re-master that fetched nothing has
# not replaced them. The terminal one also claims the row's rendition: the stored
# object IS the legacy crop, the source for anything better is gone, and saying so
# retires the row from the pending set instead of re-fetching a dead URL forever.
_MARK_IMAGE_REMASTER_TERMINAL_SQL = """
    UPDATE images
    SET rendition = 'sreality-749-crop',
        last_download_attempt_at = now()
    WHERE id = %s
    """

_MARK_IMAGE_REMASTER_DEFERRED_SQL = """
    UPDATE images
    SET last_download_attempt_at = now()
    WHERE id = %s
    """


def mark_image_remaster_terminal(conn: psycopg.Connection, image_id: int) -> None:
    """The stored crop is all there will ever be — retire the row from the queue."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_MARK_IMAGE_REMASTER_TERMINAL_SQL, (image_id,))


def mark_image_remaster_deferred(conn: psycopg.Connection, image_id: int) -> None:
    """A transient failure: leave `rendition` NULL so the next tick re-selects it."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_MARK_IMAGE_REMASTER_DEFERRED_SQL, (image_id,))


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
    LONGEST scrape job timeout so a still-running walk is never finalized — the
    binding one is idnes's index walk at 240 minutes, and bazos's is now 130 (its
    22-scope walk is ~85-90 min), so this 90-minute default is only safe for the
    lanes that finish inside it. The API's boot sweep passes an explicit cutoff
    above them all (api/main.py): falsely stamping ended_at on an in-flight walk
    makes verify_pipeline read it as a truncated run (by_category is NOT NULL
    DEFAULT '[]', so it cannot be filtered out) and leaves a later crash invisible,
    since _record_run_crash never clears an ended_at the sweep already wrote.
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


# --- the index-slice ledger (migration 454) ----------------------------------
#
# An index walk starts at the first category's first page every time. When the
# budget runs out the next run starts from the same place, so a catalogue bigger
# than one budget does not get walked slowly -- the same HEAD gets walked over
# and over while the tail is never reached. These two functions are the memory
# that turns that into monotonic progress: record what each slice reached, then
# order the next run by what is stalest.

SLICE_OUTCOME_POSITIVE = "exhausted"


def record_index_slice(
    conn: psycopg.Connection,
    *,
    source: str,
    category_main: str,
    category_type: str,
    slice_key: str,
    outcome: str,
    declared_total: int | None,
    collected: int,
    pages: int,
) -> None:
    """Latest-wins upsert of one slice's outcome. Best-effort: bookkeeping must
    never break a walk, and a walk that fails to record simply looks stale next
    run -- which errs toward walking it again, the safe direction."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portal_index_slices
                    (source, category_main, category_type, slice_key, walked_at,
                     outcome, declared_total, collected, pages)
                VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s)
                ON CONFLICT (source, category_main, category_type, slice_key)
                DO UPDATE SET walked_at = now(),
                              outcome = EXCLUDED.outcome,
                              declared_total = EXCLUDED.declared_total,
                              collected = EXCLUDED.collected,
                              pages = EXCLUDED.pages
                """,
                (source, category_main, category_type, slice_key, outcome,
                 declared_total, collected, pages),
            )
    except Exception as exc:  # noqa: BLE001 - never let the ledger break the walk
        LOG.warning("slice ledger: could not record %s/%s/%s/%s: %s",
                    source, category_main, category_type, slice_key, exc)


def slice_staleness(
    conn: psycopg.Connection, source: str,
) -> dict[tuple[str, str, str], float]:
    """Hours since each slice of this source was last walked.

    A slice with no row is ABSENT from the result, and callers must read that as
    "never walked, walk it first" -- not as "fresh". Returning 0.0 for an unknown
    slice would sort the never-walked ones LAST, which is precisely the starvation
    this ledger exists to end.
    """
    out: dict[tuple[str, str, str], float] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category_main, category_type, slice_key,
                       extract(epoch FROM (now() - walked_at)) / 3600.0
                  FROM portal_index_slices
                 WHERE source = %s
                """,
                (source,),
            )
            for cm, ct, key, hours in cur.fetchall():
                out[(cm, ct, key)] = float(hours)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger means "walk everything"
        LOG.warning("slice ledger: unreadable for %s (%s); treating all slices "
                    "as never walked", source, exc)
        return {}
    return out


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


# --- Phase 2: needs-detail queue --------------------------------------------
#
# The index-walk enqueues new / price-changed ids into listing_detail_queue;
# the detail-drain claims a bounded slice, fetches, and writes them in batches
# via scraper/listing_write.py. The queue is the "what to fetch" signal;
# listing_fetch_failures stays the Health-visible give-up ledger.

# Two service classes, not one ranking. ACQUISITION (QUEUE_PRIORITY_NEW) is a
# listing we have never fetched: latency-critical, and bounded by how fast the
# market posts adverts. REFRESH (everything else) is a listing we already hold:
# throughput work, and effectively unbounded because a walk re-enqueues on every
# pass. Ranking them on one axis and always serving the top made refresh starve
# acquisition outright — 15,064 listings discovered and never fetched, sreality
# nine days at zero (2026-08-17). claim_detail_batch now reserves a share of
# every batch for acquisition instead; within refresh the old order still holds.
QUEUE_PRIORITY_NEW = 0
QUEUE_PRIORITY_CHANGED = 1
QUEUE_PRIORITY_FAILURE = 2
# Presence checks: the walk did not see an active row, so the drain visits its
# page to learn whether it is gone. Served AFTER everything else (the claim order
# is priority DESC, and 0 is "new"), so a backlog of checks can never delay a
# brand-new listing. Negative on purpose: smallint, no CHECK, and GREATEST() on
# re-enqueue means a row already queued as new keeps its place. -2, not -1:
# -1 was the location-data refetch lane (deleted 2026-09-11) and the gap is kept
# so a presence check can never inherit a slot something else sorted above.
QUEUE_PRIORITY_VERIFY = -2
# How many given-up queue rows a walk re-arms per source (see
# enqueue_presence_checks): 50 x 5 attempts is a bounded retry budget per walk.
PRESENCE_REARM_PER_WALK = 50
# Share of each claim batch reserved for presence checks (see claim_detail_batch).
QUEUE_VERIFY_RESERVE = 0.2

# Fraction of each claim reserved for acquisition. Unused reserve backfills to
# refresh (the refresh limit is computed from the rows acquisition actually
# took), so a quiet market costs no refresh throughput.
QUEUE_ACQUISITION_RESERVE = 0.5

_QUEUE_ENQUEUE_CHUNK = 1000


def claimable_counts(conn: psycopg.Connection, source: str | None = None) -> dict[str, int]:
    """Queue rows a drain could claim right now, per source (every source when None)."""
    with conn.cursor() as cur:
        if source is None:
            cur.execute(
                "SELECT source, count(*) FROM listing_detail_queue "
                "WHERE claimed_at IS NULL AND given_up = false GROUP BY source"
            )
        else:
            cur.execute(
                "SELECT source, count(*) FROM listing_detail_queue "
                "WHERE source = %s AND claimed_at IS NULL AND given_up = false GROUP BY source",
                (source,),
            )
        return {s: int(n) for s, n in cur.fetchall()}


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


# --- W8: a page that carried no location gets a second look -----------------
#
# ceskereality and realitymix GEOCODE AN AD AFTER IT IS PUBLISHED. Measured
# 2026-09-14: of the audit page's active "no data" rows, 56 of 63 ceskereality
# and 56 of 112 realitymix listings were fetched within TWO MINUTES of first
# sighting and never fetched again — our one detail fetch read the page while
# its location fields were still empty, and the index card never changed, so
# nothing ever re-read it. Checked live the same day, ceskereality 3876635 now
# carries "Zlín, ulice Mlýnská" and coordinates. The claim lane mines PAGES, so
# the fix is a second fetch, not a contract change.
#
# The audit relation IS the list — `location_pin_audit_mv` state='unresolved' +
# quality='active_no_claims' is exactly "live, the resolver has spoken, and no
# admissible evidence was ever found" — so this asks it rather than
# re-deriving the set. `listings` is joined by primary key for the row's LIVE
# source_url / price / is_active, because the matview is an hourly snapshot and
# a re-fetch must not be aimed by a stale URL.
_LOCATION_REFETCH_AUDIT_MV = "location_pin_audit_mv"

# "Old enough to be worth another look." A listing fetched minutes ago would
# only be re-read at the same empty moment; six hours is well past the portals'
# geocoding lag and it means a row discovered today gets its second look on
# tomorrow's tick rather than in a fortnight.
_LOCATION_REFETCH_CANDIDATES_SQL = """
WITH audit AS (
    SELECT a.listing_id
    FROM location_pin_audit_mv a
    WHERE a.state = 'unresolved'
      AND a.is_active
      AND a.quality = 'active_no_claims'
),
cand AS (
    SELECT l.source,
           l.source_id_native,
           l.source_url,
           l.price_czk,
           GREATEST(COALESCE(pg.fetched_at, '-infinity'::timestamptz),
                    COALESCE(pl.observed_at, '-infinity'::timestamptz)) AS last_fetch_at
    FROM audit a
    JOIN listings l ON l.id = a.listing_id
    LEFT JOIN LATERAL (
        SELECT max(p.fetched_at) AS fetched_at
        FROM portal_raw_pages p
        WHERE p.source = l.source AND p.source_id_native = l.source_id_native
    ) pg ON true
    LEFT JOIN LATERAL (
        SELECT max(p.last_observed_at) AS observed_at
        FROM portal_raw_payloads p
        WHERE p.source = l.source AND p.source_id_native = l.source_id_native
    ) pl ON true
    WHERE l.is_active
      AND l.source_id_native IS NOT NULL
),
old AS (
    SELECT c.*,
           row_number() OVER (PARTITION BY c.source
                              ORDER BY c.last_fetch_at, c.source_id_native) AS rn,
           count(*) OVER (PARTITION BY c.source) AS source_total
    FROM cand c
    WHERE c.last_fetch_at < now() - make_interval(secs => %(min_age)s::double precision)
)
SELECT source, source_id_native, source_url, price_czk, last_fetch_at, source_total
FROM old
WHERE rn <= %(cap)s::int
ORDER BY source, rn
"""


def location_refetch_candidates(
    conn: psycopg.Connection,
    *,
    min_age_seconds: int,
    cap_per_source: int,
) -> list[dict[str, Any]] | None:
    """Listings whose page carried no location and whose page is old enough to
    re-read, oldest-fetched first, at most `cap_per_source` per portal.

    None (not an empty list) when the audit matview is absent: a caller must be
    able to tell "nothing to do" from "this database cannot answer", and on a
    branch database without the location relations the lane skips instead of
    raising every tick. `source_total` carries the UNCAPPED per-portal backlog,
    so a capped tick still says how much is left for tomorrow.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"public.{_LOCATION_REFETCH_AUDIT_MV}",),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        cur.execute(
            _LOCATION_REFETCH_CANDIDATES_SQL,
            {"min_age": float(min_age_seconds), "cap": int(cap_per_source)},
        )
        rows = cur.fetchall()
    return [
        {
            "source": r[0],
            "native_id": str(r[1]),
            "detail_ref": detail_ref(r[0], r[2]),
            "price_czk": r[3],
            "last_fetch_at": r[4],
            "source_total": int(r[5]),
        }
        for r in rows
    ]


def enqueue_location_refetch(
    conn: psycopg.Connection,
    source: str,
    entries: Sequence[tuple[str, str | None, int | None]],
) -> int:
    """Queue (native_id, detail_ref, index_price_czk) rows for a second look at
    QUEUE_PRIORITY_VERIFY. Returns the number of rows inserted or re-armed.

    Not `enqueue_detail`: that one raises a queued row's priority (GREATEST) and
    leaves `given_up` alone, which is wrong in both directions here. This lane
    must never promote a row above the presence checks it shares a class with
    (LEAST), and the rows it most wants are precisely the ones the drain gave up
    on after five failures — invisible to every claim until something re-arms
    them, exactly as `enqueue_presence_checks` re-arms its own (rule #5:
    tracked, not dropped). `enqueued_at` is left alone so the oldest wait still
    sorts first inside the verify class; a claimed row is never disturbed.
    """
    rows = list(entries)
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), _QUEUE_ENQUEUE_CHUNK):
            chunk = rows[start : start + _QUEUE_ENQUEUE_CHUNK]
            cur.execute(
                """
                INSERT INTO listing_detail_queue
                    (source, native_id, detail_ref, index_price_czk, priority,
                     sreality_id)
                SELECT %(source)s, u.nid, u.ref, u.price, %(verify)s,
                       CASE WHEN %(source)s = 'sreality'
                            THEN u.nid::bigint ELSE NULL END
                FROM unnest(
                    %(nids)s::text[], %(refs)s::text[], %(prices)s::int[]
                ) AS u(nid, ref, price)
                ON CONFLICT (source, native_id) DO UPDATE SET
                    detail_ref      = EXCLUDED.detail_ref,
                    index_price_czk = EXCLUDED.index_price_czk,
                    priority        = LEAST(listing_detail_queue.priority,
                                            EXCLUDED.priority),
                    given_up        = false,
                    attempts        = 0
                WHERE listing_detail_queue.claimed_at IS NULL
                """,
                {
                    "source": source,
                    "verify": QUEUE_PRIORITY_VERIFY,
                    "nids": [str(nid) for nid, _, _ in chunk],
                    "refs": [ref for _, ref, _ in chunk],
                    "prices": [sane_price_czk(p) for _, _, p in chunk],
                },
            )
            total += cur.rowcount or 0
    return total


def claim_detail_batch(
    conn: psycopg.Connection,
    source: str,
    limit: int,
    acquisition_reserve: float | None = None,
) -> list[tuple[str, str | None, int | None, int, datetime | None]]:
    """Atomically claim up to `limit` available rows for `source`. Returns
    (native_id, detail_ref, index_price_czk, discovery_seq, enqueued_at) —
    discovery_seq is the row's enqueue-time sequence value (see migration 368) and
    enqueued_at is when the walk first saw the id (migration 444); both are carried
    through so the drain can stamp listings.discovery_seq / listings.discovered_at
    at write time, independent of claim/fetch/write order.

    The batch is composed from two classes rather than taken off one ranking:
    up to ceil(limit * QUEUE_ACQUISITION_RESERVE) never-fetched rows
    (QUEUE_PRIORITY_NEW, oldest first), then refresh rows (priority DESC,
    enqueued_at) for whatever the reserve did not use. This is what makes
    starvation structurally impossible: refresh inflow is unbounded and can
    exceed drain throughput indefinitely, so any strict ordering that puts it
    ahead of acquisition eventually stops ingesting new listings entirely.
    Backfill is one-directional by design — acquisition cannot take refresh's
    share, because the market bounds it anyway.

    FOR UPDATE SKIP LOCKED makes concurrent drains safe; both classes are locked
    in the same order by every caller, so concurrent drains cannot deadlock. The
    claim is committed immediately (claimed_at set) so a crashed drain's rows are
    recovered by reclaim_stale_claims rather than lost.
    """
    if limit <= 0:
        return []
    reserve = (
        QUEUE_ACQUISITION_RESERVE if acquisition_reserve is None else acquisition_reserve
    )
    acq_limit = 0 if reserve <= 0 else min(limit, max(1, math.ceil(limit * reserve)))
    # Presence checks (rule #3, 2026-09-07) get a reserved share too, for the
    # same reason acquisition does: they sit at the bottom of the refresh class
    # by design, and refresh inflow is unbounded, so with no reserve a busy
    # queue would never close a listing again. Unused reserve backfills into
    # `rest` inside SQL, and `rest` still takes remaining checks at its tail.
    ver_limit = min(limit, max(1, math.ceil(limit * QUEUE_VERIFY_RESERVE)))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            WITH acq AS (
                SELECT source, native_id FROM listing_detail_queue
                WHERE source = %(source)s AND claimed_at IS NULL AND given_up = false
                  AND priority = %(new_priority)s
                ORDER BY enqueued_at
                LIMIT %(acq_limit)s
                FOR UPDATE SKIP LOCKED
            ),
            ver AS (
                SELECT source, native_id FROM listing_detail_queue
                WHERE source = %(source)s AND claimed_at IS NULL AND given_up = false
                  AND priority = %(verify_priority)s
                ORDER BY enqueued_at
                LIMIT LEAST(%(ver_limit)s, GREATEST(%(limit)s - (SELECT count(*) FROM acq), 0))
                FOR UPDATE SKIP LOCKED
            ),
            rest AS (
                SELECT source, native_id FROM listing_detail_queue
                WHERE source = %(source)s AND claimed_at IS NULL AND given_up = false
                  AND priority <> %(new_priority)s
                  AND (source, native_id) NOT IN (SELECT source, native_id FROM ver)
                ORDER BY priority DESC, enqueued_at
                LIMIT GREATEST(%(limit)s - (SELECT count(*) FROM acq) - (SELECT count(*) FROM ver), 0)
                FOR UPDATE SKIP LOCKED
            ),
            c AS (
                SELECT source, native_id FROM acq
                UNION ALL
                SELECT source, native_id FROM ver
                UNION ALL
                SELECT source, native_id FROM rest
            )
            UPDATE listing_detail_queue q SET claimed_at = now()
            FROM c WHERE q.source = c.source AND q.native_id = c.native_id
            RETURNING q.native_id, q.detail_ref, q.index_price_czk, q.discovery_seq,
                      q.enqueued_at
            """,
            {
                "source": source,
                "new_priority": QUEUE_PRIORITY_NEW,
                "verify_priority": QUEUE_PRIORITY_VERIFY,
                "acq_limit": acq_limit,
                "ver_limit": ver_limit,
                "limit": limit,
            },
        )
        return [
            (nid, ref, price, dseq, enq)
            for nid, ref, price, dseq, enq in cur.fetchall()
        ]


def queue_priorities(conn: psycopg.Connection, source: str, native_ids: Sequence[str]) -> dict[str, int]:
    """priority per queued native_id (rows still hold their queue row while
    claimed). Best-effort: the drain's gone-rate breaker treats an unknown
    priority as an ingest row, the conservative direction."""
    ids = [str(n) for n in native_ids]
    if not ids:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT native_id, priority FROM listing_detail_queue "
                "WHERE source = %s AND native_id = ANY(%s::text[])",
                (source, ids),
            )
            return {str(n): int(p) for n, p in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 - observability must not fail the drain
        LOG.warning("queue_priorities: %s", exc)
        return {}


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


# `detail` is the ONLY page_kind the payload archive stores, and the invariant is
# about GRAIN, not about the word "index": a detail body is ONE listing fetched when
# that listing is enqueued, while every other `location_page_kind` label — index, map,
# gazetteer, snapshot, archive, none — is a whole-SURFACE artefact refetched on a walk
# cadence (sreality walks its index 24x/day), so archiving one would store the same
# re-ordered page over and over for no claim anyone can mine from it.
DETAIL_PAGE_KIND = "detail"
INDEX_PAGE_KIND = "index"


def _payload_archive_enabled(source: str, page_kind: str) -> bool:
    """May this body be appended to the payload archive? Detail bodies, always.

    It used to be three gates — a per-portal `payload_dual_write` limit, a second
    `payload_index_archive` limit for every other surface, and a "has this surface been
    weighed" check against a frozen measurement corpus. All three are gone (rule 25: the
    stored page body is the hourly lane's second substrate, so archiving it is not an
    opt-in experiment any more). What is left is the grain rule: a detail body is one
    listing's page and is mined for claims; every other page_kind is a surface artefact
    that re-orders on every walk.
    """
    return page_kind == DETAIL_PAGE_KIND


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
    """Never-raising dual-write into the payload archive.

    A `detail` body is ALWAYS archived; EVERY other page_kind — index, map,
    gazetteer, snapshot, archive, none — is never, because all of them are
    surface-grain artefacts refetched on a walk cadence rather than one
    listing's body (`_payload_archive_enabled`). It was three flags until
    2026-09-11; rule 25 made the stored detail body the claim lane's second
    substrate, so archiving it is no longer an opt-in experiment.

    `portal_raw_pages` is latest-wins, so the body a claim's evidence span
    points into is gone the moment the page is refetched;
    `location_data.payloads.append_payload` is the append-on-change store that
    ends that, and this is the ONLY form scrapers should call it in. Every
    failure — normaliser, append — warns and returns: the archive is downstream
    of the scrape and must never be able to stop it.

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
        if not _payload_archive_enabled(source, page_kind):
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
) -> int | None:
    """Latest-wins upsert of one fetched HTML page into portal_raw_pages.

    Decouples fetch from parse so a page can be re-parsed without re-fetching.
    Returns the staging row id; a re-fetch overwrites the HTML and clears the
    previous parse state. With `refresh_after_hours` set, an existing row
    younger than that is left untouched and None is returned — the write-cost
    guard for index-page archiving.

    The payload dual-write hangs off the END of this function, so ONE edit
    covers every HTML writer that stages through here — the seven detail writers
    and the three index archivers — with no per-portal branch (rule #21). It does
    NOT cover the two portals that stage no body (sreality's estate JSON,
    bezrealitky's advert), which call `append_payload_if_enabled` directly. It is
    deliberately NOT gated on the staging row: a
    `refresh_after_hours` skip means portal_raw_pages already holds a body young
    enough, which says nothing about whether the CONTENT moved — and an
    append-on-change archive that drops a genuinely changed body is the one
    failure it cannot recover from. An unchanged one costs a no-op DO UPDATE.
    """
    encoded: bytes | None = None

    def _body() -> bytes:
        # Memoised: the archive asks for the bytes at most once per call.
        nonlocal encoded
        if encoded is None:
            encoded = html.encode("utf-8")
        return encoded

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
