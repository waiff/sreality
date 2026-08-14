"""Migrate the legacy `portal_raw_pages` archive into the W2a payload store (06 §6.4).

`portal_raw_pages` is latest-wins: `UNIQUE (source, source_id_native, page_kind)`, one row
per page, ~445k rows / 14 GB, and nothing has ever been pruned from it (its oldest row per
source is that portal's onboarding date). So each row is **exactly one body** — 06 §6.4
says so in as many words — and this lane is a straight historical 1:1 copy, not the
content-addressed merge `payloads.append_payload` performs across many versions of one
page. That is why it writes through its own batched INSERT rather than calling the writer:
there is no prior version to collide with, no version to evict, and 445,191 single-row
transactions with a re-pin and a cap each would be the wrong shape entirely.

Each migrated row lands as its listing's FIRST and LATEST body at once — `version_seq = 1`,
`pinned = true`, `first_observed_at = last_observed_at = fetched_at = the page's own
fetched_at` (06 Rule 1: a backfilled body keeps the time it was really fetched and must
never read as having appeared on migration day).

WHAT THIS LANE MAY DO TO ITS SOURCE: read it. Nothing else, ever. `portal_raw_pages` holds
the only surviving copy of several portals' best location signal for listings that are now
delisted — portals do not serve a delisted page again — which is why
`tests/test_portal_raw_pages_guard.py` is a CI gate rather than a convention. A DELETE here
would be permanent data loss, and the fact that the target table's name merely *resembles*
the source's is the sharpest reason to keep the two straight.

Resumability follows the claim intake verbatim (migration 387), because the failure it
closes is the same one: a budgeted run must not be able to claim ground it never covered.
The lane keeps its position on a `location_claim_batches` row and `outcome='ok'` means ONE
thing — the keyset ran off the end of the table. A `--max-seconds` stop stamps `'stopped'`,
which is the only state a later run resumes from, and only from a run of the same
`scan_mode` that was not anchored at an operator-chosen `--start-after-id`.

DISPATCH-GATED. Building this is not running it: the 445k-row migration waits on the
operator's `volatile_paths` decision and a signed storage projection (BUILD-PLAN §6 item 2,
O3/O4). The workflow is `workflow_dispatch`-only with no `schedule` for exactly that reason.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg

from location_data import loader_db, payloads
from location_data.payload_norm import (
    NORMALIZER_VERSION,
    normalise,
    resolve_normalisation,
    sniff_content_type,
)
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.payload_backfill")

BACKFILL_VERSION = "payload_backfill@1"
LANE = "location_payload_backfill"
WAVE = "W2a"

# The normaliser is PART of this lane's identity, not an implementation detail of it.
# `payload_sha256` is taken over the normalised body, so a `NORMALIZER_VERSION` bump moves
# the content address of unchanged content — and this lane is designed to be re-dispatched
# until it reports `reached_end`. Stamping the pair on the batch row is what lets a later
# run see that the ground in front of it was migrated under a different normaliser and
# refuse to duplicate it (see `_prior_progress`).
#
# The BATCH stamp stays the module version, unqualified, while each ROW carries
# `normalizer_version_for(source, page_kind)`: one scan covers every surface in
# portal_raw_pages, so "which normaliser did this batch run under" has no per-surface
# answer, and the per-surface answer belongs on the rows — where the content address it
# explains actually lives.
EXTRACTOR_VERSION = f"{BACKFILL_VERSION}+{NORMALIZER_VERSION}"

# A bare `portal_raw_pages.id` keyset, which is what migration 387 calls 'full'. The
# `scan_mode` is stored so a cursor can never be resumed by a scan that means something
# different by it; this lane only has the one mode, and stamping it keeps the guard honest
# if a second one is ever added.
SCAN_MODE = "full"

JOB_NAME = "location_payload_backfill"
CONCURRENCY_GROUP = "location-payload"

# A one-shot migration has no cadence, but `location_jobs.cadence` is NOT NULL and
# `location_jobs_stale` (migration 384) alerts on `now() - last_success_at > 3 x cadence`.
# A short interval here would page the operator forever about a lane that is finished by
# design; a year is the honest spelling of "not on a schedule".
CADENCE = "365 days"
LEASE_TTL_S = 3600

STATEMENT_TIMEOUT_ENV = "LOCATION_PAYLOAD_BACKFILL_TIMEOUT_S"
# Per BATCH, not per run, and deliberately not `0`. `loader_db`'s session GUC of
# `statement_timeout = 0` is right for a genuine bulk phase (a COPY, an index build); this
# is a batched incremental migration, where an unbounded statement is how a lane wedges for
# two hours without emitting a line (the 2026-08-10 boundary pack). 300 s is roughly 10x
# the observed cost of reading and inserting a default batch of large TOASTed bodies, so it
# only fires when something is genuinely wrong.
DEFAULT_STATEMENT_TIMEOUT_S = 300

# Both TERMINAL stamps run under this, not under the batch budget. A one-row UPDATE by
# primary key never legitimately needs five minutes, and the ceiling has to be short for
# the same reason the failure stamp's is: whatever pressure is delaying it is exactly what
# would turn a hung bookkeeping write into a batch row stranded at 'running' — invisible to
# `_RESUME_SQL`, so the next dispatch silently restarts the whole scan from id 0.
_STAMP_TIMEOUT_S = 30

# Bodies here are whole pages (bazos 41 KB ... mmreality 245 KB), so a batch is sized in
# megabytes rather than rows: 200 x 245 KB is a ~49 MB worst-case round trip, which is a
# comfortable statement and a comfortable resident set. The claim intake's 20,000-row
# batches read a JSONB column two orders of magnitude smaller.
MIN_BATCH_SIZE = 25
MAX_BATCH_SIZE = 1_000
DEFAULT_BATCH_SIZE = 200

# Concurrent R2 PUTs per batch. The whole corpus is ~445k objects and a round trip to
# Frankfurt is tens of milliseconds, so serial uploads would make the network, not the
# database, this migration's clock: six hours of pure latency. 16 keeps a default batch
# of 200 under a couple of seconds without out-running `R2Client`'s connection pool.
UPLOAD_WORKERS = 16

# `portal_raw_pages.page_kind` is free text under `check (page_kind in ('index','detail'))`
# (migration 099); `portal_raw_payloads.page_kind` is the `location_page_kind` ENUM, whose
# labels are ('index','detail','map','gazetteer','snapshot','archive','none') (migration
# 380). The two names that exist on the source side happen to coincide with enum labels,
# but the mapping is written out rather than assumed: the source is a CHECK constraint that
# a later migration can widen without the enum gaining a matching label, and an unmapped
# value must be visible as a skipped row rather than an `InvalidTextRepresentation` that
# kills a 445k-row migration mid-flight.
PAGE_KIND_MAP = {"index": "index", "detail": "detail"}

_RELATIONS = ("portal_raw_pages", "portal_raw_payloads", "location_claim_batches")

_REGCLASS_SQL = "SELECT to_regclass(%(name)s)"

# `source` alone is an index-served prefix of `portal_raw_pages_key`
# UNIQUE (source, source_id_native, page_kind), so this costs one index probe.
_SOURCE_PRESENT_SQL = """
SELECT 1 FROM portal_raw_pages WHERE source = %(source)s LIMIT 1
"""

# `convert_to(html, 'UTF8')` rather than handing psycopg the `text` and encoding it in
# Python: the archived artefact must be the bytes Postgres actually holds, and the
# round-trip verifier reads its side of the comparison through this identical expression —
# so "byte-for-byte" is symmetric by construction rather than by two matching assumptions
# about client encoding.
#
# No `parsed_at` / `parse_error` filter: an unparsed or failed page is still a page, and
# for a delisted listing it is the only copy of its location signal that will ever exist.
_SOURCE_ROWS_SQL = """
SELECT id, source, source_id_native, page_kind, convert_to(html, 'UTF8'),
       http_status, fetched_at
  FROM portal_raw_pages
 WHERE id > %(after_id)s
   AND (%(source)s::text IS NULL OR source = %(source)s)
 ORDER BY id
 LIMIT %(batch_size)s
