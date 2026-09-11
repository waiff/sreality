"""Re-master stored sreality images onto the whole-frame 1800px master template.

~3.55M stored sreality objects hold the superseded `res,749,562,3` CROP (mode 3
lost ~85% of photos' edges). This lane re-downloads each one through
`image_storage.IMAGE_TRANSFORM_OPS` and OVERWRITES the row's existing R2 object
under its stored `images.storage_path` — reused verbatim, never recomputed via
`image_key()`, because the bucket holds two key schemes and a recomputed key
would orphan the old object and re-point nothing. The stamp
(`db.mark_image_remastered`) records the new rendition + decoded dimensions and
runs `invalidate_derived_signals`, so the phash and CLIP lanes recompute the
signals the new framing invalidates.

The queue IS the cursor: pending rows are `rendition IS NULL` and leave the
selection the moment they are stamped, so no persisted cursor and no new index
are needed. Listings are paged newest-first, ACTIVE before inactive.

EVERY outcome stamps `last_download_attempt_at`, because that clock is the
pending set's only anti-thrash rail: a row left unstamped would be re-selected —
and re-downloaded — on the very next tick, forever. Retirement from the queue
(`rendition = 'sreality-749-crop'`) always takes TWO sightings 20h apart, since
it is irreversible for this lane.

sreality ROTATES image URLs when a listing is edited: on the longest-lived
actives ~75% of stored URLs are dead while the listing's CURRENT urls resolve
fine. A 404/410 (or a dead-URL 400/401/415) on the stored URL therefore
re-resolves from the live detail payload, mapping `parse_images` `sequence` ->
`images.sequence`, and the fresh URL is persisted alongside the bytes. An image
row's identity is that POSITION — the same `(listing_id, sequence)` key the
images upsert itself uses — so a reordered gallery re-points the row onto
whatever photo now sits at that order, exactly as the detail drain and
`refresh_stale_images` already do. The bytes under the key follow the position,
not the photograph.

Usage:  python -m scripts.remaster_sreality_images --shard 1/6 --max-images 16000
Required: SUPABASE_DB_URL (+ R2_* to do any work at all).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from scraper import db, image_storage, media, parser
from scraper.image_storage import IMAGE_TRANSFORM_OPS, RENDITION_SREALITY_MASTER
from scraper.main import (
    SUSPICIOUS_STOP_THRESHOLD,
    SUSPICIOUS_STOP_WINDOW,
    _image_host,
    _is_dead_url_image_error,
    _is_gone_image_error,
    _phash_or_none,
    _suspicious_stop,
)
from scraper.portal import deadline_reached
from scraper.portal_base import ListingGoneError

LOG = logging.getLogger("remaster_sreality_images")

# The serve-path key shape, re-declared because `api.routes.images` imports
# fastapi, which the scraper runtime does not carry. Parity is a test.
_KEY_RE = re.compile(r"^-?\d+/\d{4}\.jpg$")

_LISTING_PAGE = 2000
_IMAGE_BATCH = 200
# What the superseded mode-3 template returned. Bytes that still measure this
# mean the request was served the CROP, so stamping them as the master would
# poison the rendition vocabulary for every consumer that groups on it.
_LEGACY_CROP_DIMS = (749, 562)
# A wall of anomalies means the CDN stopped honouring the master template — but a
# handful of permanently odd objects must never red the lane, so the abort needs
# BOTH an absolute floor and a share of what the run actually looked at.
_ANOMALY_ABORT = 20
_ANOMALY_ABORT_RATIO = 0.5
_BREAKER_SLEEP_S = 60.0
_BREAKER_TRIP_ABORT = 3
# Six shards share one CDN, so the per-process cap is a sixth of the lane's real
# footprint — and images.yml is fetching the same host in the same window.
_MAX_CONCURRENCY_PER_HOST = 4
_MAX_BIGINT = 9223372036854775807

# `hashint8` returns a SIGNED int4, so a bare `%` yields negative residues that
# no shard index would ever match; `abs(...::bigint)` normalises it (the cast
# first, because abs(int4 minimum) overflows). Residue is 0-based while the CLI
# shard is 1-based, so 1/1 selects everything.
_LISTING_PAGE_SQL = """
    SELECT id, sreality_id, is_active
    FROM listings
    WHERE source = 'sreality'
      AND is_active = %(active)s
      AND abs(hashint8(id)::bigint) %% %(n)s = %(k)s
      AND id < %(before)s
    ORDER BY id DESC
    LIMIT %(page)s
