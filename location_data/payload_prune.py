"""The scheduled half of P4 retention: a periodic sweep of the payload archive.

* `payloads.append_payload` already re-pins and caps — but only the ONE group it just
  wrote. A group that stops being re-appended keeps whatever pin set and depth its last
  append left behind, and a contradiction that opens or closes without a new fetch moves
  the pin answer with no append coming along to notice.
* So this lane re-asserts the same two statements table-wide, and adds the one thing the
  writer's count-based cap does not do at all: TIME-based eviction of unpinned bodies
  outside the hot window (`LOCATION_PAYLOAD_HOT_WINDOW_DAYS`).
* Scoped exclusively to `portal_raw_payloads`. Claims and resolutions are never pruned
  (02 §2.3.2 P4), and the legacy staging archive is read-only substrate for the whole
  program — `tests/test_portal_raw_pages_guard.py` is the CI gate on that, and
  `tests/location_data/test_payload_prune.py` asserts this module's only removal target
  structurally rather than by convention.
* SHIPS DISABLED, and the workflow carries a real weekly `schedule` — so the safety
  property lives here, not in the trigger: `main` seeds the ops-calendar row disabled,
  reads `location_jobs.enabled`, and returns before any lease attempt and before the
  archive is read at all (`ensure_lane`, and the note above `_ENSURE_LANE_SQL`).
* Design: 02 §2.3.2 P4.1/P4.2 · 01 §10.1 (`portal_raw_payloads` is APPEND-ON-CHANGE).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from location_data import loader_db, payloads
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.payload_prune")

PRUNER_VERSION = "payload_prune@1"
LANE = "location_payload_prune"
WAVE = "W2a"

# The ops-calendar row (02 §2.3.2 P4.2, 04 §4.4). The lane NAME is the calendar's, not the
# workflow's — `payload_archive_prune` is what the design doc schedules and what the
# operator flips.
JOB_NAME = "payload_archive_prune"
CADENCE = "7 days"

# The DB-level lease group, shared with the backfill: both lanes move bodies in the same
# table, and 01 §9.1's lease is what keeps them off each other. The GH concurrency groups
# (`location-batch` outer, `location-payload-prune` inner) are a separate mechanism.
CONCURRENCY_GROUP = "location-payload"
LEASE_TTL_S = 3600

# PLACEHOLDER PENDING OPERATOR SIGN-OFF. 02 §2.3.2 P4.2 names the "hot window" and
# deliberately leaves the number to the operator, so this is a conservative stand-in, not a
# decided policy: at 90 days nothing inside a quarter is ever evicted on age, and every
# pinned body (first, latest, claim-referenced, disputed) is exempt regardless. Set
# LOCATION_PAYLOAD_HOT_WINDOW_DAYS once the storage projection is signed.
HOT_WINDOW_ENV = "LOCATION_PAYLOAD_HOT_WINDOW_DAYS"
DEFAULT_HOT_WINDOW_DAYS = 90

STATEMENT_TIMEOUT_ENV = "LOCATION_PAYLOAD_PRUNE_TIMEOUT_S"
# Per GROUP, not per run. A group is bounded by the version cap (~20 rows), so this is
# roughly two orders of magnitude of headroom and only fires when something is wrong —
# never `0`, which is the unbounded state the 2026-08-10 boundary pack wedged under.
DEFAULT_STATEMENT_TIMEOUT_S = 120

# Both terminal stamps run under this. A one-row UPDATE by primary key never legitimately
# needs the batch budget, and whatever pressure would delay it is exactly what would strand
# the batch row at 'running'.
_STAMP_TIMEOUT_S = 30

# Distinct (source, source_id_native) keys per discovery page. The per-group work is a
# handful of index-served statements, so the page exists to bound the discovery scan's
# range, not the work.
MIN_KEY_PAGE = 50
MAX_KEY_PAGE = 20_000
DEFAULT_KEY_PAGE = 2_000

_RELATIONS = ("portal_raw_payloads", "location_jobs", "location_claim_batches")

_REGCLASS_SQL = "SELECT to_regclass(%(name)s)"

# The ops-calendar row, created DISABLED and never flipped by code.
#
# `lease.held` would create this row itself on first use — with `enabled = true`, because
# every other lane in the program is meant to run the moment it is deployed. This one is
# not: it ships behind the W2a storage gate with a live weekly cron already firing, so the
# row has to exist in the disabled state BEFORE anything can take the lease. Hence a seed
# of our own, ahead of `lease.held`, rather than the shared upsert. ON CONFLICT DO NOTHING
# keeps the operator's own `enabled = true` once they flip it.
_ENSURE_LANE_SQL = """
INSERT INTO location_jobs
    (job_name, cadence, concurrency_group, runner, enabled, note)