"""

# One statement per batch, arrays in and ids out.
#
# `version_seq = 1` and `pinned = true` are literals because the source table is
# latest-wins: the row being copied is the only body that page has, so it is simultaneously
# the first version and the latest one, and both P4 edge-pins apply to it. (Should a group
# ever already carry a live-path body — only possible if `payload_dual_write` is enabled
# BEFORE this lane runs, the reverse of the sequenced order — the two rows tie at
# version_seq 1 and `payloads._REPIN_SQL` pins both as first and latest. Over-pinned, never
# under-pinned: the cap can still never evict real history.)
#
# `content_encoding` rides in as a column rather than as a literal: it comes back from
# `payloads.encode_body(..., gzip_min_bytes=0)`, the SAME encoder the live writer uses, so
# the two paths cannot drift on compression level or reproducibility. At that threshold
# every non-empty body gzips — these are whole pages, not fragments, so the storage
# projection the operator signs is still effectively one number — but a zero-length body
# honestly reports 'identity' instead of being labelled as a gzip member that
# `decode_body` would then refuse to inflate.
#
# ON CONFLICT DO NOTHING, never DO UPDATE: a re-run after a crash between the INSERT and
# the cursor stamp must be a no-op, and if a live-path row already holds this exact
# normalised content then that row is the better record (it may carry a snapshot_id and a
# contract_version this lane has neither of). Rows inserted come back through RETURNING, so
# `inserted` is a count of real work rather than of rows offered.
_INSERT_SQL = """
INSERT INTO portal_raw_payloads
    (source, source_id_native, listing_id, page_kind, payload_sha256, body_sha256,
     content_type, content_encoding, body, body_r2_key, byte_size, stored_byte_size,
     http_status, contract_version, normalizer_version, snapshot_id, pinned, version_seq,
     first_observed_at, last_observed_at, fetched_at)