"""

_LISTINGS_BY_ID_SQL = """
    SELECT id, sreality_id, is_active
    FROM listings
    WHERE source = 'sreality'
      AND id = ANY(%(ids)s::bigint[])
    ORDER BY id DESC
"""

# `last_download_attempt_at` is the anti-thrash rail: EVERY outcome stamps it, so
# a row that just failed cannot be retried by the next shard tick. Its second
# duty is the two-strike rule — `retried` is what tells a worker that this row's
# failure has already been seen once.
# A stored row's `last_download_attempt_at` was first written by the ORIGINAL
# download, so "has a stamp" is true for every legacy row and would retire it on
# its first strike. Only this lane stamps stored rows after its first production
# run, so a stamp at or after that instant is the lane's own earlier sighting.
REMASTER_EPOCH = "2026-09-11T11:40:00+00:00"

_PENDING_IMAGES_SQL = """
    SELECT i.id, i.listing_id, i.sequence, i.sreality_url, i.storage_path,
           (i.last_download_attempt_at >= %(epoch)s::timestamptz) AS retried
    FROM images i
    WHERE i.listing_id = ANY(%(ids)s::bigint[])
      AND i.storage_path IS NOT NULL
      AND i.rendition IS NULL
      AND i.sreality_url LIKE '%%sdn.cz%%'
      AND (i.last_download_attempt_at IS NULL
           OR i.last_download_attempt_at < now() - interval '20 hours')
    ORDER BY i.listing_id DESC, i.sequence
"""

# The completion question, asked ONCE per run and only by a pass that already
# walked both loops out: is any row this lane could still convert left in the
# shard? Deliberately WITHOUT the 20-hour rail — "the pass saw nothing" is also
# what a shard looks like two hours after it deferred its last rows — and
# deliberately WITHOUT keys this lane refuses to touch, which would otherwise
# block the stamp forever.
_PENDING_REMAINS_SQL = """
    SELECT 1
    FROM images i
    JOIN listings l ON l.id = i.listing_id
    WHERE l.source = 'sreality'
      AND abs(hashint8(l.id)::bigint) %% %(n)s = %(k)s
      AND i.storage_path IS NOT NULL
      AND i.storage_path ~ '^-?[0-9]+/[0-9]{4}\\.jpg$'
      AND i.rendition IS NULL
      AND i.sreality_url LIKE '%%sdn.cz%%'
    LIMIT 1
"""

# One row, one sub-key per shard, merged in: six shards finish at different
# times, so a single flat value would let whichever finished last read as "the
# lane is done".
_STAMP_COMPLETE_SQL = """
    INSERT INTO app_settings (key, value, updated_by)
    VALUES ('sreality_remaster_last_complete',
            jsonb_build_object(
                %(shard)s::text,
                jsonb_build_object(
                    'completed_at', now(),
                    'elapsed_s', %(elapsed_s)s::numeric)),
            'remaster_sreality_images')
    ON CONFLICT (key) DO UPDATE
      SET value = COALESCE(app_settings.value, '{}'::jsonb) || excluded.value,
          updated_at = now(),
          updated_by = excluded.updated_by