VALUES (%(job_name)s, %(cadence)s::interval, %(concurrency_group)s, 'github_actions',
        false, %(note)s)
ON CONFLICT (job_name) DO NOTHING
"""

_LANE_ENABLED_SQL = """
SELECT enabled FROM location_jobs WHERE job_name = %(job_name)s
"""

# One page of the keyset, over DISTINCT listings rather than rows: the cursor has to land
# on a whole (source, source_id_native), because a native id's page_kind groups are pruned
# independently and a page boundary drawn mid-native would skip the rest of it.
_KEYS_SQL = """
SELECT source, source_id_native
  FROM portal_raw_payloads
 WHERE (source, source_id_native) > (%(after_source)s::text, %(after_native)s::text)
   AND (%(source_filter)s::text IS NULL OR source = %(source_filter)s)
 GROUP BY source, source_id_native
 ORDER BY source, source_id_native
 LIMIT %(key_page)s::integer
"""

# Which groups inside that page can possibly lose a row. `count(*) > 1` is not an
# optimisation, it is the whole reason a full sweep is affordable: a single-version group's
# only row is simultaneously the first and the latest version, so it is pinned by
# definition and no cap or window can ever touch it — and after W2a-4's migration EVERY
# backfilled page is exactly such a group. Without this predicate the lane would open a
# transaction per 445k-row archive entry to delete nothing.
#
# The cold count is computed here rather than inferred from `min(last_observed_at)` so the
# same cutoff decides discovery and deletion; two spellings of "old" could disagree.
#
# `evictable` excludes the EDGE pins, and that exclusion is what keeps the sweep cheap in
# steady state: any listing tracked longer than the hot window has an old first version, so
# counting it as cold would select the group for a full transaction — re-pin, two DELETEs,
# an orphan lookup — that then removes nothing, because `repin_group` pins that row. The
# claim / contradiction pins are deliberately NOT modelled here: they need the claim store,
# they are rare, and over-selecting is only wasted work, never a wrong result.
#
# A NULL `version_seq` counts as evictable, matching `_REPIN_SQL`'s `coalesce(..., false)`
# treatment of it as unpinned — otherwise a version-less row could never be discovered.
_GROUPS_SQL = """
WITH scoped AS (
    SELECT source, source_id_native, page_kind,
           (last_observed_at < %(cutoff)s
            AND coalesce(version_seq NOT IN (min(version_seq) OVER grp,
                                             max(version_seq) OVER grp), true)) AS evictable
      FROM portal_raw_payloads
     WHERE (source, source_id_native) > (%(after_source)s::text, %(after_native)s::text)
       AND (source, source_id_native) <= (%(through_source)s::text,
                                          %(through_native)s::text)
       AND (%(source_filter)s::text IS NULL OR source = %(source_filter)s)
    WINDOW grp AS (PARTITION BY source, source_id_native, page_kind)
)
SELECT source, source_id_native, page_kind::text, count(*) AS versions,
       count(*) FILTER (WHERE evictable) AS cold
  FROM scoped
 GROUP BY source, source_id_native, page_kind
HAVING count(*) > 1
   AND (count(*) > %(version_cap)s::integer OR count(*) FILTER (WHERE evictable) > 0)
 ORDER BY source, source_id_native, page_kind