SELECT r.source, r.source_id_native, NULL::bigint, r.page_kind::location_page_kind,
       r.payload_sha256, r.body_sha256, r.content_type, r.content_encoding, r.body,
       r.body_r2_key, r.byte_size, r.stored_byte_size,
       r.http_status, NULL::integer, r.normalizer_version,
       NULL::bigint, true, 1, r.fetched_at, r.fetched_at, r.fetched_at
  FROM unnest(%(source)s::text[], %(source_id_native)s::text[], %(page_kind)s::text[],
              %(payload_sha256)s::bytea[], %(body_sha256)s::bytea[],
              %(content_type)s::text[], %(content_encoding)s::text[], %(body)s::bytea[],
              %(body_r2_key)s::text[], %(byte_size)s::integer[],
              %(stored_byte_size)s::integer[], %(http_status)s::integer[],
              %(fetched_at)s::timestamptz[], %(normalizer_version)s::text[])
    AS r(source, source_id_native, page_kind, payload_sha256, body_sha256, content_type,
         content_encoding, body, body_r2_key, byte_size, stored_byte_size, http_status,
         fetched_at, normalizer_version)
ON CONFLICT (source, source_id_native, page_kind, payload_sha256) DO NOTHING
RETURNING id
"""

_BATCH_INSERT_SQL = """
INSERT INTO location_claim_batches
    (lane, source, extractor_version, wave, job_run_id, outcome, note, scan_mode, resumable)
VALUES (%(lane)s, %(source)s, %(extractor_version)s, %(wave)s, %(job_run_id)s, 'running',
        %(note)s, %(scan_mode)s, %(resumable)s)
RETURNING id
"""

_BATCH_FINISH_SQL = """
UPDATE location_claim_batches
   SET finished_at = now(), outcome = %(outcome)s, row_count = %(row_count)s,
       cursor_after_id = %(cursor_after_id)s,
       note = concat_ws(' | ', note, %(note)s::text)
 WHERE id = %(batch_id)s
