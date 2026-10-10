"""THE writer's lease of the autodedup lane: one row of `autodedup.rt_lease`, by name.

The worker's pass, `rt_seed` and a live `apply` / `unapply` each hold it for their run, so one
of them at a time writes the live stream or production merges (A9). Lease-row CAS, never
`pg_advisory_lock`: a session lock strands over the transaction pooler. Every refusal names the
holder and when its lease ends; `release_stale` is the path for a holder that died with it (a
killed dispatch holds it for its whole TTL), and a release that fails while another error is
already on its way out never replaces that error.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any, Callable, Mapping

from autodedup.incremental_sql import (
    RT_LEASE_HOLD_SQL,
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
# A database restart takes every connection a run holds (2026-10-10 05:57Z: the lease then sat
# its whole TTL), so `release_after` tries NEW ones this many times, this far apart.
NEW_CONNECTION_ATTEMPTS: int = 3
NEW_CONNECTION_DELAY_S: float = 20.0


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


# The outcome a run reports when its lease ended under it (E941).
LEASE_LOST: str = "lease_lost"


class LeaseLost(Exception):
    """The holder's lease ended while its run still ran — released by the worker's shutdown
    (E941) or by a `release_lease=` dispatch that took the holder for dead."""


def hold(conn: Any, holder: str) -> None:
    """Inside the caller's transaction, as its last statement before the commit: lock this
    holder's live lease row until the transaction ends, or raise `LeaseLost` so it rolls back
    (E941). A run whose lease was released under it then never commits beside the next holder,
    and a release that arrives after this statement waits for the commit."""
    if not _rows(conn, RT_LEASE_HOLD_SQL, {"name": NAME, "holder": holder}):
        raise LeaseLost(f"autodedup.rt_lease is no longer held by {holder!r}: "
                        f"{describe(conn)}")


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


def release_after(conn: Any, holder: str, original: BaseException | None, *,
                  fallback: Any | None = None,
                  connect: Callable[[], Any] | None = None) -> bool:
    """Release at the end of a run. A release that fails while `original` is on its way out
    is noted ON it instead of replacing it: the lease then expires by itself.

    `fallback` is a second, live connection the caller already holds — the one a raised pass
    halves its rate on (E930) — and a release that fails on `conn` (the backend the server
    terminated) is retried there (E931): a lease left to its TTL skips every pass of the next
    ~35 min as "leased". When that fails too, as a database restart makes it, `connect` (the
    caller's factory for a NEW connection) is tried up to `NEW_CONNECTION_ATTEMPTS` times,
    `NEW_CONNECTION_DELAY_S` apart, each connection closed and each failed attempt noted, not
    raised. The statement is keyed by holder, so it ends this holder's row and no other's.
    Returns whether a release ran."""
    try:
        release(conn, holder)
        return True
    except Exception as exc:
        failed: Exception = exc
        if fallback is not None:
            try:
                release(fallback, holder)
            except Exception as again:  # noqa: BLE001 — noted below, never raised instead
                failed = again
            else:
                if original is not None:
                    original.add_note(f"releasing autodedup.rt_lease for {holder!r} failed on "
                                      f"the pass's connection ({type(exc).__name__}: {exc}); "
                                      "released on the fresh one")
                return True
        if connect is not None:
            before = failed
            for attempt in range(1, NEW_CONNECTION_ATTEMPTS + 1):
                if attempt > 1:
                    time.sleep(NEW_CONNECTION_DELAY_S)
                try:
                    _release_on_new(connect, holder)
                except Exception as again:  # noqa: BLE001 — noted, never raised instead
                    failed = again
                    if original is not None:
                        original.add_note(f"releasing autodedup.rt_lease for {holder!r} on a "
                                          f"new connection failed (attempt {attempt}/"
                                          f"{NEW_CONNECTION_ATTEMPTS}: "
                                          f"{type(again).__name__}: {again})")
                else:
                    if original is not None:
                        original.add_note(f"releasing autodedup.rt_lease for {holder!r} failed "
                                          f"({type(before).__name__}: {before}); released on a "
                                          f"new connection (attempt {attempt}/"
                                          f"{NEW_CONNECTION_ATTEMPTS})")
                    return True
        if original is None:
            raise failed
        original.add_note(f"releasing autodedup.rt_lease for {holder!r} also failed "
                          f"({type(failed).__name__}: {failed}); it expires by itself")
        return False


def _release_on_new(connect: Callable[[], Any], holder: str) -> None:
    """`release` on a connection `connect` opens, closed whatever the release did."""
    new_conn = connect()
    try:
        release(new_conn, holder)
    finally:
        close = getattr(new_conn, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