"""

# The genuinely new statement: eviction by AGE, which the writer's count-based cap does not
# express at all. It reads `pinned` and never recomputes it — `payloads.repin_group` runs
# first in the same transaction, so "pinned" means exactly one thing here and in the cap.
#
# `last_observed_at`, not `first_observed_at`: the question is how long since anything saw
# this content, and an unchanged refetch bumps only the former. A body first archived in
# May but still being re-observed is hot; one that has not been served since May is cold.
#
# The FK is the reason `NOT p.pinned` is load-bearing rather than merely polite:
# `location_claims.payload_id` references this table with NO ACTION (migration 382), so an
# age-based removal of a referenced body would raise ForeignKeyViolation and roll the whole
# group's transaction back. The re-pin ahead of it is what makes that unreachable.
#
# `stored_byte_size` (migration 405) BEFORE `octet_length(body)`, in that order, because
# since W2a-7 the body is in the bucket by default and the inline column is NULL on
# essentially every row. Reading only the inline length used to be defensible ("the bytes
# reclaimed IN POSTGRES are the ones this statement frees"); with R2 as the bodies' home it
# would report every sweep as freeing zero, on the single figure this lane exists to
# produce. The coalesce keeps a pre-405 row answering with what it does have.
_HOT_WINDOW_SQL = """
DELETE FROM portal_raw_payloads p
 WHERE p.source = %(source)s
   AND p.source_id_native = %(source_id_native)s
   AND p.page_kind = %(page_kind)s::location_page_kind
   AND NOT p.pinned
   AND p.last_observed_at < %(cutoff)s
RETURNING p.id, p.body_r2_key, p.byte_size,
          coalesce(p.stored_byte_size, octet_length(p.body))
"""

_BATCH_INSERT_SQL = """
INSERT INTO location_claim_batches
    (lane, source, extractor_version, wave, job_run_id, outcome, note, scan_mode, resumable)
VALUES (%(lane)s, %(source)s, %(extractor_version)s, %(wave)s, %(job_run_id)s, 'running',
        %(note)s, 'full', false)
RETURNING id
"""

_BATCH_FINISH_SQL = """
UPDATE location_claim_batches
   SET finished_at = now(), outcome = %(outcome)s, row_count = %(row_count)s,
       note = concat_ws(' | ', note, %(note)s::text)
 WHERE id = %(batch_id)s
"""


class PruneRefused(RuntimeError):
    """A precondition failed; no batch row was opened and nothing was removed."""


def hot_window_cutoff(days: int, *, now: datetime | None = None) -> datetime:
    """The instant before which an unpinned body is cold. Pure, so the arithmetic is testable."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def hot_window_days() -> int:
    return loader_db.env_positive_int(HOT_WINDOW_ENV, DEFAULT_HOT_WINDOW_DAYS)


def missing_relations(conn: psycopg.Connection) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in _RELATIONS:
            cur.execute(_REGCLASS_SQL, {"name": name})
            row = cur.fetchone()
            if row is None or row[0] is None:
                missing.append(name)
    return missing


def require_relations(conn: psycopg.Connection) -> None:
    """Refuse cleanly on an unmigrated database, BEFORE anything else touches it.

    * `location_jobs` is one of the three checked relations, and the lane seed writes to
      it — so this has to run ahead of `ensure_lane`, or the INSERT raises `UndefinedTable`
      and the operator gets a traceback instead of the refusal this message exists to be.
    """
    missing = missing_relations(conn)
    if missing:
        raise PruneRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-387 and 403)")


def ensure_lane(conn: psycopg.Connection, *, statement_timeout: int) -> None:
    """Create the ops-calendar row DISABLED if it does not exist yet.

    * Idempotent, and it never re-disables a lane the operator has enabled.
    * Must run before any `lease.held` call: the shared upsert there would create the same
      row with `enabled = true`.
    """
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_ENSURE_LANE_SQL, {
            "job_name": JOB_NAME,
            "cadence": CADENCE,
            "concurrency_group": CONCURRENCY_GROUP,
            "note": "W2a-5 P4 pruner. Ships disabled; enabling is gated on the W2a "
                    "storage sign-off and a decided LOCATION_PAYLOAD_HOT_WINDOW_DAYS.",
        })


def lane_enabled(conn: psycopg.Connection, *, statement_timeout: int) -> bool:
    """Whether the operator has turned this lane on. A missing row reads as disabled."""
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_LANE_ENABLED_SQL, {"job_name": JOB_NAME})
        row = cur.fetchone()
    return bool(row[0]) if row else False