"""

# The newest TERMINAL row of this (lane, source, scan_mode) among the resumable ones.
# `outcome` comes back rather than being filtered on, so that "the migration finished" and
# "the migration has never run" cannot look identical to the caller: only 'stopped' is
# resumed from, 'ok' means the scan reached the end of the table, and 'failed' means the
# cursor on that row certifies nothing.
_RESUME_SQL = """
SELECT outcome, cursor_after_id
  FROM location_claim_batches
 WHERE lane = %(lane)s
   AND source IS NOT DISTINCT FROM %(source)s
   AND scan_mode = %(scan_mode)s
   AND resumable
   AND outcome IN ('ok', 'stopped', 'failed')
 ORDER BY started_at DESC, id DESC
 LIMIT 1
"""

# Has this (lane, source, scan_mode) already put rows in the store, and under which
# normaliser? Unlike `_RESUME_SQL` this ignores `resumable`: an operator-anchored run wrote
# real rows too, and the question here is "what is already in the store", not "where may I
# pick up". `row_count > 0` keeps a run that stopped before writing anything out of it.
_PRIOR_PROGRESS_SQL = """
SELECT extractor_version, row_count
  FROM location_claim_batches
 WHERE lane = %(lane)s
   AND source IS NOT DISTINCT FROM %(source)s
   AND scan_mode = %(scan_mode)s
   AND outcome IN ('ok', 'stopped')
   AND row_count > 0
 ORDER BY started_at DESC, id DESC
 LIMIT 1
