"""THE writer's lease of the autodedup lane: one row of `autodedup.rt_lease`, by name.

The worker's pass, `rt_seed` and a live `apply` / `unapply` each hold it for their run, so one
of them at a time writes the live stream or production merges (A9). Lease-row CAS, never
`pg_advisory_lock`: a session lock strands over the transaction pooler. Every refusal names the
holder and when its lease ends; `release_stale` is the path for a holder that died with it (a
killed dispatch holds it for its whole TTL), and a release that fails while another error is
already on its way out never replaces that error.
"""

from __future__ import annotations

from typing import Any, Mapping

from autodedup.incremental_sql import (
    RT_LEASE_READ_SQL,
    RT_LEASE_RELEASE_SQL,
    RT_LEASE_TAKE_SQL,
)

NAME: str = "autodedup_realtime"
# A seed or a live dispatch holds it for its job's own timeout (`autodedup.yml`,
# `timeout-minutes: 300`), so a running one can never lose it to a pass.
DISPATCH_TTL_S: int = 300 * 60
# The dispatch argument that ends a dead holder's lease first (`release_stale`).
RELEASE_ARG: str = "release_lease"


def _rows(conn: Any, sql: str, params: Mapping[str, Any]) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))
        return list(cur.fetchall())


def take(conn: Any, holder: str, ttl: int) -> bool:
    rows = _rows(conn, RT_LEASE_TAKE_SQL, {"name": NAME, "holder": holder, "ttl": ttl})
    return bool(rows) and str(rows[0][0]) == holder


def release(conn: Any, holder: str) -> None:
    with conn.cursor() as cur:
        cur.execute(RT_LEASE_RELEASE_SQL, {"name": NAME, "holder": holder})


def current(conn: Any) -> dict[str, Any] | None:
    """The row as it stands: holder, taken_at, expires_at and whether it is still live."""
    rows = _rows(conn, RT_LEASE_READ_SQL, {"name": NAME})
    if not rows:
        return None
    holder, taken_at, expires_at, live = rows[0]
    return {"holder": holder, "taken_at": taken_at, "expires_at": expires_at, "live": bool(live)}


def describe(conn: Any) -> str:
    """Who holds the lease, for a refusal: the holder, since when, and when it ends by itself."""
    try:
        row = current(conn)
    except Exception as exc:  # noqa: BLE001 — a refusal must still be raised without it
        return f"(its holder could not be read: {type(exc).__name__})"
    if row is None:
        return "(no holder on record)"
    return (f"held by {row['holder']!r} since {row['taken_at']}, until {row['expires_at']} — "
            f"if that writer is dead (its job ended, its worker restarted), pass "
            f"{RELEASE_ARG}={row['holder']} to end it first")


def release_stale(conn: Any, holder: str) -> dict[str, Any]:
    """End the lease `holder` left behind, and only that one: a lease someone else holds now,
    or none at all, is refused. For a writer that died holding it (review A11, B13)."""
    row = current(conn)
    if row is None or not row["live"]:
        return {"released": None, "reason": "no live lease to release"}
    if str(row["holder"]) != holder:
        raise SystemExit(
            f"{RELEASE_ARG}={holder}: autodedup.rt_lease is {describe(conn)} — not that "
            "holder's; nothing was released and nothing was written.")
    release(conn, holder)
    return {"released": holder, "expires_at_was": str(row["expires_at"])}


def release_after(conn: Any, holder: str, original: BaseException | None) -> None:
    """Release at the end of a run. A release that fails while `original` is on its way out
    is noted ON it instead of replacing it: the lease then expires by itself."""
    try:
        release(conn, holder)
    except Exception as exc:
        if original is None:
            raise
        original.add_note(f"releasing autodedup.rt_lease for {holder!r} also failed "
                          f"({type(exc).__name__}: {exc}); it expires by itself")
