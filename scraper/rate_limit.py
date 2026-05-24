"""Thread-safe global request-rate limiter for the scraper.

One instance shared across worker threads (and across the per-category
clients) caps the aggregate request rate, so parallel detail fetches stay
within a polite ceiling on the single egress IP. Adaptive: ``penalize()``
widens the interval on an HTTP 429/403, and it decays back toward the base
interval on subsequent healthy acquires — sustained throttling keeps the
penalty in place (it re-applies faster than it decays), a one-off blip
recovers quickly.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(
        self,
        rate_per_s: float,
        *,
        penalty_factor: float = 2.0,
        recovery_factor: float = 0.9,
        max_interval_mult: float = 8.0,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self._base_interval = 1.0 / rate_per_s
        self._interval = self._base_interval
        self._max_interval = self._base_interval * max_interval_mult
        self._penalty_factor = penalty_factor
        self._recovery_factor = recovery_factor
        self._lock = threading.Lock()
        self._next_at = 0.0  # time.monotonic() reference

    def acquire(self) -> None:
        """Block until this thread's slot; sleep happens outside the lock."""
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self._interval
            if self._interval > self._base_interval:
                self._interval = max(
                    self._base_interval, self._interval * self._recovery_factor
                )
            wait = scheduled - now
        if wait > 0:
            time.sleep(wait)

    def penalize(self) -> None:
        """Widen the interval after sreality signals we're going too fast."""
        with self._lock:
            self._interval = min(
                self._max_interval, self._interval * self._penalty_factor
            )

    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval
