"""`--mode incremental`: the real-time shadow pass, and the Postgres store behind it.

Dark by default at two levels, and stoppable at a third. The workflow exits before python runs
unless the repository variable `AUTODEDUP_REALTIME_ENABLED` is `true`; `env_enabled` re-reads
the same variable here, so a manual dispatch cannot bypass it; and `autodedup.settings`'s
`realtime_enabled` row stops a lane that is already on WITHOUT a repository change. Shadow
mode's own posture (E39/D4) is untouched: nothing here reaches `public.listings`, a
`property_id` or a merge.

The pass itself is `incremental.run_pass_bounded`; this module is the three adapters it needs —
the store, the read-only fact source and the watermark — plus the lease that keeps two runs out
of each other's way (lease-row CAS, never `pg_advisory_lock`: a session lock strands over the
transaction pooler).

**Why this file is the one with the rails on it.** W9's verification found four blocking defects
and all four were HERE rather than in the engine: the fingerprint row was never written (so the
dirty set was always empty and every pass reported green over zero work), the certificate and
the evidence families were dropped on read-back (so a component clustered in a different edge
order than the cohort pass), the families bitmask was written as a count, and E64's rail queried
with an empty id array. None was visible from the in-memory twin, which is why
`tests/autodedup/test_incremental_sqlstore.py` now replays a cohort through THIS store against a
Postgres fake and asserts the two stores agree.

**Spend is structurally zero.** D19 gives no LLM judge merge authority on the band, so this
lane calls no provider; `spent_usd` is reported as a measured 0, not as a forecast.
"""

from __future__ import annotations

import json
import os
import random
import socket
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Mapping, Sequence

from autodedup.dataset import Image, Listing
from autodedup.export import (
    DEFAULT_CLIP_MODEL,
    build_image_record,
    build_listing_record,
    encode_clip,
    tag_pairs,
)
from autodedup.export_sql import (
    COHORT_CLIP_SQL,
    COHORT_CLIP_TAGS_SQL,
    COHORT_IMAGES_SQL,
    COHORT_LISTINGS_SQL,
    COHORT_LOCATION_SQL,
    COHORT_PRICE_HISTORY_SQL,
)
from autodedup.features import FEATURE_VERSION
from autodedup.harness import (
    DEFAULT_SETTINGS_NAME,
    PRIOR_MODEL_NAME,
    model_name_of,
    model_of_version,
    named_model,
    named_settings,
)
from autodedup.hazard_context import ContextStamp, address_block_key, category_group
from autodedup.incremental import (
    EVIDENCE_HOLD_REASON,
    GENERATION,
    Calibration,
    CellRow,
    Evidence,
    EvidenceHold,
    evidence_of,
    FpRow,
    GuardRow,
    Keyer,
    Limits,
    PairRow,
    SET_CAP,
    WorkItem,
    fp_digest,
    key_token,
    run_pass_bounded,
)
from autodedup.incremental_scope import (
    CORPUS_PROJECTION_MB,
    Scope,
    ScopeError,
    guard_agrees,
    resolve_pass_scope,
    resolve_scope,
)
from autodedup.incremental_sql import (
    RT_CALIBRATION_PRESENT_SQL,
    RT_CALIBRATION_READ_SQL,
    RT_CALIBRATION_WRITE_SQL,
    RT_CELL_READ_SQL,
    RT_CELL_UPSERT_SQL,
    RT_CHANGED_LISTINGS_SQL,
    RT_CLUSTER_DROP_SQL,
    RT_CLUSTER_MEMBERS_DROP_SQL,
    RT_CLUSTERS_TOUCHING_SQL,
    RT_CONFLICT_DROP_SQL,
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_CURSOR_WRITE_SQL,
    RT_FLIPPED_LISTINGS_SQL,
    RT_EQUIV_BATCH_SETTINGS_SQL,
    RT_EQUIV_CLOCK_FACTS_SQL,
    RT_EQUIV_PAIR_FEATURES_SQL,
    RT_EQUIV_SCORE_TYPE_SQL,
    RT_EVIDENCE_CANDIDATES_SQL,
    RT_EVIDENCE_HELD_COUNT_SQL,
    RT_EVIDENCE_PROBE_SQL,
    RT_EVIDENCE_RELEASE_SQL,
    RT_FP_COUNT_SQL,
    RT_FP_DELETE_SQL,
    RT_FRESH_BLOCK_CELL_SQL,
    RT_FRESH_CLUSTER_CONFLICTS_SQL,
    RT_FRESH_CLUSTER_MEMBERS_SQL,
    RT_FRESH_CLUSTERS_SQL,
    RT_FRESH_CURSORS_SQL,
    RT_FRESH_FP_KEY_SQL,
    RT_FRESH_LEASE_SQL,
    RT_FRESH_PAIRS_SQL,
    RT_FRESH_RETIRE_EVENT_SQL,
    RT_FRESH_RT_FP_SQL,
    RT_FRESH_SCOPE_IDS_SQL,
    RT_FRESH_SCOPE_SCAN_SQL,
    RT_FP_READ_SQL,
    RT_FP_UPSERT_SQL,
    RT_IDLE_GUARD_SQL,
    RT_LOCK_GUARD_SQL,
    RT_PARITY_CHANGE_SQL,
    RT_PHASH_POP_COUNT_SQL,
    RT_PHASH_POP_SQL,
    RT_PHASH_POP_WRITE_SQL,
    RT_KEY_DELETE_SQL,
    RT_KEY_INSERT_SQL,
    RT_KEYS_MANY_SQL,
    RT_KNOWN_SQL,
    RT_LEASE_RELEASE_SQL,
    RT_LEASE_TAKE_SQL,
    RT_LOOKUP_MANY_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_MUST_NOT_LINK_SQL,
    RT_NEW_LISTINGS_SQL,
    RT_NEW_STRAGGLERS_SQL,
    RT_PAIR_CLUSTER_SQL,
    RT_PAIR_DELETE_SQL,
    RT_PAIR_UNCLUSTER_SQL,
    RT_PAIR_UPSERT_SQL,
    RT_PAIRS_TOUCHING_SQL,
    RT_PAIRS_WITHIN_SQL,
    RT_REVIVED_SQL,
    RT_ROW_CENSUS_SQL,
    RT_ROW_ESTIMATE_SQL,
    RT_SCHEMA_SIZE_SQL,
    RT_SCOPE_BACKLOG_SQL,
    RT_SCOPE_BLOCK_SQL,
    RT_SCOPE_ENTRANTS_SQL,
    RT_SCOPE_IDS_DELETE_SQL,
    RT_SCOPE_IDS_PRUNE_BLOCKS_SQL,
    RT_SCOPE_IDS_PRUNE_SQL,
    RT_SCOPE_IDS_WRITE_SQL,
    RT_SCOPE_PARENT_OBEC_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
    RT_SCOPE_SCAN_STATE_SQL,
    RT_SCOPE_SCAN_WRITE_SQL,
    RT_SCOPE_DRIFT_SQL,
    RT_RETIRE_EVENT_WRITE_SQL,
    RT_RETIRE_WINDOW_SQL,
    RT_SEED_CURSORS_SQL,
    RT_SETTING_SQL,
    RT_SETTING_WRITE_SQL,
    RT_SETTINGS_MANY_SQL,
    RT_STAMPED_MERGES_SQL,
    RT_STATEMENT_GUARD_SQL,
    RT_STORE_PRESENT_SQL,
)
from autodedup.score_lane import (
    CLUSTER_INSERT_SQL,
    CLUSTER_MEMBER_INSERT_SQL,
    families_bitmask,
    families_of_bitmask,
    present_features,
    storable,
)
from autodedup.model import LogisticModel
from autodedup.parity_digest import Floors as ParityFloors
from autodedup.parity_digest import baseline as parity_baseline
from autodedup.parity_digest import compare as parity_compare
from autodedup.parity_digest import stratified_sample
from autodedup.parity_digest import verdict as parity_verdict
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL
from autodedup.settings import Settings

LANE_NAME: str = "autodedup_realtime"
# Longer than the workflow's own 25-minute timeout: a lease that expires while its holder is
# still running invites a second pass to write over the first.
LEASE_TTL_S: int = 2100
CURSOR_NEW: str = "rt_new"
CURSOR_CHANGED: str = "rt_changed"
CURSOR_FLIPPED: str = "rt_flipped"
CURSOR_REVIVE: str = "rt_revive"
CURSOR_SCOPE: str = "rt_scope_drift"
CURSOR_ENTER: str = "rt_scope_enter"
CURSOR_EVIDENCE: str = "rt_evidence"
ENV_FLAG: str = "AUTODEDUP_REALTIME_ENABLED"
DB_FLAG: str = "realtime_enabled"
SCOPE_SETTING: str = "rt_scope"
BUDGET_SETTING: str = "rt_max_schema_mb"
RETIRE_SETTING: str = "rt_max_retire_fraction"
SCAN_CAP_SETTING: str = "rt_enter_max_scans_per_day"
INTERVAL_SETTING: str = "rt_enter_interval_hours"
EVIDENCE_HORIZON_SETTING: str = "rt_evidence_horizon_hours"
EVIDENCE_SLICE_SETTING: str = "rt_evidence_slice"
# --- the bootstrap phase and the time budget (E98) ---
# Both are per-generation `autodedup.settings` rows, written by the seed and read by the pass.
BOOTSTRAP_SETTING: str = "rt_bootstrap"
PASS_RATE_SETTING: str = "rt_pass_rate_per_s"
PASS_BUDGET_SETTING: str = "rt_pass_budget_s"
CALIBRATION_AGE_SETTING: str = "rt_calibration_max_age_days"
RESCOPE_ARG: str = "rt_rescope"
STORAGE_WATERMARK: str = "rt_storage_last"
# The generation whose scope row may still be the LEGACY global one (W9e/R4). `rt_scope` was
# one row for every generation, so a second generation's seed silently rescoped the first; the
# row is `rt_scope:<generation>` now, and the old spelling is read exactly here and nowhere
# else, because only this generation can have written one.
LEGACY_SCOPE_GENERATION: str = GENERATION

# How long a row must have existed before the lane will claim it (E73). Portal writes commit in
# seconds; a transaction still open after five minutes would have to be a stuck one, and the
# straggler sweep catches even that.
SETTLE_LAG_S: int = 300
# How far back the anti-join looks for a row that committed after the cursor passed its id.
STRAGGLER_WINDOW: int = 5000
# One round-robin slice of the revive sweep. 434k inactive rows at 20k a pass is a full cycle
# every ~22 passes (~3.7 h at the `*/10` cadence), which is the lane's revival latency.
REVIVE_SLICE: int = 20000
# How many rows of its OWN cursor index a forward feed reads before the scope is resolved for
# them (E79). Measured on the live corpus: the three feeds together take 23,234 rows a day —
# 161 in a ten-minute pass — so 1,000 a feed is >6x headroom on the corpus rate and >200x on
# the scope's own. It is a bound on work, not on progress: whatever the window holds, the
# cursor crosses it in one pass.
FEED_WINDOW: int = 1000
# One round-robin slice of the scope-drift sweep, over this generation's own fingerprint rows.
# Under the trial scope (4,969 listings) one slice covers the whole store every pass.
DRIFT_SLICE: int = 20000
# How many rows one REFRESH of a scope block may snapshot (W9e/R3). The trial scope's largest
# block is Praha-Vysočany at 1,439 rows; the cap exists so a mis-typed obec code cannot pull a
# whole city into `rt_scope_ids`, and it does not reduce the scan's cost — the `limit` is
# applied after the bitmap heap scan, which is precisely why a smaller `enter_slice` was never
# the answer to what that scan costs.
ENTER_SLICE: int = 20000
# --- the evidence sweep (E92) ---------------------------------------------------------------
#
# One round-robin slice of the SEVENTH feed, over this generation's own `rt_fp` rows. The
# candidate arm reads `autodedup` only; what it costs `public` is the probe below it, and the
# probe is the only read of `public.images` this lane makes outside a fact fetch. Measured on
# the trial scope with `EXPLAIN (ANALYZE, BUFFERS)`: 200 listings = 3,085 images = 2,375
# buffers (920 heap blocks, 261 of them read cold), 12 buffers a listing. At the shipped slice
# and the `*/10` cadence that is 400 x 144 = 57,600 listing probes a day IF the candidate set
# were that large; it is not — the trial scope carries 76 rows of incomplete evidence in
# steady state plus ~74 inside the 48 h horizon, so a pass probes ~150 listings (~1,800
# buffers, 14 MB) and the day costs ~259,000 buffers, 2.0 GB — the same order as the entrant
# feed's 1.6 GB after W9e cut it 7.8x. `rt_evidence_slice = 0` turns the feed off.
EVIDENCE_SLICE: int = 400
# How long after its FIRST decision a listing's photographs are still expected. The producers
# are hourly (dHash at :20, CLIP at :40) and measured p90 4.51 h to a first tag, so 48 h is
# ~10x the observed tail rather than a guess at it — and it is the bound on how long a merge
# may wait (E93) as much as on how long the sweep chases one. Data: `rt_evidence_horizon_hours`.
EVIDENCE_HORIZON_HOURS: float = 48.0
# How often a scope block's membership is re-walked on `public`, by grain and in HOURS. A town
# block is an index-served bitmap scan of its own obec (Jablonec: 2,745 buffers = 21 MB, 61 ms);
# a QUARTER has no index of its own at all and costs 29,568 buffers = 231 MB, 6.4 s, cold every
# time. So they do not deserve the same cadence, and neither is per-pass work. Data, overridable
# per grain: `rt_enter_interval_hours`.
ENTER_INTERVAL_HOURS: dict[str, float] = {"obec": 1.0, "cast_obce": 6.0}
# The hard ceiling on those walks, per generation and per rolling day (W9e/R3). The cadence
# above is what the lane INTENDS to spend; this is what it is allowed to spend when an interval
# is mis-set to zero or the block list grows. Under the trial scope the cadence asks for 52 a
# day (24 + 24 + 4) against this 60. Data: `rt_enter_max_scans_per_day`.
MAX_ENTER_SCANS_PER_DAY: int = 60
# --- the bootstrap phase (E98) ---------------------------------------------------------------
#
# What a SEEDED generation costs when the seed wrote no pairs: nothing merges. A backfilled seed
# writes `rt_fp` and `fp_key` for every listing of the cohort and NOT ONE PAIR, so the store
# holds the whole scope and knows of no duplicate in it; a later arrival can only ever be
# compared with what arrives after it, and its group misses every duplicate the seed backfilled.
# The fix is not a "backfill pairs" step — it is to make every in-scope listing an ARRIVAL, which
# is the one path the replay-equivalence proof covers (E70). `backfill=false` does that; this
# phase is what makes it finish in hours instead of days.
#
# During the phase the entrant claim is the WHOLE `max_listings` rather than a seventh of it, and
# the block walks ignore the per-grain cadence until every block has been walked once — a cadence
# is what keeps a STEADY-STATE lane off `public`, and a scope nothing has listed yet has no
# steady state. The rolling-day scan cap (`rt_enter_max_scans_per_day`) still holds, because that
# rail exists for the case where the cadence is wrong. The phase ends BY ITSELF — the pass that
# finds the entrant backlog empty writes the row false — so nothing has to remember to end it.
BOOTSTRAP_MIN_CLAIM: int = 1
# --- the time budget (E98) -------------------------------------------------------------------
#
# The claim is bounded by SECONDS as well as by counts, because the count bound is not a bound on
# time and the job's is 25 minutes. Measured: the one live pass that ever did work claimed 77
# listings and took 876.7 s (2026-09-20, run 35520830343) — 0.088 claimed listings a second, 14.6
# minutes of a 25-minute timeout on a claim a sixth of the shipped one. A claim of 500 at that
# rate is an hour and a half, and a pass that dies on the runner's timeout has written nothing,
# advanced no cursor and left the lease held until it expires.
#
# So every pass measures its OWN rate (claimed listings per second of pass wall time) into
# `rt_pass_rate_per_s:<generation>` and the next pass claims at most `budget x rate`. The default
# below is the one live measurement, which is the pathological case this branch removes — the
# first pass of a build therefore claims ~90 listings, measures the real rate, and the second
# claims the full slice. One extra pass is the price of never guessing the rate upward.
PASS_BUDGET_S: float = 900.0
PASS_RATE_PER_S: float = 0.1
# A rate is only recorded when the pass actually claimed enough for the quotient to mean
# something: an idle pass is 0 listings in 7 seconds and would otherwise wedge the claim at 1.
PASS_RATE_MIN_CLAIM: int = 20
# How much of the new measurement the stored rate takes. A build ramps from the conservative
# default to the real rate in two passes at 0.5 and cannot be knocked out of it by one slow pass.
PASS_RATE_ALPHA: float = 0.5
# The window the retirement rail is measured over (W9e/R2). A slice-sized rail could only fire
# while one drift slice was itself a twentieth of the store; a rolling day is independent of
# `drift_slice` and of the store's size.
RETIRE_WINDOW_HOURS: int = 24
# The share of the store one pass's drift sweep may retire before the lane STOPS instead (W9d-1).
# A geocode correction moves a listing or two; a scope that has gone wrong moves everything, and
# the difference between those two is the only thing standing between a hand-edited settings row
# and a store that has to be re-seeded. Data, not a constant: `rt_max_retire_fraction`.
MAX_RETIRE_FRACTION: float = 0.05
# The storage budget, in megabytes of schema `autodedup` (pg_total_relation_size, indexes and
# TOAST included). The schema is ~148 MB today and the operator pays for it; a lane that has
# not been watched for a week must not be able to double it.
MAX_SCHEMA_MB: float = 400.0
# --- the parity gate (E91) ---------------------------------------------------------------
#
# The seed writes a baseline of per-listing fact digests into `autodedup.settings` (one row, a
# few kilobytes — no migration, and it is cut at exactly the moment the calibration is) and
# every pass re-checks a slice of it before it opens its transaction. The tolerance is ZERO
# breaches: a stored fact or a frozen statistic that differs on a listing nothing has changed
# is the W9f defect, and the one thing W9f proved is that it can run for a whole pass unnoticed.
PARITY_BASELINE_SETTING: str = "rt_parity_baseline"
PARITY_SAMPLE_SETTING: str = "rt_parity_sample"
# How many listings the SEED puts in the baseline, and how many of them a PASS re-checks. The
# pass's slice is drawn fresh each time, so a day of passes covers the baseline many times over
# at one fact read (6 statements, ~0.7 s) a pass.
PARITY_BASELINE_N: int = 120
PARITY_PASS_SAMPLE: int = 25
PARITY_TOLERANCE: int = 0
# The four FLOORS a pass must clear before a green gate means anything (E94). All data:
# `rt_parity_min_checked`, `rt_calibration_max_age_days`, `rt_parity_max_unknown_pop_share`.
PARITY_MIN_CHECKED_SETTING: str = "rt_parity_min_checked"
PARITY_MIN_SHARE_SETTING: str = "rt_parity_min_checked_share"
PARITY_UNKNOWN_POP_SETTING: str = "rt_parity_max_unknown_pop_share"
PARITY_MIN_CHECKED: int = 15
PARITY_MIN_CHECKED_SHARE: float = 0.6
CALIBRATION_MAX_AGE_DAYS: float = 14.0
PARITY_MAX_UNKNOWN_POP_SHARE: float = 0.15
# How many `phash_pop` rows one `executemany` carries. The trial cohort's 41,791 hashes are
# 9 chunks; the number is the score lane's, for the same reason (bound-parameter size).
POP_CHUNK: int = 5_000
STATEMENT_TIMEOUT_MS: int = 120_000
LOCK_TIMEOUT_MS: int = 5_000
IDLE_TIMEOUT_MS: int = 300_000
_EPOCH: str = "epoch"


def _iso(value: Any) -> str | None:
    """A timestamp as text, for the one statement that carries a possibly all-NULL column."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))
        return list(cur.fetchall())


def _exec(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))


def _exec_many(conn: Any, sql: str, params: Sequence[Mapping[str, Any]]) -> None:
    if not params:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, [dict(row) for row in params])


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


class SqlStore:
    """`incremental.Store` over Postgres. Every write is inside schema `autodedup` (D4).

    Three things are buffered rather than written statement by statement (E74): the fingerprint
    rows and postings of one pass, the census cells, and the lookups already answered. Measured:
    a whole 24-listing cohort decided in one pass costs **14 statements** and a pass over an
    unchanged corpus costs **4**, where the per-listing spelling cost ~17,700 for a 166-listing
    claim."""

    def __init__(self, conn: Any, generation: str = GENERATION,
                 store_floor: float = 0.02, model_version: str | None = None,
                 calibration_digest: str | None = None) -> None:
        self.conn = conn
        self.generation = generation
        # WHICH SCORER TOOK THE DECISION, stamped on every row this store writes (E90a). The
        # columns existed and the lane wrote `evidence["_model"]` into them — a key nothing has
        # ever set — so all 15,923 rows of the live generation `rt` carry a NULL model_version
        # against `w6_gold` on the batch generation's, and the store could not say which
        # scorer it had been. The pass's model and calibration are the generation's, so they
        # are held here rather than fished out of a per-pair evidence bag.
        self.model_version = model_version
        self.calibration_digest = calibration_digest
        # The batch lane's own retention floor, carried here so the two stores keep the same
        # rows. It comes from the pass's settings, never from a constant of this module.
        self.store_floor = float(store_floor)
        self.pairs_retained = 0
        self.pairs_evicted = 0
        self.statements = 0
        self._pending: dict[int, tuple[FpRow, list[tuple[str, str]]]] = {}
        self._lookups: dict[tuple[str, str], list[int]] = {}
        self._fp: dict[int, FpRow | None] = {}
        self._cells: dict[tuple[str, str], CellRow] = {}
        self._cells_read: set[str] = set()
        self._cells_dirty: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ plumbing
    def _query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
        self.statements += 1
        return _rows(self.conn, sql, params)

    def _run(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        self.statements += 1
        _exec(self.conn, sql, params)

    def _run_many(self, sql: str, params: Sequence[Mapping[str, Any]]) -> None:
        if not params:
            return
        self.statements += 1
        _exec_many(self.conn, sql, params)

    def _barrier(self) -> None:
        """Make the pass's buffered fingerprint writes visible before anything reads them."""
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        ids = sorted(pending)
        self._run(RT_KEY_DELETE_SQL, {"generation": self.generation, "ids": ids})
        probes: list[str] = []
        tokens: list[str] = []
        owners: list[int] = []
        for listing_id in ids:
            for probe, token in pending[listing_id][1]:
                probes.append(probe)
                tokens.append(token)
                owners.append(listing_id)
        if probes:
            self._run(RT_KEY_INSERT_SQL, {
                "generation": self.generation, "probes": probes, "tokens": tokens,
                "listing_ids": owners})
        self._run_many(RT_FP_UPSERT_SQL, [
            {
                "generation": self.generation, "listing_id": listing_id,
                "category_main": pending[listing_id][0].guard.category_main,
                "category_type": pending[listing_id][0].guard.category_type,
                "area_m2": pending[listing_id][0].guard.area_m2,
                "disposition": pending[listing_id][0].guard.disposition,
                "floor": pending[listing_id][0].guard.floor,
                "fp_digest": pending[listing_id][0].digest,
                "cell_key": pending[listing_id][0].cell_key,
                "cell_group": pending[listing_id][0].cell_group,
                "is_active": pending[listing_id][0].is_active,
                "ev_images": pending[listing_id][0].evidence.n_images,
                "ev_phash": pending[listing_id][0].evidence.n_phash,
                "ev_clip": pending[listing_id][0].evidence.n_clip,
                "ev_tags": pending[listing_id][0].evidence.n_tags,
                "ev_complete": pending[listing_id][0].evidence.complete,
            }
            for listing_id in ids
        ])
        # A posting list cached before these keys landed is now wrong.
        self._lookups.clear()

    # ------------------------------------------------------------------ postings
    def lookup(self, probe: str, token: str) -> list[int]:
        return list(self.lookup_many([(probe, token)]).get((probe, token), ()))

    def lookup_many(self, keys: Sequence[tuple[str, str]]
                    ) -> dict[tuple[str, str], list[int]]:
        self._barrier()
        missing = sorted({key for key in keys if key not in self._lookups})
        if missing:
            found: dict[tuple[str, str], list[int]] = {key: [] for key in missing}
            for row in self._query(RT_LOOKUP_MANY_SQL, {
                    "generation": self.generation,
                    "probes": [key[0] for key in missing],
                    "tokens": [key[1] for key in missing]}):
                found[(str(row[0]), str(row[1]))].append(int(row[2]))
            self._lookups.update(found)
        return {key: list(self._lookups[key]) for key in keys if self._lookups.get(key)}

    def put_listing(self, listing_id: int, row: FpRow,
                    keys: Sequence[tuple[str, str]]) -> None:
        self._pending[int(listing_id)] = (row, list(keys))
        self._fp[int(listing_id)] = row

    def drop_listing(self, listing_id: int) -> None:
        self._pending.pop(int(listing_id), None)
        self._fp[int(listing_id)] = None
        self._run(RT_KEY_DELETE_SQL, {"generation": self.generation, "ids": [int(listing_id)]})
        self._run(RT_FP_DELETE_SQL, {"generation": self.generation, "ids": [int(listing_id)]})
        self._run(RT_SCOPE_IDS_DELETE_SQL,
                  {"generation": self.generation, "ids": [int(listing_id)]})
        self._lookups.clear()

    def keys_many(self, ids: Iterable[int]) -> dict[int, list[tuple[str, str]]]:
        self._barrier()
        wanted = sorted({int(i) for i in ids})
        out: dict[int, list[tuple[str, str]]] = {i: [] for i in wanted}
        if not wanted:
            return out
        for row in self._query(RT_KEYS_MANY_SQL,
                               {"generation": self.generation, "ids": wanted}):
            out[int(row[0])].append((str(row[1]), str(row[2])))
        return out

    def rows(self, ids: Iterable[int]) -> dict[int, FpRow]:
        self._barrier()
        wanted = {int(i) for i in ids}
        missing = sorted(wanted - set(self._fp))
        if missing:
            found = {int(row[0]): FpRow(
                GuardRow(int(row[0]), row[1], row[2],
                         float(row[3]) if row[3] is not None else None, row[4],
                         int(row[5]) if row[5] is not None else None),
                str(row[6] or ""), str(row[7] or ""), str(row[8] or ""), bool(row[9]),
                Evidence(int(row[10] or 0), int(row[11] or 0), int(row[12] or 0),
                         int(row[13] or 0)),
                float(row[14]) if row[14] is not None else None,
            ) for row in self._query(RT_FP_READ_SQL,
                                     {"generation": self.generation, "ids": missing})}
            for listing_id in missing:
                self._fp[listing_id] = found.get(listing_id)
        out: dict[int, FpRow] = {}
        for listing_id in sorted(wanted):
            row = self._fp.get(listing_id)
            if row is not None:
                out[listing_id] = row
        return out

    def known(self, ids: Iterable[int]) -> set[int]:
        self._barrier()
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return set()
        return {int(row[0]) for row in self._query(
            RT_KNOWN_SQL, {"generation": self.generation, "ids": wanted})}

    # ------------------------------------------------------------------ pair grain
    def _pair_row(self, row: Sequence[Any]) -> PairRow:
        evidence = row[11] if isinstance(row[11], dict) else json.loads(row[11] or "{}")
        context = row[12] if isinstance(row[12], dict) else json.loads(row[12] or "{}")
        veto = row[8]
        return PairRow(
            lo=int(row[0]), hi=int(row[1]), probes=list(row[2] or ()),
            from_lo=bool(row[3]), from_hi=bool(row[4]), score=float(row[5] or 0.0),
            # A guard veto is stored as `reject` (the table's CHECK) and read back as the zone
            # the engine decided, so a round-trip through the store cannot move a decision.
            zone="veto" if veto else str(row[6]),
            # Both of these are the CLUSTER's inputs, not decoration: E33 orders a component's
            # edges certificate-first, E57 gates its second offer on the certificate and
            # `cluster_rows` counts the families — a read-back that dropped either would union
            # a component in a different order than the cohort pass.
            families=families_of_bitmask(int(row[9] or 0)),
            certificate=str(row[10]) if row[10] else None,
            veto=veto, reason=str(row[7] or ""),
            evidence={str(k): str(v) for k, v in evidence.items()},
            context=context, fp_lo=str(row[13] or ""), fp_hi=str(row[14] or ""),
        )

    def pairs_touching(self, ids: Iterable[int]) -> dict[tuple[int, int], PairRow]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        out: dict[tuple[int, int], PairRow] = {}
        for row in self._query(RT_PAIRS_TOUCHING_SQL,
                               {"generation": self.generation, "ids": wanted}):
            pair = self._pair_row(row)
            out[(pair.lo, pair.hi)] = pair
        return out

    def pairs_within(self, members: Iterable[int]) -> list[PairRow]:
        wanted = sorted({int(i) for i in members})
        if not wanted:
            return []
        return [self._pair_row(row) for row in self._query(
            RT_PAIRS_WITHIN_SQL, {"generation": self.generation, "ids": wanted})]

    def merge_neighbours(self, ids: Iterable[int]) -> dict[int, set[int]]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        out: dict[int, set[int]] = {i: set() for i in wanted}
        for row in self._query(RT_MERGE_NEIGHBOURS_SQL,
                               {"generation": self.generation, "ids": wanted}):
            out.setdefault(int(row[0]), set()).add(int(row[1]))
        return out

    def upsert_pairs(self, rows: Sequence[PairRow]) -> None:
        # RETENTION (E79), and it is the batch lane's rule rather than a new one:
        # `score_lane.persist` writes only `storable(row, store_floor)` — the whole merge and
        # band zones whatever they scored, plus the reject tail at or above `store_floor`
        # (0.02 at w8) — so the store grows with duplicates and not with comparisons. Measured
        # on the scoped cohort: 13,043 of 44,724 decisions are storable (the live `pairs` table
        # says the same of the batch generations: 15,811 of 48,908 over the whole cohort), so
        # this is a 3.4x saving and not a rounding. A pair that falls BELOW the floor is
        # deleted, not left behind, and being re-decided next pass costs CPU rather than bytes.
        keep: list[PairRow] = []
        evicted: list[tuple[int, int]] = []
        for row in rows:
            if storable({"zone": row.zone, "score": row.score}, self.store_floor):
                keep.append(row)
            else:
                evicted.append((row.lo, row.hi))
        self.pairs_retained += len(keep)
        self.pairs_evicted += len(evicted)
        if evicted:
            self.delete_pairs(evicted)
        self._run_many(RT_PAIR_UPSERT_SQL, [{
            "generation": self.generation,
            "listing_lo": row.lo, "listing_hi": row.hi, "probes": list(row.probes),
            "from_lo": row.from_lo, "from_hi": row.from_hi,
            # The W5 contract: `families` is a BITMASK (ATTR 1, PRICE 2, TXT 4, BRK 8, LOC 16,
            # IMG 32), which is what the UI's `families & 32` filter reads. Writing the COUNT
            # here — W9's third blocker — stored {IMG} as ATTR and {IMG,TXT} as PRICE.
            "families": families_bitmask(row.families),
            "certificate": row.certificate,
            "features": _features_json(row),
            "fp_lo": row.fp_lo, "fp_hi": row.fp_hi,
            "score": float(row.score),
            # The table's CHECK knows three zones; a veto is a reject that names its rule.
            "zone": "reject" if row.zone == "veto" else row.zone,
            "decision": row.reason, "guard_veto": row.veto,
            "evidence": json.dumps(row.evidence, ensure_ascii=False),
            "context": json.dumps(row.context),
            "calibration_digest": row.evidence.get("_calibration") or self.calibration_digest,
            "feature_version": FEATURE_VERSION,
            "model_version": row.evidence.get("_model") or self.model_version,
        } for row in keep])

    def delete_pairs(self, keys: Sequence[tuple[int, int]]) -> None:
        self._run_many(RT_PAIR_DELETE_SQL, [
            {"generation": self.generation, "listing_lo": lo, "listing_hi": hi}
            for lo, hi in keys])

    # ------------------------------------------------------------------ cluster grain
    def clusters_touching(self, members: Iterable[int]) -> dict[int, list[int]]:
        wanted = sorted({int(i) for i in members})
        if not wanted:
            return {}
        out: dict[int, list[int]] = {}
        for row in self._query(RT_CLUSTERS_TOUCHING_SQL,
                               {"generation": self.generation, "ids": wanted}):
            out.setdefault(int(row[0]), []).append(int(row[1]))
        return out

    def write_clusters(self, drop_keys: Sequence[int], rows: Sequence[Mapping[str, Any]],
                       conflicts: Sequence[Mapping[str, Any]]) -> None:
        keys = [int(key) for key in drop_keys] + [int(row["cluster_key"]) for row in rows]
        members_of: dict[int, list[int]] = {
            int(row["cluster_key"]): [int(i) for i in row["members"]] for row in rows}
        touched = sorted({i for ids in members_of.values() for i in ids})
        if keys:
            self._run(RT_PAIR_UNCLUSTER_SQL, {"generation": self.generation, "keys": keys})
            self._run(RT_CLUSTER_MEMBERS_DROP_SQL,
                      {"generation": self.generation, "keys": keys})
            self._run(RT_CLUSTER_DROP_SQL, {"generation": self.generation, "keys": keys})
        if touched:
            self._run(RT_CONFLICT_DROP_SQL, {"generation": self.generation, "ids": touched})
        self._run_many(CLUSTER_INSERT_SQL,
                       [_cluster_params(row, self.generation) for row in rows])
        self._run_many(CLUSTER_MEMBER_INSERT_SQL, [
            {"generation": self.generation, "cluster_key": key, "listing_id": listing_id,
             "joined_via_lo": None, "joined_via_hi": None}
            for key, ids in sorted(members_of.items()) for listing_id in ids])
        # A pair is an EDGE of the cluster both of its sides landed in — the score lane's rule,
        # so the UI's edge list reads the same whichever lane wrote it.
        self._run_many(RT_PAIR_CLUSTER_SQL, [
            {"generation": self.generation, "cluster_key": key, "ids": ids}
            for key, ids in sorted(members_of.items())])
        self._run_many(CLUSTER_CONFLICT_INSERT_SQL,
                       [_conflict_params(row, self.generation) for row in conflicts])

    def must_not_link(self) -> set[tuple[int, int]]:
        return {(int(row[0]), int(row[1]))
                for row in self._query(RT_MUST_NOT_LINK_SQL)}

    # ------------------------------------------------------------------ live census
    def cells(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], CellRow]:
        wanted = {(str(key), str(group)) for key, group in keys}
        missing = sorted({key for key, _group in wanted if key not in self._cells_read})
        if missing:
            for row in self._query(RT_CELL_READ_SQL,
                                   {"generation": self.generation, "keys": missing}):
                cell = (str(row[0]), str(row[1]))
                if cell not in self._cells:
                    self._cells[cell] = CellRow(
                        cell[0], cell[1], int(row[2]), list(row[3] or ()),
                        list(row[4] or ()), list(row[5] or ()), bool(row[6]))
            self._cells_read.update(missing)
        return {key: self._cells[key] for key in wanted if key in self._cells}

    def _cell(self, cell: tuple[str, str]) -> CellRow:
        if cell not in self._cells:
            self.cells([cell])
        return self._cells.setdefault(cell, CellRow(cell[0], cell[1]))

    def bump_cell(self, listing: Listing) -> None:
        cell = (address_block_key(listing), category_group(listing))
        row = self._cell(cell)
        row.n_listings += 1
        for bucket, value in (
            (row.shapes, key_token((listing.disposition,
                                    None if not listing.area_m2 else
                                    round(float(listing.area_m2), 1)))),
            (row.brokers, listing.broker_key),
            (row.source_ids, listing.source_id_native),
        ):
            if not value or value in bucket:
                continue
            if len(bucket) >= SET_CAP:
                row.capped = True
                continue
            bucket.append(value)
        self._cells_dirty.add(cell)

    def unbump_cell(self, cell: tuple[str, str]) -> None:
        key = (str(cell[0]), str(cell[1]))
        row = self._cell(key)
        row.n_listings = max(0, row.n_listings - 1)
        self._cells_dirty.add(key)

    def stamped_merges(self, blocks: Sequence[str]
                       ) -> list[tuple[int, int, ContextStamp, str, bool]]:
        wanted = sorted({str(block) for block in blocks if block})
        if not wanted:
            return []
        out: list[tuple[int, int, ContextStamp, str, bool]] = []
        for row in self._query(RT_STAMPED_MERGES_SQL,
                               {"generation": self.generation, "blocks": wanted}):
            pair = self._pair_row(row)
            stamp = ContextStamp.from_evidence(pair.evidence)
            if stamp is None:
                continue
            out.append((pair.lo, pair.hi, stamp, str(pair.context.get("block") or ""),
                        bool(pair.certificate)))
        return out

    def flush(self) -> None:
        self._barrier()
        self._run_many(RT_CELL_UPSERT_SQL, [{
            "generation": self.generation, "cell_key": cell[0],
            "category_group": cell[1], "n_listings": self._cells[cell].n_listings,
            "shapes": json.dumps(self._cells[cell].shapes),
            "brokers": json.dumps(self._cells[cell].brokers),
            "source_ids": json.dumps(self._cells[cell].source_ids),
            "capped": self._cells[cell].capped,
        } for cell in sorted(self._cells_dirty)])
        self._cells_dirty.clear()


def _features_json(row: PairRow) -> str | None:
    """The pair's feature vector in the score lane's shape — one definition, not two (E12).

    A row re-read from the store carries no vector (nothing re-scored it), and the upsert
    coalesces, so a probe-only update keeps the vector the decision was taken on rather than
    replacing it with an empty object."""
    if not row.feats:
        return None
    present = present_features({"feats": {name: list(value) for name, value in
                                          row.feats.items()}})
    return json.dumps(present, ensure_ascii=False, sort_keys=True)


def _cluster_params(row: Mapping[str, Any], generation: str) -> dict[str, Any]:
    block = (row.get("block_key") or [None])[0] if isinstance(row.get("block_key"), list) \
        else row.get("block_key")
    grain, code = (None, None)
    if isinstance(block, str) and len(block) > 1 and block[0] in "co":
        grain, code = ("cast_obce" if block[0] == "c" else "obec"), int(block[1:])
    return {
        "cluster_key": int(row["cluster_key"]), "generation": generation,
        "size": int(row["size"]), "block_key": code, "block_grain": grain,
        "cat_group": (row.get("cat_group") or [None])[0],
        "category_main": (row.get("category_main") or [None])[0],
        "category_type": (row.get("category_type") or [None])[0],
        "area_min": row.get("area_min"), "area_max": row.get("area_max"),
        "sources": list(row.get("sources") or ()),
        "medoid_listing_id": None, "min_edge_score": row.get("min_edge_score"),
        "mean_edge_score": row.get("mean_edge_score"), "n_judged_edges": 0,
        "n_certificate_edges": int(row.get("n_certificate_edges") or 0),
        "evidence_families": families_bitmask(row.get("evidence_families") or ()),
        "max_gap_days": None,
        "shared_photo_warning": bool(row.get("shared_photo_warning")),
        "status": "proposed", "model_version": None, "feature_version": FEATURE_VERSION,
    }


def _conflict_params(row: Mapping[str, Any], generation: str) -> dict[str, Any]:
    """A refused union or a refused bridge, in the score lane's own row shape."""
    lo, hi = sorted((int(row["lo"]), int(row["hi"])))
    kind = str(row.get("kind") or "invariant")
    detail: dict[str, Any] = {
        "generation": generation,
        "score": row.get("score"),
        "certificate": row.get("certificate"),
        "families": list(row.get("families") or ()),
    }
    if kind == "bridge":
        detail["left_members"] = list(row.get("left_members") or ())
        detail["right_members"] = list(row.get("right_members") or ())
    else:
        detail["members"] = list(row.get("members") or ())
    return {
        "kind": kind,
        "cluster_key_a": row.get("left_cluster"),
        "cluster_key_b": row.get("right_cluster"),
        "listing_lo": lo, "listing_hi": hi,
        "invariant": str(row.get("invariant") or "") or None,
        "detail": json.dumps(detail, ensure_ascii=False, sort_keys=True),
    }


class SqlFacts:
    """The read-only half. `public` is READ here and nowhere written (D4).

    It reads what the EXPORT lane reads and assembles it with the export's own builders, so a
    listing's facts are one definition rather than two. W9 hand-wrote a thinner version: the
    schema gate caught `loc.lat` (the store keeps a `geom`), and behind that symptom sat the
    quiet ones — no `attrs`, so every attribute feature would have been absent in production
    while the replay had them; no price history, so E19's price events; no `broker_key`, so the
    whole BRK family; `granularity` read as an enum rather than text. A replay over an exported
    artifact cannot see any of that (E77).

    The one deliberate difference from the export is the pHash population: frozen (E70) and
    read from `autodedup.phash_pop`, never recounted. A hash that table does not carry is
    UNKNOWN — `pop = None`, exactly as the export writes it when its own probe did not run —
    and never 0. W9f read it as 0 against an EMPTY table: `pop_is_measured` then said no for
    every gallery, `build_fingerprint` published `catalog_ratio = None` for every listing,
    `catalog_ratio_max` was absent on every pair and `certificate_c` — which requires the ratio
    to be PRESENT — could not fire at all. One live pass, 36,940 pairs, zero K-C against the
    batch generation's 1,771 (E91). The table has a writer now (the seed) and this counts what
    it could not measure, so a pass says so rather than scoring on it silently."""

    def __init__(self, conn: Any, clip_model: str = DEFAULT_CLIP_MODEL,
                 population: Mapping[int, int] | None = None) -> None:
        self.conn = conn
        self.clip_model = clip_model
        # A population handed in INSTEAD of the frozen table, for the one read-only caller that
        # needs to see what the table WOULD hold: `--mode rt_parity population=artifact` reads
        # the cohort's own counts so the operator can measure a seed's effect before seeding.
        # The pass never passes this — its population is the frozen one or nothing (E91).
        self.population = None if population is None else {
            int(key): int(value) for key, value in population.items()}
        self.reads = 0
        self.statements = 0
        # The population readout, for the pass summary: how many phash-bearing images this pass
        # scored with a population the frozen table could not give it.
        self.images_with_phash = 0
        self.images_unmeasured = 0
        self.hashes_unmeasured: set[int] = set()

    def _dicts(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.statements += 1
        with self.conn.cursor() as cur:
            cur.execute(sql, dict(params))
            rows = list(cur.fetchall())
            if rows and isinstance(rows[0], dict):
                return [dict(row) for row in rows]
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in rows]

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        locations = {int(row["listing_id"]): row
                     for row in self._dicts(COHORT_LOCATION_SQL, {"ids": wanted})}
        history: dict[int, list[dict[str, Any]]] = {}
        for row in self._dicts(COHORT_PRICE_HISTORY_SQL, {"ids": wanted}):
            history.setdefault(int(row["listing_id"]), []).append(row)
        listings: dict[int, Listing] = {}
        for row in self._dicts(COHORT_LISTINGS_SQL, {"ids": wanted}):
            listing_id = int(row["id"])
            # `block` is the COHORT's draw label (a reporting field the engine never reads —
            # decisions key on the fingerprint's `block_key`), and a real-time arrival belongs
            # to no draw, so it is empty here rather than invented.
            listings[listing_id] = Listing.from_json(build_listing_record(
                row, block="", location=locations.get(listing_id),
                history=history.get(listing_id, ())))
        galleries = self._galleries(wanted)
        self.reads += len(listings)
        return {i: (listing, galleries.get(i, [])) for i, listing in listings.items()}

    def _galleries(self, ids: Sequence[int]) -> dict[int, list[Image]]:
        rows = self._dicts(COHORT_IMAGES_SQL, {"ids": list(ids)})
        if not rows:
            return {}
        image_ids = [int(row["image_id"]) for row in rows]
        hashes = sorted({int(row["phash"]) for row in rows if row.get("phash") is not None})
        clips = {int(row["image_id"]): encode_clip(row["embedding"])
                 for row in self._dicts(COHORT_CLIP_SQL,
                                        {"ids": image_ids, "model": self.clip_model})}
        tags: dict[int, list[dict[str, Any]]] = {}
        for row in self._dicts(COHORT_CLIP_TAGS_SQL,
                               {"ids": image_ids, "model": self.clip_model}):
            tags.setdefault(int(row["image_id"]), []).append(row)
        # Frozen, never recounted (E70). Absent from the table is UNKNOWN, not zero (E91):
        # `public.images` carries no index on `phash` (checked: `images_phash_idx` is on
        # `sreality_id`), D8 forbids adding one, and there is no in-schema mirror to count
        # from — so the only way to measure a hash the calibration never saw is the export's
        # own sequential scan, which belongs to the export and the re-seed, never to a pass.
        if self.population is not None:
            population = {h: self.population[h] for h in hashes if h in self.population}
        else:
            population = {int(row[0]): int(row[1]) for row in
                          _rows(self.conn, RT_PHASH_POP_SQL,
                                {"hashes": hashes})} if hashes else {}
            self.statements += 1
        self.images_with_phash += sum(1 for row in rows if row.get("phash") is not None)
        unmeasured = [h for h in hashes if h not in population]
        self.hashes_unmeasured.update(unmeasured)
        if unmeasured:
            unknown = set(unmeasured)
            self.images_unmeasured += sum(
                1 for row in rows if row.get("phash") is not None
                and int(row["phash"]) in unknown)
        out: dict[int, list[Image]] = {}
        for row in rows:
            image_id = int(row["image_id"])
            record = build_image_record(row, clip=clips.get(image_id),
                                        tags=tag_pairs(tags.get(image_id, [])),
                                        pop=population)
            out.setdefault(int(row["listing_id"]), []).append(Image.from_json(record))
        return out


