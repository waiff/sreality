"""The one listing content write (rule 2): a fetched payload, its media, snapshot-on-change, failure clear and dirty marks — one transaction."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from scraper import hashing, media
from scraper.db import (
    LISTING_COLUMNS, _LISTING_COLUMN_PGTYPE, _gate2_null_sreality_id_enabled,
    _listing_update_set_sql, sane_listing_numerics, sane_price_czk,
)
from scraper.scraped_listing import ScrapedListing
from toolkit.broker_sources import (
    BROKER_FINGERPRINT_KEYS, BROKER_FINGERPRINTED_SOURCES, BROKER_SOURCE_NAMES,
)

LOG = logging.getLogger(__name__)

Result = Literal["new", "updated", "unchanged"]


@dataclass(frozen=True)
class ListingWrite:
    """One fetched payload as its adapter parsed it. The digest is computed here, never passed in."""
    source: str
    source_id_native: str
    row: Mapping[str, Any]                 # LISTING_COLUMNS values; an absent key is NULL (or preserved, per contract)
    raw: Mapping[str, Any]                 # stored verbatim in listings.raw_json and the snapshot's raw_json
    hash_doc: InitVar[Mapping[str, Any]]   # what "content" means for this adapter; consumed, not stored
    images: tuple[Mapping[str, Any], ...] = ()   # {"url", "sequence"}
    videos: tuple[Mapping[str, Any], ...] = ()   # {"url", "sequence"}
    sreality_id: int | None = None         # sreality's own id; None = a crawler row (first sight mints per Gate 2)
    discovery_seq: int | None = None
    discovered_at: datetime | None = None
    content_hash: str = field(init=False)

    def __post_init__(self, hash_doc: Mapping[str, Any]) -> None:
        object.__setattr__(self, "content_hash", hashing.digest(hash_doc))


@dataclass(frozen=True)
class WriteOutcome:
    source: str
    source_id_native: str
    listing_id: int          # the surrogate listings.id, new or existing
    result: Result
    snapshot_id: int         # the listing's latest snapshot after the write (appended or pre-existing)
    content_hash: str
    images_inserted: int


@dataclass(frozen=True)
class SnapshotRef:
    id: int
    content_hash: str
    raw_json: Mapping[str, Any]


def from_sreality(
    raw: Mapping[str, Any], row: Mapping[str, Any], images: Iterable[Mapping[str, Any]], *,
    discovery_seq: int | None = None, discovered_at: datetime | None = None,
) -> ListingWrite:
    """sreality's adapter: parse_listing's row + parse_images' rows; hashes the wire payload."""
    sid = int(row["sreality_id"])
    return ListingWrite(
        source="sreality", source_id_native=str(sid), row=dict(row), raw=raw,
        hash_doc=hashing.sreality_hash_doc(dict(raw)), images=tuple(images),
        sreality_id=sid, discovery_seq=discovery_seq, discovered_at=discovered_at)


def from_scraped(
    listing: ScrapedListing, *,
    discovery_seq: int | None = None, discovered_at: datetime | None = None,
) -> ListingWrite:
    """The 8 crawlers' adapter: the contract's columns + its media split; hashes the 28 _HASH_FIELDS."""
    image_rows, video_rows = media.split_media_rows((listing.raw or {}).get("image_urls") or [])
    return ListingWrite(
        source=listing.source, source_id_native=listing.source_id_native,
        row=listing.listing_columns(), raw=listing.raw or {}, hash_doc=listing.hash_doc(),
        images=tuple(image_rows), videos=tuple(video_rows),
        discovery_seq=discovery_seq, discovered_at=discovered_at)


# jsonb_to_recordset keeps every statement's text fixed (only the one jsonb param varies), so
# the session pooler prepares each plan once per source.
_RECORD_SPEC = ", ".join(f"{c} {_LISTING_COLUMN_PGTYPE[c]}" for c in LISTING_COLUMNS)

# THE one "latest snapshot" order. scraped_at alone ties inside one transaction; id breaks it.
_LATEST_ORDER_BY = "ORDER BY s.scraped_at DESC, s.id DESC"