"""

# A detail payload that is gone, or that carries no entry at this row's
# sequence: the stored URL is dead and nothing can replace it.
_DETAIL_GONE = object()


@dataclass(frozen=True)
class _ImageRow:
    image_id: int
    listing_id: int
    sequence: int | None
    sreality_url: str
    storage_path: str | None
    sreality_id: int | None
    retried: bool = False


@dataclass(frozen=True)
class _Outcome:
    row: _ImageRow
    kind: str  # remastered | terminal | deferred | anomaly | bad_key
    host: str = ""
    width: int | None = None
    height: int | None = None
    phash: int | None = None
    fresh_url: str | None = None
    note: str = ""


@dataclass
class _Stats:
    shard: str = "1/1"
    active_listings: int = 0
    inactive_listings: int = 0
    scanned: int = 0
    remastered: int = 0
    terminal: int = 0
    deferred: int = 0
    anomalies: int = 0
    bad_key: int = 0
    detail_fetches: int = 0
    elapsed_s: float = 0.0
    dry_run: bool = False
    stopped: str = ""
    complete: bool = False

    def line(self) -> str:
        return (
            f"REMASTER done shard={self.shard} "
            f"active_listings={self.active_listings} "
            f"inactive_listings={self.inactive_listings} "
            f"scanned={self.scanned} remastered={self.remastered} "
            f"terminal={self.terminal} deferred={self.deferred} "
            f"anomalies={self.anomalies} bad_key={self.bad_key} "
            f"detail_fetches={self.detail_fetches} "
            f"elapsed={self.elapsed_s:.1f}s dry_run={self.dry_run}"
        )


class _StopRun(Exception):
    """Raised to unwind the nested page/batch loops on a bounded stop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _HostSemaphores:
    """One BoundedSemaphore per CDN host, so the worker pool never dogpiles one CDN."""

    def __init__(self, per_host: int) -> None:
        self._per_host = per_host
        self._lock = threading.Lock()
        self._by_host: dict[str, threading.BoundedSemaphore] = {}

    def get(self, host: str) -> threading.BoundedSemaphore | None:
        if self._per_host <= 0:
            return None
        sem = self._by_host.get(host)
        if sem is not None:
            return sem
        with self._lock:
            sem = self._by_host.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(self._per_host)
                self._by_host[host] = sem
            return sem