class SqlWork:
    """The five bounded watermark feeds, restricted to `rt_scope`, and the rule that a cursor
    moves only over what a pass actually decided.

    Every feed is settle-lagged and paged on its FULL key (E73), because neither of the two
    things that look like watermarks here is monotone on its own: `id` is assigned at INSERT
    (so overlapping batch transactions commit out of order) and `inactive_at` is the
    transaction timestamp (so a `mark_inactive` batch shares one stamp — 189 live tie groups
    are larger than one pass's share, the largest 5,534 rows).

    **The scope is what a cursor steps OVER, not what it stops at (E79).** The lane holds ~0.6%
    of the corpus, so each forward feed reads a bounded WINDOW off its own index, resolves the
    scope for that window through `listing_location_pkey`, and advances to the window's end
    even when nothing in it was in scope — a feed that advanced only over survivors would need
    as many passes as the corpus has arrivals to reach the handful that matter. When more rows
    survive than the claim's share, the list is cut and the cursor stops at the last row taken,
    so the invariant survives the optimisation: no cursor passes a row this pass did not decide.

    `scope` has no default on purpose. An unscoped `SqlWork` is the whole-corpus lane the
    operator refused, and the cheapest way to keep it unwritten is to keep it unconstructible."""

    def __init__(self, conn: Any, scope: Scope, generation: str = GENERATION,
                 lag: int = SETTLE_LAG_S, straggler_window: int = STRAGGLER_WINDOW,
                 revive_slice: int = REVIVE_SLICE, window: int = FEED_WINDOW,
                 drift_slice: int = DRIFT_SLICE, enter_slice: int = ENTER_SLICE,
                 parents: Mapping[int, int] | None = None,
                 max_retire_fraction: float = MAX_RETIRE_FRACTION,
                 enter_interval_hours: Mapping[str, float] | None = None,
                 max_enter_scans_per_day: int = MAX_ENTER_SCANS_PER_DAY,
                 evidence_slice: int = EVIDENCE_SLICE,
                 evidence_horizon_hours: float = EVIDENCE_HORIZON_HOURS,
                 bootstrap: bool = False,
                 pass_budget_s: float = PASS_BUDGET_S,
                 rate_per_s: float = PASS_RATE_PER_S) -> None:
        self.conn = conn
        self.scope = scope
        self.generation = generation
        self.lag = lag
        self.straggler_window = straggler_window
        self.revive_slice = revive_slice
        self.window = window
        self.drift_slice = drift_slice
        self.enter_slice = enter_slice
        self.max_retire_fraction = float(max_retire_fraction)
        self.enter_interval_hours = dict(enter_interval_hours or ENTER_INTERVAL_HOURS)
        self.max_enter_scans_per_day = int(max_enter_scans_per_day)
        self.evidence_slice = int(evidence_slice)
        self.evidence_horizon_hours = float(evidence_horizon_hours)
        self.bootstrap = bool(bootstrap)
        self.pass_budget_s = float(pass_budget_s)
        self.rate_per_s = float(rate_per_s)
        # Set by `claim` while the phase is on: the entrant backlog it measured, and whether
        # this pass is the one that empties it (E98).
        self.bootstrap_done = False
        self.bootstrap_backlog: int | None = None
        # What bound this pass's claim — the count or the clock — for the run summary.
        self.claim_bound: dict[str, Any] = {}
        self.parents = dict(parents or {})
        self.enter_blocks = _enter_blocks(scope, self.parents)
        self.statements = 0
        self.windows: dict[str, int] = {}
        # What this pass spent on `public` for the entrant feed, for the run summary: a block
        # refresh, a refusal against the daily cap, or nothing at all.
        self.enter_scan: dict[str, Any] = {}
        # What the evidence sweep (E92) cost and found this pass, for the run summary.
        self.evidence: dict[str, Any] = {}
        self._pending: dict[str, Any] = {}
        self._claimed: dict[str, set[int]] = {}
        self._retire: tuple[int, int] | None = None

    def _query(self, sql: str, params: Mapping[str, Any]) -> list[tuple]:
        self.statements += 1
        return _rows(self.conn, sql, params)

    def cursors(self) -> dict[str, tuple[int, int, Any]]:
        names = [CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, CURSOR_REVIVE, CURSOR_SCOPE,
                 CURSOR_ENTER, CURSOR_EVIDENCE]
        out = {name: (0, 0, _EPOCH) for name in names}
        for row in self._query(RT_CURSOR_READ_SQL, {"names": names}):
            out[str(row[0])] = (int(row[1] or 0), int(row[2] or 0), row[3] or _EPOCH)
        return out

    def claim(self, limit: int) -> list[WorkItem]:
        # THE TIME BUDGET (E98). `max_listings` bounds the work; it does not bound the CLOCK,
        # and the runner's timeout is a clock. The rate is what the last pass of this
        # generation measured itself at, so the bound is evidence rather than a guess, and it
        # is applied here — before a statement is issued — because an aborted pass has spent
        # its time whether or not it wrote anything.
        by_time = max(BOOTSTRAP_MIN_CLAIM, int(self.pass_budget_s * self.rate_per_s))
        effective = max(1, min(int(limit), by_time))
        self.claim_bound = {"max_listings": int(limit), "pass_budget_s": self.pass_budget_s,
                            "rate_per_s": self.rate_per_s, "by_time": by_time,
                            "limit": effective,
                            "bound_by": "time" if by_time < int(limit) else "count"}
        limit = effective
        cursors = self.cursors()
        share = max(1, limit // 5)
        # During the bootstrap phase the entrant feed is not one of seven equals: it is the
        # build. Every other feed keeps its share, so an arrival, a delisting or a photograph
        # landing mid-build is still decided in the pass that sees it.
        enter_share = limit if self.bootstrap else share
        scope = self.scope.params()
        items: list[WorkItem] = []
        self._pending = {}
        self.windows = {}
        self._claimed = {}

        after_new = cursors[CURSOR_NEW][0]
        # The straggler sweep FIRST: a row that committed after the cursor passed its id is
        # older work than anything the forward feed is about to hand over. It carries no
        # cursor value — it is BEHIND the watermark by definition.
        if after_new and self.straggler_window:
            for row in self._query(RT_NEW_STRAGGLERS_SQL, {
                    "after_id": after_new, "window": self.straggler_window,
                    "generation": self.generation, "limit": share, **scope}):
                items.append(WorkItem(int(row[0]), "straggler", _epoch(row[1]), None))

        end, size, ids, stamps = _window(self._query(RT_NEW_LISTINGS_SQL, {
            "after_id": after_new, "lag": self.lag, "window": self.window, **scope}), after_new)
        self.windows["new"] = size
        if size:
            taken = min(len(ids), share)
            self._pending[CURSOR_NEW] = (int(ids[taken - 1]) if taken < len(ids) else int(end))
            for listing_id, stamp in zip(ids[:taken], stamps[:taken]):
                items.append(WorkItem(int(listing_id), "new", _epoch(stamp), None))

        after_changed = cursors[CURSOR_CHANGED][1]
        end, size, ids, stamps, snapshot_ids = _window(self._query(RT_CHANGED_LISTINGS_SQL, {
            "after_id": after_changed, "lag": self.lag, "window": self.window, **scope}),
            after_changed, extra=True)
        self.windows["changed"] = size
        if size:
            taken = min(len(ids), share)
            self._pending[CURSOR_CHANGED] = (int(snapshot_ids[taken - 1])
                                             if taken < len(ids) else int(end))
            for listing_id, stamp in zip(ids[:taken], stamps[:taken]):
                items.append(WorkItem(int(listing_id), "changed", _epoch(stamp), None))

        stamp_end, id_end, size, ids, stamps = _flip_window(self._query(
            RT_FLIPPED_LISTINGS_SQL, {
                "after": cursors[CURSOR_FLIPPED][2], "after_id": cursors[CURSOR_FLIPPED][0],
                "lag": self.lag, "window": self.window, **scope}))
        self.windows["flipped"] = size
        if size:
            taken = min(len(ids), share)
            self._pending[CURSOR_FLIPPED] = ((stamps[taken - 1], int(ids[taken - 1]))
                                             if taken < len(ids)
                                             else (stamp_end, int(id_end)))
            for listing_id, stamp in zip(ids[:taken], stamps[:taken]):
                items.append(WorkItem(int(listing_id), "flipped", _epoch(stamp), None))

        if self.revive_slice:
            after_revive = cursors[CURSOR_REVIVE][0]
            rows = self._query(RT_REVIVED_SQL, {
                "generation": self.generation, "after_id": after_revive,
                "limit": self.revive_slice, **scope})
            slice_max, slice_size, revived = (
                (int(rows[0][0]), int(rows[0][1]), list(rows[0][2] or ()))
                if rows else (after_revive, 0, []))
            # A short slice is the end of the sweep, so the cursor wraps and the next pass
            # starts the cycle again.
            self._pending[CURSOR_REVIVE] = 0 if slice_size < self.revive_slice else slice_max
            for listing_id in revived:
                items.append(WorkItem(int(listing_id), "revived", None, None))

        # The fifth feed (E79): the rows of this generation the scope has stopped holding.
        if self.drift_slice:
            after_scope = cursors[CURSOR_SCOPE][0]
            rows = self._query(RT_SCOPE_DRIFT_SQL, {
                "generation": self.generation, "after_id": after_scope,
                "limit": self.drift_slice, **scope})
            slice_max, slice_size, departed = (
                (int(rows[0][0]), int(rows[0][1]), list(rows[0][2] or ()))
                if rows else (after_scope, 0, []))
            retiring = [int(value) for value in departed[:share]]
            self._guard_retirement(departed, len(retiring))
            self._pending[CURSOR_SCOPE] = 0 if slice_size < self.drift_slice else slice_max
            for listing_id in retiring:
                items.append(WorkItem(listing_id, "drifted", None, None, retire=True))

        # The sixth feed (W9d-3), at W9e's cost (R3): the rows the scope has started holding
        # and no cursor has ever seen, because `listing_location` is written after the listing
        # is. A pass reads them out of `autodedup.rt_scope_ids` — an anti-join inside this
        # schema, ZERO blocks of `public` — and the wide scan that fills that snapshot runs on
        # a cadence, one block at a time, under a rolling-day cap.
        if self.enter_slice and self.enter_blocks:
            pointer = self._refresh_scope_ids(cursors[CURSOR_ENTER][1])
            after_enter = cursors[CURSOR_ENTER][0]
            rows = self._query(RT_SCOPE_ENTRANTS_SQL, {
                "generation": self.generation, "after_id": after_enter,
                "lag": self.lag, "limit": enter_share})
            self.windows["entered"] = len(rows)
            # Short of the share means the snapshot is walked out: the cursor wraps so the
            # next cycle re-offers whatever the settle lag held back this time.
            self._pending[CURSOR_ENTER] = (
                (int(rows[-1][0]), pointer) if len(rows) >= enter_share else (0, pointer))
            for row in rows:
                items.append(WorkItem(int(row[0]), "entered", _epoch(row[1]), None))
            if self.bootstrap:
                self._measure_bootstrap(len(rows))

        # The SEVENTH feed (E92): the listings whose PHOTOGRAPHS moved since this generation
        # decided them, and the merges whose hold has run out of horizon. Neither is visible
        # to any of the six above — a phash or a CLIP vector arriving appends no snapshot,
        # flips no flag and moves no location — which is why ~80% of arrivals were decided
        # once, blind, and never again.
        if self.evidence_slice:
            # A listing the drift sweep is RETIRING this pass is not re-decided: the scope no
            # longer holds it, so there is nothing to re-decide it into.
            retiring = {item.listing_id for item in items if item.retire}
            items.extend(self._evidence_feed(cursors[CURSOR_EVIDENCE][0], share, retiring))
        for item in items:
            self._claimed.setdefault(item.feed, set()).add(item.listing_id)
        return items

    def _measure_bootstrap(self, taken: int) -> None:
        """Does this pass end the phase (E98)? Two conditions, both read inside `autodedup`.

        Every block has to have been walked at least once — otherwise a block's listings have
        never been OFFERED — and the backlog the snapshot still holds has to fit in what this
        pass just claimed. The phase then ends by itself: no operator step, no second dispatch,
        and nothing to remember when the build finishes at three in the morning."""
        walked = self._walked_ever()
        backlog = int(self._query(RT_SCOPE_BACKLOG_SQL,
                                  {"generation": self.generation})[0][0] or 0)
        self.bootstrap_backlog = backlog
        self.bootstrap_done = bool(
            backlog <= int(taken) and all(block.key in walked for block in self.enter_blocks))

    def _evidence_feed(self, after_id: int, share: int,
                       retiring: set[int] | None = None) -> list[WorkItem]:
        """Round-robin this generation's incomplete or young rows, probe `public.images` for
        what their photographs ARE now, and hand over the ones that disagree with what the
        stored decision rested on (E92) — plus the endpoints of every merge whose hold the
        horizon has ended (E93).

        The candidate arm reads `autodedup.rt_fp` only. The probe is the one read of
        `public.images` outside a fact fetch, it is served by `images_listing_id_idx`, and it
        reads NO other table: `images.clip_tagged_at` is the CLIP job's own stamp, and on the
        trial scope's 73,208 images it agrees with `image_clip_embeddings` and
        `image_clip_tags` on every row (72,171 each, 0 disagreements), at a fourteenth of the
        buffers the two joins cost."""
        horizon = {"horizon_hours": int(round(self.evidence_horizon_hours))}
        rows = self._query(RT_EVIDENCE_CANDIDATES_SQL, {
            "generation": self.generation, "after_id": int(after_id),
            "limit": self.evidence_slice, **horizon})
        recorded = {int(row[0]): (row[1], row[2], row[3], row[4]) for row in rows}
        probed: dict[int, tuple[int, int, int]] = {}
        if recorded:
            probed = {int(row[0]): (int(row[1]), int(row[2]), int(row[3]))
                      for row in self._query(RT_EVIDENCE_PROBE_SQL,
                                             {"ids": sorted(recorded)})}
        moved: list[int] = []
        for listing_id, (images, phash, clip, tags) in sorted(recorded.items()):
            live = probed.get(listing_id, (0, 0, 0))
            # A NULL count is a row written before migration 540 — unmeasured, not zero, so
            # it is re-decided once and then carries real counts.
            if images is None or phash is None:
                moved.append(listing_id)
                continue
            if (live[0] != int(images) or live[1] != int(phash)
                    or live[2] != int(clip or 0) or live[2] != int(tags or 0)):
                moved.append(listing_id)
        released: list[int] = []
        for row in self._query(RT_EVIDENCE_RELEASE_SQL, {
                "generation": self.generation, "reason": EVIDENCE_HOLD_REASON,
                "limit": max(1, share), **horizon}):
            released.extend((int(row[0]), int(row[1])))
        held_total = int(self._query(RT_EVIDENCE_HELD_COUNT_SQL, {
            "generation": self.generation, "reason": EVIDENCE_HOLD_REASON})[0][0] or 0)
        # A short slice is the end of the sweep, so the cursor wraps — the same round-robin
        # the drift sweep uses, and the reason a candidate cut by `share` is re-offered.
        self._pending[CURSOR_EVIDENCE] = (
            0 if len(rows) < self.evidence_slice else max(recorded, default=int(after_id)))
        self.windows["evidence"] = len(rows)
        self.evidence = {
            "candidates": len(rows), "listings_probed": len(recorded),
            "images_probed": sum(row[0] for row in probed.values()),
            "moved": len(moved), "release_candidates": len(set(released)),
            "held_pairs": held_total,
            "horizon_hours": self.evidence_horizon_hours, "slice": self.evidence_slice,
        }
        gone = set(retiring or ())
        wanted: list[int] = []
        for listing_id in moved[:share] + sorted(set(released)):
            if listing_id not in wanted and listing_id not in gone:
                wanted.append(listing_id)
        return [WorkItem(listing_id, "evidence", None, None, redecide=True)
                for listing_id in wanted]

    def _refresh_scope_ids(self, pointer: int) -> int:
        """Re-walk ONE scope block on `public`, but only when its cadence says so (W9e/R3).

        This is the whole cost of the entrant feed, and it used to be paid every cycle: the
        quarter block is a 29,265-block bitmap heap scan of its parent obec — 231 MB and 6.4 s,
        cold every time, because the working set does not stay in shared_buffers — and there is
        no narrower path (no index on `cast_obce_kod`, D8 forbids adding one, and `listings`
        carries no indexed obec or quarter column either). At ~48 walks a day that was ~11 GB a
        day of cold reads on the instance that serves Browse, whether or not anything had
        entered. Now it is `rt_enter_interval_hours` a block — 6 h for a quarter, 1 h for a
        town — the block the interval has been waiting on longest first, at most ONE a pass,
        and never more than `rt_enter_max_scans_per_day` in a rolling day. What that cadence
        costs in latency it costs only on the TAIL: an entrant that arrived recently is already
        the straggler sweep's.

        Returns the block pointer the cursor should carry, so ties (nothing scanned yet) rotate
        instead of always naming the same block."""
        blocks = self.enter_blocks
        self.statements += 1
        _exec(self.conn, RT_SCOPE_IDS_PRUNE_BLOCKS_SQL, {
            "generation": self.generation, "block_keys": [block.key for block in blocks]})
        state = {str(row[0]): (row[1], int(row[2] or 0))
                 for row in self._query(RT_SCOPE_SCAN_STATE_SQL, {
                     "generation": self.generation, "hours": 24})}
        start = int(pointer) % len(blocks)
        due: list[tuple[float, int, int]] = []
        for offset in range(len(blocks)):
            index = (start + offset) % len(blocks)
            block = blocks[index]
            age, _scans = state.get(block.key, (None, 0))
            interval = float(self.enter_interval_hours.get(
                block.grain, ENTER_INTERVAL_HOURS.get(block.grain, 6.0))) * 3600.0
            if age is None:
                due.append((float("inf"), offset, index))
            elif float(age) >= interval:
                due.append((float(age), offset, index))
        if not due:
            return start
        scans_24h = sum(scans for _age, scans in state.values())
        # THE BOOTSTRAP PHASE (E98). A cadence is what keeps a steady-state lane off `public`;
        # a scope nothing has listed yet has no steady state, and one block a pass means the
        # entrant feed offers nothing at all until the pass that walks the block a listing is
        # in. So while the phase is on, every block this generation has NEVER walked is walked
        # in the same pass — the cadence is ignored, the cap is not — and a block already
        # walked once falls back to the cadence immediately, because re-walking Vysočany is
        # 231 MB of cold heap reads and the phase is about listing it, not about refreshing it.
        seen = self._walked_ever() if self.bootstrap else set()
        order = [index for _age, _offset, index in
                 sorted(due, key=lambda row: (-row[0], row[1]))]
        wanted = ([index for index in order if blocks[index].key not in seen]
                  if self.bootstrap else []) or order[:1]
        walked: list[dict[str, Any]] = []
        last = start
        for index in wanted:
            if scans_24h >= self.max_enter_scans_per_day:
                # A count, not a crash: the pass goes on claiming out of the snapshot it has.
                self.enter_scan = {"skipped": "daily cap", "scans_24h": scans_24h,
                                   "cap": self.max_enter_scans_per_day, "blocks": walked}
                return last if walked else start
            block = blocks[index]
            started = time.time()
            rows = self._query(RT_SCOPE_BLOCK_SQL, {
                "obec": block.obec, "cast_obce": block.cast_obce, "limit": self.enter_slice})
            elapsed_ms = (time.time() - started) * 1000.0
            listing_ids = [int(row[0]) for row in rows]
            params = {"generation": self.generation, "block_key": block.key,
                      "listing_ids": listing_ids}
            if listing_ids:
                self.statements += 1
                _exec(self.conn, RT_SCOPE_IDS_WRITE_SQL,
                      {**params, "resolved": [_iso(row[1]) for row in rows]})
            self.statements += 1
            _exec(self.conn, RT_SCOPE_IDS_PRUNE_SQL, params)
            self.statements += 1
            _exec(self.conn, RT_SCOPE_SCAN_WRITE_SQL, {
                "generation": self.generation, "block_key": block.key,
                "rows_found": len(listing_ids), "elapsed_ms": round(elapsed_ms, 3)})
            scans_24h += 1
            walked.append({"block": block.key, "rows": len(listing_ids),
                           "elapsed_ms": round(elapsed_ms, 3)})
            last = (index + 1) % len(blocks)
        self.enter_scan = {**walked[-1], "scans_24h": scans_24h,
                           "cap": self.max_enter_scans_per_day, "blocks": walked}
        return last

    def _walked_ever(self) -> set[str]:
        """The scope blocks this generation HAS a scan row for, ever (E98).

        Not the cadence's own state query: that one is windowed to a rolling day, so a block
        walked two days ago reads the same as a block never walked, and "every block has been
        walked once" is a question about all of history."""
        return {str(row[0]) for row in
                self._query(RT_SCOPE_SCAN_SEEN_SQL, {"generation": self.generation})}

    def _guard_retirement(self, departed: Sequence[Any], wanted: int) -> None:
        """Refuse a drift sweep that is not drift (W9d-1), measured over a ROLLING DAY (W9e/R2).

        The sweep reads the scope as a PREDICATE, so a scope that holds nothing — a settings
        row hand-written as `","`, a rescope that lost its blocks — reports the whole store as
        departed and the lane grinds it away a share at a time, silently, until a re-seed is
        the only recovery. W9d's rail compared ONE slice's departed count with a fraction of
        the store, which is a rail only while the slice is itself that big: above ~400,000 rows
        — or under a `drift_slice=1` dispatch argument — the grind was unguarded. So the
        measurement is what the generation has RETIRED in the last `RETIRE_WINDOW_HOURS`, this
        pass included, against the store as it stood when that window opened. Three rails, each
        independent of the others: a scope with no blocks retires nothing at all, the rolling
        day is capped, and over it the lane STOPS — non-zero, transaction rolled back, cursor
        unmoved."""
        self._retire = None
        if not departed:
            return
        if not self.scope.whole_corpus and not self.scope.blocks:
            raise RetireRefusal(
                f"the drift sweep would retire {len(departed)} listings under an EMPTY scope — "
                "refusing. Nothing was written and no cursor moved.")
        store_rows = int(_rows(self.conn, RT_FP_COUNT_SQL,
                               {"generation": self.generation})[0][0] or 0)
        self.statements += 1
        window = _rows(self.conn, RT_RETIRE_WINDOW_SQL,
                       {"generation": self.generation, "hours": RETIRE_WINDOW_HOURS})
        self.statements += 1
        retired_before = int((window[0][0] if window else 0) or 0)
        baseline_raw = window[0][1] if window else None
        # The store as the window OPENED: a denominator that shrank with every retirement
        # would let a slow grind stay under the fraction for ever.
        baseline = int(baseline_raw) if baseline_raw else store_rows
        allowed = max(1, int(self.max_retire_fraction * baseline))
        if retired_before + wanted > allowed:
            raise RetireRefusal(
                f"the drift sweep would retire {wanted} listings on top of the "
                f"{retired_before} this generation has retired in the last "
                f"{RETIRE_WINDOW_HOURS} h, over the {self.max_retire_fraction:.0%} "
                f"{RETIRE_SETTING} rail ({allowed} of the {baseline} rows the store held when "
                "that window opened) — refusing. That is a scope that has gone wrong, not a "
                "geocode correction. Nothing was written and no cursor moved; check "
                f"autodedup.settings {SCOPE_SETTING}:{self.generation}.")
        self._retire = (wanted, baseline)

    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]:
        """Advance each feed to the end of the window THIS pass decided, and no further.

        The cursor VALUES were fixed at claim time, because a feed's watermark is its window's
        end and a window that yielded no in-scope row still has one. What `done` decides is
        whether they are written at all: a feed advances only when every item it handed over
        comes back decided, so a refused pass (E75) — which hands back nothing — advances
        nothing, while an all-out-of-scope window still crosses."""
        out: dict[str, Any] = {}
        pending, self._pending = self._pending, {}
        claimed, self._claimed = self._claimed, {}
        decided = {item.listing_id for item in done}
        for feed, name in (("new", CURSOR_NEW), ("changed", CURSOR_CHANGED),
                           ("flipped", CURSOR_FLIPPED), ("revived", CURSOR_REVIVE),
                           ("drifted", CURSOR_SCOPE), ("entered", CURSOR_ENTER),
                           ("evidence", CURSOR_EVIDENCE)):
            if not claimed.get(feed, set()) <= decided:
                pending.pop(name, None)
        if CURSOR_NEW in pending:
            value = int(pending[CURSOR_NEW])
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_NEW, "last_listing_id": value,
                "last_snapshot_id": None, "watermark": None})
            out[CURSOR_NEW] = value
        if CURSOR_CHANGED in pending:
            value = int(pending[CURSOR_CHANGED])
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_CHANGED, "last_listing_id": None,
                "last_snapshot_id": value, "watermark": None})
            out[CURSOR_CHANGED] = value
        if CURSOR_FLIPPED in pending:
            stamp, listing_id = pending[CURSOR_FLIPPED]
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_FLIPPED, "last_listing_id": int(listing_id),
                "last_snapshot_id": None, "watermark": stamp})
            out[CURSOR_FLIPPED] = [str(stamp), int(listing_id)]
        for name in (CURSOR_REVIVE, CURSOR_SCOPE, CURSOR_EVIDENCE):
            if name in pending:
                self.statements += 1
                _exec(self.conn, RT_CURSOR_SET_SQL, {
                    "name": name, "last_listing_id": int(pending[name])})
                out[name] = int(pending[name])
        # The retirement ledger (W9e/R2): written where the retirement itself is, inside the
        # pass's transaction, so a rolled-back pass leaves the rolling day exactly as it found
        # it. `store_rows` is the denominator the rail measured, not today's shrinking store.
        retired = sorted(claimed.get("drifted", set()) & decided)
        if retired and self._retire is not None:
            self.statements += 1
            _exec(self.conn, RT_RETIRE_EVENT_WRITE_SQL, {
                "generation": self.generation, "n_retired": len(retired),
                "store_rows": int(self._retire[1])})
        if CURSOR_ENTER in pending:
            after_id, block = pending[CURSOR_ENTER]
            self.statements += 1
            # Both halves are written as VALUES: the sweep wraps to 0 inside a block and
            # rolls the block pointer over, and the write coalesces on NULL, never on 0.
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_ENTER, "last_listing_id": int(after_id),
                "last_snapshot_id": int(block), "watermark": None})
            out[CURSOR_ENTER] = [int(after_id), int(block)]
        return out


