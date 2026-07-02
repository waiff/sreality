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
healthy lease decays the factor (x0.9, floor 1.0).

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

_ENSURE_SQL = """
    INSERT INTO portal_rate_state (source, interval_ms)
    VALUES (%(source)s, %(interval_ms)s)
    ON CONFLICT (source) DO NOTHING
"""

# One statement leases N slots: window_start = the shared frontier (never in
# the past), the frontier advances by N pre-decay slot widths, and the caller
# gets back (seconds until its window starts, slot width). interval_ms is
# refreshed to the caller's config so an operator limits edit propagates.
_LEASE_SQL = """
    WITH cur AS (
        SELECT source,
               greatest(next_slot_at, now()) AS window_start,
               penalty_factor
          FROM portal_rate_state
         WHERE source = %(source)s
           FOR UPDATE
    )
    UPDATE portal_rate_state p
       SET next_slot_at   = cur.window_start + make_interval(
               secs => %(n)s * %(interval_ms)s * cur.penalty_factor / 1000.0),
           interval_ms    = %(interval_ms)s,
           penalty_factor = greatest(1.0, cur.penalty_factor * %(decay)s),
           updated_at     = now()
      FROM cur
     WHERE p.source = cur.source
 RETURNING greatest(extract(epoch FROM (cur.window_start - now())), 0.0)::float8,
           (%(interval_ms)s * cur.penalty_factor / 1000.0)::float8
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
    ) -> None:
        if lease_n < 1:
            raise ValueError("lease_n must be >= 1")
        super().__init__(rate_per_s)
        self._source = source
        self._local_base = 1.0 / rate_per_s
        self._interval_ms = max(1, round(1000.0 / rate_per_s))
        self._lease_n = lease_n
        self._connect = connect or db.connect
        self._conn: Any = None
        self._ensured = False
        self._slots_left = 0
        self._fallen_back = False
        # Separate from the inherited pacing lock: held across the lease round
        # trip so exactly one thread leases; never taken while holding _lock.
        self._lease_lock = threading.Lock()

    def acquire(self) -> None:
        with self._lease_lock:
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
            # at the widened shared interval.
            self._slots_left = 0
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

    def _lease_locked(self) -> None:
        try:
            with self._cursor() as cur:
                cur.execute(_LEASE_SQL, {
                    "source": self._source,
                    "interval_ms": self._interval_ms,
                    "n": self._lease_n,
                    "decay": RECOVERY_FACTOR,
                })
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("portal_rate_state row missing after ensure")
            delay_s, slot_s = float(row[0]), float(row[1])
        except Exception as exc:  # noqa: BLE001 - any DB error -> local
            self._fall_back(exc)
            return
        self._slots_left = self._lease_n
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
) -> RateLimiter:
    """The runner's one-line seam: the plain per-process RateLimiter unless the
    portal's `shared_rate_limiter` limit flag is on (then the DB-backed ledger)."""
    if not shared:
        return RateLimiter(rate_per_s)
    return LedgerRateLimiter(source, rate_per_s, lease_n=lease_n)
