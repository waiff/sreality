"""DB-backed shared politeness ledger (realtime-scrapers Wave C-1).

`RateLimiter` is per-process, so two runtimes hitting one portal (GitHub
Actions walks + the always-on Railway worker) would each spend a full budget
and a 429/403 penalty learned in one would never reach the other.
`LedgerRateLimiter` shares ONE per-portal budget through `portal_rate_state`
(migration 268): it *leases* batches of request slots — a single autocommit
UPDATE advances the shared `next_slot_at` frontier by N slots and returns the
window start + slot width — then paces locally between the leased slots using
the inherited `RateLimiter` machinery (`reschedule`). One ~10-20 ms round trip
per N requests, no DB locks held between leases.

Adaptive semantics mirror the local limiter, at lease grain: `penalize()`
multiplies the shared `penalty_factor` (x2, capped at 8x) and pushes
`next_slot_at`, so the OTHER runtime backs off on its next lease too; every
healthy lease decays the factor (x0.9, floor 1.0). The lease that follows a
penalize takes ONE slot, not a batch.

Bounded waits: a lease whose window starts more than MAX_WAIT_S away, or after
the caller's deadline, is refused without moving the frontier, and the limiter
raises RateBudgetUnavailable on that acquire and every later one. On 2026-09-29
ceskereality answered 403 to everything, each retry leased a fresh batch, the
frontier ran an hour ahead and every caller slept there: the realtime worker's
probe and drain threads never came back and starved its thread pool.

Failure posture: politeness must never depend on DB availability. Any DB error
in lease/penalize logs once and permanently falls back to pure-local pacing for
the rest of the run. A process that exits mid-window leaves its unused slots
leased (a few seconds of budget), which is the polite direction to err.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from scraper import db
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

# Amortizes the lease round trip (~10-20 ms to Frankfurt) across a drain's
# request stream; a low-volume probe caller can pass lease_n=1-5 instead.
DEFAULT_LEASE_N = 20

# Mirror RateLimiter's adaptive defaults so flipping a portal to the ledger
# keeps the same widen/decay behavior, just shared.
PENALTY_MULT = 2.0
PENALTY_CAP = 8.0
RECOVERY_FACTOR = 0.9

# The longest any caller waits for its leased window. At penalty 1x the frontier sits
# at most one lease per OTHER in-flight caller ahead: for the slowest ledger portal
# (0.5 req/s) with a walk, two drains and a probe leasing at once that is ~110 s, and
# ceskereality's is ~80 s, so a healthy caller is not refused. A window further out
# means the portal is pushing back (a penalty) or the budget is oversubscribed, and
# waiting through it only parks a thread. It is also the worker drain's whole budget
# (realtime_worker.DRAIN_MAX_SECONDS): a longer wait is never useful to that lane.
MAX_WAIT_S = 120.0


class RateBudgetUnavailable(Exception):
    """The shared ledger has no slot for this caller within MAX_WAIT_S or its deadline."""


_ENSURE_SQL = """
    INSERT INTO portal_rate_state (source, interval_ms)
    VALUES (%(source)s, %(interval_ms)s)
    ON CONFLICT (source) DO NOTHING
"""

# One statement leases N slots: window_start = the shared frontier (never in
# the past), the frontier advances by N pre-decay slot widths, and the caller
# gets back (seconds until its window starts, slot width, granted). A window
# starting more than max_wait_s away is NOT leased and the row is left as it
# was, so a refused caller pushes nobody else back. interval_ms is refreshed to
# the caller's config so an operator limits edit propagates.
_LEASE_SQL = """
    WITH cur AS (
        SELECT source,
               greatest(next_slot_at, now()) AS window_start,
               penalty_factor
          FROM portal_rate_state
         WHERE source = %(source)s
           FOR UPDATE
    ), leased AS (
        UPDATE portal_rate_state p
           SET next_slot_at   = cur.window_start + make_interval(
                   secs => %(n)s * %(interval_ms)s * cur.penalty_factor / 1000.0),
               interval_ms    = %(interval_ms)s,
               penalty_factor = greatest(1.0, cur.penalty_factor * %(decay)s),
               updated_at     = now()
          FROM cur
         WHERE p.source = cur.source
           AND cur.window_start <= now() + make_interval(secs => %(max_wait_s)s)
     RETURNING p.source
    )
    SELECT greatest(extract(epoch FROM (cur.window_start - now())), 0.0)::float8,
           (%(interval_ms)s * cur.penalty_factor / 1000.0)::float8,
           EXISTS (SELECT 1 FROM leased)
      FROM cur
"""

_PENALIZE_SQL = """
    UPDATE portal_rate_state
       SET penalty_factor = least(%(cap)s, penalty_factor * %(mult)s),
           next_slot_at   = greatest(next_slot_at, now()) + make_interval(
               secs => interval_ms * least(%(cap)s, penalty_factor * %(mult)s) / 1000.0),
           penalized_at   = now(),
           updated_at     = now()
     WHERE source = %(source)s