def _window(rows: Sequence[Sequence[Any]], after: int, extra: bool = False) -> tuple:
    """A window feed's single row: its end, its size, the in-scope ids and their stamps."""
    if not rows:
        return (after, 0, [], [], []) if extra else (after, 0, [], [])
    row = rows[0]
    end = int(row[0] if row[0] is not None else after)
    size = int(row[1] or 0)
    ids = [int(value) for value in (row[2] or ())]
    stamps = list(row[3] or ())
    if extra:
        return end, size, ids, stamps, [int(value) for value in (row[4] or ())]
    return end, size, ids, stamps


def _flip_window(rows: Sequence[Sequence[Any]]) -> tuple[Any, Any, int, list[int], list[Any]]:
    """The flip feed's window end is the LAST row of `(inactive_at, id)` order, never the
    maximum of either column on its own."""
    if not rows or not int(rows[0][2] or 0):
        return (None, 0, 0, [], [])
    row = rows[0]
    return (row[0], int(row[1] or 0), int(row[2] or 0),
            [int(value) for value in (row[3] or ())], list(row[4] or ()))


class StorageRefusal(Exception):
    """The schema is already over budget. The lane exits non-zero with nothing written."""


class RetireRefusal(Exception):
    """The drift sweep wants to retire more of the store than drift ever could (W9d-1)."""


@dataclass(frozen=True, slots=True)
class EnterBlock:
    """One block of the scope as the entrant sweep reads it: always an obec (the index this
    schema HAS), plus the quarter code when the block is a quarter (a filter, not an index).

    `grain` and `code` are the SCOPE's own spelling of the block, because that is what the
    cadence and the scan ledger are keyed by (W9e/R3) — a quarter walked through its parent
    obec is still a quarter, and it is the quarter's 231 MB that earns the slower interval."""

    grain: str
    code: int
    obec: int
    cast_obce: int | None = None

    @property
    def key(self) -> str:
        return f"{self.grain}:{self.code}"


def _enter_blocks(scope: Scope, parents: Mapping[int, int]) -> tuple[EnterBlock, ...]:
    if scope.whole_corpus:
        return ()        # nothing can enter a scope that already holds everything
    blocks: list[EnterBlock] = []
    for code in scope.obec_codes:
        blocks.append(EnterBlock("obec", int(code), int(code)))
    for code in scope.cast_obce_codes:
        parent = parents.get(int(code))
        # A quarter with no parent is refused by `resolve_scope_parents` before a pass is
        # built, so here it can only be a caller that resolved none: sweep what CAN be swept
        # rather than refusing a second time in the wrong place.
        if parent is not None:
            blocks.append(EnterBlock("cast_obce", int(code), int(parent), int(code)))
    return tuple(blocks)


def resolve_scope_parents(conn: Any, scope: Scope) -> dict[int, int]:
    """Each quarter block's parent obec, read once a pass from the RÚIAN register (W9d-3).

    A quarter the register cannot place is a hard error rather than a block the entrant sweep
    quietly never walks — silent partial coverage is the defect this feed exists to close."""
    codes = [int(code) for code in scope.cast_obce_codes]
    if scope.whole_corpus or not codes:
        return {}
    found = {int(row[0]): int(row[1])
             for row in _rows(conn, RT_SCOPE_PARENT_OBEC_SQL, {"codes": codes})}
    missing = [code for code in codes if code not in found]
    if missing:
        raise ScopeError(
            f"rt_scope names cast_obce {missing} which public.ruian_admin_units has no obec "
            "parent for — refusing to run a scope the entrant sweep cannot walk")
    return found


def lane_settings(conn: Any, keys: Sequence[str]) -> dict[str, Any]:
    """The lane's own control rows out of `autodedup.settings` — one statement, not one each."""
    out: dict[str, Any] = {}
    for row in _rows(conn, RT_SETTINGS_MANY_SQL, {"keys": list(keys)}):
        out[str(row[0])] = row[1]
    return out


def _setting_number(value: Any, fallback: float) -> float:
    """A control number, or a HARD ERROR (W9e/R6).

    W9d fell back to the default whenever `float()` refused the value, so
    `rt_max_retire_fraction = "0,05"` — or a settings row holding an object the lane did not
    expect — disabled nothing and said nothing: the rail simply ran at a number nobody had
    chosen. A control row that cannot be read is a lane that must not run."""
    if value is None:
        return float(fallback)
    raw = value
    if isinstance(raw, Mapping):
        raw = raw.get("value", raw.get("mb"))
        if raw is None:
            return float(fallback)
    if isinstance(raw, bool):
        raise ScopeError(f"expected a number, got {value!r}")
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ScopeError(f"expected a number, got {value!r}") from None


def scope_setting_key(generation: str) -> str:
    """`rt_scope:<generation>` (W9e/R4). One global row meant a second generation's seed
    silently rescoped the first — and a scope that differs a LITTLE is retirement under a rail
    that was built for the case where it differs a lot."""
    return f"{SCOPE_SETTING}:{generation}"


def bootstrap_setting_key(generation: str) -> str:
    """`rt_bootstrap:<generation>` — the phase is a property of ONE generation's build."""
    return f"{BOOTSTRAP_SETTING}:{generation}"


def pass_rate_key(generation: str) -> str:
    """`rt_pass_rate_per_s:<generation>` — what a pass of THIS generation measured itself at."""
    return f"{PASS_RATE_SETTING}:{generation}"


def setting_flag(value: Any) -> bool:
    """A settings row read as a boolean. `true`, `True` and `"true"` all mean the same thing,
    and anything else — a missing row included — means off, because the phases and switches
    these rows carry are all opt-in."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def read_scope_setting(control: Mapping[str, Any], generation: str) -> Any:
    """The generation's scope row, and the legacy global row for `rt` alone (W9e/R4)."""
    value = control.get(scope_setting_key(generation))
    if value is None and generation == LEGACY_SCOPE_GENERATION:
        return control.get(SCOPE_SETTING)
    return value


def control_number(key: str, args: Mapping[str, Any], control: Mapping[str, Any],
                   fallback: float, rescope: bool) -> float:
    """A control number's resolution order — and what a DISPATCH may do to it (W9e/R6).

    `rt_max_retire_fraction` was a plain dispatch argument, so one `args=rt_max_retire_fraction=1`
    disabled the store's last rail for that pass with nothing persisted and nothing said. These
    numbers are `autodedup.settings` rows: the argument is accepted only beside `rt_rescope=true`
    — the same word the operator already has to say to move a generation's scope — and a value
    that is not a number stops the lane rather than reverting to a default."""
    if key in args and str(args.get(key) or "").strip() != "":
        if not rescope:
            raise ScopeError(
                f"{key} is an autodedup.settings row, not a dispatch argument — it governs a "
                f"rail, and a pass that could move it could disable it. Write the row, or "
                f"pass {RESCOPE_ARG}=true to mean it for this pass.")
        return _setting_number(args[key], fallback)
    return _setting_number(control.get(key), fallback)


def enter_intervals(args: Mapping[str, Any], control: Mapping[str, Any],
                    rescope: bool) -> dict[str, float]:
    """The per-grain entrant cadence (W9e/R3), as data: a number for every grain, or an object
    keyed by grain (`{"obec": 1, "cast_obce": 6}`). Unreadable is a hard error, never a
    default — the cadence IS the cost control."""
    out = dict(ENTER_INTERVAL_HOURS)
    value = control.get(INTERVAL_SETTING)
    if isinstance(value, Mapping) and not {"value", "mb"} & set(value):
        for grain, hours in value.items():
            out[str(grain)] = _setting_number(hours, ENTER_INTERVAL_HOURS.get(str(grain), 6.0))
    elif value is not None:
        out = {grain: _setting_number(value, hours) for grain, hours in out.items()}
    if INTERVAL_SETTING in args and str(args.get(INTERVAL_SETTING) or "").strip() != "":
        if not rescope:
            raise ScopeError(
                f"{INTERVAL_SETTING} is an autodedup.settings row, not a dispatch argument — "
                f"pass {RESCOPE_ARG}=true to mean it for this pass.")
        hours = _setting_number(args[INTERVAL_SETTING], 0.0)
        out = {grain: hours for grain in out}
    return out