@lru_cache(maxsize=None)
def _upsert_sql(source: str) -> str:
    """ONE fixed text per source: the contract picks the preserve set, the broker registry the pre-read."""
    stored_broker = ("l.raw_json->'broker'" if source in BROKER_FINGERPRINTED_SOURCES
                     else "NULL::jsonb")
    cols = ", ".join(LISTING_COLUMNS)
    j_cols = ", ".join(f"j.{c}" for c in LISTING_COLUMNS)
    # `prior` reads the pre-statement rows: the old is_active (a revival) and, for the
    # fingerprinted sources only, the broker block before raw_json is replaced. `source` is
    # stamped inline (its column default is 'sreality'; the #825 drain wedge). The arbiter is
    # the natural key, so neither sreality_id nor source is ever in the SET clause, and the
    # mint CASE draws a synthetic id only for a first-sight crawler row with Gate 2 off.
    return f"""
    WITH j AS (
        SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS j(
            source_id_native text, sreality_id bigint, {_RECORD_SPEC},
            raw_json jsonb, discovery_seq bigint, discovered_at timestamptz)
    ), prior AS (
        SELECT l.source_id_native, l.is_active, {stored_broker} AS stored_broker
        FROM listings l
        WHERE l.source = %(source)s
          AND l.source_id_native IN (SELECT source_id_native FROM j)
    ), up AS (
        INSERT INTO listings (
            sreality_id, last_seen_at, is_active, {cols},
            source, source_id_native, raw_json, discovery_seq, discovered_at)
        SELECT
            COALESCE(j.sreality_id,
                     CASE WHEN prior.source_id_native IS NULL AND %(mint)s
                          THEN nextval('synthetic_listing_id_seq') END),
            now(), true, {j_cols},
            %(source)s, j.source_id_native, j.raw_json, j.discovery_seq, j.discovered_at
        FROM j LEFT JOIN prior ON prior.source_id_native = j.source_id_native
        ORDER BY j.source_id_native
        ON CONFLICT (source, source_id_native) DO UPDATE SET
          last_seen_at = now(),
          is_active = true,
          inactive_at = NULL,
          {_listing_update_set_sql(source)},
          raw_json = EXCLUDED.raw_json,
          discovery_seq = COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq),
          discovered_at = COALESCE(listings.discovered_at, EXCLUDED.discovered_at)
        RETURNING id, source_id_native, (xmax = 0) AS inserted
    )
    SELECT up.id, up.source_id_native, up.inserted,
           COALESCE(prior.is_active = false, false) AS reactivated,
           prior.stored_broker
    FROM up LEFT JOIN prior ON prior.source_id_native = up.source_id_native
    """


# Refresh a stale CDN path on a not-yet-downloaded image (and clear its error state); the
# storage_path guard never disturbs a stored one. xmax = 0 counts genuine inserts only.
_IMAGES_SQL = """
    INSERT INTO images (sreality_id, listing_id, sreality_url, sequence)
    SELECT l.sreality_id, l.id, j.url, j.sequence
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS j(source_id_native text, url text, sequence integer)
    JOIN listings l ON l.source = %(source)s AND l.source_id_native = j.source_id_native
    ON CONFLICT (listing_id, sequence) DO UPDATE SET
        sreality_url = EXCLUDED.sreality_url,
        download_attempts = 0,
        last_error = NULL,
        unavailable_reason = NULL
    WHERE images.storage_path IS NULL
    RETURNING listing_id, (xmax = 0) AS inserted
"""

_VIDEOS_SQL = """
    INSERT INTO listing_videos (sreality_id, listing_id, source_url, sequence)
    SELECT l.sreality_id, l.id, j.url, j.sequence
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS j(source_id_native text, url text, sequence integer)
    JOIN listings l ON l.source = %(source)s AND l.source_id_native = j.source_id_native
    ON CONFLICT (listing_id, sequence) DO UPDATE SET source_url = EXCLUDED.source_url
    WHERE listing_videos.storage_path IS NULL
"""

_FAILURE_CLEAR_SQL = "DELETE FROM listing_fetch_failures WHERE sreality_id = ANY(%(sids)s::bigint[])"

# Snapshot-on-change. A separate statement from the upsert on purpose: under READ COMMITTED
# it starts after the upsert took the row locks, so it sees a concurrent writer's committed
# snapshot, and statement_timestamp() (not the transaction-start now()) makes per-listing
# scraped_at order follow commit order. raw_json is joined only for changed rows.
_SNAPSHOT_SQL = f"""
    WITH j AS (
        SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb)
            AS j(source_id_native text, price_czk integer, content_hash text)
    ), w AS (
        SELECT l.id AS listing_id, l.sreality_id, j.source_id_native, j.price_czk, j.content_hash,
               latest.id AS latest_id,
               latest.content_hash IS DISTINCT FROM j.content_hash AS changed
        FROM j
        JOIN listings l ON l.source = %(source)s AND l.source_id_native = j.source_id_native
        LEFT JOIN LATERAL (
            SELECT s.id, s.content_hash FROM listing_snapshots s
            WHERE s.listing_id = l.id
            {_LATEST_ORDER_BY}
            LIMIT 1
        ) latest ON true
    ), ins AS (
        INSERT INTO listing_snapshots
            (sreality_id, listing_id, scraped_at, price_czk, content_hash, raw_json)
        SELECT w.sreality_id, w.listing_id, statement_timestamp(), w.price_czk, w.content_hash, l.raw_json
        FROM w JOIN listings l ON l.id = w.listing_id
        WHERE w.changed
        ORDER BY w.listing_id
        RETURNING id, listing_id
    )
    SELECT w.source_id_native, w.listing_id,
           COALESCE(ins.id, w.latest_id) AS snapshot_id,
           ins.id IS NOT NULL AS appended
    FROM w LEFT JOIN ins ON ins.listing_id = w.listing_id
"""