"""


class LedgerRateLimiter(RateLimiter):
    """RateLimiter whose budget lives in `portal_rate_state`, shared across
    runtimes. Duck-type identical (`acquire()` / `penalize()` / `interval`);
    the inherited state is the local between-slot pacer, re-aimed per lease."""

    def __init__(
        self,
        source: str,
        rate_per_s: float,
        *,
        lease_n: int = DEFAULT_LEASE_N,
        connect: Callable[[], Any] | None = None,
        deadline: float | None = None,
    ) -> None:
        if lease_n < 1:
            raise ValueError("lease_n must be >= 1")
        super().__init__(rate_per_s)
        self._source = source
        self._local_base = 1.0 / rate_per_s
        self._interval_ms = max(1, round(1000.0 / rate_per_s))
        self._lease_n = lease_n
        self._connect = connect or db.connect
        self._deadline = deadline
        self._conn: Any = None
        self._ensured = False
        self._slots_left = 0
        self._single_leases_owed = 0
        self._fallen_back = False
        # Separate from the inherited pacing lock: held across the lease round
        # trip so exactly one thread leases; never taken while holding _lock.
        self._lease_lock = threading.Lock()

    def acquire(self) -> None:
        with self._lease_lock:
            if self.refused:
                raise RateBudgetUnavailable(
                    f"{self._source}: the shared ledger already refused this run "
                    f"({self.refused})")
            if not self._fallen_back:
                if self._slots_left <= 0:
                    self._lease_locked()
                if not self._fallen_back:
                    self._slots_left -= 1
        super().acquire()

    def penalize(self) -> None:
        with self._lease_lock:
            if self._fallen_back:
                super().penalize()
                return
            # Drop the rest of the leased window so the next acquire re-leases
            # at the widened shared interval -- one slot for the retry, not a batch.
            self._slots_left = 0
            self._single_leases_owed += 1
            try:
                with self._cursor() as cur:
                    cur.execute(_PENALIZE_SQL, {
                        "source": self._source,
                        "mult": PENALTY_MULT,
                        "cap": PENALTY_CAP,
                    })
            except Exception as exc:  # noqa: BLE001 - any DB error -> local
                self._fall_back(exc)
                super().penalize()

    # --- internals ---

    def _cursor(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        cur = self._conn.cursor()
        if not self._ensured:
            try:
                cur.execute(_ENSURE_SQL, {
                    "source": self._source, "interval_ms": self._interval_ms,
                })
                self._ensured = True
            except Exception:
                cur.close()
                raise
        return cur

    def _max_wait(self) -> float:
        if self._deadline is None:
            return MAX_WAIT_S
        return max(0.0, min(MAX_WAIT_S, self._deadline - time.monotonic()))

    def _lease_locked(self) -> None:
        n = 1 if self._single_leases_owed else self._lease_n
        max_wait = self._max_wait()
        try:
            with self._cursor() as cur:
                cur.execute(_LEASE_SQL, {
                    "source": self._source,
                    "interval_ms": self._interval_ms,
                    "n": n,
                    "decay": RECOVERY_FACTOR,
                    "max_wait_s": max_wait,
                })
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("portal_rate_state row missing after ensure")
            delay_s, slot_s, granted = float(row[0]), float(row[1]), bool(row[2])
        except Exception as exc:  # noqa: BLE001 - any DB error -> local
            self._fall_back(exc)
            return
        if not granted:
            self.refused = "cap" if delay_s > MAX_WAIT_S else "deadline"
            if self.refused == "cap":
                LOG.warning(
                    "RATE budget refused source=%s: the next shared slot is %.0fs away "
                    "(cap %.0fs); giving up on this portal for this run",
                    self._source, delay_s, MAX_WAIT_S,
                )
            raise RateBudgetUnavailable(
                f"{self._source}: next shared slot {delay_s:.0f}s away, "
                f"bound {max_wait:.0f}s ({self.refused})")
        self._single_leases_owed = max(0, self._single_leases_owed - 1)
        self._slots_left = n
        self.reschedule(slot_s, time.monotonic() + delay_s)

    def _fall_back(self, exc: Exception) -> None:
        """One-way switch to pure-local pacing for the rest of the run."""
        LOG.warning(
            "RATE ledger unavailable source=%s; per-process pacing for the rest "
            "of the run: %r", self._source, exc,
        )
        self._fallen_back = True
        self._slots_left = 0
        with self._lock:
            # Restore the adaptive baseline reschedule() overrode; keep any
            # wider leased spacing so a known shared penalty decays, not snaps.
            self._base_interval = self._local_base
            self._interval = max(self._interval, self._local_base)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - teardown only
                pass
            self._conn = None


def build_rate_limiter(
    source: str,
    rate_per_s: float,
    shared: bool,
    *,
    lease_n: int = DEFAULT_LEASE_N,
    deadline: float | None = None,
) -> RateLimiter:
    """The runner's one-line seam: the plain per-process RateLimiter unless the
    portal's `shared_rate_limiter` limit flag is on (then the DB-backed ledger).
    `deadline` (a time.monotonic() instant) bounds the ledger's waits; the local
    limiter's are bounded by its own spacing and take none."""
    if not shared:
        return RateLimiter(rate_per_s)
    return LedgerRateLimiter(source, rate_per_s, lease_n=lease_n, deadline=deadline)