def schema_bytes(conn: Any) -> int:
    return int(_rows(conn, RT_SCHEMA_SIZE_SQL)[0][0] or 0)


def rt_rows(conn: Any, generation: str, scope: Scope) -> dict[str, int]:
    """What the generation holds, by table. Exact while the scope is small — which is the
    point of a scope — and the planner's estimate when there is none to keep it cheap."""
    sql = RT_ROW_ESTIMATE_SQL if scope.whole_corpus else RT_ROW_CENSUS_SQL
    params = None if scope.whole_corpus else {"generation": generation}
    return {str(row[0]): int(row[1] or 0) for row in _rows(conn, sql, params)}


def storage_guard(conn: Any, generation: str, scope: Scope,
                  max_schema_mb: float) -> dict[str, Any]:
    """Read what schema `autodedup` costs BEFORE the pass writes anything, and refuse over
    budget (E79).

    The operator pays for this store by the megabyte, and the failure this guards is not a
    crash but a silent one: a lane nobody watches for a week, adding rows at 10-minute
    cadence. So the check is a REFUSAL — non-zero, cursor unmoved, nothing written — and never
    a warning in a log line. It also holds the second half of the whole-corpus gate: `all` is a
    scope only when the budget could hold what `all` costs."""
    bytes_now = schema_bytes(conn)
    mb = bytes_now / 1_048_576.0
    previous = lane_settings(conn, [STORAGE_WATERMARK]).get(STORAGE_WATERMARK) or {}
    last = previous.get("bytes") if isinstance(previous, Mapping) else None
    report: dict[str, Any] = {
        "schema_mb": round(mb, 2),
        "max_schema_mb": round(float(max_schema_mb), 2),
        "rows": rt_rows(conn, generation, scope),
        "growth_mb": (round((bytes_now - int(last)) / 1_048_576.0, 3)
                      if last is not None else None),
        "scope": scope.as_json(),
    }
    if mb > float(max_schema_mb):
        raise StorageRefusal(
            f"schema autodedup is {mb:.1f} MB, over the {float(max_schema_mb):.0f} MB "
            f"{BUDGET_SETTING} budget — refusing to write. Prune a generation or raise the "
            "setting; nothing was written and no cursor moved.")
    if not guard_agrees(scope, max_schema_mb):
        raise StorageRefusal(
            f"rt_scope=all needs {BUDGET_SETTING} >= {CORPUS_PROJECTION_MB} MB (the measured "
            f"whole-corpus projection) and it is {float(max_schema_mb):.0f} — refusing. A "
            "whole-corpus generation is a budget decision, not a scope one.")
    return report


def record_storage(conn: Any, generation: str, bytes_now: int) -> None:
    _exec(conn, RT_SETTING_WRITE_SQL, {
        "key": STORAGE_WATERMARK,
        "value": json.dumps({"bytes": int(bytes_now), "generation": generation,
                             "at": datetime.now(timezone.utc).isoformat()}),
        "updated_by": LANE_NAME})


# The cursor rows ONE generation owns. `autodedup.scan_cursor` is keyed on the name alone, so
# the generation's rows are named here rather than filtered by a column the table does not have
# — which is also why two real-time generations cannot run at once, and why the reset says so.
RESET_CURSORS: tuple[str, ...] = (CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, CURSOR_REVIVE,
                                  CURSOR_SCOPE, CURSOR_ENTER, CURSOR_EVIDENCE)

# The reset's statements, in the order they run: members and conflicts before the clusters they
# name, pairs before the fingerprints they were scored from. Nothing here is an FK requirement —
# schema `autodedup` declares none between these — it is so a half-applied reset, if one were
# ever possible, could not leave a cluster with no members.
RESET_TABLES: tuple[tuple[str, str], ...] = (
    ("pairs", RT_FRESH_PAIRS_SQL),
    ("cluster_members", RT_FRESH_CLUSTER_MEMBERS_SQL),
    ("cluster_conflicts", RT_FRESH_CLUSTER_CONFLICTS_SQL),
    ("clusters", RT_FRESH_CLUSTERS_SQL),
    ("rt_fp", RT_FRESH_RT_FP_SQL),
    ("fp_key", RT_FRESH_FP_KEY_SQL),
    ("rt_block_cell", RT_FRESH_BLOCK_CELL_SQL),
    ("rt_scope_ids", RT_FRESH_SCOPE_IDS_SQL),
    ("rt_scope_scan", RT_FRESH_SCOPE_SCAN_SQL),
    ("rt_retire_event", RT_FRESH_RETIRE_EVENT_SQL),
)


def reset_generation(conn: Any, generation: str) -> dict[str, int]:
    """Empty ONE generation and nothing else (E97), counting what went.

    `reseed=true` re-cuts the calibration, the frozen population and the parity baseline, and
    leaves every pair and cluster the generation already holds exactly where they are — which is
    right when the re-seed is a REFRESH of a generation scored under the same scorer (E95's
    refresh recipe) and wrong when it is a rebuild. On 2026-09-20 it was a rebuild: the 15,923
    pairs and 703 clusters written by the defective first pass (wrong scorer, `model_version`
    NULL on every row, no photo evidence) survived the re-seed, and nothing in the lane will ever
    re-decide a pair whose fingerprint digests still agree. So `fresh=true` is the other half of
    `reseed=true`, and it is a DELETE of this generation's rows, never a TRUNCATE.

    The caller runs it inside the seed's transaction: a seed that refuses after this point — an
    empty scope, a refused parity baseline, a storage guard — leaves the store exactly as it
    found it."""
    deleted: dict[str, int] = {}
    for name, sql in RESET_TABLES:
        rows = _rows(conn, sql, {"generation": generation})
        deleted[name] = int(rows[0][0] or 0) if rows else 0
    rows = _rows(conn, RT_FRESH_CURSORS_SQL, {"names": list(RESET_CURSORS)})
    deleted["scan_cursor"] = int(rows[0][0] or 0) if rows else 0
    rows = _rows(conn, RT_FRESH_LEASE_SQL, {"name": LANE_NAME})
    deleted["rt_lease"] = int(rows[0][0] or 0) if rows else 0
    return deleted


def env_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The workflow's own gate, re-read here so a dispatched run cannot bypass the variable."""
    source = os.environ if env is None else env
    return str(source.get(ENV_FLAG, "")).strip().lower() == "true"


def db_enabled(conn: Any) -> bool:
    """The operator's stop button, in the database: `realtime_enabled = false` halts the lane.

    Absent means NOT BLOCKED — the repository variable is what makes the lane dark by default,
    and this row is what stops one already running, with no workflow edit and no deploy."""
    rows = _rows(conn, RT_SETTING_SQL, {"key": DB_FLAG})
    if not rows:
        return True
    value = rows[0][0]
    if isinstance(value, dict):
        value = value.get("enabled", value.get("value"))
    return str(value).strip().lower() not in ("false", "0", "off", "no")


def take_lease(conn: Any, holder: str, ttl: int = LEASE_TTL_S) -> bool:
    rows = _rows(conn, RT_LEASE_TAKE_SQL,
                 {"name": LANE_NAME, "holder": holder, "ttl": ttl})
    return bool(rows) and str(rows[0][0]) == holder


def release_lease(conn: Any, holder: str) -> None:
    _exec(conn, RT_LEASE_RELEASE_SQL, {"name": LANE_NAME, "holder": holder})


def _transaction(conn: Any) -> ContextManager[Any]:
    """One pass, one transaction (E75). `db.connect` is autocommit, so without this a crash
    between the cluster DELETE and its INSERT loses those clusters permanently — the re-claim
    finds matching digests, skips, and never rebuilds them."""
    opener = getattr(conn, "transaction", None)
    return opener() if callable(opener) else nullcontext()


class _Refused(Exception):
    """The pair budget refused this claim — roll the pass back and report it (E75)."""


def seed_parity(conn: Any, dataset: Any, in_scope: Sequence[int], generation: str,
                n: int, floors: ParityFloors) -> dict[str, Any]:
    """The seed's own gate, and the baseline every later pass re-checks against.

    Here the whole artifact is in hand, so the comparison is the real one: a stratified sample
    of the generation's own listings, built from the artifact with the export's builders and
    read back through `SqlFacts` from the database, digest against digest. A seed that cannot
    show the two agree is refused — it would be freezing a calibration for a lane that cannot
    reproduce the facts it was cut from."""
    sample = stratified_sample(dataset.listings, set(int(i) for i in in_scope), n, seed=1)
    reader = SqlFacts(conn)
    live: dict[int, tuple[Listing, list[Image]]] = {}
    for start in range(0, len(sample), 100):
        live.update(reader.facts(sample[start:start + 100]))
    rows = parity_baseline({i: dataset.listings[i] for i in sample},
                           {i: dataset.images(i) for i in sample})
    drifted = drifted_since(conn, sample, dataset.meta.exported_at)
    report = parity_compare(rows, {i: value[0] for i, value in live.items()},
                            {i: value[1] for i, value in live.items()}, drifted)
    report.update({
        "sampled": len(sample), "tolerance": floors.tolerance,
        "phash_pop_rows": phash_pop_rows(conn),
        "images_with_phash": reader.images_with_phash,
        "images_unmeasured": reader.images_unmeasured,
        "unknown_pop_share": _unknown_share(reader),
        "age_days": age_days(dataset.meta.exported_at),
        "floors": floors_json(floors),
        "statements": reader.statements + 1,
    })
    refused = parity_verdict(report, generation=generation, floors=floors, what="rt_seed")
    if refused:
        raise SystemExit(refused)
    report["ok"] = True
    return {
        "report": report,
        "baseline": {
            "generation": generation,
            "rows": rows,
            "exported_at": dataset.meta.exported_at,
            "cut_at": datetime.now(timezone.utc).isoformat(),
            "phash_pop_rows": report["phash_pop_rows"],
            "tolerance": floors.tolerance,
            "n": len(rows),
        },
    }


def _unknown_share(reader: "SqlFacts") -> float | None:
    """The share of phash-bearing images whose hash the frozen population cannot measure."""
    if not reader.images_with_phash:
        return None
    return round(reader.images_unmeasured / float(reader.images_with_phash), 6)


def age_days(stamp: Any) -> float | None:
    parsed = _stamp(stamp)
    if parsed is None:
        return None
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0, 3)


def floors_json(floors: ParityFloors) -> dict[str, Any]:
    return {"tolerance": floors.tolerance, "min_checked": floors.min_checked,
            "min_checked_share": floors.min_checked_share,
            "max_age_days": floors.max_age_days,
            "max_unknown_pop_share": floors.max_unknown_pop_share}


def resolve_floors(args: Mapping[str, str], control: Mapping[str, Any],
                   rescope: bool = False) -> ParityFloors:
    """The four floors as SETTINGS rows (E94/E95). Like every other rail on this lane, a
    dispatch argument may only move one beside `rt_rescope=true` — a rail one `-f args=` can
    turn into a formality is not a rail (W9e/R6)."""
    return ParityFloors(
        tolerance=PARITY_TOLERANCE,
        min_checked=int(control_number(PARITY_MIN_CHECKED_SETTING, args, control,
                                       float(PARITY_MIN_CHECKED), rescope)),
        min_checked_share=control_number(PARITY_MIN_SHARE_SETTING, args, control,
                                         PARITY_MIN_CHECKED_SHARE, rescope),
        max_age_days=control_number(CALIBRATION_AGE_SETTING, args, control,
                                    CALIBRATION_MAX_AGE_DAYS, rescope),
        max_unknown_pop_share=control_number(PARITY_UNKNOWN_POP_SETTING, args, control,
                                             PARITY_MAX_UNKNOWN_POP_SHARE, rescope),
    )


def parity_gate(conn: Any, generation: str, control: Mapping[str, Any],
                args: Mapping[str, str], floors: ParityFloors,
                built_at: Any = None) -> dict[str, Any]:
    """The permanent rail: no pass runs on facts that are not the export's (E91).

    A generation with no baseline is refused rather than waved through — the baseline is cut by
    the seed at the same moment as the calibration, so its absence means this generation was
    seeded before the gate existed and nothing has ever checked its facts."""
    payload = control.get(parity_baseline_key(generation))
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    if not isinstance(payload, Mapping) or "rows" not in payload:
        raise SystemExit(
            f"PARITY GATE: generation {generation!r} carries no fact baseline "
            f"(autodedup.settings {parity_baseline_key(generation)}) — it was seeded before "
            "the gate existed, so nothing has ever checked that the live lane reads the "
            "export's facts (E91). Re-seed it "
            "(`-f mode=rt_seed -f args=export_run=<id>,settings=w8,model=w6_gold,"
            "backfill=true,reseed=true`). Nothing was written and no cursor moved.")
    # The knob may only RAISE the sample (E82's lesson at a rail one dispatch argument could
    # otherwise have turned into a formality): `rt_parity_sample=1` cannot weaken the gate.
    try:
        asked = _setting_number(args.get(PARITY_SAMPLE_SETTING,
                                         control.get(PARITY_SAMPLE_SETTING)),
                                float(PARITY_PASS_SAMPLE))
    except ScopeError as exc:
        raise SystemExit(f"{PARITY_SAMPLE_SETTING}: {exc}") from exc
    report = check_parity(conn, generation, payload,
                          max(PARITY_PASS_SAMPLE, int(asked)))
    report["floors"] = floors_json(floors)
    report["calibration_built_age_days"] = age_days(built_at)
    # The W9f defect itself FIRST, because it is the specific diagnosis of what the sample is
    # about to report generically: a frozen population that HAD rows when the generation was
    # seeded and has none now is the table emptied under a running lane.
    at_seed = int(report.get("phash_pop_rows_at_seed") or 0)
    if at_seed and not int(report.get("phash_pop_rows") or 0):
        raise SystemExit(
            f"PARITY GATE: autodedup.phash_pop is EMPTY and generation {generation!r} was "
            f"seeded with {at_seed} hashes in it (E91). Every image would read an unknown "
            "population, every catalog_ratio would go absent and no K-C certificate could "
            "fire. Nothing was written and no cursor moved — re-seed the generation.")
    # The EXPORT's age has to be knowable for the age rail to mean anything (E95). A baseline
    # that cannot say when its cohort was cut is a baseline the rail cannot read, so it is
    # refused rather than waved through on a missing field.
    if report.get("age_days") is None:
        raise SystemExit(
            f"PARITY GATE: generation {generation!r}'s baseline carries no `exported_at`, so "
            "the calibration-age rail cannot be evaluated and a frozen population of unknown "
            "age would be read as fresh (E95). Re-seed the generation. Nothing was written "
            "and no cursor moved.")
    refused = parity_verdict(report, generation=generation, floors=floors,
                             what="the scheduled pass")
    if refused:
        raise SystemExit(refused)
    report["ok"] = True
    return report


# --- WHICH SCORER (E90a) -----------------------------------------------------------------


def named_config(args: Mapping[str, str], *, what: str
                 ) -> tuple[Settings, LogisticModel, str, str]:
    """The settings row and the model a dispatch NAMES — and silence is not a name.

    W9f's seed and its first live pass both ran on empty arguments, so `Settings()` and the
    uncalibrated `hand_initialised()` prior scored a generation the operator believed was the
    batch pass's `w8` + `w6_gold` (t_lo 0.30 against 0.1823, t_hi 0.97 against 1.0, no
    per-stratum ceiling, no bridge, no context rule). That is 5,888 of 7,878 shared pairs
    scored differently and 1,039 pairs merged that the batch generation bands — and no
    argument was mis-typed, because `settings=w8` would have been read as a PATH and found no
    file. Both halves are fixed: the name resolves inside the repo (`harness.named_settings`),
    and choosing the defaults is spelled `settings=default,model=prior`."""
    settings_name = str(args.get("settings") or "").strip()
    model_name = str(args.get("model") or "").strip()
    if not settings_name or not model_name:
        raise SystemExit(
            f"{what} needs settings=<row in autodedup/settings> and model=<file in "
            f"autodedup/models> — a generation is scored by ONE scorer and every stored "
            f"decision is stamped with it. The batch generation g6 is "
            f"`settings=w8,model=w6_gold`; the uncalibrated defaults are "
            f"`settings={DEFAULT_SETTINGS_NAME},model={PRIOR_MODEL_NAME}`, which is a choice "
            "this lane will not make on your behalf again (E90a).")
    return (named_settings(settings_name), named_model(model_name),
            settings_name, model_name)