def prune_one_group(
    conn: psycopg.Connection,
    *,
    source: str,
    source_id_native: str,
    page_kind: str,
    version_cap: int,
    cutoff: datetime,
    statement_timeout: int,
) -> dict[str, Any]:
    """Re-pin, re-assert the cap, then evict outside the hot window — one transaction.

    * The order is the invariant: `repin_group` is authoritative, so both removals below
      read a pin set computed against the CURRENT claims and contradictions rather than
      against whatever the last append left.
    """
    with loader_db.bounded(conn, statement_timeout) as cur:
        payloads.repin_group(
            cur, source=source, source_id_native=source_id_native, page_kind=page_kind)
        capped = payloads.prune_group(
            cur, source=source, source_id_native=source_id_native, page_kind=page_kind,
            version_cap=version_cap)
        cur.execute(_HOT_WINDOW_SQL, {
            "source": source,
            "source_id_native": source_id_native,
            "page_kind": page_kind,
            "cutoff": cutoff,
        })
        cold = [payloads.EvictedBody(*row) for row in cur.fetchall()]
        # BOTH paths, not just the window. The cap is the majority of a first sweep — a
        # listing's fetch history is far deeper than 20 versions — so summing only the
        # cold rows understated the one number this lane exists to report.
        evicted = capped + cold
        orphaned = payloads.orphaned_r2_keys(
            cur, [row.r2_key for row in evicted if row.r2_key])
    return {
        "capped": len(capped),
        "cold": len(cold),
        # `byte_size` is the body as fetched; `stored_bytes` is the encoded size, which
        # since W2a-7 is R2 bytes rather than Postgres bytes for all but the smallest
        # bodies. Reporting both is the difference between "how much archive was dropped"
        # and "how much storage came back".
        "bytes_uncompressed": sum(row.byte_size for row in evicted),
        "bytes_freed": sum(
            row.stored_bytes for row in evicted if row.stored_bytes is not None),
        "r2_keys_orphaned": orphaned,
    }


