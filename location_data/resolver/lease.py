"""Lease-row CAS for the location lanes (03 §3.14.3 rule 3, 01 §9.1).

**Never a session advisory lock.** Every service-role path in this repo uses the
transaction-mode pooler, where a lock taken on one backend and released on another silently
strands. `location_jobs.lease_holder` / `lease_expires_at` are the CAS row: acquiring is a
conditional UPDATE, a crashed run's lease simply expires, and two runs of one concurrency
group never overlap.

`location_jobs` ships with no seeded rows (01 §9.1 owns the shape, 04 owns the contents), so
`acquire` INSERTs the lane row on first use with its canonical name, cadence and
concurrency group from 00 §14.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from contextlib import contextmanager

import psycopg

LOG = logging.getLogger("location_data.resolver.lease")

_UPSERT_JOB_SQL = """
INSERT INTO location_jobs (job_name, cadence, concurrency_group, runner, enabled)
VALUES (%s, %s::interval, %s, %s, true)
ON CONFLICT (job_name) DO NOTHING
"""

_ACQUIRE_SQL = """
UPDATE location_jobs
   SET lease_holder = %s,
       lease_expires_at = now() + make_interval(secs => %s),
       last_started_at = now(),
       last_outcome = 'running'
 WHERE job_name = %s
   AND enabled
   AND (lease_holder IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())
RETURNING job_name
"""

_RELEASE_SQL = """
UPDATE location_jobs
   SET lease_holder = NULL,
       lease_expires_at = NULL,
       last_outcome = %s,
       last_error = %s,
       last_success_at = CASE WHEN %s = 'ok' THEN now() ELSE last_success_at END,
       consecutive_failures = CASE WHEN %s = 'ok' THEN 0 ELSE consecutive_failures + 1 END
 WHERE job_name = %s AND lease_holder = %s
"""


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@contextmanager
def held(
    conn: psycopg.Connection,
    job_name: str,
    *,
    cadence: str,
    concurrency_group: str,
    runner: str = "github_actions",
    ttl_seconds: int = 3600,
):
    """Yields True when this run owns the lane, False when another run holds it."""
    holder = holder_id()
    with conn.cursor() as cur:
        cur.execute(_UPSERT_JOB_SQL, (job_name, cadence, concurrency_group, runner))
        cur.execute(_ACQUIRE_SQL, (holder, ttl_seconds, job_name))
        acquired = cur.fetchone() is not None
    if not acquired:
        LOG.info("LEASE busy job=%s", job_name)
        yield False
        return
    outcome, error = "ok", None
    try:
        yield True
    except Exception as exc:  # noqa: BLE001 - the lease must be released either way
        outcome, error = "failed", str(exc)[:500]
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute(_RELEASE_SQL, (outcome, error, outcome, outcome, job_name, holder))