"""


class BackfillRefused(RuntimeError):
    """A precondition failed; no batch row was opened and nothing was written."""


def encode_for_archive(body: bytes, *, source: str, page_kind: str) -> dict[str, Any]:
    """The per-row derivation: content type, both hashes, the stored bytes, the cohort.

    Pure — no DB, no network, no clock — so the whole value of a migrated row can be
    asserted from a fixture body alone.

    `page_kind` is not decoration. This lane is the ONLY writer that carries index
    bodies (portal_raw_pages holds 7,659 of them across five portals, four of which
    never had an index profile measured), and `payload_sha256` is the archive's
    identity — so normalising an index body under a portal's DETAIL profile would
    write a permanent content address taken over the wrong projection. The profile
    and the cohort stamp both come from the (source, page_kind) pair.

    The compression goes through `payloads.encode_body`, the live writer's own encoder,
    rather than a second `gzip.compress` call here. The two produce identical bytes today;
    the point is that they cannot stop doing so. If `encode_body`'s level or its
    `mtime=0` reproducibility guarantee is ever tightened, a hand-rolled copy would
    silently start writing different stored bytes for the same content than the live path
    writes — which is precisely the divergence the round-trip verifier exists to catch, and
    it would be invisible to it, because the verifier decodes both through `decode_body`.
    """
    content_type = sniff_content_type(body)
    resolved = resolve_normalisation(source, page_kind)
    norm = normalise(body, content_type=content_type, volatile=resolved.profile)
    # gzip_min_bytes=0: a legacy page is a whole document, so the writer's 4 KB "leave it
    # verbatim" branch is dead weight here — but it stays honest for the degenerate
    # zero-length body, which comes back 'identity' rather than as an empty gzip member.
    stored, encoding = payloads.encode_body(body, gzip_min_bytes=0)
    return {
        "content_type": content_type,
        "payload_sha256": norm.norm_sha256,
        "body_sha256": norm.raw_sha256,
        "byte_size": norm.byte_size,
        "stored": stored,
        "content_encoding": encoding,
        "normalizer_version": resolved.normalizer_version,
    }


def missing_relations(conn: psycopg.Connection) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in _RELATIONS:
            cur.execute(_REGCLASS_SQL, {"name": name})
            row = cur.fetchone()
            if row is None or row[0] is None:
                missing.append(name)
    return missing


def _resume_point(
    conn: psycopg.Connection, *, source: str | None, statement_timeout: int
) -> int | None:
    """The id to pick up after, or None to start at the beginning of the table."""
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_RESUME_SQL, {"lane": LANE, "source": source, "scan_mode": SCAN_MODE})
        row = cur.fetchone()
    if not row:
        return None
    outcome, cursor_after_id = row
    if outcome != "stopped" or cursor_after_id is None:
        return None
    return int(cursor_after_id)


def _prior_progress(
    conn: psycopg.Connection, *, source: str | None, statement_timeout: int
) -> tuple[str, int] | None:
    """(extractor_version, row_count) of the newest run that actually wrote rows."""
    with loader_db.bounded(conn, statement_timeout) as cur:
        cur.execute(_PRIOR_PROGRESS_SQL,
                    {"lane": LANE, "source": source, "scan_mode": SCAN_MODE})
        row = cur.fetchone()
    return (str(row[0]), int(row[1])) if row else None


def run(
    conn: psycopg.Connection,
    *,
    source: str | None,
    batch_size: int,
    max_seconds: float | None,
    limit: int | None,
    start_after_id: int,
    statement_timeout: int,
    dry_run: bool,
    note: str | None,
    force: bool = False,
) -> dict[str, Any]:
    missing = missing_relations(conn)
    if missing:
        raise BackfillRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-387 and 403)")
    if source is not None:
        with loader_db.bounded(conn, statement_timeout) as cur:
            cur.execute(_SOURCE_PRESENT_SQL, {"source": source})
            if cur.fetchone() is None:
                raise BackfillRefused(
                    f"portal_raw_pages holds no rows for source={source!r} — a typo would "
                    f"otherwise stamp an immediate 'ok' over an untouched portal")

    # An operator-anchored run does not certify that everything below its anchor was
    # migrated, so it neither resumes from a stored cursor nor becomes one (the same guard
    # the claim intake and `mapy_inventory_runs.resumable` carry).
    anchored = start_after_id > 0
    after_id = start_after_id
    resumed = False
    if not anchored:
        resume_id = _resume_point(conn, source=source, statement_timeout=statement_timeout)
        if resume_id is not None:
            after_id, resumed = resume_id, True
            LOG.info("BACKFILL resuming a budget-stopped scan for source=%s from id>%d",
                     source or "*", after_id)

    # A NORMALIZER_VERSION bump moves `payload_sha256` for unchanged content, so ON CONFLICT
    # DO NOTHING stops firing and a re-walk appends a SECOND version_seq=1 pinned row per
    # page. Nothing evicts those: this lane never runs `payloads`' re-pin/cap, and a pinned
    # row is exempt from the cap by definition — the duplicate cohort is permanent, and it
    # inflates exactly the storage number the W2a gate exists to bound. RESUMING is safe
    # (it walks only ground no earlier run reached); re-walking is not.
    prior = _prior_progress(conn, source=source, statement_timeout=statement_timeout)
    if prior is not None and prior[0] != EXTRACTOR_VERSION:
        if resumed:
            LOG.warning(
                "BACKFILL mixed cohort: rows already migrated under %s, this run writes "
                "%s. No page is duplicated (the resume cursor only walks new ground), but "
                "the archive now spans two normaliser cohorts — portal_raw_payloads."
                "normalizer_version is what tells them apart.", prior[0], EXTRACTOR_VERSION)
        elif not force:
            raise BackfillRefused(
                f"source={source or '*'} already has {prior[1]} rows migrated under "
                f"extractor_version={prior[0]!r}, and this run writes {EXTRACTOR_VERSION!r} "
                f"starting from id>{after_id}. Re-walking that ground under a different "
                f"normaliser appends a SECOND pinned version_seq=1 row for every page — "
                f"content that normalises differently now hashes differently, so the "
                f"ON CONFLICT that makes a re-run a no-op will not fire, and no pruner can "
                f"ever evict a pinned row. Resume instead (dispatch with no start_after_id, "
                f"so a 'stopped' cursor is picked up), or pass --force if a second cohort "
                f"is genuinely intended.")

    batch_id: int | None = None
    if not dry_run:
        with loader_db.bounded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": EXTRACTOR_VERSION,
                "wave": WAVE, "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
                "scan_mode": SCAN_MODE, "resumable": not anchored,
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("BACKFILL start source=%s batch_size=%d after_id=%d resumed=%s batch_id=%s "
             "dry_run=%s", source or "*", batch_size, after_id, resumed, batch_id, dry_run)

    # Resolved even in --dry-run, and deliberately: a rehearsal whose whole job is to
    # catch what would break the real run must catch a missing bucket too. It is the
    # UPLOAD that --dry-run suppresses, not the check.
    store = payloads.open_store()
    started = time.monotonic()
    stats: dict[str, Any] = {
        "pages": 0, "inserted": 0, "skipped_existing": 0, "unmapped_page_kind": 0,
        "spilled": 0, "uploaded": 0, "bytes_read": 0, "bytes_stored": 0,
        "stopped_early": False, "reached_end": False,
        "resumed_from_id": after_id, "resumed": resumed,
    }
    unmapped_kinds: set[str] = set()
    try:
        while True:
            if limit is not None and stats["pages"] >= limit:
                LOG.info("BACKFILL stopping: --limit reached")
                stats["stopped_early"] = True
                break
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                stats["stopped_early"] = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats["pages"])

            with loader_db.bounded(conn, statement_timeout) as cur:
                cur.execute(_SOURCE_ROWS_SQL, {
                    "after_id": after_id, "source": source, "batch_size": size})
                records = cur.fetchall()
            if not records:
                # The ONLY way this migration earns outcome='ok'. A budget, a limit or an
                # exception all leave rows behind the cursor, and a lane that claimed 'ok'
                # over them would look finished while a slice of the archive had never
                # been copied at all.
                stats["reached_end"] = True
                break

            # The cursor advances over EVERY row read, including ones this lane declines
            # to migrate: an unmappable page_kind is a permanent property of that row, and
            # leaving the cursor behind it would wedge the migration on the same row
            # forever.
            batch_after_id = int(records[-1][0])
            columns = _columns(records, store=store, dry_run=dry_run, stats=stats,
                               unmapped_kinds=unmapped_kinds)

            if columns and not dry_run:
                # `normalizer_version` rides in `columns`, per row: one batch mixes
                # detail and index bodies, and only the detail ones were normalised
                # under a measured profile.
                with loader_db.bounded(conn, statement_timeout) as cur:
                    cur.execute(_INSERT_SQL, columns)
                    inserted = len(cur.fetchall())
                stats["inserted"] += inserted
                stats["skipped_existing"] += len(columns["source"]) - inserted

            after_id = batch_after_id
            stats["pages"] += len(records)
            LOG.info("BACKFILL progress pages=%d inserted=%d existing=%d unmapped=%d "
                     "mb_read=%.1f mb_stored=%.1f through_id=%d",
                     stats["pages"], stats["inserted"], stats["skipped_existing"],
                     stats["unmapped_page_kind"], stats["bytes_read"] / 1e6,
                     stats["bytes_stored"] / 1e6, after_id)
        # The TERMINAL stamp lives inside the try, not after it. Outside, a transient error
        # on this one UPDATE would propagate with the row still at 'running' — a state
        # `_RESUME_SQL` cannot see, so the next dispatch would silently restart the whole
        # scan from id 0 and re-walk everything already migrated. Inside, the same error
        # falls through to the failure stamp below and the row ends terminal either way.
        outcome = "ok" if stats["reached_end"] else "stopped"
        stats["outcome"] = outcome
        if unmapped_kinds:
            LOG.warning("BACKFILL skipped %d row(s) whose page_kind is not a "
                        "location_page_kind label: %s — widen PAGE_KIND_MAP",
                        stats["unmapped_page_kind"], ", ".join(sorted(unmapped_kinds)))
        if batch_id is not None:
            with loader_db.bounded(conn, _STAMP_TIMEOUT_S) as cur:
                cur.execute(_BATCH_FINISH_SQL, {
                    "batch_id": batch_id,
                    "outcome": outcome,
                    "row_count": stats["inserted"],
                    "cursor_after_id": after_id,
                    "note": f"pages={stats['pages']} inserted={stats['inserted']} "
                            f"existing={stats['skipped_existing']} "
                            f"unmapped_page_kind={stats['unmapped_page_kind']} "
                            f"spilled={stats['spilled']} uploaded={stats['uploaded']} "
                            f"reached_end={stats['reached_end']} through_id={after_id}",
                })
    except Exception as exc:
        if batch_id is not None:
            # Guarded on the FAILURE path too, and on a short ceiling: whatever broke the
            # run may be the same pressure that would hang this stamp, and a bookkeeping
            # write that wedges replaces the exception the operator needs with silence.
            try:
                with loader_db.bounded(conn, _STAMP_TIMEOUT_S) as cur:
                    cur.execute(_BATCH_FINISH_SQL, {
                        "batch_id": batch_id, "outcome": "failed",
                        "row_count": stats["inserted"], "cursor_after_id": after_id,
                        "note": f"{type(exc).__name__}: {exc}"[:500],
                    })
            except Exception:  # noqa: BLE001 - never mask the exception being reported
                LOG.exception("BACKFILL could not stamp batch %s as failed", batch_id)
            # A 'failed' row is deliberately not resumable — its cursor stopped wherever
            # the exception found it and certifies nothing about the rows behind it — so
            # the next plain dispatch restarts at the beginning of the table. That is safe
            # (ON CONFLICT DO NOTHING makes the re-walk a no-op) but on a 14 GB source it
            # is not cheap, and the operator should be told the one flag that skips it.
            LOG.error("BACKFILL failed at id=%d after %d pages; a plain re-dispatch "
                      "restarts from the beginning. To pick up here instead, dispatch "
                      "with start_after_id=%d (an anchored run, so it writes no resumable "
                      "cursor of its own).", after_id, stats["pages"], after_id)
        raise

    stats["cursor_after_id"] = after_id
    stats["batch_id"] = batch_id
    return stats


def _columns(
    records: list[tuple[Any, ...]],
    *,
    store: payloads.ObjectStore | None,
    dry_run: bool,
    stats: dict[str, Any],
    unmapped_kinds: set[str],
) -> dict[str, list[Any]]:
    """Column-major arrays for `_INSERT_SQL`, one entry per migratable row.

    Placement goes through `payloads.plan_placement`, the live writer's own decision, so
    the legacy corpus lands where new bodies land. It has to: this lane is the single
    largest write the archive will ever take (447 k pages, 7.7 GB gzipped), and a
    backfill that wrote inline while the live path spilled would put the whole legacy
    corpus in Postgres and make the footprint the operator signed wrong by roughly the
    size of the archive.

    Uploads happen HERE, before the INSERT, and OUTSIDE the batch transaction — the
    opposite ordering from `payloads.append_payload`, for a reason that is specific to a
    bulk lane. There, ordering the upload after the insert is what keeps a committed row
    from pointing at a missing object; here `_INSERT_SQL` is `ON CONFLICT DO NOTHING`
    over a whole batch, so an interleaved per-row upload would mean holding a
    several-hundred-row transaction open across as many network round trips. Uploading
    first inverts the risk into the harmless direction: an object nothing references,
    which the next run adopts because the key is the hash of its bytes.
    """
    out: dict[str, list[Any]] = {
        "source": [], "source_id_native": [], "page_kind": [], "payload_sha256": [],
        "body_sha256": [], "content_type": [], "content_encoding": [], "body": [],
        "body_r2_key": [], "byte_size": [], "stored_byte_size": [], "http_status": [],
        "fetched_at": [], "normalizer_version": [],
    }
    pending: dict[str, bytes] = {}
    for _id, source, source_id_native, page_kind, body, http_status, fetched_at in records:
        kind = PAGE_KIND_MAP.get(page_kind)
        if kind is None:
            unmapped_kinds.add(str(page_kind))
            stats["unmapped_page_kind"] += 1
            continue
        raw = bytes(body)
        # The MAPPED kind, not the source column's raw text: it is the value the row
        # lands under, so the profile and the cohort must be resolved from the same one.
        derived = encode_for_archive(raw, source=source, page_kind=kind)
        # gzip_min_bytes=0 for the same reason `encode_for_archive` uses it: a legacy
        # page is a whole document, so the writer's "leave it verbatim" branch is dead
        # weight, and the degenerate zero-length body still reports 'identity'.
        placement = payloads.plan_placement(
            source, raw, derived["body_sha256"], gzip_min_bytes=0)
        if placement.spills:
            # Deduped within the batch: two listings served byte-identical pages (a
            # portal's "listing removed" interstitial) share one content address.
            pending[placement.r2_key or ""] = placement.stored
            stats["spilled"] += 1
        out["source"].append(source)
        out["source_id_native"].append(source_id_native)
        out["page_kind"].append(kind)
        out["payload_sha256"].append(derived["payload_sha256"])
        out["body_sha256"].append(derived["body_sha256"])
        out["content_type"].append(derived["content_type"])
        out["content_encoding"].append(placement.content_encoding)
        out["body"].append(None if placement.spills else placement.stored)
        out["body_r2_key"].append(placement.r2_key)
        out["byte_size"].append(derived["byte_size"])
        out["stored_byte_size"].append(len(placement.stored))
        out["http_status"].append(http_status)
        out["fetched_at"].append(fetched_at)
        out["normalizer_version"].append(derived["normalizer_version"])
        stats["bytes_read"] += derived["byte_size"]
        stats["bytes_stored"] += len(placement.stored)
    if pending:
        if store is None:
            raise BackfillRefused(
                f"{len(pending)} of this batch's bodies exceed the R2 threshold and R2 "
                f"is not configured; set R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / "
                f"R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME before migrating the legacy "
                f"archive")
        if not dry_run:
            _upload_all(store, pending)
            stats["uploaded"] += len(pending)
    return out if out["source"] else {}


def _upload_all(store: payloads.ObjectStore, objects: dict[str, bytes]) -> None:
    """PUT a batch's spilled bodies, concurrently, raising on the first failure.

    Serial uploads would set this lane's pace: 447 k objects at a ~50 ms round trip is
    six hours of pure latency for ~8 GB of payload. The pool is sized like the image
    lane's (which is where `R2Client`'s connection-pool default comes from), and any
    exception propagates so the batch is not inserted — the run stamps 'stopped' and the
    resumable cursor replays it, re-uploading objects that are already there as the same
    key with the same bytes.
    """
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        futures = [pool.submit(store.upload_bytes, key, data, "application/gzip")
                   for key, data in objects.items()]
        for future in futures:
            future.result()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill portal_raw_pages into "
                                                 "portal_raw_payloads (W2a, 06 §6.4).")
    parser.add_argument("--source", default=None,
                        help="one portal only (default: every source in the archive)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many source rows (a budget, so it stamps "
                             "'stopped' and the next run resumes)")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--start-after-id", type=int, default=0,
                        help="operator anchor; the run neither resumes from nor becomes a "
                             "resumable cursor")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--dry-run", action="store_true",
                        help="read and encode; write no payload row and no batch row")
    parser.add_argument("--force", action="store_true",
                        help="re-walk ground already migrated under a different normaliser, "
                             "accepting a permanent second pinned row per page")
    parser.add_argument("--note", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, args.batch_size))

    kwargs: dict[str, Any] = {
        "source": args.source, "batch_size": batch_size, "max_seconds": args.max_seconds,
        "limit": args.limit, "start_after_id": args.start_after_id,
        "statement_timeout": args.statement_timeout, "dry_run": args.dry_run,
        "note": args.note, "force": args.force,
    }
    with db.connect() as conn:
        try:
            if args.dry_run:
                # NO LEASE on a dry run: it writes nothing, so there is nothing to
                # serialise against — and releasing the lease as 'ok' would stamp
                # `location_jobs.last_success_at` and let the staleness monitor read a
                # migration that copied no rows as a healthy one.
                LOG.info("BACKFILL dry run: not taking the %s lease", JOB_NAME)
                stats = run(conn, **kwargs)
            else:
                with lease.held(
                    conn, JOB_NAME, cadence=CADENCE,
                    concurrency_group=CONCURRENCY_GROUP, ttl_seconds=LEASE_TTL_S,
                ) as acquired:
                    if not acquired:
                        LOG.info("BACKFILL skipped: another run holds the %s lease",
                                 JOB_NAME)
                        return 0
                    stats = run(conn, **kwargs)
        except BackfillRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    LOG.info("BACKFILL done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