def run(
    conn: psycopg.Connection,
    *,
    source: str | None,
    version_cap: int,
    hot_window: int,
    key_page: int,
    max_seconds: float | None,
    max_groups: int | None,
    start_after_source: str,
    start_after_native: str,
    statement_timeout: int,
    dry_run: bool,
    note: str | None,
) -> dict[str, Any]:
    require_relations(conn)

    cutoff = hot_window_cutoff(hot_window)
    batch_id: int | None = None
    if not dry_run:
        with loader_db.bounded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source,
                "extractor_version": PRUNER_VERSION, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("PRUNE start source=%s cap=%d hot_window_days=%d cutoff=%s batch_id=%s "
             "dry_run=%s", source or "*", version_cap, hot_window, cutoff.isoformat(),
             batch_id, dry_run)

    started = time.monotonic()
    after = (start_after_source, start_after_native)
    stats: dict[str, Any] = {
        "keys_scanned": 0, "groups_examined": 0, "groups_changed": 0,
        "rows_capped": 0, "rows_cold": 0, "bytes_freed": 0, "bytes_uncompressed": 0,
        "r2_keys_orphaned": 0, "group_failures": 0,
        "stopped_early": False, "reached_end": False,
        "hot_window_days": hot_window, "version_cap": version_cap,
        "cutoff": cutoff.isoformat(),
    }
    orphaned_keys: list[str] = []
    try:
        while True:
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                LOG.info("PRUNE stopping: --max-seconds reached")
                stats["stopped_early"] = True
                break
            if max_groups is not None and stats["groups_examined"] >= max_groups:
                LOG.info("PRUNE stopping: --max-groups reached")
                stats["stopped_early"] = True
                break

            with loader_db.bounded(conn, statement_timeout) as cur:
                cur.execute(_KEYS_SQL, {
                    "after_source": after[0], "after_native": after[1],
                    "source_filter": source, "key_page": key_page,
                })
                keys = cur.fetchall()
            if not keys:
                stats["reached_end"] = True
                break
            through = (str(keys[-1][0]), str(keys[-1][1]))

            with loader_db.bounded(conn, statement_timeout) as cur:
                cur.execute(_GROUPS_SQL, {
                    "after_source": after[0], "after_native": after[1],
                    "through_source": through[0], "through_native": through[1],
                    "source_filter": source, "cutoff": cutoff,
                    "version_cap": version_cap,
                })
                groups = cur.fetchall()

            stats["keys_scanned"] += len(keys)
            for grp_source, grp_native, grp_kind, _versions, _cold in groups:
                stats["groups_examined"] += 1
                if dry_run:
                    continue
                try:
                    result = prune_one_group(
                        conn, source=str(grp_source), source_id_native=str(grp_native),
                        page_kind=str(grp_kind), version_cap=version_cap, cutoff=cutoff,
                        statement_timeout=statement_timeout)
                except Exception as exc:  # noqa: BLE001 - one group must not end the sweep
                    # A weekly sweep of the whole archive that dies on its third group has
                    # done nothing for the other 400k. The count is stamped on the batch
                    # row, so a systematic fault is visible rather than merely survived.
                    stats["group_failures"] += 1
                    LOG.warning("PRUNE group failed source=%s native=%s kind=%s: %s",
                                grp_source, grp_native, grp_kind, exc)
                    continue
                stats["rows_capped"] += result["capped"]
                stats["rows_cold"] += result["cold"]
                stats["bytes_freed"] += result["bytes_freed"]
                stats["bytes_uncompressed"] += result["bytes_uncompressed"]
                orphaned_keys.extend(result["r2_keys_orphaned"])
                if result["capped"] or result["cold"]:
                    stats["groups_changed"] += 1

            after = through
            LOG.info("PRUNE progress keys=%d groups=%d capped=%d cold=%d mb_freed=%.1f "
                     "through=%s/%s", stats["keys_scanned"], stats["groups_examined"],
                     stats["rows_capped"], stats["rows_cold"],
                     stats["bytes_freed"] / 1e6, after[0], after[1])

        stats["r2_keys_orphaned"] = len(orphaned_keys)
        outcome = "ok" if stats["reached_end"] else "stopped"
        stats["outcome"] = outcome
        if orphaned_keys:
            # Reported, not deleted. `payloads.ObjectStore` has no delete verb on purpose,
            # and reclaiming bucket objects is a separate decision from reclaiming rows.
            LOG.info("PRUNE %d R2 object(s) are now unreferenced: %s",
                     len(orphaned_keys), ", ".join(sorted(orphaned_keys)[:20]))
        if batch_id is not None:
            with loader_db.bounded(conn, _STAMP_TIMEOUT_S) as cur:
                cur.execute(_BATCH_FINISH_SQL, {
                    "batch_id": batch_id,
                    "outcome": outcome,
                    "row_count": stats["rows_capped"] + stats["rows_cold"],
                    "note": f"keys={stats['keys_scanned']} "
                            f"groups={stats['groups_examined']} "
                            f"changed={stats['groups_changed']} "
                            f"capped={stats['rows_capped']} cold={stats['rows_cold']} "
                            f"bytes_freed={stats['bytes_freed']} "
                            f"bytes_uncompressed={stats['bytes_uncompressed']} "
                            f"r2_orphaned={stats['r2_keys_orphaned']} "
                            f"failures={stats['group_failures']} "
                            f"hot_window_days={hot_window} cap={version_cap} "
                            f"reached_end={stats['reached_end']} "
                            f"through={after[0]}/{after[1]}",
                })
    except Exception as exc:
        if batch_id is not None:
            try:
                with loader_db.bounded(conn, _STAMP_TIMEOUT_S) as cur:
                    cur.execute(_BATCH_FINISH_SQL, {
                        "batch_id": batch_id, "outcome": "failed",
                        "row_count": stats["rows_capped"] + stats["rows_cold"],
                        "note": f"{type(exc).__name__}: {exc}"[:500],
                    })
            except Exception:  # noqa: BLE001 - never mask the exception being reported
                LOG.exception("PRUNE could not stamp batch %s as failed", batch_id)
        raise

    stats["batch_id"] = batch_id
    stats["through_source"], stats["through_native"] = after
    return stats