# Rule 20: a written row's property is dirty when the row appended, revived, or sits under a
# property that reads inactive. New rows have no property yet; the straggler attach owns them.
_DIRTY_PROPERTIES_SQL = """
    INSERT INTO dirty_properties (property_id)
    SELECT DISTINCT l.property_id
    FROM listings l
    LEFT JOIN properties p ON p.id = l.property_id
    WHERE l.id = ANY(%(ids)s::bigint[])
      AND l.property_id IS NOT NULL
      AND (l.id = ANY(%(marked)s::bigint[]) OR p.is_active IS NOT TRUE)
    ORDER BY 1
    ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""

_DIRTY_BROKERS_SQL = """
    INSERT INTO dirty_broker_listings (listing_id)
    SELECT DISTINCT u FROM unnest(%(ids)s::bigint[]) AS u
    ORDER BY 1
    ON CONFLICT (listing_id) DO UPDATE SET marked_at = now()
"""

_LATEST_SNAPSHOT_SQL = f"""
    SELECT latest.id, latest.content_hash, latest.raw_json
    FROM listings l
    JOIN LATERAL (
        SELECT s.id, s.content_hash, s.raw_json FROM listing_snapshots s
        WHERE s.listing_id = l.id
        {_LATEST_ORDER_BY}
        LIMIT 1
    ) latest ON true
    WHERE l.source = %(source)s AND l.source_id_native = %(native)s