def pass_config(args: Mapping[str, str], recorded: Any, model_version: Any,
                generation: str) -> tuple[Settings, LogisticModel]:
    """The GENERATION's scorer, read back from its frozen calibration row.

    The seed writes both columns and W9's pass read neither, so the schedule scored with
    whatever the dispatch happened to carry. Now the row decides and a dispatch argument may
    only AGREE with it: changing a generation's scorer is a re-seed, because every decision
    already in the store was taken under the old one."""
    if recorded is None:
        raise SystemExit(
            f"generation {generation!r} carries no settings on its frozen calibration — it "
            "was seeded before the scorer was recorded (E90a). Re-seed it "
            "(`-f mode=rt_seed -f args=export_run=<id>,settings=w8,model=w6_gold,"
            "reseed=true`) before the schedule runs.")
    try:
        settings = Settings.from_dict(dict(recorded))
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"generation {generation!r} carries settings this build cannot read ({exc}) — "
            "re-seed it.") from exc
    model = model_of_version(str(model_version) if model_version else None)
    asked_settings = str(args.get("settings") or "").strip()
    asked_model = str(args.get("model") or "").strip()
    if asked_settings:
        chosen = named_settings(asked_settings)
        if chosen != settings:
            moved = {key: {"generation": settings.to_dict().get(key), "dispatch": value}
                     for key, value in chosen.to_dict().items()
                     if settings.to_dict().get(key) != value}
            raise SystemExit(
                f"settings={asked_settings} is not what generation {generation!r} was seeded "
                f"with: {json.dumps(moved, sort_keys=True, default=str)}. A pass may name the "
                "generation's scorer, never a different one — re-seed to change it.")
    if asked_model and named_model(asked_model).version != model.version:
        raise SystemExit(
            f"model={asked_model} is not generation {generation!r}'s model "
            f"({model.version}) — re-seed to change it.")
    return settings, model


# --- the frozen pHash population (E91) ----------------------------------------------------


def phash_pop_rows(conn: Any) -> int:
    return int(_rows(conn, RT_PHASH_POP_COUNT_SQL)[0][0] or 0)


def write_population(conn: Any, dataset: Any) -> dict[str, Any]:
    """Materialise the calibration's own pHash population into `autodedup.phash_pop`.

    The number is the artifact's: `COHORT_PHASH_POP_SQL` counted `count(DISTINCT listing_id)`
    over `public.images` when the cohort was exported, and every image record carries it. So
    the seed copies it rather than recounting it — zero blocks of `public`, and the live lane
    then joins against EXACTLY the statistic the batch engine scored with. What that buys and
    what it costs: the population is frozen at export time (E70), it can only have grown since,
    and a hash the export never saw stays unknown until the next export and re-seed."""
    population: dict[int, int] = {}
    images = with_phash = 0
    for image in dataset.all_images():
        images += 1
        if image.phash is None:
            continue
        with_phash += 1
        if image.pop is None:
            continue
        population[int(image.phash)] = max(population.get(int(image.phash), 0), int(image.pop))
    if with_phash and not population:
        raise SystemExit(
            "the cohort artifact carries no MEASURED pHash population (`phash_pop_ok` false: "
            "the export's population probe timed out) — seeding from it would freeze a "
            "generation in which every catalog_ratio is absent and no K-C certificate can "
            "ever fire (E91). Re-run the export first.")
    rows = [{"phash": phash, "n_listings": n} for phash, n in sorted(population.items())]
    # Chunked like the score lane's writes: the trial cohort carries 41,791 distinct hashes and
    # one `executemany` of that many bound parameters is megabytes in a single call.
    for start in range(0, len(rows), POP_CHUNK):
        _exec_many(conn, RT_PHASH_POP_WRITE_SQL, rows[start:start + POP_CHUNK])
    return {"images": images, "images_with_phash": with_phash,
            "hashes_written": len(population),
            "hashes_at_or_above_2": sum(1 for n in population.values() if n >= 2)}


# --- the parity gate (E91) ----------------------------------------------------------------


def parity_baseline_key(generation: str) -> str:
    return f"{PARITY_BASELINE_SETTING}:{generation}"


def drifted_since(conn: Any, ids: Sequence[int], exported_at: Any) -> set[int]:
    """The sampled listings whose CONTENT changed after the export — genuine drift, never a
    parity breach (rule #2: a snapshot row is appended only when the content hash moves)."""
    cut = _stamp(exported_at)
    if cut is None or not ids:
        return set()
    out: set[int] = set()
    for row in _rows(conn, RT_PARITY_CHANGE_SQL, {"ids": list(ids)}):
        stamp = _stamp(row[1])
        if stamp is not None and stamp > cut:
            out.add(int(row[0]))
    return out


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parity_sample(rows: Mapping[str, Any], k: int) -> list[int]:
    """A fresh slice of the baseline each pass, so a day of passes covers all of it."""
    ids = sorted(int(key) for key in rows)
    if len(ids) <= k:
        return ids
    return sorted(random.sample(ids, k))


def check_parity(conn: Any, generation: str, payload: Mapping[str, Any], k: int,
                 facts: "SqlFacts | None" = None) -> dict[str, Any]:
    """Re-read a slice of the seeded baseline through `SqlFacts` and compare the digests."""
    rows = payload.get("rows") or {}
    sample = parity_sample(rows, k)
    reader = facts or SqlFacts(conn)
    live: dict[int, tuple[Listing, list[Image]]] = {}
    for start in range(0, len(sample), 100):
        live.update(reader.facts(sample[start:start + 100]))
    drifted = drifted_since(conn, sample, payload.get("exported_at"))
    report = parity_compare(
        {str(i): rows[str(i)] for i in sample},
        {i: value[0] for i, value in live.items()},
        {i: value[1] for i, value in live.items()},
        drifted)
    report["sampled"] = len(sample)
    report["baseline_listings"] = len(rows)
    report["baseline_cut_at"] = payload.get("cut_at")
    report["exported_at"] = payload.get("exported_at")
    report["tolerance"] = PARITY_TOLERANCE
    report["phash_pop_rows"] = phash_pop_rows(conn)
    report["phash_pop_rows_at_seed"] = payload.get("phash_pop_rows")
    # The two staleness readouts every pass owes (E95): how old the EXPORT the calibration was
    # cut from is, and how much of what this gate just read the frozen population could not
    # measure. Reported whether or not they refuse, so the drift is watchable before it bites.
    report["age_days"] = age_days(payload.get("exported_at"))
    report["images_with_phash"] = reader.images_with_phash
    report["images_unmeasured"] = reader.images_unmeasured
    report["unknown_pop_share"] = _unknown_share(reader)
    report["statements"] = reader.statements + 2
    return report


def _measure_rate(result: Any, control: Mapping[str, Any], generation: str,
                  elapsed_s: float) -> float | None:
    """This pass's claimed-listings-per-second, blended with what the generation had (E98).

    None when the pass claimed too little for the quotient to mean anything — an idle pass is
    0 listings in 7 seconds, and a lane that learned 0 from it would claim one listing a pass
    for ever."""
    claimed = len(getattr(result, "claimed", ()) or ())
    if claimed < PASS_RATE_MIN_CLAIM or elapsed_s <= 0:
        return None
    previous = max(1e-6, _setting_number(control.get(pass_rate_key(generation)),
                                         PASS_RATE_PER_S))
    blended = PASS_RATE_ALPHA * (claimed / float(elapsed_s)) + (1.0 - PASS_RATE_ALPHA) * previous
    return round(blended, 6)


