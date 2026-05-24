"""On-demand freshness check for a single listing.

Looks up the current snapshot, refetches the listing from sreality,
classifies the outcome, and writes an audit row to
listing_freshness_checks. On 'updated' the new snapshot is written via
db.upsert_listing; on 'gone' the listing is flipped to is_active=false.

Does NOT bump listings.last_seen_at. The cron index walk remains the
sole driver of last_seen_at (architectural rule #4); a freshness check
is a separate signal recorded in listing_freshness_checks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import requests

from scraper import db, hashing, parser
from scraper.sreality_client import ListingGoneError, SrealityClient

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)

GONE_STATUSES: frozenset[int] = frozenset({404, 410})

Outcome = Literal["unchanged", "updated", "gone", "fetch_error"]

_DIFF_SKIP_KEYS: frozenset[str] = frozenset({"sreality_id", "lon", "lat"})


class FreshnessResult(TypedDict):
    sreality_id: int
    outcome: Outcome
    prev_hash: str | None
    new_hash: str | None
    what_changed: list[str]
    error_message: str | None
    checked_at: str
    snapshot_id: int | None


def freshness_check(
    conn: psycopg.Connection,
    client: SrealityClient,
    sreality_id: int,
) -> FreshnessResult:
    """Refetch one listing, classify, and audit. Never raises."""
    prev = _fetch_prev_snapshot(conn, sreality_id)

    try:
        raw = client.get_detail(sreality_id)
    except ListingGoneError:
        return _record_gone(conn, sreality_id, prev)
    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if getattr(exc, "response", None) is not None
            else None
        )
        if status in GONE_STATUSES:
            return _record_gone(conn, sreality_id, prev)
        return _record_fetch_error(conn, sreality_id, prev, exc)
    except Exception as exc:
        return _record_fetch_error(conn, sreality_id, prev, exc)

    try:
        row = parser.parse_listing(raw)
        images = parser.parse_images(raw)
        new_hash = hashing.content_hash(raw)
    except Exception as exc:
        return _record_fetch_error(conn, sreality_id, prev, exc)

    if prev is not None and prev["content_hash"] == new_hash:
        return _record_unchanged(conn, sreality_id, prev, new_hash)

    try:
        db.upsert_listing(conn, row, raw, new_hash)
        db.record_images(conn, sreality_id, images)
    except Exception as exc:
        return _record_fetch_error(conn, sreality_id, prev, exc)

    new_snap_id = _fetch_latest_snapshot_id(conn, sreality_id)
    what_changed = _diff_fields(prev, row, images)
    return _record_updated(
        conn, sreality_id, prev, new_hash, new_snap_id, what_changed
    )


def _fetch_prev_snapshot(
    conn: psycopg.Connection, sreality_id: int
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content_hash, raw_json
            FROM listing_snapshots
            WHERE sreality_id = %s
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (sreality_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "content_hash": row[1], "raw_json": row[2]}


def _fetch_latest_snapshot_id(
    conn: psycopg.Connection, sreality_id: int
) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM listing_snapshots
            WHERE sreality_id = %s
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (sreality_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _diff_fields(
    prev: dict[str, Any] | None,
    new_row: dict[str, Any],
    new_images: list[dict[str, Any]],
) -> list[str]:
    if prev is None:
        return []
    try:
        prev_row = parser.parse_listing(prev["raw_json"])
        prev_images = parser.parse_images(prev["raw_json"])
    except Exception:
        return []

    changed: list[str] = []
    keys = set(prev_row) | set(new_row)
    for key in sorted(keys):
        if key in _DIFF_SKIP_KEYS:
            continue
        if prev_row.get(key) != new_row.get(key):
            changed.append(key)

    prev_imgs = {(img.get("sequence"), img["url"]) for img in prev_images}
    new_imgs = {(img.get("sequence"), img["url"]) for img in new_images}
    if prev_imgs != new_imgs:
        changed.append("images")
    return changed


def _record_unchanged(
    conn: psycopg.Connection,
    sreality_id: int,
    prev: dict[str, Any],
    new_hash: str,
) -> FreshnessResult:
    _insert_log(
        conn, sreality_id, "unchanged",
        prev_hash=prev["content_hash"], new_hash=new_hash, error=None,
    )
    return _build_result(
        sreality_id, "unchanged",
        prev_hash=prev["content_hash"], new_hash=new_hash,
        what_changed=[], error=None,
        snapshot_id=prev["id"],
    )


def _record_updated(
    conn: psycopg.Connection,
    sreality_id: int,
    prev: dict[str, Any] | None,
    new_hash: str,
    snapshot_id: int | None,
    what_changed: list[str],
) -> FreshnessResult:
    prev_hash = prev["content_hash"] if prev else None
    _insert_log(
        conn, sreality_id, "updated",
        prev_hash=prev_hash, new_hash=new_hash, error=None,
    )
    return _build_result(
        sreality_id, "updated",
        prev_hash=prev_hash, new_hash=new_hash,
        what_changed=what_changed, error=None,
        snapshot_id=snapshot_id,
    )


def _record_gone(
    conn: psycopg.Connection,
    sreality_id: int,
    prev: dict[str, Any] | None,
) -> FreshnessResult:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE listings SET is_active = false WHERE sreality_id = %s",
            (sreality_id,),
        )
    prev_hash = prev["content_hash"] if prev else None
    _insert_log(
        conn, sreality_id, "gone",
        prev_hash=prev_hash, new_hash=None, error=None,
    )
    return _build_result(
        sreality_id, "gone",
        prev_hash=prev_hash, new_hash=None,
        what_changed=[], error=None,
        snapshot_id=None,
    )


def _record_fetch_error(
    conn: psycopg.Connection,
    sreality_id: int,
    prev: dict[str, Any] | None,
    exc: BaseException,
) -> FreshnessResult:
    msg = f"{type(exc).__name__}: {exc}"[:500]
    prev_hash = prev["content_hash"] if prev else None
    _insert_log(
        conn, sreality_id, "fetch_error",
        prev_hash=prev_hash, new_hash=None, error=msg,
    )
    return _build_result(
        sreality_id, "fetch_error",
        prev_hash=prev_hash, new_hash=None,
        what_changed=[], error=msg,
        snapshot_id=None,
    )


def _insert_log(
    conn: psycopg.Connection,
    sreality_id: int,
    outcome: Outcome,
    prev_hash: str | None,
    new_hash: str | None,
    error: str | None,
) -> None:
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO listing_freshness_checks
                    (sreality_id, outcome, prev_hash, new_hash, error_message)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (sreality_id, outcome, prev_hash, new_hash, error),
            )
    except Exception as exc:
        LOG.warning("could not write freshness audit row: %s", exc)


def _build_result(
    sreality_id: int,
    outcome: Outcome,
    prev_hash: str | None,
    new_hash: str | None,
    what_changed: list[str],
    error: str | None,
    snapshot_id: int | None,
) -> FreshnessResult:
    return {
        "sreality_id": sreality_id,
        "outcome": outcome,
        "prev_hash": prev_hash,
        "new_hash": new_hash,
        "what_changed": what_changed,
        "error_message": error,
        "checked_at": _now_iso(),
        "snapshot_id": snapshot_id,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