class _DetailResolver:
    """Current image URL by (sreality_id, sequence) — one detail fetch per listing per run.

    The cache is filled under the lock, so concurrent workers on the same
    listing make ONE request; a transient failure is cached as None too, which
    defers that listing's whole batch instead of hammering a sick endpoint.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._cache: dict[int, Any] = {}
        self.fetches = 0

    def fresh_url(self, sreality_id: int | None, sequence: int | None) -> Any:
        if sreality_id is None:
            return _DETAIL_GONE
        with self._lock:
            if sreality_id not in self._cache:
                self._cache[sreality_id] = self._fetch(sreality_id)
            entry = self._cache[sreality_id]
        if entry is _DETAIL_GONE:
            return _DETAIL_GONE
        if entry is None:
            return None
        if sequence is None:
            return _DETAIL_GONE
        url = entry.get(sequence)
        return url if url else _DETAIL_GONE

    def _fetch(self, sreality_id: int) -> Any:
        self.fetches += 1
        try:
            raw = self._client.get_detail(sreality_id)
        except ListingGoneError:
            return _DETAIL_GONE
        except Exception as exc:  # noqa: BLE001 - a sick detail endpoint defers, never kills
            LOG.warning("REMASTER detail sid=%s transient: %s", sreality_id, exc)
            return None
        mapping: dict[int, str] = {}
        for image in parser.parse_images(raw if isinstance(raw, dict) else {}):
            sequence = image.get("sequence")
            url = image.get("url")
            if isinstance(sequence, int) and isinstance(url, str) and url:
                mapping[sequence] = url
        return mapping


def _download_bytes(url: str) -> bytes:
    return image_storage.download_image(url, transform_ops=IMAGE_TRANSFORM_OPS)


def _download(url: str, semaphore: threading.BoundedSemaphore | None) -> bytes:
    if semaphore is None:
        return _download_bytes(url)
    with semaphore:
        return _download_bytes(url)


def _dead_url(error: Exception) -> bool:
    """The stored URL is unusable — a rotated/removed photo, not a throttle.

    403 is deliberately absent: sreality throttles with it (see
    `scraper.main._is_dead_url_image_error`).
    """
    return _is_gone_image_error(error) or _is_dead_url_image_error(error)


def _dead_outcome(row: _ImageRow, host: str, note: str) -> _Outcome:
    """Nothing left to fetch — but retire the row only on the SECOND sighting.

    `mark_image_remaster_terminal` claims the rendition, which drops the row out
    of the pending set for good, so one transient CDN miss must not be enough.
    `retried` is the row's own `last_download_attempt_at`, so the confirmation is
    a different run at least 20 hours later.
    """
    return _Outcome(
        row=row, kind="terminal" if row.retried else "deferred", host=host, note=note
    )


def remaster_one(
    row: _ImageRow,
    r2: Any,
    resolver: _DetailResolver | None,
    semaphores: _HostSemaphores,
) -> _Outcome:
    """Worker: download, validate, overwrite the R2 object. No DB I/O here."""
    host = _image_host(row.sreality_url)
    if not row.storage_path or not _KEY_RE.match(row.storage_path):
        return _Outcome(row=row, kind="bad_key", host=host, note=str(row.storage_path))

    fresh_url: str | None = None
    try:
        data = _download(image_storage.with_transform(row.sreality_url), semaphores.get(host))
    except Exception as exc:  # noqa: BLE001 - classified below, never fatal
        if isinstance(exc, image_storage.NotAnImageError):
            # That type's own contract is TERMINAL, not transient: the CDN
            # answered with something that is not an image (or is oversize), and
            # re-requesting it cannot change that. Anomaly, so it defers once and
            # then retires rather than deferring forever with no ceiling.
            return _Outcome(row=row, kind="anomaly", host=host, note=str(exc))
        if not _dead_url(exc):
            return _Outcome(row=row, kind="deferred", host=host, note=str(exc))
        candidate = (
            resolver.fresh_url(row.sreality_id, row.sequence)
            if resolver is not None
            else _DETAIL_GONE
        )
        if candidate is _DETAIL_GONE:
            return _dead_outcome(row, host, str(exc))
        if candidate is None:
            return _Outcome(row=row, kind="deferred", host=host, note=str(exc))
        fresh_url = str(candidate)
        if fresh_url == row.sreality_url:
            # The listing was never edited: the live detail hands back the very
            # URL that just died, so a retry is the same request twice and its
            # failure is no corroboration at all.
            return _dead_outcome(row, host, str(exc))
        fresh_host = _image_host(fresh_url)
        try:
            data = _download(
                image_storage.with_transform(fresh_url), semaphores.get(fresh_host)
            )
        except Exception as retry_exc:  # noqa: BLE001
            if isinstance(retry_exc, image_storage.NotAnImageError):
                return _Outcome(row=row, kind="anomaly", host=fresh_host, note=str(retry_exc))
            if _dead_url(retry_exc):
                return _dead_outcome(row, fresh_host, str(retry_exc))
            return _Outcome(row=row, kind="deferred", host=fresh_host, note=str(retry_exc))

    if media.is_image_bytes(data) != "image/jpeg":
        return _Outcome(row=row, kind="anomaly", host=host, note="not a jpeg")
    dimensions = image_storage.image_dimensions(data)
    if dimensions is None:
        return _Outcome(row=row, kind="anomaly", host=host, note="undecodable")
    if dimensions == _LEGACY_CROP_DIMS:
        return _Outcome(row=row, kind="anomaly", host=host, note="served the legacy crop")

    try:
        r2.upload_bytes(row.storage_path, data, "image/jpeg")
    except Exception as exc:  # noqa: BLE001 - R2 trouble is transient, retry next tick
        return _Outcome(row=row, kind="deferred", host=host, note=str(exc))
    return _Outcome(
        row=row,
        kind="remastered",
        host=host,
        width=dimensions[0],
        height=dimensions[1],
        phash=_phash_or_none(data),
        fresh_url=fresh_url,
    )


def _fetchall(conn: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _iter_listing_pages(
    conn: Any, *, shard: tuple[int, int], listing_ids: Sequence[int] | None
) -> Iterator[list[tuple[Any, ...]]]:
    if listing_ids:
        rows = _fetchall(conn, _LISTINGS_BY_ID_SQL, {"ids": list(listing_ids)})
        if rows:
            yield rows
        return
    shard_index, shard_count = shard
    for active in (True, False):
        before = _MAX_BIGINT
        while True:
            rows = _fetchall(
                conn,
                _LISTING_PAGE_SQL,
                {
                    "active": active,
                    "n": shard_count,
                    "k": shard_index - 1,
                    "before": before,
                    "page": _LISTING_PAGE,
                },
            )
            if not rows:
                break
            yield rows
            if len(rows) < _LISTING_PAGE:
                break
            before = rows[-1][0]


def _pending_images(conn: Any, listing_rows: Sequence[tuple[Any, ...]]) -> list[_ImageRow]:
    sreality_ids = {row[0]: row[1] for row in listing_rows}
    rows = _fetchall(
        conn, _PENDING_IMAGES_SQL, {"ids": list(sreality_ids), "epoch": REMASTER_EPOCH}
    )
    return [
        _ImageRow(
            image_id=image_id,
            listing_id=listing_id,
            sequence=sequence,
            sreality_url=url,
            storage_path=storage_path,
            sreality_id=sreality_ids.get(listing_id),
            retried=bool(retried),
        )
        for image_id, listing_id, sequence, url, storage_path, retried in rows
    ]


def _chunks(rows: Sequence[_ImageRow], size: int) -> Iterator[list[_ImageRow]]:
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])


def _apply(conn: Any, outcome: _Outcome, stats: _Stats) -> None:
    """Main thread only: every DB write for one finished download.

    EVERY branch writes, because `last_download_attempt_at` is the pending set's
    only anti-thrash rail: a row left unstamped is re-selected — and, except for
    `bad_key`, re-downloaded — two hours later, forever.
    """
    if outcome.kind == "remastered":
        db.mark_image_remastered(
            conn,
            outcome.row.image_id,
            rendition=RENDITION_SREALITY_MASTER,
            width=outcome.width,
            height=outcome.height,
            phash=outcome.phash,
            sreality_url=outcome.fresh_url,
        )
        stats.remastered += 1
    elif outcome.kind == "terminal":
        db.mark_image_remaster_terminal(conn, outcome.row.image_id)
        stats.terminal += 1
        LOG.info("REMASTER terminal id=%s: %s", outcome.row.image_id, outcome.note)
    elif outcome.kind == "deferred":
        db.mark_image_remaster_deferred(conn, outcome.row.image_id)
        stats.deferred += 1
    elif outcome.kind == "anomaly":
        stats.anomalies += 1
        if outcome.row.retried:
            # Twice now the CDN has answered with something that is not a master.
            # The stored object IS the legacy crop and nothing better is coming:
            # retire it, or it sits in the pending set re-downloading forever.
            db.mark_image_remaster_terminal(conn, outcome.row.image_id)
        else:
            db.mark_image_remaster_deferred(conn, outcome.row.image_id)
        LOG.warning(
            "REMASTER anomaly id=%s: %s — nothing uploaded (%s)",
            outcome.row.image_id, outcome.note,
            "retired as the legacy crop" if outcome.row.retried else "one more look in 20h",
        )
    else:
        stats.bad_key += 1
        # No rendition claim: the key shape is this lane's refusal, not a fact
        # about the bytes, and widening `_KEY_RE` later must be able to recover
        # these rows. `_PENDING_REMAINS_SQL` skips them so they can't block the
        # completion stamp.
        db.mark_image_remaster_deferred(conn, outcome.row.image_id)
        LOG.warning("REMASTER bad_key id=%s key=%r", outcome.row.image_id, outcome.note)


def run_remaster(
    conn: Any,
    r2: Any,
    resolver: _DetailResolver | None,
    *,
    shard: tuple[int, int] = (1, 1),
    max_images: int = 0,
    deadline: float | None = None,
    workers: int = 16,
    listing_ids: Sequence[int] | None = None,
    dry_run: bool = False,
) -> _Stats:
    """One bounded pass. `max_images` 0 means "until the deadline or the queue ends"."""
    stats = _Stats(shard=f"{shard[0]}/{shard[1]}", dry_run=dry_run)
    host_windows: dict[str, deque[str]] = defaultdict(
        lambda: deque(maxlen=SUSPICIOUS_STOP_WINDOW)
    )
    semaphores = _HostSemaphores(_MAX_CONCURRENCY_PER_HOST)
    trips = 0
    pool = None if dry_run else ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        for listing_rows in _iter_listing_pages(conn, shard=shard, listing_ids=listing_ids):
            # At the top of the PAGE loop, not just the batch loop: once the
            # shard's pending set empties from the newest end, a run walks
            # millions of listings finding nothing, and a budget only the inner
            # loop checks is no budget at all.
            if deadline_reached(deadline):
                raise _StopRun("deadline")
            for _, _, is_active in listing_rows:
                if is_active:
                    stats.active_listings += 1
                else:
                    stats.inactive_listings += 1
            pending = _pending_images(conn, listing_rows)
            if not pending:
                continue
            for batch in _chunks(pending, _IMAGE_BATCH):
                if deadline_reached(deadline):
                    raise _StopRun("deadline")
                if max_images:
                    remaining = max_images - stats.scanned
                    if remaining <= 0:
                        raise _StopRun("max_images")
                    batch = batch[:remaining]
                stats.scanned += len(batch)
                if dry_run or pool is None:
                    continue
                futures = [
                    pool.submit(remaster_one, row, r2, resolver, semaphores) for row in batch
                ]
                for future in as_completed(futures):
                    outcome = future.result()
                    _apply(conn, outcome, stats)
                    if outcome.host:
                        host_windows[outcome.host].append(
                            "transient" if outcome.kind == "deferred" else "ok"
                        )
                    if _anomalies_exceeded(stats):
                        raise _StopRun("anomalies")
                LOG.info(
                    "REMASTER progress scanned=%d remastered=%d terminal=%d "
                    "deferred=%d anomalies=%d",
                    stats.scanned, stats.remastered, stats.terminal,
                    stats.deferred, stats.anomalies,
                )
                trips = _check_breaker(host_windows, trips)
    except _StopRun as stop:
        stats.stopped = stop.reason
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    if resolver is not None:
        stats.detail_fetches = resolver.fetches
    if not stats.stopped and not listing_ids:
        stats.complete = not _pending_remains(conn, shard)
    return stats


def _anomalies_exceeded(stats: _Stats) -> bool:
    """A share AND a floor: a scatter of odd objects must not red the lane.

    What this alarm is for is a template that silently regressed, and that looks
    like EVERY download coming back wrong — not like twenty of them.
    """
    return (
        stats.anomalies >= _ANOMALY_ABORT
        and stats.anomalies >= _ANOMALY_ABORT_RATIO * stats.scanned
    )


def _pending_remains(conn: Any, shard: tuple[int, int]) -> bool:
    """Is there a row left in this shard that the lane could still convert?"""
    shard_index, shard_count = shard
    rows = _fetchall(
        conn, _PENDING_REMAINS_SQL, {"n": shard_count, "k": shard_index - 1}
    )
    return bool(rows)


def _check_breaker(host_windows: dict[str, "deque[str]"], trips: int) -> int:
    """Quarantine-by-pause: a host failing transiently gets 60s, three times, then we stop."""
    for host, window in host_windows.items():
        if not _suspicious_stop(window):
            continue
        window.clear()
        trips += 1
        LOG.warning(
            "REMASTER breaker host=%s — transient rate over its last %d outcomes "
            "exceeded %.0f%% (trip %d/%d)",
            host, SUSPICIOUS_STOP_WINDOW, SUSPICIOUS_STOP_THRESHOLD * 100,
            trips, _BREAKER_TRIP_ABORT,
        )
        if trips >= _BREAKER_TRIP_ABORT:
            raise _StopRun("breaker")
        time.sleep(_BREAKER_SLEEP_S)
    return trips


def stamp_complete(conn: Any, stats: _Stats) -> None:
    """Merge THIS shard's completion under the one key — never overwrite the row."""
    with conn.cursor() as cur:
        cur.execute(
            _STAMP_COMPLETE_SQL,
            {"shard": stats.shard, "elapsed_s": round(stats.elapsed_s, 3)},
        )