"""


def _broker_fingerprint(block: Any) -> tuple[str | None, ...]:
    """The attribution-relevant identity of a raw["broker"] block.

    Total by construction — a portal that emits a list, a string or a missing block
    yields the empty fingerprint rather than raising, because this runs inside the
    live write transaction and must never abort an otherwise-valid listing."""
    if not isinstance(block, dict):
        return ()
    return tuple(
        None if block.get(k) is None else str(block[k]).strip()
        for k in BROKER_FINGERPRINT_KEYS
    )


def _coerce_numerics(obj: dict[str, Any]) -> None:
    """Make float column values recordset-safe, in place: non-finite -> NULL, integer -> round().

    json.dumps emits NaN (invalid jsonb, the whole flush fails), and jsonb_to_recordset will not
    cast 2.0 into an integer column the way a per-item float8 bind did (half-even, as round())."""
    for col, pgtype in _LISTING_COLUMN_PGTYPE.items():
        v = obj.get(col)
        if not isinstance(v, float):
            continue
        if not math.isfinite(v):
            LOG.warning("NUMERIC dropped col=%s value=%s (non-finite)", col, v)
            obj[col] = None
        elif pgtype == "integer":
            obj[col] = round(v)


def _media_rows(native: str, rows: Iterable[Mapping[str, Any]], *,
                images_only: bool) -> list[dict[str, Any]]:
    """De-dupe on (native, sequence), first wins (NULL sequences never conflict, all kept)."""
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for r in rows:
        url = r.get("url")
        if not url or (images_only and not media.is_image_url(url)):
            continue
        seq = r.get("sequence")
        if seq is not None:
            if seq in seen:
                continue
            seen.add(seq)
        out.append({"source_id_native": native, "url": url, "sequence": seq})
    return out


def _stage(batch: Mapping[str, ListingWrite]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """The no-DB pre-pass: sanitised listing rows, snapshot rows, image rows, video rows."""
    rows: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    for native, w in batch.items():
        obj: dict[str, Any] = {c: w.row.get(c) for c in LISTING_COLUMNS}
        obj["price_czk"] = sane_price_czk(obj["price_czk"])
        _coerce_numerics(obj)
        sane_listing_numerics(obj)
        obj |= {"source_id_native": native, "sreality_id": w.sreality_id, "raw_json": dict(w.raw),
                "discovery_seq": w.discovery_seq, "discovered_at": w.discovered_at}
        rows.append(obj)
        snaps.append({"source_id_native": native, "price_czk": obj["price_czk"],
                      "content_hash": w.content_hash})
        images += _media_rows(native, w.images, images_only=True)
        videos += _media_rows(native, w.videos, images_only=False)
    return rows, snaps, images, videos


def write_listings(conn: psycopg.Connection, writes: Sequence[ListingWrite]) -> list[WriteOutcome]:
    """THE listing content write (rule 2): one source per call, one transaction, one outcome per listing."""
    if not writes:
        return []
    sources = sorted({w.source for w in writes})
    if len(sources) != 1:
        raise ValueError(f"write_listings writes one source per call, got {sources}")
    source = sources[0]
    batch: dict[str, ListingWrite] = {}
    for w in writes:
        batch[w.source_id_native] = w          # first-occurrence order, last payload wins
    rows, snaps, image_rows, video_rows = _stage(batch)
    sids = sorted({w.sreality_id for w in batch.values() if w.sreality_id is not None})

    with conn.transaction(), conn.cursor() as cur:
        mint = (any(w.sreality_id is None for w in batch.values())
                and not _gate2_null_sreality_id_enabled(conn))
        cur.execute(_upsert_sql(source), {"rows": Jsonb(rows), "source": source, "mint": mint})
        up_by_native = {
            native: (int(lid), bool(inserted), bool(reactivated), stored_broker)
            for lid, native, inserted, reactivated, stored_broker in cur.fetchall()
        }
        images_by_id: Counter[int] = Counter()
        if image_rows:
            cur.execute(_IMAGES_SQL, {"rows": Jsonb(image_rows), "source": source})
            images_by_id.update(int(lid) for lid, inserted in cur.fetchall() if inserted)
        if video_rows:
            cur.execute(_VIDEOS_SQL, {"rows": Jsonb(video_rows), "source": source})
        if sids:
            cur.execute(_FAILURE_CLEAR_SQL, {"sids": sids})
        cur.execute(_SNAPSHOT_SQL, {"rows": Jsonb(snaps), "source": source})
        snap_by_native = {
            native: (int(snapshot_id), bool(appended))
            for native, _lid, snapshot_id, appended in cur.fetchall()
        }
        unresolved = [n for n in batch if n not in up_by_native or n not in snap_by_native]
        if unresolved:
            raise RuntimeError(
                f"listing_write: {len(unresolved)} written row(s) not resolved on "
                f"(source, source_id_native): {unresolved[:20]}")

        ids = sorted(up_by_native[n][0] for n in batch)
        marked = sorted(up_by_native[n][0] for n in batch
                        if snap_by_native[n][1] or up_by_native[n][2])
        cur.execute(_DIRTY_PROPERTIES_SQL, {"ids": ids, "marked": marked})

        broker_ids: set[int] = set()
        if source in BROKER_SOURCE_NAMES:
            broker_ids |= {up_by_native[n][0] for n in batch if snap_by_native[n][1]}
        if source in BROKER_FINGERPRINTED_SOURCES:
            broker_ids |= {
                up_by_native[n][0] for n, w in batch.items()
                if _broker_fingerprint(up_by_native[n][3]) != _broker_fingerprint(w.raw.get("broker"))
            }
        if broker_ids:
            cur.execute(_DIRTY_BROKERS_SQL, {"ids": sorted(broker_ids)})

    outcomes: list[WriteOutcome] = []
    for native, w in batch.items():
        listing_id, inserted, _reactivated, _stored = up_by_native[native]
        snapshot_id, appended = snap_by_native[native]
        result: Result = "new" if inserted else ("updated" if appended else "unchanged")
        outcomes.append(WriteOutcome(
            source=source, source_id_native=native, listing_id=listing_id, result=result,
            snapshot_id=snapshot_id, content_hash=w.content_hash,
            images_inserted=images_by_id.get(listing_id, 0)))
    return outcomes


def tally(outcomes: Iterable[WriteOutcome]) -> dict[str, int]:
    """{new, updated, unchanged, images_discovered}: the DRAIN flush / scrape_runs shape."""
    counts = {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}
    for o in outcomes:
        counts[o.result] += 1
        counts["images_discovered"] += o.images_inserted
    return counts


def latest_snapshot(conn: psycopg.Connection, source: str, source_id_native: str) -> SnapshotRef | None:
    """The listing's latest snapshot under THE one ordering (scraped_at DESC, id DESC)."""
    with conn.cursor() as cur:
        cur.execute(_LATEST_SNAPSHOT_SQL, {"source": source, "native": source_id_native})
        row = cur.fetchone()
    if row is None:
        return None
    return SnapshotRef(id=int(row[0]), content_hash=row[1], raw_json=row[2])
