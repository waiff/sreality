"""compare_snapshots: how a single listing has evolved over time.

Pure read. Pulls all snapshots for one listing, re-parses each raw_json
with the current parser, and emits price trajectory + per-field change
history.

Production density (May 2026): avg 1.01 snapshots/listing, max 3. Cost
of re-parsing is microseconds. If density ever grows significantly the
parsed fields could be denormalised onto a column in listing_snapshots;
not needed today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import psycopg


PricePattern = Literal[
    "stable", "single_drop", "stairstep_dropping", "rising", "volatile",
]

_DIFF_SKIP_KEYS: frozenset[str] = frozenset({"sreality_id", "lon", "lat"})


def compare_snapshots(
    conn: "psycopg.Connection",
    sreality_id: int,
    since: timedelta | None = None,
) -> dict[str, Any]:
    from scraper import parser
    from toolkit import _now_iso

    snapshots = _fetch_snapshots(conn, sreality_id, since)
    first_seen = _fetch_first_seen_at(conn, sreality_id)

    trajectory = _build_trajectory(snapshots)
    price_change_count, price_change_total = _price_change_stats(trajectory)
    pattern = _classify_pattern(trajectory)
    field_changes = _build_field_changes(snapshots, parser)

    if first_seen is not None:
        tom_days = (datetime.now(timezone.utc) - first_seen).days
    elif snapshots:
        tom_days = (datetime.now(timezone.utc) - snapshots[0]["scraped_at"]).days
    else:
        tom_days = 0

    data: dict[str, Any] = {
        "sreality_id": sreality_id,
        "snapshot_count": len(snapshots),
        "first_snapshot_at": (
            snapshots[0]["scraped_at"].isoformat() if snapshots else None
        ),
        "last_snapshot_at": (
            snapshots[-1]["scraped_at"].isoformat() if snapshots else None
        ),
        "time_on_market_days": tom_days,
        "price_trajectory": trajectory,
        "price_change_count": price_change_count,
        "price_change_total_czk": price_change_total,
        "price_change_pattern": pattern,
        "field_changes": field_changes,
    }

    return {
        "data": data,
        "metadata": {
            "tool": "compare_snapshots",
            "filters_used": {
                "sreality_id": sreality_id,
                "since_days": since.days if since else None,
            },
            "result_count": len(snapshots),
            "queried_at": _now_iso(),
            "data_freshness": (
                snapshots[-1]["scraped_at"].isoformat() if snapshots else None
            ),
        },
    }


def _fetch_snapshots(
    conn: "psycopg.Connection",
    sreality_id: int,
    since: timedelta | None,
) -> list[dict[str, Any]]:
    if since is not None:
        sql = """
        SELECT id, scraped_at, price_czk, raw_json
        FROM listing_snapshots
        WHERE sreality_id = %s
          AND scraped_at > now() - make_interval(secs => %s)
        ORDER BY scraped_at ASC
        """
        params: tuple[Any, ...] = (sreality_id, int(since.total_seconds()))
    else:
        sql = """
        SELECT id, scraped_at, price_czk, raw_json
        FROM listing_snapshots
        WHERE sreality_id = %s
        ORDER BY scraped_at ASC
        """
        params = (sreality_id,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {"id": r[0], "scraped_at": r[1], "price_czk": r[2], "raw_json": r[3]}
        for r in rows
    ]


def _fetch_first_seen_at(
    conn: "psycopg.Connection", sreality_id: int
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at FROM listings WHERE sreality_id = %s",
            (sreality_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _build_trajectory(
    snapshots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": s["id"],
            "at": s["scraped_at"].isoformat(),
            "price_czk": s["price_czk"],
        }
        for s in snapshots
    ]


def _price_change_stats(
    trajectory: list[dict[str, Any]]
) -> tuple[int, int]:
    if len(trajectory) < 2:
        return 0, 0
    count = 0
    for prev, nxt in zip(trajectory, trajectory[1:]):
        if prev["price_czk"] is None or nxt["price_czk"] is None:
            continue
        if prev["price_czk"] != nxt["price_czk"]:
            count += 1
    first = next(
        (e["price_czk"] for e in trajectory if e["price_czk"] is not None),
        None,
    )
    last = next(
        (e["price_czk"] for e in reversed(trajectory) if e["price_czk"] is not None),
        None,
    )
    if first is None or last is None:
        return count, 0
    return count, last - first


def _classify_pattern(
    trajectory: list[dict[str, Any]]
) -> PricePattern:
    diffs: list[int] = []
    for prev, nxt in zip(trajectory, trajectory[1:]):
        if prev["price_czk"] is None or nxt["price_czk"] is None:
            continue
        d = nxt["price_czk"] - prev["price_czk"]
        if d != 0:
            diffs.append(d)
    if not diffs:
        return "stable"
    all_down = all(d < 0 for d in diffs)
    all_up = all(d > 0 for d in diffs)
    if all_down:
        return "single_drop" if len(diffs) == 1 else "stairstep_dropping"
    if all_up:
        return "rising"
    return "volatile"


def _build_field_changes(
    snapshots: list[dict[str, Any]],
    parser_module: Any,
) -> list[dict[str, Any]]:
    if len(snapshots) < 2:
        return []
    parsed: list[dict[str, Any]] = []
    image_sets: list[set[tuple[Any, str]]] = []
    for s in snapshots:
        try:
            parsed.append(parser_module.parse_listing(s["raw_json"]))
        except Exception:
            parsed.append({})
        try:
            imgs = parser_module.parse_images(s["raw_json"])
            image_sets.append({(i.get("sequence"), i["url"]) for i in imgs})
        except Exception:
            image_sets.append(set())

    out: list[dict[str, Any]] = []
    for i in range(1, len(snapshots)):
        prev = parsed[i - 1]
        curr = parsed[i]
        keys = set(prev) | set(curr)
        for key in sorted(keys):
            if key in _DIFF_SKIP_KEYS:
                continue
            if prev.get(key) != curr.get(key):
                out.append({
                    "field": key,
                    "from": prev.get(key),
                    "to": curr.get(key),
                    "at": snapshots[i]["scraped_at"].isoformat(),
                    "snapshot_id": snapshots[i]["id"],
                })
        if image_sets[i - 1] != image_sets[i]:
            out.append({
                "field": "images",
                "from": None,
                "to": None,
                "at": snapshots[i]["scraped_at"].isoformat(),
                "snapshot_id": snapshots[i]["id"],
            })
    return out