def _parse_shard(value: str) -> tuple[int, int]:
    try:
        index, count = (int(part) for part in value.split("/", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"shard must be k/N, got {value!r}") from exc
    if count < 1 or not 1 <= index <= count:
        raise argparse.ArgumentTypeError(f"shard out of range: {value!r}")
    return index, count


_REOPEN_SQL = """
    UPDATE images
    SET rendition = NULL, last_download_attempt_at = NULL
    WHERE listing_id = ANY(%(ids)s::bigint[])
      AND rendition = 'sreality-749-crop'
"""


def reopen(conn: Any, listing_ids: Sequence[int]) -> int:
    """Un-retire a pilot cohort so it takes the two-strike path again.

    Retirement is the lane's only irreversible decision, so this is deliberately
    bounded to explicit listing ids: the rendition claim and the attempt stamp
    both go, which makes the rows pending AND first-sighting.
    """
    with conn.cursor() as cur:
        cur.execute(_REOPEN_SQL, {"ids": list(listing_ids)})
        return int(cur.rowcount or 0)


def _parse_listing_ids(value: str) -> list[int]:
    return [int(part) for part in value.replace(",", " ").split() if part]


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shard", type=_parse_shard, default=(1, 1), help="k/N slice of listings.")
    ap.add_argument("--max-images", type=int, default=16000, help="Per-run cap; 0 = uncapped.")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="Wall-clock bound, checked between batches; 0 = unbounded.")
    ap.add_argument("--workers", type=int, default=16, help="Parallel CDN downloads.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Select and report only: no network, no writes.")
    ap.add_argument("--listing-ids", type=_parse_listing_ids, default=None,
                    help="Pilot mode: only these listings.id (ignores --shard).")
    ap.add_argument("--reopen", action="store_true",
                    help="With --listing-ids: un-retire their 'sreality-749-crop' rows first.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.reopen and (not args.listing_ids or args.dry_run):
        ap.error("--reopen needs --listing-ids and is incompatible with --dry-run")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not image_storage.is_configured():
        LOG.info("REMASTER skip: R2 env vars missing")
        return 0

    from scraper.sreality_client import SrealityClient

    started = time.monotonic()
    deadline = started + args.max_seconds if args.max_seconds > 0 else None
    r2 = None if args.dry_run else image_storage.R2Client.from_env(
        max_pool_connections=args.workers
    )
    resolver = None if args.dry_run else _DetailResolver(
        SrealityClient(category_main=1, category_type=2)
    )
    with db.connect_session() as conn:
        if args.reopen:
            LOG.info("REMASTER reopened %d retired rows for listings %s",
                     reopen(conn, args.listing_ids), args.listing_ids)
        stats = run_remaster(
            conn,
            r2,
            resolver,
            shard=args.shard,
            max_images=max(0, args.max_images),
            deadline=deadline,
            workers=args.workers,
            listing_ids=args.listing_ids,
            dry_run=args.dry_run,
        )
        stats.elapsed_s = time.monotonic() - started
        if stats.complete and not args.dry_run and not args.listing_ids:
            stamp_complete(conn, stats)
            LOG.info("REMASTER shard %s has no convertible rows left", stats.shard)
    LOG.info("%s", stats.line())
    # A tripped breaker is a healthy backoff (exit 0); a wall of anomalies means
    # the CDN served something other than the master and must go red.
    return 1 if stats.stopped == "anomalies" else 0


if __name__ == "__main__":
    sys.exit(main())