def _explicit_or_env(flag: str, value: int | None, env: str, default: int) -> int:
    """An explicitly passed retention budget wins over the env; a non-positive one is refused.

    * `is not None`, never `or`: 0 is falsy, so `value or fallback` would silently replace
      an operator's explicit zero with the default and run under a budget they did not ask
      for — the pattern `payloads.append_payload` already avoids.
    * Non-positive is REFUSED rather than floored the way `env_positive_int` floors an
      ambient env var. This lane removes rows, so a typo'd budget has to fail loudly
      instead of quietly running under a different one, and nothing is lost by refusing:
      `--version-cap 1` already means "keep only the pins", because rank 1 is the latest
      version and the latest version is always pinned.
    """
    if value is None:
        return loader_db.env_positive_int(env, default)
    if value <= 0:
        raise PruneRefused(
            f"{flag}={value} is not positive. This lane deletes rows, so a non-positive "
            f"budget is refused rather than quietly replaced by ${env}. For the strictest "
            f"legal retention pass `{flag} 1` — the pins (first version, latest version, "
            f"claim-referenced and disputed bodies) survive either way.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-assert the payload archive's version cap and evict unpinned "
                    "bodies outside the hot window (W2a, 02 §2.3.2 P4).")
    parser.add_argument("--source", default=None, help="one portal only")
    parser.add_argument("--version-cap", type=int, default=None,
                        help=f"default: ${payloads.VERSION_CAP_ENV} or "
                             f"{payloads.DEFAULT_VERSION_CAP}")
    parser.add_argument("--hot-window-days", type=int, default=None,
                        help=f"default: ${HOT_WINDOW_ENV} or {DEFAULT_HOT_WINDOW_DAYS}")
    parser.add_argument("--key-page", type=int, default=DEFAULT_KEY_PAGE)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--max-groups", type=int, default=None)
    parser.add_argument("--start-after-source", default="")
    parser.add_argument("--start-after-native", default="")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--dry-run", action="store_true",
                        help="count the groups that would be visited; remove nothing, "
                             "open no batch row, take no lease")
    parser.add_argument("--note", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    try:
        version_cap = _explicit_or_env(
            "--version-cap", args.version_cap,
            payloads.VERSION_CAP_ENV, payloads.DEFAULT_VERSION_CAP)
        hot_window = _explicit_or_env(
            "--hot-window-days", args.hot_window_days,
            HOT_WINDOW_ENV, DEFAULT_HOT_WINDOW_DAYS)
    except PruneRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    kwargs: dict[str, Any] = {
        "source": args.source, "version_cap": version_cap, "hot_window": hot_window,
        "key_page": max(MIN_KEY_PAGE, min(MAX_KEY_PAGE, args.key_page)),
        "max_seconds": args.max_seconds, "max_groups": args.max_groups,
        "start_after_source": args.start_after_source,
        "start_after_native": args.start_after_native,
        "statement_timeout": args.statement_timeout, "dry_run": args.dry_run,
        "note": args.note,
    }

    with db.connect() as conn:
        try:
            # THE SAFETY SEQUENCE, and the reason a live weekly cron is safe to ship. The
            # schema is checked, the ops-calendar row is created disabled, the flag is
            # read, and a disabled lane returns HERE — before the lease, before the keyset,
            # before the archive is read at all. `lease._ACQUIRE_SQL` carries `AND enabled`
            # as the second, independent rail: even if this gate were bypassed, a disabled
            # lane cannot take the lease and `run` is never reached.
            #
            # The preflight leads, because the seed below WRITES to one of the relations it
            # checks: on an unmigrated database its INSERT would raise UndefinedTable and
            # replace the refusal with a traceback.
            require_relations(conn)
            ensure_lane(conn, statement_timeout=args.statement_timeout)
            if not lane_enabled(conn, statement_timeout=args.statement_timeout):
                LOG.info("PRUNE lane %s is disabled; nothing scanned, nothing removed. "
                         "Enable it with UPDATE location_jobs SET enabled = true WHERE "
                         "job_name = '%s' once the W2a storage gate is signed and "
                         "%s is decided.", JOB_NAME, JOB_NAME, HOT_WINDOW_ENV)
                return 0
            if args.dry_run:
                # No lease on a dry run: it removes nothing, so there is nothing to
                # serialise against, and releasing the lease as 'ok' would stamp
                # `last_success_at` for a sweep that reclaimed nothing.
                LOG.info("PRUNE dry run: not taking the %s lease", JOB_NAME)
                stats = run(conn, **kwargs)
            else:
                with lease.held(
                    conn, JOB_NAME, cadence=CADENCE,
                    concurrency_group=CONCURRENCY_GROUP, ttl_seconds=LEASE_TTL_S,
                ) as acquired:
                    if not acquired:
                        LOG.info("PRUNE skipped: another run holds the %s lease", JOB_NAME)
                        return 0
                    stats = run(conn, **kwargs)
        except PruneRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    LOG.info("PRUNE done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