def run_incremental(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
) -> dict[str, Any]:
    """One bounded real-time pass. Dark unless BOTH switches are on; writes nothing else."""
    generation = (args.get("generation") or "").strip() or GENERATION
    limits = Limits(
        max_listings=int(args.get("max_listings") or 500),
        max_pairs=int(args.get("max_pairs") or Limits().max_pairs),
        max_component=int(args.get("max_component") or 400),
    )
    if not env_enabled():
        return {"skipped": "dark", "reason": f"{ENV_FLAG} is not true", "spent_usd": 0.0}

    holder = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
    conn = conn_factory()
    leased = False
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539 not applied")
        if not db_enabled(conn):
            return {"skipped": "dark", "reason": f"autodedup.settings {DB_FLAG} is false",
                    "spent_usd": 0.0}
        # Is the generation SEEDED (W9e/R1)? Seeding is the step BEFORE the switch, so the
        # variable being on says nothing about whether `rt_seed` has run. An unseeded
        # generation is a loud green skip — the schedule says so every ten minutes without
        # failing every ten minutes — and never the hard error the missing `rt_scope` row
        # would otherwise raise two statements below.
        if not _rows(conn, RT_CALIBRATION_PRESENT_SQL, {"generation": generation}):
            return {
                "skipped": "unseeded",
                "reason": (f"generation {generation!r} has no frozen calibration (E70) — seed "
                           "it with `gh workflow run autodedup.yml -f mode=rt_seed` before the "
                           "schedule can do anything"),
                "generation": generation, "spent_usd": 0.0}
        rescope = str(args.get(RESCOPE_ARG) or "").strip().lower() == "true"
        scope_key = scope_setting_key(generation)
        control = lane_settings(conn, [scope_key, SCOPE_SETTING, BUDGET_SETTING,
                                       RETIRE_SETTING, SCAN_CAP_SETTING, INTERVAL_SETTING,
                                       PARITY_SAMPLE_SETTING, PARITY_MIN_CHECKED_SETTING,
                                       PARITY_MIN_SHARE_SETTING,
                                       PARITY_UNKNOWN_POP_SETTING, CALIBRATION_AGE_SETTING,
                                       EVIDENCE_HORIZON_SETTING, EVIDENCE_SLICE_SETTING,
                                       PASS_BUDGET_SETTING,
                                       bootstrap_setting_key(generation),
                                       pass_rate_key(generation),
                                       parity_baseline_key(generation)])
        try:
            # The PERSISTED scope is the generation's, and a dispatch argument is a re-scope
            # the operator has to ask for by name (W9d-2). The row is PER GENERATION (W9e/R4).
            scope, rescoped = resolve_pass_scope(
                args.get(SCOPE_SETTING), read_scope_setting(control, generation),
                rescope=rescope)
            parents = resolve_scope_parents(conn, scope)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        try:
            max_schema_mb = control_number(BUDGET_SETTING, args, control, MAX_SCHEMA_MB,
                                           rescope)
            max_retire_fraction = control_number(RETIRE_SETTING, args, control,
                                                 MAX_RETIRE_FRACTION, rescope)
            max_enter_scans = control_number(SCAN_CAP_SETTING, args, control,
                                             float(MAX_ENTER_SCANS_PER_DAY), rescope)
            intervals = enter_intervals(args, control, rescope)
            evidence_horizon = control_number(EVIDENCE_HORIZON_SETTING, args, control,
                                              EVIDENCE_HORIZON_HOURS, rescope)
            evidence_slice = control_number(EVIDENCE_SLICE_SETTING, args, control,
                                            float(EVIDENCE_SLICE), rescope)
            floors = resolve_floors(args, control, rescope)
            # The build phase and the clock the claim is bounded by (E98), both data.
            bootstrap = setting_flag(control.get(bootstrap_setting_key(generation)))
            pass_budget_s = _setting_number(control.get(PASS_BUDGET_SETTING), PASS_BUDGET_S)
            rate_per_s = max(1e-6, _setting_number(control.get(pass_rate_key(generation)),
                                                   PASS_RATE_PER_S))
        except ScopeError as exc:
            raise SystemExit(str(exc)) from exc
        try:
            storage = storage_guard(conn, generation, scope, max_schema_mb)
        except StorageRefusal as exc:
            raise SystemExit(str(exc)) from exc
        if not take_lease(conn, holder):
            return {"skipped": "leased", "reason": "another pass holds the lease",
                    "spent_usd": 0.0}
        leased = True
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
        if not rows:
            raise SystemExit(
                f"no frozen calibration for generation {generation!r} (E70) — seed it with "
                "`--mode rt_seed` before the lane runs")
        payload = rows[0][3]
        calibration = Calibration.from_json(
            payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        # The scorer is the GENERATION's, read off the row the seed wrote (E90a).
        settings, model = pass_config(args, rows[0][5], rows[0][6], generation)
        # THE GATE (E91). Before the lease's transaction, before a single write: a slice of the
        # seeded baseline re-read through the same `SqlFacts` the pass is about to score with.
        # A breach stops the pass — non-zero, nothing written, no cursor moved.
        parity = parity_gate(conn, generation, control, args, floors, rows[0][7])
        store = SqlStore(conn, generation, store_floor=settings.store_floor,
                         model_version=model.version,
                         calibration_digest=calibration.digest())
        facts = SqlFacts(conn)
        work = SqlWork(conn, scope, generation,
                       lag=int(args.get("settle_lag") or SETTLE_LAG_S),
                       straggler_window=int(args.get("straggler_window") or STRAGGLER_WINDOW),
                       revive_slice=int(args.get("revive_slice") or REVIVE_SLICE),
                       window=int(args.get("feed_window") or FEED_WINDOW),
                       drift_slice=int(args.get("drift_slice") or DRIFT_SLICE),
                       enter_slice=int(args.get("enter_slice") or ENTER_SLICE),
                       parents=parents, max_retire_fraction=max_retire_fraction,
                       enter_interval_hours=intervals,
                       max_enter_scans_per_day=int(max_enter_scans),
                       evidence_slice=int(evidence_slice),
                       evidence_horizon_hours=evidence_horizon,
                       bootstrap=bootstrap, pass_budget_s=pass_budget_s,
                       rate_per_s=rate_per_s)
        result = None
        try:
            with _transaction(conn):
                # The three bounds are the transaction's first statements and LOCAL to it
                # (W9d-4): over the transaction-mode pooler a session-level guard may belong to
                # a backend this transaction never runs on.
                _exec(conn, RT_STATEMENT_GUARD_SQL,
                      {"statement_timeout_ms": STATEMENT_TIMEOUT_MS})
                _exec(conn, RT_LOCK_GUARD_SQL, {"lock_timeout_ms": LOCK_TIMEOUT_MS})
                _exec(conn, RT_IDLE_GUARD_SQL, {"idle_timeout_ms": IDLE_TIMEOUT_MS})
                if rescoped:
                    # A rescope is PERSISTED, not applied for one pass, and the entrant sweep
                    # restarts so everything the new scope holds is (re)claimed (W9d-2).
                    _exec(conn, RT_SETTING_WRITE_SQL, {
                        "key": scope_key, "value": json.dumps(scope.as_json()),
                        "updated_by": f"{LANE_NAME}:rescope"})
                    _exec(conn, RT_CURSOR_WRITE_SQL, {
                        "name": CURSOR_ENTER, "last_listing_id": 0,
                        "last_snapshot_id": 0, "watermark": None})
                # E93's clock. The hold exists only where there IS one: the batch engine
                # has none, so the replay's default is no hold and the equivalence proof is
                # untouched by it.
                pass_now = time.time()
                pass_clock = time.perf_counter()
                result = run_pass_bounded(store, facts, work, settings, model, calibration,
                                          limits=limits, generation=generation,
                                          now=pass_now,
                                          hold=EvidenceHold(pass_now,
                                                            evidence_horizon * 3600.0))
                if result.aborted:
                    # Nothing this pass wrote survives a refusal, and no cursor moved.
                    raise _Refused()
                # What this pass measured ITSELF at, for the next pass's time budget (E98).
                # Written inside the transaction, so a refused pass records nothing and a
                # slow one cannot teach the lane to claim more than it can finish.
                measured = _measure_rate(result, control, generation, time.perf_counter()
                                         - pass_clock)
                if measured is not None:
                    _exec(conn, RT_SETTING_WRITE_SQL, {
                        "key": pass_rate_key(generation), "value": json.dumps(measured),
                        "updated_by": f"{LANE_NAME}:rate"})
                # The phase ends by ITSELF, in the transaction that empties the backlog: the
                # entrant claim goes back to a seventh and the cadence takes the blocks back.
                if bootstrap and work.bootstrap_done:
                    _exec(conn, RT_SETTING_WRITE_SQL, {
                        "key": bootstrap_setting_key(generation), "value": json.dumps(False),
                        "updated_by": f"{LANE_NAME}:bootstrap_done"})
        except _Refused:
            pass
        except RetireRefusal as exc:
            # The transaction rolled back on the way out: nothing written, no cursor moved.
            raise SystemExit(str(exc)) from exc
        summary = result.to_json() if result is not None else {"aborted": "unknown"}
        summary["fact_reads"] = facts.reads
        summary["statements"] = (store.statements + facts.statements + work.statements
                                 + int(parity.get("statements") or 0))
        summary["parity"] = parity
        summary["settings"] = settings.to_dict()
        summary["model_version"] = model.version
        # What this pass could not measure: images whose hash the frozen population does not
        # carry (E91). Zero on a generation whose seed and export are the same cut; it grows
        # with every photograph that arrived after the export, and it is the number that says
        # when the generation is due a re-export and re-seed.
        summary["population"] = {
            "phash_pop_rows": parity.get("phash_pop_rows"),
            "images_with_phash": facts.images_with_phash,
            "images_unmeasured": facts.images_unmeasured,
            "hashes_unmeasured": len(facts.hashes_unmeasured),
            # COVERAGE, the number the age rail is a proxy for: the share of the photographs
            # this pass actually scored that the frozen population could measure (E95).
            "coverage": (None if not facts.images_with_phash else round(
                1.0 - facts.images_unmeasured / float(facts.images_with_phash), 6)),
            "gate_unknown_share": parity.get("unknown_pop_share"),
            "max_unknown_share": floors.max_unknown_pop_share,
        }
        summary["calibration_n_listings"] = int(rows[0][2] or 0)
        summary["store_rows"] = int(
            _rows(conn, RT_FP_COUNT_SQL, {"generation": generation})[0][0])
        summary["scope"] = scope.as_json()
        summary["scope_key"] = scope_key
        summary["rescoped"] = bool(rescoped)
        summary["windows"] = dict(work.windows)
        # What the entrant feed cost the production instance THIS pass: a block refresh, a
        # refusal against the rolling-day cap, or nothing at all (W9e/R3).
        summary["enter_scan"] = dict(work.enter_scan)
        # The build phase and the clock (E98): what bounded this claim, what the phase still
        # owes, and whether this pass was the one that ended it.
        summary["claim_bound"] = dict(work.claim_bound)
        summary["bootstrap"] = {
            "active": bool(bootstrap),
            "backlog": work.bootstrap_backlog,
            "ended_this_pass": bool(bootstrap and work.bootstrap_done
                                    and result is not None and not result.aborted),
            "setting_key": bootstrap_setting_key(generation),
            "rate_key": pass_rate_key(generation),
        }
        summary["enter_interval_hours"] = dict(intervals)
        summary["max_enter_scans_per_day"] = int(max_enter_scans)
        # The seventh feed and the hold it serves (E92/E93): what the sweep probed on
        # `public.images`, what it found moved, and how many merges are waiting.
        summary["evidence"] = {
            **dict(work.evidence),
            "held_this_pass": result.held if result is not None else 0,
            "released_this_pass": result.released if result is not None else 0,
            "redecided": result.redecided if result is not None else 0,
        }
        # The staleness readout E95 owes every pass, refused above and REPORTED here.
        summary["calibration"] = {
            "digest": calibration.digest(),
            "built_at": _iso(rows[0][7]),
            "built_age_days": age_days(rows[0][7]),
            "export_age_days": parity.get("age_days"),
            "max_age_days": floors.max_age_days,
        }
        summary["retention"] = {"store_floor": settings.store_floor,
                                "pairs_retained": store.pairs_retained,
                                "pairs_evicted": store.pairs_evicted}
        # The storage readout is taken AFTER the pass, against the reading taken before it, so
        # "growth" is this pass's own growth and not the last one's.
        after_bytes = schema_bytes(conn)
        summary["storage"] = {
            **storage,
            "schema_mb_after": round(after_bytes / 1_048_576.0, 2),
            "pass_growth_mb": round(
                (after_bytes / 1_048_576.0) - float(storage["schema_mb"]), 3),
            "rows_after": rt_rows(conn, generation, scope),
        }
        record_storage(conn, generation, after_bytes)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "incremental.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return summary
    finally:
        try:
            if leased:
                release_lease(conn, holder)
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()


def run_rt_seed(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
) -> dict[str, Any]:
    """`--mode rt_seed`: cut a generation's FROZEN calibration and start its cursors AT TODAY.

    Without this the lane has no calibration at all (it refuses to run on a guess) and its
    cursors would start at id 0 — 83 days of walking history at the shipped slice before the
    first live arrival is reached (E76). The seed writes the calibration from the same cohort
    artifact the batch pass scored, stamps the forward cursors at the corpus's current maxima,
    and — with `backfill=true` — writes the cohort's own fingerprints and postings so the
    generation starts from a populated store rather than an empty one.

    Read-only against `public` like the pass itself — and deliberately NOT behind the dark
    switch (W9e/R1). Seeding is the step BEFORE the switch: the documented enable sequence is
    apply 539, merge, SEED, verify, then flip `AUTODEDUP_REALTIME_ENABLED`. W9 gated this mode
    on that variable, so the recipe printed in the workflow header and in PROGRAM.md exited 0
    having done nothing, and flipping the variable afterwards left the `*/10` schedule
    hard-erroring on a generation nothing had seeded. It is idempotent instead: a generation
    that already carries a frozen calibration is REFUSED unless `reseed=true`, because a
    re-seed re-cuts the calibration every stored decision of that generation was taken under."""
    from autodedup.dataset import load
    from autodedup.fingerprint import build_all
    from autodedup.judge_lane import download_cohort

    artifact = (args.get("artifact") or "").strip()
    export_run = (args.get("export_run") or "").strip()
    if export_run:
        # The same input the batch pass took, fetched the same way the score and judge lanes
        # fetch it: a runner has no cohort file until `gh run download` puts one there, so a
        # documented recipe that only took a PATH could not be run on a runner at all.
        if not export_run.isdigit():
            raise SystemExit(f"export_run must be a GitHub run id, got {export_run!r}")
        artifact = str(download_cohort(export_run, Path(out_dir) / "artifact"))
    if not artifact:
        raise SystemExit(
            "rt_seed needs export_run=<the export lane's run id> or artifact=<cohort.jsonl.gz>")
    generation = (args.get("generation") or "").strip() or GENERATION
    settings, model, settings_name, model_name = named_config(args, what="rt_seed")
    backfill = str(args.get("backfill") or "").strip().lower() == "true"
    reseed = str(args.get("reseed") or "").strip().lower() == "true"
    # A CLEAN RESET (E97), and only ever beside the word that already means "I mean it": a
    # `fresh` that could run without `reseed` would be one fat-fingered dispatch away from
    # emptying a generation somebody is reading.
    fresh = str(args.get("fresh") or "").strip().lower() == "true"
    if fresh and not reseed:
        raise SystemExit(
            "fresh=true empties THIS generation's pairs, clusters, fingerprints, postings, "
            "census cells, scope snapshot and cursors — it is only valid with reseed=true, "
            "which is how this lane spells 'I mean it'. Nothing was written.")
    bootstrap = str(args.get("rt_bootstrap") or "").strip().lower() == "true"
    if bootstrap and backfill:
        raise SystemExit(
            "rt_bootstrap=true builds the generation through the ARRIVAL path (E98) and "
            "backfill=true writes the fingerprints without a single pair (E97) — a generation "
            "seeded with both would spend the phase walking a scope whose every listing is "
            "already in the store, and would still hold no pairs. Pass backfill=false.")
    parity_n = int(_setting_number(args.get("parity_n"), float(PARITY_BASELINE_N)))

    conn = conn_factory()
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539 not applied")
        existing = _rows(conn, RT_CALIBRATION_PRESENT_SQL, {"generation": generation})
        if existing and not reseed:
            raise SystemExit(
                f"generation {generation!r} is already seeded (calibration "
                f"{str(existing[0][1])!r}, cut {existing[0][2]}) — a re-seed re-cuts the frozen "
                "calibration every stored decision of this generation was taken under. Pass "
                "reseed=true to mean it, or seed a new generation.")
        if fresh and not existing:
            # The reset deletes pairs, clusters, members and conflicts BY GENERATION NAME. A
            # batch generation (g4..g7) has no rt_calibration row, so without this refusal
            # `generation=g7 reseed=true fresh=true` would empty the operator's reviewed pass.
            raise SystemExit(
                f"fresh=true refused: generation {generation!r} carries no real-time "
                "calibration, so it was never seeded by this lane and is not this lane's to "
                "empty. Seed it first (without fresh), or name the real-time generation.")
        scope_key = scope_setting_key(generation)
        control = lane_settings(conn, [scope_key, SCOPE_SETTING, BUDGET_SETTING,
                                       PARITY_MIN_CHECKED_SETTING, PARITY_MIN_SHARE_SETTING,
                                       CALIBRATION_AGE_SETTING, PARITY_UNKNOWN_POP_SETTING])
        # The same four floors the scheduled pass is held to (E94/E95). A seed cut from an
        # export already past the age rail would be refused by the first pass anyway; it is
        # refused HERE, where the recipe that fixes it is one re-export away.
        floors = resolve_floors({}, control)
        try:
            scope = resolve_scope(args.get(SCOPE_SETTING),
                                  read_scope_setting(control, generation))
            # Proved at SEED time rather than at the first pass: a quarter the register cannot
            # place is a scope whose entrant sweep could never walk it (W9d-3).
            resolve_scope_parents(conn, scope)
            max_schema_mb = _setting_number(
                args.get(BUDGET_SETTING, control.get(BUDGET_SETTING)), MAX_SCHEMA_MB)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        try:
            storage = storage_guard(conn, generation, scope, max_schema_mb)
        except StorageRefusal as exc:
            raise SystemExit(str(exc)) from exc

        ds = load(artifact)
        # The artifact is the BATCH cohort and carries the assembled negative control, which
        # has no arrival feed and is therefore outside every real-time scope (E79). The seed
        # drops it here rather than backfilling rows the lane could never maintain — and the
        # frozen calibration is cut over what the generation will actually HOLD, because every
        # statistic in it is cohort-relative.
        in_scope = sorted(i for i, listing in ds.listings.items() if scope.holds(listing))
        if not in_scope:
            raise SystemExit(
                f"{SCOPE_SETTING} {scope.label()!r} holds none of the {len(ds.listings)} "
                f"listings in {artifact} — seeding it would freeze an empty calibration")
        fps = {i: fp for i, fp in build_all(ds, settings).items() if i in set(in_scope)}
        calibration = Calibration.build(
            fps, {i: ds.listings[i] for i in in_scope}, settings, generation)
        payload = json.dumps(calibration.to_json(), ensure_ascii=False, sort_keys=True)
        written = 0
        reset: dict[str, int] = {}
        with _transaction(conn):
            # FIRST, inside the seed's own transaction (E97): a refusal anywhere below — an
            # empty scope, a parity baseline that cannot clear the floors — puts every deleted
            # row back, so there is no state in which the generation is emptied and unseeded.
            if fresh:
                reset = reset_generation(conn, generation)
            # THE FROZEN POPULATION FIRST (E91). Everything below — the parity gate included —
            # reads galleries through `SqlFacts`, which joins this table, so it has to be
            # written before anything looks at an image.
            population = write_population(conn, ds)
            parity = seed_parity(conn, ds, in_scope, generation, parity_n, floors)
            _exec(conn, RT_CALIBRATION_WRITE_SQL, {
                "generation": generation, "digest": calibration.digest(),
                "n_listings": calibration.n_listings,
                "payload": payload if len(payload) < 40_000_000 else None,
                "artifact_url": str(artifact),
                "settings": json.dumps(settings.to_dict(), sort_keys=True),
                "model_version": model.version})
            seeded = _rows(conn, RT_SEED_CURSORS_SQL)[0]
            _exec(conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_NEW, "last_listing_id": int(seeded[0]),
                "last_snapshot_id": None, "watermark": None})
            _exec(conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_CHANGED, "last_listing_id": None,
                "last_snapshot_id": int(seeded[1]), "watermark": None})
            _exec(conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_FLIPPED, "last_listing_id": int(seeded[3]),
                "last_snapshot_id": None, "watermark": seeded[2]})
            _exec(conn, RT_CURSOR_SET_SQL, {"name": CURSOR_REVIVE, "last_listing_id": 0})
            _exec(conn, RT_CURSOR_SET_SQL, {"name": CURSOR_SCOPE, "last_listing_id": 0})
            _exec(conn, RT_CURSOR_WRITE_SQL, {"name": CURSOR_ENTER, "last_listing_id": 0,
                                              "last_snapshot_id": 0, "watermark": None})
            # The generation's scope, written where the lane reads it: a pass dispatched
            # without an argument then runs the scope this generation was seeded for, and can
            # never quietly widen to one it has no fingerprints for.
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": scope_key, "value": json.dumps(scope.as_json()),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            # The gate's baseline, cut at the same moment as the calibration and stored where
            # the lane already reads its control rows — a settings row needs no migration (E91).
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": parity_baseline_key(generation),
                "value": json.dumps(parity["baseline"], sort_keys=True, default=str),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            # The build phase, as DATA rather than as a flag on one dispatch (E98): the passes
            # that do the building are the `*/10` schedule's, and none of them carries an
            # argument. The row is written on every seed — true when the operator asked for the
            # phase, false when they did not — so a re-seed can never leave a stale one on.
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": bootstrap_setting_key(generation),
                "value": json.dumps(bool(bootstrap)),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            if backfill:
                store = SqlStore(conn, generation, store_floor=settings.store_floor,
                                 model_version=model.version,
                                 calibration_digest=calibration.digest())
                keyer = Keyer(settings, calibration)
                for listing_id in sorted(fps):
                    fp = fps[listing_id]
                    listing = ds.listings[listing_id]
                    store.put_listing(
                        listing_id,
                        FpRow(GuardRow(listing_id, fp.category_main, fp.category_type,
                                       fp.area_m2, fp.disposition, fp.floor),
                              fp_digest(fp, ds.images(listing_id)),
                              address_block_key(listing), category_group(listing),
                              bool(listing.is_active),
                              # What the COHORT's own decision rested on (E92). A backfilled
                              # listing whose artifact gallery was already hashed is complete
                              # and never enters the evidence sweep.
                              evidence_of(ds.images(listing_id))),
                        keyer.index_keys(fp))
                    store.bump_cell(listing)
                    written += 1
                store.flush()
        summary = {
            "generation": generation,
            "calibration_digest": calibration.digest(),
            "calibration_n_listings": calibration.n_listings,
            "settings_name": settings_name,
            "model_name": model_name,
            "model_version": model.version,
            "population": population,
            "parity": parity["report"],
            "floors": floors_json(floors),
            "evidence_horizon_hours": EVIDENCE_HORIZON_HOURS,
            "cursors": {CURSOR_NEW: int(seeded[0]), CURSOR_CHANGED: int(seeded[1]),
                        CURSOR_FLIPPED: str(seeded[2]), CURSOR_REVIVE: 0, CURSOR_SCOPE: 0},
            "backfilled": written,
            # What the reset removed, per table (E97). `{}` when `fresh` was not asked for —
            # which is itself the receipt that the generation's old rows are still there.
            "fresh": bool(fresh),
            "reset": reset,
            "bootstrap": bool(bootstrap),
            "bootstrap_key": bootstrap_setting_key(generation),
            "scope": scope.as_json(),
            "scope_key": scope_key,
            "reseed": bool(reseed),
            "artifact": str(artifact),
            "export_run": export_run or None,
            "artifact_listings": len(ds.listings),
            "in_scope_listings": len(in_scope),
            "storage": storage,
            "spent_usd": 0.0,
        }
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "rt_seed.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return summary
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()
