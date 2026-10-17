"""THE autodedup lane: one bounded pass that decides, groups and reconciles, and its store.

W5 made this the ONE production path (E914): the always-on worker's `autodedup` lane
(`scraper/realtime_worker.py`) is its only caller and its interval (0 = stop) its only switch.
One pass, under the lane's lease (`autodedup.rt_lease`), claims what moved, decides it with the
batch engine's own functions (`incremental.run_pass_bounded`), re-clusters under the relation
and the operator's rulings the batch pass clusters under (F2, E909; E910), commits, and then
RECONCILES production (`autodedup/reconcile.py`, A9): the groups it re-clustered become merges
through `toolkit.property_identity.merge_property_set` inside the area
`app_settings.autodedup_apply_scope` names. It never splits. Its calibration is cut from the
database (A10, E912) — at `rt_seed` and again whenever the pHash population a pass reads has
fallen below `COVERAGE_FLOOR`, in a tick of its own after that pass (E944) — and its controls
are constants of this module, not settings rows.

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
import logging
import math
import os
import socket
import sys
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Iterator, Mapping, Sequence

try:
    import resource
except ImportError:  # pragma: no cover — no getrusage outside Unix
    resource = None  # type: ignore[assignment]

from autodedup import indistinguishable, normalize, reconcile, rt_lease
from autodedup.calibration_cut import CalibrationCut, decode_payload, encode_payload
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
    COHORT_PHASH_POP_SQL,
    COHORT_PRICE_HISTORY_SQL,
)
from autodedup.features import FEATURE_VERSION
from autodedup.fingerprint import build_fingerprint
from autodedup.harness import (
    DEFAULT_SETTINGS_NAME,
    PRIOR_MODEL_NAME,
    model_of_version,
    named_model,
    named_settings,
)
from autodedup.hazard_context import ContextStamp, address_block_key, category_group
from autodedup.incremental import (
    EVIDENCE_HOLD_REASON,
    GENERATION,
    SEED_VERSION,
    Calibration,
    CellRow,
    Evidence,
    EvidenceHold,
    FpRow,
    GuardRow,
    Limits,
    PairRow,
    PassDeadline,
    PassStopped,
    SET_CAP,
    WorkItem,
    _in_time,
    bootstrap_key,
    engine_digest,
    key_token,
    rss_mb,
    run_pass_bounded,
    seed_current,
    seed_version_key,
    setting_flag,
)
from autodedup.incremental_scope import (
    CORPUS_PROJECTION_MB,
    Scope,
    ScopeError,
    guard_agrees,
    parse_scope,
    resolve_scope,
)
from autodedup.incremental_sql import (
    ENTER_SLICE,
    RT_CALIBRATION_PRESENT_SQL,
    RT_CALIBRATION_READ_SQL,
    RT_CALIBRATION_WRITE_SQL,
    RT_CELL_READ_SQL,
    RT_CELL_UPSERT_SQL,
    RT_CHANGED_LISTINGS_SQL,
    RT_CLOSURE_CONFLICT_APPEND_SQL,
    RT_CLUSTER_DROP_SQL,
    RT_CLUSTER_MEMBERS_DROP_SQL,
    RT_CLUSTERS_TOUCHING_SQL,
    RT_CONFLICT_DROP_SQL,
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_CURSOR_WRITE_SQL,
    RT_CUT_HASHES_SQL,
    RT_CUT_SCOPE_IDS_SQL,
    RT_FLIPPED_LISTINGS_SQL,
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
    RT_FRESH_SETTINGS_SQL,
    RT_FRESH_PAIRS_SQL,
    RT_FRESH_REJECT_LEDGER_SQL,
    RT_FRESH_RETIRE_EVENT_SQL,
    RT_FRESH_RT_FP_SQL,
    RT_FRESH_SCOPE_IDS_SQL,
    RT_FRESH_SCOPE_SCAN_SQL,
    RT_ENGINE_WRITE_SQL,
    RT_FP_DD_SQL,
    RT_FP_READ_SQL,
    RT_FP_UPSERT_SQL,
    RT_GENERATION_BYTES_SQL,
    RT_IDLE_GUARD_SQL,
    RT_LOCK_GUARD_SQL,
    RT_PHASH_POP_COUNT_SQL,
    RT_PHASH_POP_SQL,
    RT_PHASH_POP_WRITE_SQL,
    RT_KEY_DELETE_SQL,
    RT_KEY_INSERT_SQL,
    RT_KEYS_MANY_SQL,
    RT_KNOWN_SQL,
    RT_LOOKUP_MANY_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_MUST_LINK_SQL,
    RT_MUST_NOT_LINK_SQL,
    RT_RULINGS_CHANGED_SQL,
    RT_NEW_LISTINGS_SQL,
    RT_NEW_STRAGGLERS_SQL,
    RT_PAIR_CLUSTER_SQL,
    RT_PAIR_DELETE_SQL,
    RT_PAIR_UNCLUSTER_SQL,
    RT_PAIR_UPSERT_SQL,
    RT_PAIRS_TOUCHING_SQL,
    RT_PAIRS_WITHIN_SQL,
    RT_REJECT_LEDGER_DELETE_SQL,
    RT_REJECT_LEDGER_KEYED_SQL,
    RT_REJECT_LEDGER_LOOKUP_SQL,
    RT_REJECT_LEDGER_UPSERT_SQL,
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
    RT_SCOPE_OKRES_BLOCK_SQL,
    RT_SCOPE_OKRES_OBCE_SQL,
    RT_SCOPE_PARENT_OBEC_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
    RT_SCOPE_SCAN_STATE_SQL,
    RT_SCOPE_SCAN_WRITE_SQL,
    RT_SCOPE_DRIFT_SQL,
    RT_RECUT_PUT_BACK_SQL,
    RT_RETIRE_EVENT_WRITE_SQL,
    RT_RETIRE_WINDOW_SQL,
    RT_SEED_CURSORS_SQL,
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
)
from autodedup.store_score import storable
from autodedup.model import LogisticModel
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL
from autodedup.settings import Settings
from autodedup.text_facts import body_cache_entries, clear_body_caches

LOG = logging.getLogger(__name__)

LANE_NAME: str = rt_lease.NAME
# THE PASS'S OWN DEADLINE (E913), in seconds of wall clock from the moment it starts. The
# worker used to wrap the connection in a statement-refusing deadline and back off in process;
# the engine now bounds itself: `run_pass` checks it between steps, a pass past it rolls back
# (E75: nothing written, no cursor moved) and records its rate HALVED, so the next claim is
# half the size. Sized so the deadline plus one statement at `statement_timeout` stays under
# the worker's 1,200 s stall warning.
PASS_DEADLINE_S: float = 1050.0
# The lease outlives the longest pass by construction: the deadline, one statement past it and
# the reconcile's last group. A calibration re-cut (A10) is a tick of its own under the same
# deadline (E944).
LEASE_TTL_S: int = 2400
# `rt_seed` takes the SAME lease (E97's reset, the calibration and the cursors it rewrites are
# exactly what a pass reads and writes). Its TTL is the seeding job's own timeout
# (`autodedup.yml`, `timeout-minutes: 300`), so a running seed can never lose it to a pass.
SEED_LEASE_TTL_S: int = rt_lease.DISPATCH_TTL_S
CURSOR_NEW: str = "rt_new"
CURSOR_CHANGED: str = "rt_changed"
CURSOR_FLIPPED: str = "rt_flipped"
CURSOR_REVIVE: str = "rt_revive"
CURSOR_SCOPE: str = "rt_scope_drift"
CURSOR_ENTER: str = "rt_scope_enter"
CURSOR_EVIDENCE: str = "rt_evidence"
# G4 (E920): the previous pass's start, so a ruling changed since then seeds its listings. Read
# back less RULINGS_OVERLAP_S: a ruling whose transaction began before that start and committed
# after that pass read is still seen (a few passes re-seed it, which re-clusters to the same).
CURSOR_RULINGS: str = "rt_rulings"
RULINGS_OVERLAP_S: float = 300.0
# The rows the lane still keeps in `autodedup.settings` are WRITTEN BY IT, never set by hand:
# the scope the seed cut the generation for, the seed's version and the build phase the seed
# opens and the pass closes (both named in `incremental`, which the API reads too), the rate
# each pass measures, the claim cap a death halves (E948) and a bigger container resets
# (E948b), the storage watermark (E914), and the re-cut a pass found due (E944).
SCOPE_SETTING: str = "rt_scope"
# E940: each scope block's page cursor, ONE row a generation (`rt_scope_pages:<generation>`),
# `{block key: {after, page, of}}` — the listing id its next walk starts after (0: a new cycle),
# the pages its current cycle has walked, and the pages its last full cycle took (0: none has
# ended yet, so the block is not read whole; `RT_SCOPE_SCAN_SEEN_SQL` spells the key too). Only
# a block that pages has an entry; the fresh reset empties the row with its generation.
PAGES_SETTING: str = "rt_scope_pages"
RECUT_SETTING: str = "rt_recut"
PASS_RATE_SETTING: str = "rt_pass_rate_per_s"
CLAIM_CAP_SETTING: str = "rt_claim_cap"
ENGINE_SETTING: str = "rt_engine"
# The reconcile's skip over a generation no seed of this design built.
SEED_MISMATCH: str = "seed_version"
# E941: a pass that sees the worker shutting down, and one whose lease ended under it.
STOPPING: str = "stopping"
LEASE_LOST: str = rt_lease.LEASE_LOST
STORAGE_WATERMARK: str = "rt_storage_last"
# THE ONE CLAIM BOUND (E75): how many listings a pass may claim before the time budget cuts it.
PASS_LIMITS: Limits = Limits(max_listings=500, max_component=400)
# The calibration re-cut (A10): when the share of a pass's hashed photographs whose population
# the table can measure falls below the floor — photographs that arrived after the last cut —
# on a pass that measured at least `RECUT_MIN_IMAGES` of them, the lane's next tick re-cuts
# instead of passing (E944), at most once per `RECUT_MIN_AGE_H` since the last cut or attempt.
COVERAGE_FLOOR: float = 0.85
RECUT_MIN_IMAGES: int = 200
RECUT_MIN_AGE_H: float = 6.0
# A re-cut runs only where one tick can finish it: never in a build (the seed cut the calibration
# the build runs under) and never over more than RECUT_MAX_LISTINGS adverts. The seed's cut over
# 39,470 adverts took 632.9 s, ~16.0 ms an advert and read-bound; a tick's reads stop
# RECUT_WRITE_RESERVE_S before its 1,050 s deadline (930 s), and 50,000 adverts read in ~800 s.
RECUT_MAX_LISTINGS: int = 50_000
RECUT_WRITE_RESERVE_S: float = 120.0
# A cut that killed its worker twice under one calibration (the OOM killer, most likely) is not
# tried again until a seed: one death is not proof, since a deploy's SIGKILL after the 30 s drain
# reads the same, but every further attempt would cost a worker restart.
RECUT_MAX_DEATHS: int = 2
# The cut reads the scope's facts in chunks of this many listings, and its one corpus-wide
# statement (the pHash population, a sequential scan of `public.images` the export always ran)
# gets a longer statement timeout than a pass.
CUT_CHUNK: int = 200
CUT_STATEMENT_TIMEOUT_MS: int = 600_000

# How long a row must have existed before the lane will claim it (E73). Portal writes commit in
# seconds; a transaction still open after five minutes would have to be a stuck one, and the
# straggler sweep catches even that.
SETTLE_LAG_S: int = 300
# How far back the anti-join looks for a row that committed after the cursor passed its id.
STRAGGLER_WINDOW: int = 5000
# One round-robin slice of the revive sweep. 434k inactive rows at 20k a pass is a full cycle
# every ~22 passes (~22 min at the worker's 60 s interval), which is the lane's revival latency.
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
# `ENTER_SLICE`, the walk's PAGE, lives beside the walks it pages (`incremental_sql`, E961, E940).
# A block whose current cycle has walked this many pages without reaching its end is `stalled` in
# the walk's report and the log (E940): Praha, the largest block the register holds, is 9 pages,
# so 16 is a cursor that does not advance, never a block that large.
PAGE_STALL_PAGES: int = 16
# --- the evidence sweep (E92) ---------------------------------------------------------------
#
# One round-robin slice of the SEVENTH feed, over this generation's own `rt_fp` rows. The
# candidate arm reads `autodedup` only; what it costs `public` is the probe below it, and the
# probe is the only read of `public.images` this lane makes outside a fact fetch. Measured on
# the trial scope with `EXPLAIN (ANALYZE, BUFFERS)`: 200 listings = 3,085 images = 2,375
# buffers (920 heap blocks, 261 of them read cold), 12 buffers a listing. At the shipped slice
# and a ten-minute cadence that is 400 x 144 = 57,600 listing probes a day IF the candidate set
# were that large; it is not — the trial scope carries 76 rows of incomplete evidence in
# steady state plus ~74 inside the 48 h horizon, so a pass probes ~150 listings (~1,800
# buffers, 14 MB) and the day costs ~259,000 buffers, 2.0 GB — the same order as the entrant
# feed's 1.6 GB after W9e cut it 7.8x.
EVIDENCE_SLICE: int = 400
# How long after its FIRST decision a listing's photographs are still expected. The producers
# are hourly (dHash at :20, CLIP at :40) and measured p90 4.51 h to a first tag, so 48 h is
# ~10x the observed tail rather than a guess at it — and it is the bound on how long a merge
# may wait (E93) as much as on how long the sweep chases one.
EVIDENCE_HORIZON_HOURS: float = 48.0
# How often a scope block's membership is re-walked on `public`, by grain and in HOURS. A town
# block is an index-served bitmap scan of its own obec (Jablonec: 2,745 buffers = 21 MB, 61 ms);
# a QUARTER has no index of its own at all and costs 29,568 buffers = 231 MB, 6.4 s, cold every
# time. So they do not deserve the same cadence, and neither is per-pass work. An OKRES (E961)
# is index-served like a town but is a dozen towns' rows: Praha-východ, kraj 27's largest, is
# 13,935 buffers = 109 MB (0.70 a row; the kraj's 12 ~7,600 at that). At a town's 1 h one would
# read 334k buffers a day, more than a quarter's 118k; at a quarter's 6 h it reads at most 56k
# (avg 30k), under what one town (66k) or quarter (118k) block already spends; a kraj-only
# scope's 12 districts ask 48 walks a day. The walk is the TAIL's feed (an arrival is the
# forward and straggler feeds'), so 6 h costs latency only where it is measured in days.
ENTER_INTERVAL_HOURS: dict[str, float] = {"obec": 1.0, "cast_obce": 6.0, "okres": 6.0}
# The hard ceiling on those walks, per generation and per rolling day (W9e/R3). The cadence
# above is what the lane INTENDS to spend; this is what it is allowed to spend when an interval
# is mis-set to zero or the block list grows. The trial scope's cadence asked 52 a day (24 + 24
# + 4); the live scope (8 towns + 112 Prague quarters) asks 640 and Prague + kraj 27's 12 okresy
# 688, so the cap binds, and it binds the TAIL only: an arrival is the forward feeds' (E961). A
# page is a walk (E940) and the cadence is per walk, so paging adds no walk a day: a block of n
# pages is read whole every n of its walks. The cap governs PASSES; the seed walks its whole
# scope under none (E940), and its walks count in the rolling day the passes after it read.
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
# steady state. The rolling-day scan cap (`MAX_ENTER_SCANS_PER_DAY`) still holds, because that
# rail exists for the case where the cadence is wrong. The phase ends BY ITSELF — the pass that
# finds the entrant backlog empty writes the row false — so nothing has to remember to end it.
BOOTSTRAP_MIN_CLAIM: int = 1
# --- the time budget (E98) -------------------------------------------------------------------
#
# The claim is bounded by SECONDS as well as by counts: every pass measures its OWN rate (claimed
# listings per second of pass wall time, reconcile included) into
# `rt_pass_rate_per_s:<generation>`, and the next pass claims at most `budget x rate`. The
# budget is half the deadline (a build's three quarters, E951), so a pass may run twice (a
# build's 4/3) as slow as its measured rate before the deadline stops it. The default rate is
# the one measurement taken before any was recorded — a conservative start the second pass
# corrects. The row is halved BEFORE every pass, put back as soon as the pass commits and then
# overwritten by its measurement (E941), so a pass the system kills mid-way has already halved
# the next claim.
PASS_BUDGET_S: float = PASS_DEADLINE_S / 2
PASS_RATE_PER_S: float = 0.1
# A rate is only recorded when the pass actually claimed enough for the quotient to mean
# something: an idle pass is 0 listings in 7 seconds and would otherwise wedge the claim at 1.
PASS_RATE_MIN_CLAIM: int = 20
# How much of the new measurement the stored rate takes. A build ramps from the conservative
# default to the real rate in two passes at 0.5 and cannot be knocked out of it by one slow pass.
PASS_RATE_ALPHA: float = 0.5
# E951: a pass the clock cut that claimed all of it and committed inside this share of the
# DEADLINE doubles the stored rate instead of blending it: at a small claim the pass's fixed
# cost holds claimed/elapsed down, so the blend cannot climb. A doubled pass of T < 525 s stays
# under 2T only if its next adverts cost no more each, its fixed part has not grown and the
# database is no slower (2 s to spare at the threshold); the margin is E71's neighbourhood
# saturating (2026-10-10: 62/125/250 adverts in 331/395/450 s, x1.14-1.19 a doubling).
RAMP_HEADROOM: float = 0.5
# E951: a build's pass budget as a share of the deadline: a build's pass need not finish fast
# for latency, and its fixed cost is spread over more adverts. The margin is 4/3, not 2, so most
# deadline hits a build meets come from here: each rolls back, halves the rate, costs ~18 min.
BOOTSTRAP_BUDGET_FRACTION: float = 0.75
# --- the claim cap (E948) --------------------------------------------------------------------
#
# The rate bounds a pass's TIME, never its MEMORY, and the neighbourhood a pass re-reads grows
# with the store (E71). A worker process that died holding the lease (no SIGTERM, so no E941
# release: most likely the kernel's OOM killer) took on more than its container could hold, so
# the next process of the same container, which releases that lease before its take
# (`rt_lease.release_predecessor`), halves `rt_claim_cap:<generation>` in the same commit, and
# the limit every feed's share is cut from is min(max_listings, cap, the time budget's). The
# death is the signal; no memory reading decides a halving. Nothing raises the cap again but a
# seed or a bigger container (E948b): the store keeps growing under a generation (inactive
# adverts stay in it), so a size that died in this container dies again, and doubling back to
# it would kill the whole worker again.
#
# E948b: the row records the limit its cap was halved under (`limit_mb`): the first death since
# a seed or a reset writes its container's, every later one the bigger of the row's and its
# own (an unknown limit keeps the row's), so a death in a smaller container never lets the
# bigger one reset to a size that already died there. A pass whose limit is at least
# `CLAIM_CAP_LIMIT_GROWTH` above it resets the cap to `max_listings` (a cap already there only
# takes the new limit); a smaller limit resets nothing, so after a reset a move back down
# claims the whole `max_listings` again and its deaths walk the cap down from there. Every pass
# fits the row to its own limit and commits that before its take, so a growth a boot sees
# proves its dead predecessor ran under the smaller limit: that boot releases the lease and
# halves nothing, since the death was the smaller container's.
#
# Below it a pass's memory is its neighbourhood read, not its claim: a smaller claim would only
# slow the build. So at the floor a death is no claim's to cure — the neighbourhood read, the
# revived feed (every revived id of a `REVIVE_SLICE` scan), a re-cut under the same lease, one
# bad advert — and its lease is left to its TTL, as before E948: releasing it at every boot
# would restart the whole worker as fast as it can boot, and the TTL holds that to one death a
# lease.
CLAIM_CAP_FLOOR: int = 25
# E948b: the share a container's memory limit must grow by, over the `limit_mb` its cap was
# halved under, to reset the cap to `max_listings`; a limit that merely jitters must not.
CLAIM_CAP_LIMIT_GROWTH: float = 0.25
# E948 wrote its rows without a `limit_mb` key, and halved only in the 8 GB container, whose
# cgroup limit reads 7,629.4 MiB (8,000,000,000 bytes). A halved row without the key grows from
# it, so the row E948 left resets in a bigger container whether or not a pass ran in the 8 GB
# one first. E948b always writes the key, so any other row without a limit (a seed's, or one
# halved while no limit could be read) takes the limit its next pass reads and resets nothing.
E948_LIMIT_MB: float = 7_629.4
# The window the retirement rail is measured over (W9e/R2). A slice-sized rail could only fire
# while one drift slice was itself a twentieth of the store; a rolling day is independent of
# `drift_slice` and of the store's size.
RETIRE_WINDOW_HOURS: int = 24
# The share of the store one pass's drift sweep may retire before the lane STOPS instead (W9d-1).
# A geocode correction moves a listing or two; a scope that has gone wrong moves everything, and
# the difference between those two is the only thing standing between a hand-edited settings row
# and a store that has to be re-seeded.
MAX_RETIRE_FRACTION: float = 0.05
# The storage budget, in megabytes of schema `autodedup` (pg_total_relation_size, indexes and
# TOAST included). It is the TRIAL's shadow rail and nothing more: the operator pays for the
# schema by the megabyte, so a lane that has not been watched for a week must not be able to
# double it. `rt_scope = all` is gated by `CORPUS_PROJECTION_MB` (17,000) through
# `guard_agrees`, and this budget stays below it, so raising it never enables the corpus.
#
# 10,000 MB since E940 (2026-10-17; 6,000 since E946, 1,500 since E941, 800 since E916, 400
# before that; MB here are MiB, as the guard computes them), sized for the re-seed that lists
# Prague whole and kraj 27 (E939 S4, E961): seven towns (25,527 located adverts), obec:554782
# paged (167,548, 9 pages) and the 12 okresy of kraj 27 (130,053), 323,128 adverts — measured
# 2026-10-17 06:25Z, read-only. The schema reads 1,241.6 MB (pairs 756, fp_key 173, the reject
# ledger 124, phash_pop 63), generation `rt` ~953 MB of it by row share (the space a fresh reset
# frees for reuse); the walk writes ~0.29 kB a snapshot row (~90 MB), the cut upserts the new
# scope's photo hashes into phash_pop (CUT_BYTES_PER_LISTING, 339 MB for 323,128), and the build
# writes BUILD_BYTES_PER_LISTING an advert, 6,311 MB. So the seed's second check (E941, before
# the cut since E940) projects 1,242 - 953 + 90 + 339 + 6,311 = ~7,030 MB, over E946's 6,000;
# 10,000 leaves ~2,970 MB, about two months of arrivals at E79's ~20 % of the store a month
# (~1,262 MiB at that size), not a further widening, and stays under
# CORPUS_PROJECTION_MB, so `rt_scope = all` stays refused. The database is 131.7 GB of a disk of
# ~200 GB (67 %, D907), so 10 GB is ~5 % of it. E946's 6,000 was sized the same way for the
# eight towns plus all 157 Prague quarters (~170,000, ~3,700 MB projected) and E941's 1,500 for
# S1's 38,000. A DELETE never shrinks a file, so the schema's size is a high-water mark that
# pruned generations leave behind.
MAX_SCHEMA_MB: float = 10000.0
# What the build writes for ONE in-scope advert — its rt_fp row, its fp_key rows, its share of
# the stored pairs and of the groups — at what the live store costs, not at E916's row bytes:
# on 2026-10-09 generation `rt` held 129.2 MB for 7,298 adverts, 18.1 KiB an advert (its
# 25,609 pairs 100.8 MB, 14.1 KiB an advert: 3.5 pairs an advert at ~4.1 kB each on disk with
# their indexes, TOAST and reused dead space apportioned; fp_key 2.8 KiB). E916's ~12 kB was
# the trial build's row cost (2.76 pairs an advert at 2,869 B). Plus E943's reject ledger:
# ~10-12 below-floor rejects an advert at the end of a build, ~182 B each with its key, ~2 KiB.
BUILD_BYTES_PER_LISTING: int = 20 * 1024
# What the seed's calibration CUT writes for one in-scope advert: its share of the pHash
# population (`phash_pop`, upserted by hash). The seed's second storage check runs BEFORE the cut
# (E940: a refusal must not spend the cut first), so it projects this instead of reading it.
# Measured 2026-10-17: phash_pop 63 MiB for the 10-16 seed's 428,798 hashes (~154 B a row with
# its index) at 7.08 hashes an advert, ~1.1 kB; an upper bound, since a hash already held costs
# nothing.
CUT_BYTES_PER_LISTING: int = 1100
# What the cut HOLDS for one advert at its peak. Until E962 it kept every scope advert's facts
# and fingerprint until the calibration was built (~41 kB held, ~58 kB at the peak, cohort 18);
# it now folds each chunk and drops it (`calibration_cut`), so an advert costs its counts and
# its share of the pHash population. The largest count is `block_token_df`, keyed per (obec,
# cast_obce) block: a vocabulary saturates inside its block, not across blocks, so the cost
# follows how many adverts share a block. Cohort 18 (17,458 adverts, w31; 74 blocks, 90 % of
# its adverts in blocks of 200 or more): 10.4 token entries an advert, ~4 kB of RSS. Re-blocked
# to kraj 27's distribution (3,334 blocks; 28 % of rows in blocks of 10-49, 32 % in 50-199; the
# review of E962): 43.9 entries, 8.06 kB of RSS and 678 B of payload an advert; a seed of mostly
# villages ~9-10 kB all in. Plus the population (120 B a hash at its peak, 7.08 hashes an
# advert: 0.85 kB), libpq's copy of its rows and the payload's two serialisations: 12 kB with
# headroom. The seed runs on GitHub's ubuntu-latest, 16 GiB, and a cut past it is OOM-killed,
# stranding `rt_lease` for SEED_LEASE_TTL_S (the lease is taken before the transaction and
# SIGKILL runs no release). So right after its walk, before the cut, the seed refuses a scope
# whose peak would pass HALF the runner — the rule a widened scope's gate holds the worker to
# (E939) — 8,192 MiB / 12 kB = 699,050 adverts (E940's whole cut: 56 kB, ~149,800), though
# MAX_SCHEMA_MB binds first, above ~470k. The 323,128 of the re-seed: ~3.7 GiB.
CUT_PEAK_BYTES_PER_LISTING: int = 12 * 1024
SEED_MEMORY_MB: float = 8192.0
# How many `phash_pop` rows one `executemany` carries. The trial cohort's 41,791 hashes are
# 9 chunks; the number is the score lane's, for the same reason (bound-parameter size).
POP_CHUNK: int = 5_000
# How many listing ids one fact statement set carries (`SqlFacts.facts`). The pass re-reads the
# WHOLE probe-key neighbourhood of its claim every pass, and a bootstrap's neighbourhood grows
# with the store (~3,300 listings and ~40,000 images with a ~kB CLIP vector each by the eighth
# pass of a dense scope), so unsliced the galleries' reads were the statements that grew towards
# STATEMENT_TIMEOUT_MS. Every cohort statement is a plain `= any(ids)` filter ordered within a
# listing, so slices of whole listings read exactly what the whole array would.
FACT_CHUNK: int = 500
# How many pairs one reject-ledger statement carries (E943): a lookup or a write of a full
# 150k-pair pass is eight statements of ~1 MB of bound arrays each, not one that grows with it.
LEDGER_CHUNK: int = 20_000
STATEMENT_TIMEOUT_MS: int = 120_000
LOCK_TIMEOUT_MS: int = 5_000
# Above the pass's own deadline (PASS_DEADLINE_S) and under the lease (LEASE_TTL_S): the scoring
# loop is DB-silent by design (no statement between the endpoint reads and `upsert_pairs`) and
# the deadline already bounds it, so an idle guard tighter than the deadline bites a HEALTHY
# pass — at 300 s it terminated every bootstrap pass past the seventh on the worker (2026-10-02).
IDLE_TIMEOUT_MS: int = 1_200_000
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

    def __init__(self, conn: Any, generation: str = GENERATION, *, calibration_digest: str,
                 engine_digest: str, store_floor: float = 0.02,
                 model_version: str | None = None) -> None:
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
        # E943: the reject ledger's key besides the two decision digests is the store's ALONE —
        # this calibration digest, this engine digest and `FEATURE_VERSION` — read by the
        # lookup and written by the write, so the two can never disagree about a row.
        self.engine_digest = engine_digest
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
        # This pass's start, as `rulings_changed` read it: the `rt_rulings` cursor `flush` writes.
        self._rulings_read_at: Any = None
        self._ledger_keyed: bool | None = None

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
                "dd": pending[listing_id][0].dd or None,
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

    def put_decision_digests(self, dds: Mapping[int, str]) -> None:
        ids = sorted(int(i) for i in dds)
        if not ids:
            return
        self._run(RT_FP_DD_SQL, {"generation": self.generation, "ids": ids,
                                 "dds": [dds[i] for i in ids]})
        for listing_id in ids:
            row = self._fp.get(listing_id)
            if row is not None:
                self._fp[listing_id] = replace(row, dd=dds[listing_id])

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
                str(row[15] or ""),
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
            slots=_slots(row[15] if len(row) > 15 else None),
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

    def upsert_pairs(self, rows: Sequence[PairRow]) -> int:
        # RETENTION (E79), and it is the batch lane's rule rather than a new one:
        # `score_lane.persist` writes only `storable(row, store_floor)` — the whole merge and
        # band zones whatever they scored, plus the reject tail at or above `store_floor`
        # (0.02 at w8) — so the store grows with duplicates and not with comparisons. Measured
        # on the scoped cohort: 13,043 of 44,724 decisions are storable (the live `pairs` table
        # says the same of the batch generations: 15,811 of 48,908 over the whole cohort), so
        # this is a 3.4x saving and not a rounding. A pair that falls BELOW the floor is
        # deleted, not left behind, and ledgered (E943) so the next pass does not re-decide it.
        keep: list[PairRow] = []
        evicted: list[PairRow] = []
        for row in rows:
            if storable({"zone": row.zone, "score": row.score, "evidence": row.evidence},
                        self.store_floor):
                keep.append(row)
            else:
                evicted.append(row)
        self.pairs_retained += len(keep)
        self.pairs_evicted += len(evicted)
        if evicted:
            self.delete_pairs([(row.lo, row.hi) for row in evicted])
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
        self._ledger(evicted, keep)
        return len(evicted)

    def _ledger(self, evicted: Sequence[PairRow], kept: Sequence[PairRow]) -> None:
        """E943: every pair deleted for its score goes into the reject ledger under the store's
        key, and every pair stored leaves it — inside the pass's transaction, so a pass that
        rolls back ledgers nothing."""
        for start in range(0, len(evicted), LEDGER_CHUNK):
            chunk = evicted[start:start + LEDGER_CHUNK]
            self._run(RT_REJECT_LEDGER_UPSERT_SQL, {
                "generation": self.generation,
                "los": [row.lo for row in chunk], "his": [row.hi for row in chunk],
                "dd_los": [row.dd_lo for row in chunk], "dd_his": [row.dd_hi for row in chunk],
                "calibration_digest": self.calibration_digest,
                "feature_version": FEATURE_VERSION, "engine_digest": self.engine_digest})
        for start in range(0, len(kept), LEDGER_CHUNK):
            chunk = kept[start:start + LEDGER_CHUNK]
            self._run(RT_REJECT_LEDGER_DELETE_SQL, {
                "generation": self.generation,
                "los": [row.lo for row in chunk], "his": [row.hi for row in chunk]})

    def rejected(self, keys: Sequence[tuple[int, int, str, str]]) -> set[tuple[int, int]]:
        out: set[tuple[int, int]] = set()
        for start in range(0, len(keys), LEDGER_CHUNK):
            chunk = keys[start:start + LEDGER_CHUNK]
            out.update((int(row[0]), int(row[1])) for row in self._query(
                RT_REJECT_LEDGER_LOOKUP_SQL, {
                    "generation": self.generation,
                    "los": [key[0] for key in chunk], "his": [key[1] for key in chunk],
                    "dd_los": [key[2] for key in chunk], "dd_his": [key[3] for key in chunk],
                    "calibration_digest": self.calibration_digest,
                    "feature_version": FEATURE_VERSION, "engine_digest": self.engine_digest}))
        return out

    def ledger_keyed(self) -> bool:
        if self._ledger_keyed is None:
            self._ledger_keyed = bool(self._query(RT_REJECT_LEDGER_KEYED_SQL, {
                "generation": self.generation, "calibration_digest": self.calibration_digest,
                "feature_version": FEATURE_VERSION,
                "engine_digest": self.engine_digest})[0][0])
        return self._ledger_keyed

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
                       [_conflict_params(row, self.generation) for row in conflicts
                        if not row.get("must_link")])
        self._run_many(RT_CLOSURE_CONFLICT_APPEND_SQL,
                       [_conflict_params(row, self.generation) for row in conflicts
                        if row.get("must_link")])

    def must_not_link(self) -> set[tuple[int, int]]:
        return {(int(row[0]), int(row[1]))
                for row in self._query(RT_MUST_NOT_LINK_SQL)}

    def must_link(self) -> set[tuple[int, int]]:
        return {(int(row[0]), int(row[1]))
                for row in self._query(RT_MUST_LINK_SQL)}

    def rulings_changed(self) -> set[int]:
        rows = self._query(RT_RULINGS_CHANGED_SQL,
                           {"name": CURSOR_RULINGS, "overlap_s": RULINGS_OVERLAP_S})
        if not rows:
            return set()
        self._rulings_read_at = rows[0][0]
        return {int(listing_id) for listing_id in (rows[0][1] or ())}

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
        if self._rulings_read_at is not None:
            # Inside the pass's transaction: a pass that rolls back re-reads the same rulings.
            self._run(RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_RULINGS, "last_listing_id": None, "last_snapshot_id": None,
                "watermark": self._rulings_read_at})
            self._rulings_read_at = None


def _slots(raw: Any) -> dict[str, tuple[float, bool]]:
    """The relation's three slots as the store hands them back: `{name: [value, true]}`."""
    value = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return {str(name): (float(entry[0]), bool(entry[1]) if len(entry) > 1 else True)
            for name, entry in value.items() if isinstance(entry, (list, tuple)) and entry}


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
    """A refused union, a refused bridge or a dissolved closure (E926), in the score lane's
    own row shape."""
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
    if row.get("pair"):
        detail["pair"] = [int(listing_id) for listing_id in row["pair"]]
    if row.get("must_link"):
        detail["must_link"] = [list(pair) for pair in row["must_link"]]
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
                 population: Mapping[int, int] | None = None,
                 deadline: float | None = None,
                 stopping: Callable[[], bool] | None = None) -> None:
        self.conn = conn
        self.clip_model = clip_model
        # The pass's own deadline (a `time.perf_counter()` instant, E913), read between the
        # FACT_CHUNK slices of every read (E931): a slow read is four statement sets of up to
        # STATEMENT_TIMEOUT_MS a slice, and the pass reads its clock nowhere else until the
        # step after. The cut bounds its own reads (`_cut_bound`) and leaves this unset. The
        # worker's shutdown is read at the same place (E941: `PassStopped`).
        self.deadline = deadline
        self.stopping = stopping
        # A population handed in INSTEAD of the table, for the one caller that is about to
        # WRITE the table: the calibration cut (A10) measures the scope's hashes first and
        # builds the fingerprints it is cut from on exactly those counts. The pass never
        # passes this — its population is the table's or unknown (E91).
        # Held as handed, never copied (E962: the cut's is ~2.3M hashes at the re-seed).
        self.population = population
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

    def _slices(self, ids: Sequence[int]) -> Iterator[list[int]]:
        """`ids` in FACT_CHUNK slices of whole listings, the deadline read before each: past
        it the read stops at a slice boundary and raises `PassDeadline` for the pass's
        transaction to roll back, as the pass's own between-step checks do."""
        for start in range(0, len(ids), FACT_CHUNK):
            _in_time(self.deadline, self.stopping)
            yield list(ids[start:start + FACT_CHUNK])

    def _dicts_over(self, sql: str, ids: Sequence[int], **params: Any) -> list[dict[str, Any]]:
        """`_dicts` over `ids` in FACT_CHUNK slices, concatenated."""
        rows: list[dict[str, Any]] = []
        for ids_slice in self._slices(ids):
            rows.extend(self._dicts(sql, {"ids": ids_slice, **params}))
        return rows

    def facts(self, ids: Iterable[int], *, clip: bool = True
              ) -> dict[int, tuple[Listing, list[Image]]]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        locations = {int(row["listing_id"]): row
                     for row in self._dicts_over(COHORT_LOCATION_SQL, wanted)}
        history: dict[int, list[dict[str, Any]]] = {}
        for row in self._dicts_over(COHORT_PRICE_HISTORY_SQL, wanted):
            history.setdefault(int(row["listing_id"]), []).append(row)
        listings: dict[int, Listing] = {}
        for row in self._dicts_over(COHORT_LISTINGS_SQL, wanted):
            listing_id = int(row["id"])
            # `block` is the COHORT's draw label (a reporting field the engine never reads —
            # decisions key on the fingerprint's `block_key`), and a real-time arrival belongs
            # to no draw, so it is empty here rather than invented.
            listings[listing_id] = Listing.from_json(build_listing_record(
                row, block="", location=locations.get(listing_id),
                history=history.get(listing_id, ())))
        # One statement set per slice of whole listings: a listing's frames, their vectors,
        # tags and hash counts all key by listing or image, and the counters `_galleries`
        # adds up are per frame, so the slices read what the whole array would.
        galleries: dict[int, list[Image]] = {}
        for ids_slice in self._slices(wanted):
            galleries.update(self._galleries(ids_slice, clip))
        self.reads += len(listings)
        return {i: (listing, galleries.get(i, [])) for i, listing in listings.items()}

    def vectors(self, image_ids: Iterable[int]) -> dict[int, str]:
        """The CLIP vectors of a gallery `facts` read without them (E933), and nothing else. Each
        FACT_CHUNK slice is encoded as it arrives and its text dropped before the next is read
        (E949): the text is ~6 kB a vector, and the slices concatenated were hundreds of MB."""
        wanted = sorted({int(i) for i in image_ids})
        encoded: dict[int, str | None] = {}
        for ids_slice in self._slices(wanted):
            encoded.update((int(row["image_id"]), encode_clip(row["embedding"]))
                           for row in self._dicts(COHORT_CLIP_SQL,
                                                  {"ids": ids_slice, "model": self.clip_model}))
        return {image_id: clip for image_id, clip in encoded.items() if clip is not None}

    def _galleries(self, ids: Sequence[int], clip: bool) -> dict[int, list[Image]]:
        rows = self._dicts(COHORT_IMAGES_SQL, {"ids": list(ids)})
        if not rows:
            return {}
        image_ids = [int(row["image_id"]) for row in rows]
        hashes = sorted({int(row["phash"]) for row in rows if row.get("phash") is not None})
        clips = {int(row["image_id"]): encode_clip(row["embedding"])
                 for row in self._dicts(COHORT_CLIP_SQL,
                                        {"ids": image_ids, "model": self.clip_model})
                 } if clip else {}
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
            # E943: the CLIP job's stamp, for the decision digest only; the export's record
            # (and so every artifact) does not carry it.
            out.setdefault(int(row["listing_id"]), []).append(Image.from_json(
                {**record, "clip_tagged_at": _iso(row.get("clip_tagged_at"))}))
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
                 districts: Mapping[int, Sequence[int]] | None = None,
                 max_retire_fraction: float = MAX_RETIRE_FRACTION,
                 enter_interval_hours: Mapping[str, float] | None = None,
                 max_enter_scans_per_day: int = MAX_ENTER_SCANS_PER_DAY,
                 evidence_slice: int = EVIDENCE_SLICE,
                 evidence_horizon_hours: float = EVIDENCE_HORIZON_HOURS,
                 bootstrap: bool = False,
                 pass_budget_s: float = PASS_BUDGET_S,
                 rate_per_s: float = PASS_RATE_PER_S,
                 claim_cap: int | None = None,
                 stale_claims: Sequence[int] = (),
                 stale_waiting: int = 0,
                 stale_budget: int = reconcile.STALE_FLOOR) -> None:
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
        self.claim_cap = claim_cap
        # E960: the adverts of the groups the last reconcile handed on that this pass re-claims
        # (chosen and stamped ahead of the pass), offered on its FIRST claim only, so a retry
        # after the pair budget refused is the normal feeds alone (E75); and how many wait.
        self.stale_claims = [int(i) for i in stale_claims]
        self.stale_waiting = int(stale_waiting)
        self.stale_budget = int(stale_budget)
        self.attempt = 0
        # Set by `claim` while the phase is on: the entrant backlog it measured, and whether
        # this pass is the one that empties it (E98).
        self.bootstrap_done = False
        self.bootstrap_backlog: int | None = None
        # What bound this pass's claim — the count, the cap or the clock — for the run summary.
        self.claim_bound: dict[str, Any] = {}
        self.parents = dict(parents or {})
        self.districts = dict(districts or {})
        self.enter_blocks = walk_blocks(scope, self.parents, self.districts)
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
        # and the pass's deadline is a clock. The rate is what the last pass of this
        # generation measured itself at, so the bound is evidence rather than a guess, and it
        # is applied here — before a statement is issued — because an aborted pass has spent
        # its time whether or not it wrote anything.
        by_time = max(BOOTSTRAP_MIN_CLAIM, int(self.pass_budget_s * self.rate_per_s))
        # E948: the size this container survived. It lowers the limit below, so the build's
        # entered feed (the whole limit) and every other feed's fifth shrink with it; only the
        # revived feed takes every revived id of its `revive_slice` scan, whatever the limit
        # (owed since E941).
        cap = int(limit) if self.claim_cap is None else max(1, int(self.claim_cap))
        effective = max(1, min(int(limit), cap, by_time))
        self.claim_bound = {"max_listings": int(limit), "cap": cap,
                            "pass_budget_s": self.pass_budget_s,
                            "rate_per_s": self.rate_per_s, "by_time": by_time,
                            "limit": effective,
                            "bound_by": ("time" if by_time < min(int(limit), cap)
                                         else "cap" if cap < int(limit) else "count")}
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
            pointer = self.refresh_scope_ids(cursors[CURSOR_ENTER][1])
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
        # E960: the groups a merge waits on, re-claimed WHOLE, oldest wait first, so their pairs
        # are re-decided on facts read now and the group merges in this pass's reconcile — on
        # the pass's first claim only.
        self.attempt += 1
        if self.stale_claims or self.stale_waiting:
            retiring = {item.listing_id for item in items if item.retire}
            taken = ([i for i in self.stale_claims if i not in retiring]
                     if self.attempt == 1 else [])
            self.evidence = {**self.evidence, "stale": self.stale_waiting,
                             "stale_claimed": len(taken), "stale_budget": self.stale_budget}
            items.extend(WorkItem(listing_id, "stale", None, None, redecide=True)
                         for listing_id in taken)
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

    def refresh_scope_ids(self, pointer: int, *, seed: bool = False) -> int:
        """Re-walk scope blocks on `public`, but only when the cadence says so (W9e/R3), ONE PAGE
        a walk (E940).

        This is the whole cost of the entrant feed, and it used to be paid every cycle: the
        quarter block is a 29,265-block bitmap heap scan of its parent obec — 231 MB and 6.4 s,
        cold every time, because the working set does not stay in shared_buffers — and there is
        no narrower path (no index on `cast_obce_kod`, D8 forbids adding one, and `listings`
        carries no indexed obec or quarter column either). At ~48 walks a day that was ~11 GB a
        day of cold reads on the instance that serves Browse, whether or not anything had
        entered. Now it is `ENTER_INTERVAL_HOURS` a block — 6 h for a quarter, 1 h for a
        town — the block the interval has been waiting on longest first, at most ONE a pass,
        and never more than `MAX_ENTER_SCANS_PER_DAY` in a rolling day. What that cadence
        costs in latency it costs only on the TAIL: an entrant that arrived recently is already
        the straggler sweep's.

        A walk reads the `enter_slice` rows after the block's page cursor (`PAGES_SETTING`); a
        full page moves the cursor to its last row and a short one — the block's end — back to
        0, and each page prunes only its own id range, so a block larger than a page is listed
        whole over consecutive walks and the entrant feed claims each page as it lands. The
        cadence is per walk: a block of n pages is read whole every n intervals.

        `seed=True` is `rt_seed`'s walk: every block of the scope, each paged to its end in this
        one call and under no daily cap — a seed is a one-off listing that the calibration and
        the build's storage check are cut over; the cap governs passes.

        Returns the block pointer the cursor should carry, so ties (nothing scanned yet) rotate
        instead of always naming the same block."""
        blocks = self.enter_blocks
        began = time.time()
        self.statements += 1
        _exec(self.conn, RT_SCOPE_IDS_PRUNE_BLOCKS_SQL, {
            "generation": self.generation, "block_keys": [block.key for block in blocks]})
        state = {str(row[0]): (row[1], int(row[2] or 0))
                 for row in self._query(RT_SCOPE_SCAN_STATE_SQL, {
                     "generation": self.generation, "hours": 24})}
        pages, held = self._pages()
        start = int(pointer) % len(blocks)
        # THE BOOTSTRAP PHASE (E98). A cadence is what keeps a steady-state lane off `public`;
        # a scope nothing has listed yet has no steady state, and one block a pass means the
        # entrant feed offers nothing at all until the pass that walks the block a listing is
        # in. So while the phase is on, every block this generation has not yet READ TO ITS END
        # is walked in the same pass — a page each, the cadence ignored, the cap not — and a
        # block read whole once falls back to the cadence immediately, because re-walking
        # Vysočany is 231 MB of cold heap reads and the phase is about listing it, not about
        # refreshing it.
        seen = self._walked_ever() if self.bootstrap and not seed else set()
        due: list[tuple[float, int, int]] = []
        for offset in range(len(blocks)):
            index = (start + offset) % len(blocks)
            block = blocks[index]
            age, _scans = state.get(block.key, (None, 0))
            interval = float(self.enter_interval_hours.get(
                block.grain, ENTER_INTERVAL_HOURS.get(block.grain, 6.0))) * 3600.0
            if seed or age is None or (self.bootstrap and block.key not in seen):
                due.append((float("inf"), offset, index))
            elif float(age) >= interval:
                due.append((float(age), offset, index))
        if not due:
            self._report_pages(pages, [], 0, began)
            return start
        scans_24h = sum(scans for _age, scans in state.values())
        order = [index for _age, _offset, index in
                 sorted(due, key=lambda row: (-row[0], row[1]))]
        if seed:
            wanted = order
        else:
            wanted = ([index for index in order if blocks[index].key not in seen]
                      if self.bootstrap else []) or order[:1]
        walked: list[dict[str, Any]] = []
        last = start
        unwalked: list[str] | None = None
        for position, index in enumerate(wanted):
            if not seed and scans_24h >= self.max_enter_scans_per_day:
                # A count, not a crash: the pass goes on claiming out of the snapshot it has.
                unwalked = [blocks[rest].key for rest in wanted[position:]]
                break
            block = blocks[index]
            entry = pages.get(block.key) or {}
            after = int(entry.get("after") or 0)
            page = int(entry.get("page") or 0) if after else 0
            # A block with no entry but a scan behind it was read whole by its last walk (only a
            # block that pages keeps an entry), so a page it now fills opens a LATER cycle, not
            # its first: it stays read. `of` 0 is a first cycle only with no walk behind it.
            of = (int(entry.get("of") or 0) if block.key in pages
                  else 1 if block.key in state else 0)
            started = time.time()
            for rows, end in block.pages(self._query, self.enter_slice, after):
                elapsed_ms = (time.time() - started) * 1000.0
                listing_ids = [int(row[0]) for row in rows]
                params = {"generation": self.generation, "block_key": block.key,
                          "listing_ids": listing_ids}
                if listing_ids:
                    self.statements += 1
                    _exec(self.conn, RT_SCOPE_IDS_WRITE_SQL,
                          {**params, "resolved": [_iso(row[1]) for row in rows]})
                self.statements += 1
                _exec(self.conn, RT_SCOPE_IDS_PRUNE_SQL,
                      {**params, "after_id": after, "page_end": end})
                self.statements += 1
                _exec(self.conn, RT_SCOPE_SCAN_WRITE_SQL, {
                    "generation": self.generation, "block_key": block.key,
                    "rows_found": len(listing_ids), "elapsed_ms": round(elapsed_ms, 3)})
                scans_24h += 1
                page += 1
                walked.append({"block": block.key, "rows": len(listing_ids), "after_id": after,
                               "page": page, "of": page if end is None else (of or None),
                               "end": end is None, "elapsed_ms": round(elapsed_ms, 3)})
                after, page, of = (0, 0, page) if end is None else (end, page, of)
                pages[block.key] = {"after": after, "page": page, "of": of}
                if not seed:
                    break
                started = time.time()
            last = (index + 1) % len(blocks)
        if walked:
            self._write_pages(pages, held)
        self._report_pages(pages, walked, scans_24h, began, unwalked=unwalked)
        return last if walked else start

    def _pages(self) -> tuple[dict[str, dict[str, int]], bool]:
        """Each scope block's page cursor (E940), and whether the generation has a row: a block
        with no entry starts at page one."""
        key = pages_key(self.generation)
        self.statements += 1
        raw = lane_settings(self.conn, [key]).get(key)
        held = raw if isinstance(raw, Mapping) else {}
        return ({str(block): {name: int((entry or {}).get(name) or 0)
                              for name in ("after", "page", "of")}
                 for block, entry in held.items()}, bool(held))

    def _write_pages(self, pages: Mapping[str, Mapping[str, int]], held: bool) -> None:
        """The page cursors, written with the snapshot they describe, in the same transaction
        (a rolled-back pass rolls its pages back with it). Only a block that pages keeps an
        entry — one mid-cycle, or one whose last full cycle took more than a page — and a block
        the scope no longer holds keeps none."""
        keys = {block.key for block in self.enter_blocks}
        kept = {key: dict(entry) for key, entry in sorted(pages.items())
                if key in keys and (entry.get("after") or int(entry.get("of") or 0) > 1)}
        if not (kept or held):
            return
        self.statements += 1
        _exec(self.conn, RT_SETTING_WRITE_SQL, {
            "key": pages_key(self.generation), "value": json.dumps(kept),
            "updated_by": f"{LANE_NAME}:walk"})

    def _report_pages(self, pages: Mapping[str, Mapping[str, int]],
                      walked: list[dict[str, Any]], scans_24h: int, began: float, *,
                      unwalked: Sequence[str] | None = None) -> None:
        """The walk's report (E940): every walk (`blocks`, each with its `page` of its cycle and
        the cycle's `of`, None before the block's first cycle has ended, and whether it reached
        the `end`), how many were full `pages` (the block goes on past them), each block inside
        a cycle as `page_of` `[page, of]`, the blocks a cursor that does not advance has
        `stalled`, and — when the cap stopped the pass (`unwalked`: the due blocks it never
        reached) — the blocks it left `unfinished`: those, and every block inside a cycle.
        Never a silent truncation."""
        page_of = {key: [int(entry["page"]), int(entry.get("of") or 0) or None]
                   for key, entry in sorted(pages.items()) if entry.get("after")}
        stalled = sorted(key for key, (page, _of) in page_of.items()
                         if page >= PAGE_STALL_PAGES)
        # Logged when a stalled block is WALKED, not on every pass that reads the row.
        if paged := sorted({walk["block"] for walk in walked if walk["block"] in stalled}):
            LOG.warning("AUTODEDUP: scope block(s) %s have walked %d+ pages without reaching "
                        "their end — the page cursor is not advancing (E940)",
                        ", ".join(paged), PAGE_STALL_PAGES)
        paging = {"page_of": page_of, "stalled": stalled}
        if not walked and unwalked is None:
            # Nothing was due: what an earlier claim of this pass walked stands.
            if page_of or self.enter_scan:
                self.enter_scan = {**self.enter_scan, **paging}
            return
        self.enter_scan = {**(walked[-1] if walked else {}), "scans_24h": scans_24h,
                           "cap": self.max_enter_scans_per_day, "blocks": walked,
                           "pages": sum(1 for walk in walked if not walk["end"]), **paging,
                           "seconds": round(time.time() - began, 3)}
        if unwalked is not None:
            self.enter_scan["skipped"] = "daily cap"
            self.enter_scan["unfinished"] = sorted({*page_of, *unwalked})

    def _walked_ever(self) -> set[str]:
        """The scope blocks this generation has READ TO THEIR END at least once, ever (E98,
        E940): a scan row, and no page cursor still inside the block's first cycle.

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
                f"MAX_RETIRE_FRACTION rail ({allowed} of the {baseline} rows the store held "
                "when that window opened) — refusing. That is a scope that has gone wrong, not "
                "a geocode correction. Nothing was written and no cursor moved; check "
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
    """One block of the scope as the entrant sweep reads it: always through obce (the index
    this schema HAS) — a town's own, a quarter's parent with the quarter code as a filter, or
    an okres's own obce with the okres code as a filter (E961; neither code is indexed).

    `grain` and `code` are the SCOPE's own spelling of the block, because that is what the
    cadence and the scan ledger are keyed by (W9e/R3) — a quarter walked through its parent
    obec is still a quarter, and it is the quarter's 231 MB that earns the slower interval."""

    grain: str
    code: int
    obec: int | None = None
    cast_obce: int | None = None
    obce: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.grain}:{self.code}"

    def walk(self, limit: int, after_id: int = 0) -> tuple[str, dict[str, Any]]:
        """The statement and parameters that list ONE PAGE of this block on `public` (E940):
        its first `limit` rows after `after_id`, in listing-id order — a keyset, never an
        OFFSET."""
        page = {"after_id": int(after_id), "limit": int(limit)}
        if self.grain == "okres":
            return RT_SCOPE_OKRES_BLOCK_SQL, {"okres": self.code, "obce": list(self.obce),
                                              **page}
        return RT_SCOPE_BLOCK_SQL, {"obec": self.obec, "cast_obce": self.cast_obce, **page}

    def pages(self, query: Callable[[str, Mapping[str, Any]], Sequence[Sequence[Any]]],
              limit: int, after_id: int = 0) -> Iterator[tuple[Sequence[Sequence[Any]],
                                                               int | None]]:
        """The block a page a statement from `after_id`: each page's rows and the id the next
        page starts after — None once a page comes back short, which is the block's END. A full
        page is never the end, however many rows the block holds (E940). The sweep takes one
        page a walk, the seed and `rt_equivalence` every page."""
        limit = max(int(limit), 1)
        while True:
            rows = query(*self.walk(limit, after_id))
            end = int(rows[-1][0]) if len(rows) >= limit else None
            yield rows, end
            if end is None:
                return
            after_id = end


def walk_blocks(scope: Scope, parents: Mapping[int, int],
                districts: Mapping[int, Sequence[int]] | None = None) -> tuple[EnterBlock, ...]:
    """The scope's blocks as the entrant sweep walks them (and `rt_equivalence` reads them)."""
    if scope.whole_corpus:
        return ()        # nothing can enter a scope that already holds everything
    blocks: list[EnterBlock] = []
    for code in scope.obec_codes:
        blocks.append(EnterBlock("obec", int(code), int(code)))
    for code in scope.cast_obce_codes:
        parent = parents.get(int(code))
        # A quarter with no parent is refused by `resolve_scope_parents` before a pass is
        # built, so here it can only be a caller that resolved none: sweep what CAN be swept
        # rather than refusing a second time in the wrong place. An okres alike.
        if parent is not None:
            blocks.append(EnterBlock("cast_obce", int(code), int(parent), int(code)))
    for code in scope.okres_codes:
        obce = (districts or {}).get(int(code))
        if obce:
            blocks.append(EnterBlock("okres", int(code), obce=tuple(sorted(int(o) for o in obce))))
    return tuple(blocks)


def resolve_scope_parents(conn: Any, scope: Scope) -> dict[int, int]:
    """Each quarter block's parent obec, read once a pass from the RÚIAN register (W9d-3).

    A quarter the register cannot place is a hard error rather than a block the entrant sweep
    quietly never walks — silent partial coverage is the defect this feed exists to close. So
    is a quarter beside its own town (E941): the town's block already holds every advert of
    the quarter, so the snapshot would carry them twice, and the reconcile, which waits for
    every block holding a group's adverts to be read whole (`block_not_fully_read`), would tie
    the quarter's merges to the whole town's walk."""
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
    towns = set(scope.obec_codes)
    beside = [code for code in codes if found[code] in towns]
    if beside:
        raise ScopeError(
            "rt_scope names " + ", ".join(f"cast_obce:{code}" for code in beside)
            + " beside its own town " + ", ".join(sorted({f"obec:{found[code]}"
                                                          for code in beside}))
            + " — the town's block already holds every advert of the quarter, so they would "
            "be snapshotted twice and the quarter's merges would wait on the whole town's "
            "walk. Name the town or the quarter, not both")
    return found


def resolve_scope_districts(conn: Any, scope: Scope,
                            parents: Mapping[int, int]) -> dict[int, tuple[int, ...]]:
    """Each okres block's obce, read once a pass from the RÚIAN register (E961) — the index
    its walk rides, since `okres_kod` has none.

    The quarter's two rules, one grain up: an okres the register cannot place under a kraj
    with its obce is a hard error, and so is a town or a quarter beside its own okres — the
    district's block already holds every advert of it, twice in the snapshot and its merges
    tied to the whole district's walk (E941's reason). A kraj grain would carry the same rule
    over its okresy; there is none. `parents` is `resolve_scope_parents`' answer, the obec each
    quarter sits in."""
    codes = [int(code) for code in scope.okres_codes]
    if scope.whole_corpus or not codes:
        return {}
    found: dict[int, set[int]] = {}
    for okres, obec in _rows(conn, RT_SCOPE_OKRES_OBCE_SQL, {"codes": codes}):
        found.setdefault(int(okres), set()).add(int(obec))
    missing = [code for code in codes if code not in found]
    if missing:
        raise ScopeError(
            f"rt_scope names okres {missing} which public.ruian_admin_units cannot place under "
            "a kraj with its obce — refusing to run a scope the entrant sweep cannot walk")
    okres_of = {obec: okres for okres, obce in found.items() for obec in obce}
    beside = ([(f"obec:{code}", okres_of[code]) for code in scope.obec_codes
               if code in okres_of]
              + [(f"cast_obce:{code}", okres_of[parents[code]]) for code in scope.cast_obce_codes
                 if parents.get(code) in okres_of])
    if beside:
        raise ScopeError(
            "rt_scope names " + ", ".join(block for block, _okres in beside)
            + " beside its own okres "
            + ", ".join(sorted({f"okres:{okres}" for _block, okres in beside}))
            + " — the okres's block already holds every advert of them, so they would be "
            "snapshotted twice and their merges would wait on the whole okres's walk. Name "
            "the okres or what lies inside it, not both")
    return {okres: tuple(sorted(obce)) for okres, obce in found.items()}


def lane_settings(conn: Any, keys: Sequence[str]) -> dict[str, Any]:
    """The lane's own rows out of `autodedup.settings` — one statement, not one each."""
    out: dict[str, Any] = {}
    for row in _rows(conn, RT_SETTINGS_MANY_SQL, {"keys": list(keys)}):
        out[str(row[0])] = row[1]
    return out


def _number(value: Any, fallback: float) -> float:
    """A number the lane measured and wrote itself (the rate), or `fallback` without one."""
    raw = value.get("value") if isinstance(value, Mapping) else value
    if raw is None or isinstance(raw, bool):
        return float(fallback)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(fallback)


def scope_setting_key(generation: str) -> str:
    """`rt_scope:<generation>` (W9e/R4) — the scope the seed cut the generation for."""
    return f"{SCOPE_SETTING}:{generation}"


def pages_key(generation: str) -> str:
    """`rt_scope_pages:<generation>` (E940) — the page cursors of the blocks that page."""
    return f"{PAGES_SETTING}:{generation}"


def bootstrap_setting_key(generation: str) -> str:
    """`rt_bootstrap:<generation>` — the phase is a property of ONE generation's build."""
    return bootstrap_key(generation)


def pass_rate_key(generation: str) -> str:
    """`rt_pass_rate_per_s:<generation>` — what a pass of THIS generation measured itself at."""
    return f"{PASS_RATE_SETTING}:{generation}"


def engine_key(generation: str) -> str:
    """`rt_engine:<generation>` — the engine digest the lane runs under and since when (E960):
    the reconcile applies a merge only on members refreshed after it."""
    return f"{ENGINE_SETTING}:{generation}"


def claim_cap_key(generation: str) -> str:
    """`rt_claim_cap:<generation>` — the claim size THIS generation's passes survive (E948)."""
    return f"{CLAIM_CAP_SETTING}:{generation}"


def recut_key(generation: str) -> str:
    """`rt_recut:<generation>` — `due`: the cut stamp of the calibration a pass found the
    population drifted under; `tried_at`: when the lane last re-cut, done or not (E944)."""
    return f"{RECUT_SETTING}:{generation}"


def full_claim_cap(reason: str | None = None, limit_mb: float | None = None) -> dict[str, Any]:
    """A seed's cap, an absent row's and a bigger container's: all of `max_listings` (E948b)."""
    return {"cap": PASS_LIMITS.max_listings, "reason": reason, "limit_mb": limit_mb}


def _known_limit_mb(value: Any) -> float | None:
    """A memory limit in MiB, the row's or a reading: None unless a number above 0 (E948b)."""
    limit_mb = _number(value, 0.0)
    return limit_mb if limit_mb > 0 else None


def read_claim_cap(value: Any) -> dict[str, Any]:
    """The cap row as {cap, reason, limit_mb}, the cap inside [floor, max_listings] (E948b)."""
    record = value if isinstance(value, Mapping) else {}
    try:
        cap = int(record["cap"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return full_claim_cap()
    return {"cap": max(CLAIM_CAP_FLOOR, min(PASS_LIMITS.max_listings, cap)),
            "reason": record.get("reason"), "limit_mb": _known_limit_mb(record.get("limit_mb"))}


def _utc_second(epoch: float) -> str:
    """An epoch second as a cap row's reason spells it: 2026-10-10T07:40:53Z."""
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _halved(record: Mapping[str, Any]) -> bool:
    """A cap below `max_listings` was halved by a death since the last seed or reset (E948b)."""
    return int(record["cap"]) < PASS_LIMITS.max_listings


def halve_claim_cap(record: Mapping[str, Any], died: str, booted_epoch: int,
                    limit_mb: float | None) -> dict[str, Any]:
    """A death halves the cap, never below the floor (E948). The row records this container's
    limit, or the row's when the row was already halved under a bigger one or this limit is
    unknown (E948b)."""
    recorded = record.get("limit_mb")
    if recorded is not None and (limit_mb is None or (_halved(record) and recorded > limit_mb)):
        limit_mb = recorded
    return {"cap": max(CLAIM_CAP_FLOOR, int(record["cap"]) // 2),
            "reason": f"{died} died before {_utc_second(booted_epoch)}", "limit_mb": limit_mb}


def read_scope_setting(control: Mapping[str, Any], generation: str) -> Any:
    """The generation's scope row, as the seed wrote it."""
    return control.get(scope_setting_key(generation))


def pass_scope(setting: Any) -> Scope:
    """A PASS's scope is the one `rt_seed` persisted, and nothing else: the pass takes no
    argument, and a missing row under a seeded generation is a hard error — falling back to a
    default would retire everything the store holds outside it (W9d-2)."""
    if setting is None:
        raise ScopeError(
            "this generation has a calibration but no rt_scope row — the row IS the scope its "
            "store was built under. Re-seed it (`--mode rt_seed fresh=true`)")
    return parse_scope(setting)


def schema_bytes(conn: Any) -> int:
    return int(_rows(conn, RT_SCHEMA_SIZE_SQL)[0][0] or 0)


def rt_rows(conn: Any, generation: str, scope: Scope) -> dict[str, int]:
    """What the generation holds, by table. Exact while the scope is small — which is the
    point of a scope — and the planner's estimate when there is none to keep it cheap."""
    sql = RT_ROW_ESTIMATE_SQL if scope.whole_corpus else RT_ROW_CENSUS_SQL
    params = None if scope.whole_corpus else {"generation": generation}
    return {str(row[0]): int(row[1] or 0) for row in _rows(conn, sql, params)}


def generation_bytes(conn: Any, generation: str) -> dict[str, dict[str, int]]:
    """What ONE generation holds of each table the fresh reset empties (E916): the table's
    `pg_total_relation_size` times the generation's share of its rows — indexes, TOAST and the
    table's dead space apportioned with them, because that is the space the next build reuses
    once the reset's DELETE has made it free."""
    held: dict[str, dict[str, int]] = {}
    for name, total, n_rows, n_generation in _rows(conn, RT_GENERATION_BYTES_SQL,
                                                   {"generation": generation}):
        total, n_rows, n_generation = int(total or 0), int(n_rows or 0), int(n_generation or 0)
        held[str(name)] = {
            "bytes": total, "rows": n_rows, "generation_rows": n_generation,
            "generation_bytes": (round(total * n_generation / n_rows) if n_rows else 0)}
    return held


def storage_guard(conn: Any, generation: str, scope: Scope,
                  max_schema_mb: float = MAX_SCHEMA_MB, *,
                  replacing: bool = False, replaced_bytes: int | None = None,
                  build_listings: int | None = None) -> dict[str, Any]:
    """Read what schema `autodedup` costs BEFORE the pass writes anything, and refuse over
    budget (E79) — non-zero, cursor unmoved, nothing written, never a warning in a log line.
    It also holds the second half of the whole-corpus gate: `all` is a scope only when the
    budget could hold what `all` costs.

    `replacing=True` is `rt_seed fresh=true` and nothing else (E916): the budget is checked
    against the PROJECTED size — the schema minus what this generation's rows hold
    (`generation_bytes`) — because the seed deletes exactly those rows before it writes one.
    A fresh seed can therefore never be refused by the generation it is about to drop, and is
    still refused when what REMAINS is over budget. A pass and a non-fresh seed check the
    schema as it stands.

    `build_listings` is the seed's SECOND check (E941), inside its transaction after the walk
    and BEFORE the cut (E940): the schema as it now stands — the snapshot the walk just wrote
    included — plus what the cut and the build will write, `CUT_BYTES_PER_LISTING` and
    `BUILD_BYTES_PER_LISTING` for every listing in scope. A fresh seed hands in the first
    check's `replaced_bytes`: its reset's DELETE shrank no file, and the generation's share of
    each table now reads as nothing."""
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
    checked_mb = mb
    replaced: int | None = None
    if replacing:
        held = generation_bytes(conn, generation)
        replaced = sum(table["generation_bytes"] for table in held.values())
        report["replaced_by_table_mb"] = {
            name: round(table["generation_bytes"] / 1_048_576.0, 3)
            for name, table in sorted(held.items())}
    elif replaced_bytes is not None:
        replaced = max(int(replaced_bytes), 0)
    if replaced is not None:
        checked_mb = max(bytes_now - replaced, 0) / 1_048_576.0
        report["replaced_mb"] = round(replaced / 1_048_576.0, 2)
        report["replaced_bytes"] = int(replaced)       # carried raw into the second check
    if build_listings is not None:
        listings = max(int(build_listings), 0)
        build = listings * BUILD_BYTES_PER_LISTING
        cut = listings * CUT_BYTES_PER_LISTING
        checked_mb += (build + cut) / 1_048_576.0
        report["build_listings"] = listings
        report["build_mb"] = round(build / 1_048_576.0, 2)
        report["cut_mb"] = round(cut / 1_048_576.0, 2)
    if replaced is not None or build_listings is not None:
        report["projected_mb"] = round(checked_mb, 2)
    if checked_mb > float(max_schema_mb):
        if build_listings is not None:
            freed = (f", less the {report['replaced_mb']:.1f} MB of generation {generation!r} "
                     "its reset deleted" if replaced is not None else "")
            raise StorageRefusal(
                f"after the seed's walk, schema autodedup is {mb:.1f} MB{freed}; the cut's "
                f"pHash population (~{report['cut_mb']:.1f} MB) and the build of the "
                f"{report['build_listings']:,} listings in scope (~{report['build_mb']:.1f} MB, "
                f"{BUILD_BYTES_PER_LISTING // 1024} kB a listing) bring it to a projected "
                f"{checked_mb:.1f} MB, over the {float(max_schema_mb):.0f} MB MAX_SCHEMA_MB "
                "budget — refusing before the cut: the seed's transaction rolls back and "
                "nothing was written. Narrow rt_scope, prune a batch generation (score "
                "keep_generations=<n>) or raise the constant.")
        if replacing:
            raise StorageRefusal(
                f"schema autodedup is {mb:.1f} MB and {report['replaced_mb']:.1f} MB of it is "
                f"generation {generation!r}, which this fresh seed deletes first — the "
                f"projected {checked_mb:.1f} MB is still over the {float(max_schema_mb):.0f} "
                "MB MAX_SCHEMA_MB budget, so refusing to write. Prune a batch generation "
                "(score keep_generations=<n>) or raise the constant; nothing was written.")
        raise StorageRefusal(
            f"schema autodedup is {mb:.1f} MB, over the {float(max_schema_mb):.0f} MB "
            "MAX_SCHEMA_MB budget — refusing to write. Prune a generation or raise the "
            "constant; nothing was written and no cursor moved.")
    if not guard_agrees(scope, max_schema_mb):
        raise StorageRefusal(
            f"rt_scope=all needs MAX_SCHEMA_MB >= {CORPUS_PROJECTION_MB} MB (the measured "
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
                                  CURSOR_SCOPE, CURSOR_ENTER, CURSOR_EVIDENCE, CURSOR_RULINGS,
                                  reconcile.CURSOR)

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
    ("rt_reject_ledger", RT_FRESH_REJECT_LEDGER_SQL),
)


def reset_generation(conn: Any, generation: str, *, holder: str) -> dict[str, int]:
    """Empty ONE generation and nothing else (E97), counting what went.

    A DELETE of this generation's rows, never a TRUNCATE, run inside the seed's transaction: a
    seed that refuses after this point leaves the store exactly as it found it. The ledger of
    production merges (`autodedup.applied_merges`) is NOT here: a rebuild changes what the lane
    will decide, never what it has already merged. `holder` is the seed's own lease, which the
    reset keeps: the seed holds it so no pass can write into the generation while it empties."""
    deleted: dict[str, int] = {}
    for name, sql in RESET_TABLES:
        rows = _rows(conn, sql, {"generation": generation})
        deleted[name] = int(rows[0][0] or 0) if rows else 0
    rows = _rows(conn, RT_FRESH_CURSORS_SQL, {"names": list(RESET_CURSORS)})
    deleted["scan_cursor"] = int(rows[0][0] or 0) if rows else 0
    rows = _rows(conn, RT_FRESH_SETTINGS_SQL, {
        "keys": [engine_key(generation), reconcile.redecide_key(generation),
                 reconcile.tried_key(generation), reconcile.budget_key(generation),
                 pages_key(generation)]})
    deleted["settings"] = int(rows[0][0] or 0) if rows else 0
    rows = _rows(conn, RT_FRESH_LEASE_SQL, {"name": LANE_NAME, "holder": holder})
    deleted["rt_lease"] = int(rows[0][0] or 0) if rows else 0
    return deleted


def take_lease(conn: Any, holder: str, ttl: int = LEASE_TTL_S) -> bool:
    return rt_lease.take(conn, holder, ttl)


def release_lease(conn: Any, holder: str) -> None:
    rt_lease.release(conn, holder)


def _transaction(conn: Any) -> ContextManager[Any]:
    """One pass, one transaction (E75). `db.connect` is autocommit, so without this a crash
    between the cluster DELETE and its INSERT loses those clusters permanently — the re-claim
    finds matching digests, skips, and never rebuilds them."""
    opener = getattr(conn, "transaction", None)
    return opener() if callable(opener) else nullcontext()


class _Refused(Exception):
    """The pair budget refused this claim — roll the pass back and report it (E75)."""


# --- WHICH SCORER (E90a) -----------------------------------------------------------------


def named_config(args: Mapping[str, str], *, what: str
                 ) -> tuple[Settings, LogisticModel, str, str]:
    """The settings row and the model a dispatch NAMES — and silence is not a name (E90a).

    The generation's scorer is chosen once, by the seed, and every stored decision is stamped
    with it; the defaults are spelled `settings=default,model=prior`, never implied."""
    settings_name = str(args.get("settings") or "").strip()
    model_name = str(args.get("model") or "").strip()
    if not settings_name or not model_name:
        raise SystemExit(
            f"{what} needs settings=<row in autodedup/settings> and model=<file in "
            f"autodedup/models> — a generation is scored by ONE scorer and every stored "
            f"decision is stamped with it. The production engine is "
            f"`settings=w29,model=w6_gold`; the uncalibrated defaults are "
            f"`settings={DEFAULT_SETTINGS_NAME},model={PRIOR_MODEL_NAME}`, which is a choice "
            "this lane will not make on your behalf (E90a).")
    return (named_settings(settings_name), named_model(model_name),
            settings_name, model_name)


def pass_config(recorded: Any, model_version: Any, generation: str
                ) -> tuple[Settings, LogisticModel]:
    """The GENERATION's scorer, read back from its calibration row: changing it is a re-seed,
    because every decision already in the store was taken under the old one."""
    if recorded is None:
        raise SystemExit(
            f"generation {generation!r} carries no settings on its calibration — re-seed it "
            "(`-f mode=rt_seed -f args=settings=w29,model=w6_gold,fresh=true`)")
    try:
        settings = Settings.from_dict(dict(recorded))
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"generation {generation!r} carries settings this build cannot read ({exc}) — "
            "re-seed it.") from exc
    return settings, model_of_version(str(model_version) if model_version else None)


def phash_pop_rows(conn: Any) -> int:
    return int(_rows(conn, RT_PHASH_POP_COUNT_SQL)[0][0] or 0)


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


def age_days(stamp: Any) -> float | None:
    parsed = _stamp(stamp)
    if parsed is None:
        return None
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0, 3)


# --- A10: THE CALIBRATION, CUT FROM THE DATABASE (E912) -----------------------------------


class CalibrationRefusal(Exception):
    """The scope holds nothing the cut could be taken over."""


def _cut_bound(conn: Any, deadline: float | None,
               stopping: Callable[[], bool] | None = None) -> None:
    """The cut's statement timeout: the export's own (CUT_STATEMENT_TIMEOUT_MS) at a seed, and
    never past the tick's deadline in a re-cut (review A5/B6; E944) — the pHash aggregate is a
    sequential scan of `public.images`, and an unbounded one could outlive the worker's stall
    warning. Past the deadline the cut stops between chunks and its transaction rolls back; so
    it does once the worker is `stopping` (E941: `PassStopped`)."""
    _in_time(None, stopping)
    if deadline is None:
        timeout_ms = CUT_STATEMENT_TIMEOUT_MS
    else:
        left_ms = int((deadline - time.perf_counter()) * 1000)
        if left_ms <= 0:
            raise CalibrationRefusal("the tick's time ran out mid-cut; the next attempt waits "
                                     "RECUT_MIN_AGE_H")
        timeout_ms = min(CUT_STATEMENT_TIMEOUT_MS, left_ms)
    _exec(conn, RT_STATEMENT_GUARD_SQL, {"statement_timeout_ms": timeout_ms})


# E944: the calibration this process last read, by generation, under the row's digest. The row is
# content-addressed (`Calibration.digest`), so a pass reads and parses the payload only when the
# digest moved — a cut or a seed — and every other pass reads it back from here.
_CALIBRATIONS: dict[str, tuple[str, Calibration]] = {}


def read_calibration(conn: Any, generation: str = GENERATION
                     ) -> tuple[Sequence[Any], Calibration]:
    """The generation's calibration row and its calibration, the payload read once a digest.

    The one reader of `payload_zlib` (E963). The row handed back holds no payload, so a pass
    does not keep the compressed bytes beside the calibration they decoded to."""
    held = _CALIBRATIONS.get(generation)
    rows = _rows(conn, RT_CALIBRATION_READ_SQL,
                 {"generation": generation, "held": held[0] if held else None})
    if not rows:
        raise SystemExit(f"no calibration for generation {generation!r} — seed it")
    row = (*rows[0][:3], None, *rows[0][4:])
    digest, blob = str(rows[0][1]), rows[0][3]
    if held is not None and blob is None and held[0] == digest:
        return row, held[1]
    if blob is None:
        # A row cut before E963 carries its payload in the retired jsonb column, which nothing
        # reads: deciding under an empty calibration would stamp decisions nobody took.
        raise SystemExit(
            f"the calibration of generation {generation!r} (digest {digest!r}) has no "
            "payload_zlib — it was cut before E963 (migration 616). Re-seed it under the brake: "
            "`gh workflow run autodedup.yml -f mode=rt_seed "
            "-f args=\"settings=<name>,model=<name>,fresh=true\"`.")
    calibration = decode_payload(bytes(blob))
    # The stored bytes must decode to the calibration the row names: a drift between the
    # encoding and the decoding refuses here, before it is memoised under the wrong digest.
    if calibration.digest() != digest:
        raise SystemExit(
            f"the calibration of generation {generation!r} decodes to digest "
            f"{calibration.digest()!r}, not the row's {digest!r} — refusing to decide under it")
    _CALIBRATIONS[generation] = (digest, calibration)
    return row, calibration


def recut_due(recut: Any, built_at: Any) -> bool:
    """A pass found the population drifted under the calibration still in force (E944): a seed
    or a cut since then rewrote `built_at`, and what it measured is void."""
    due = recut.get("due") if isinstance(recut, Mapping) else None
    return due is not None and due == _iso(built_at)


def _recut_age_ok(recut: Any, built_at: Any) -> bool:
    """At most one re-cut a `RECUT_MIN_AGE_H`, counted from the last cut or attempt (E944)."""
    tried = _stamp(recut.get("tried_at") if isinstance(recut, Mapping) else None)
    stamps = [stamp for stamp in (_stamp(built_at), tried) if stamp is not None]
    return not stamps or (
        datetime.now(timezone.utc) - max(stamps)).total_seconds() >= RECUT_MIN_AGE_H * 3600.0


def recut_unfit(bootstrap: bool, scope_listings: int, recut: Any = None,
                built_at: Any = None) -> str | None:
    """Why no tick could finish a re-cut now (E944): a build, a scope too big for one tick, or a
    cut that already killed its worker RECUT_MAX_DEATHS times under this calibration."""
    if bootstrap:
        return "bootstrap"
    if scope_listings > RECUT_MAX_LISTINGS:
        return "unfit"
    if (isinstance(recut, Mapping) and recut.get("died_under") == _iso(built_at)
            and int(recut.get("deaths") or 0) >= RECUT_MAX_DEATHS):
        return "died"
    return None


def _deaths(recut: Mapping[str, Any]) -> dict[str, Any]:
    return {key: recut[key] for key in ("deaths", "died_under") if key in recut}


def _write_recut(conn: Any, generation: str, holder: str, value: Mapping[str, Any],
                 why: str) -> None:
    """`rt_recut:<generation>`, fenced like every lane write (E941)."""
    with _transaction(conn):
        _exec(conn, RT_SETTING_WRITE_SQL, {"key": recut_key(generation),
                                           "value": json.dumps(value),
                                           "updated_by": f"{LANE_NAME}:{why}"})
        rt_lease.hold(conn, holder)


def _recut_memory() -> dict[str, Any]:
    return {"peak_rss_mb": peak_rss_mb(), "rss_mb": rss_mb()}


def recut_died(conn: Any, generation: str, holder: str, recut: Any, built_at: Any) -> Any:
    """A re-cut stamped `cutting` and never ended: its worker died inside it (OOM, SIGKILL).
    Recorded as a failed attempt, counted under the calibration in force, so its
    RECUT_MIN_AGE_H wait stands and the heartbeat says so; the row as it now stands is
    returned."""
    if not isinstance(recut, Mapping) or not recut.get("cutting"):
        return recut
    deaths = int(recut.get("deaths") or 0) if recut.get("died_under") == _iso(built_at) else 0
    value = {key: item for key, item in recut.items() if key != "cutting"}
    value.update(deaths=deaths + 1, died_under=_iso(built_at))
    if deaths + 1 >= RECUT_MAX_DEATHS:
        value["due"] = None
    value["last"] = {"skipped": f"died mid-cut ({recut['cutting']})",
                     "at": recut.get("tried_at"), "failed": True, "deaths": deaths + 1}
    with suppress(rt_lease.LeaseLost):
        _write_recut(conn, generation, holder, value, "recut_died")
    return value


def run_recut(conn: Any, settings: Settings, model_version: str | None, generation: str, *,
              holder: str, before: str, recut: Any, deadline: float,
              stopping: Callable[[], bool] | None = None) -> dict[str, Any]:
    """The re-cut as a tick of its own (E944): `cut_calibration` in its own transaction, under
    the pass's lease and fence and a deadline of its own, so no pass pays for it; its outcome is
    kept in `rt_recut:<generation>` (`last`) in the same commit. The attempt is stamped first
    (`tried_at`, `cutting`), so a worker that dies inside it waits RECUT_MIN_AGE_H instead of
    re-cutting into the same death. A cut that fails waits too (`failed`). One that its fence
    or the worker's shutdown stopped is taken back — the row as it was, the one write after the
    lease is gone — so the next holder re-cuts at once."""
    previous = dict(recut) if isinstance(recut, Mapping) else {}
    at = datetime.now(timezone.utc).isoformat()
    clock = time.perf_counter()
    try:
        _write_recut(conn, generation, holder, {**previous, "tried_at": at, "cutting": holder},
                     "recut_start")
    except rt_lease.LeaseLost as exc:
        return {"skipped": f"{type(exc).__name__}: {exc}"[:300], "at": at}
    try:
        with _transaction(conn):
            out: dict[str, Any] = cut_calibration(conn, settings, model_version, generation,
                                                  deadline=deadline, stopping=stopping,
                                                  reserve_s=RECUT_WRITE_RESERVE_S)
            out.update(digest_before=before, moved=out["digest"] != before, at=at,
                       **_recut_memory())
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": recut_key(generation),
                "value": json.dumps({**_deaths(previous), "due": None, "tried_at": at,
                                     "last": out}),
                "updated_by": f"{LANE_NAME}:recut"})
            rt_lease.hold(conn, holder)
        return out
    except Exception as exc:  # noqa: BLE001 — rolled back; the row and the heartbeat say why
        out = {"skipped": f"{type(exc).__name__}: {exc}"[:300], "at": at,
               "seconds": round(time.perf_counter() - clock, 3), **_recut_memory()}
        if isinstance(exc, (rt_lease.LeaseLost, PassStopped)) or (stopping and stopping()):
            # Only while the stamp is still this attempt's: a next holder may have stamped its
            # own by now, and the row is its to write.
            with suppress(Exception):
                _exec(conn, RT_RECUT_PUT_BACK_SQL, {"key": recut_key(generation),
                                                    "value": json.dumps(previous),
                                                    "holder": holder,
                                                    "updated_by": f"{LANE_NAME}:recut_stopped"})
            return out
        out["failed"] = True
        with suppress(rt_lease.LeaseLost):
            _write_recut(conn, generation, holder,
                         {**_deaths(previous), "due": None, "tried_at": at, "last": out},
                         "recut_failed")
        return out


def seed_memory(scope_listings: int, memory_mb: float = SEED_MEMORY_MB) -> dict[str, Any]:
    """What the seed's calibration cut will hold at its peak (E940; streamed since E962), refused
    over SEED_MEMORY_MB before the cut starts: an OOM kill mid-cut strands `rt_lease` for
    SEED_LEASE_TTL_S."""
    peak_mb = max(int(scope_listings), 0) * CUT_PEAK_BYTES_PER_LISTING / 1_048_576.0
    report = {"scope_listings": int(scope_listings), "cut_peak_mb": round(peak_mb, 1),
              "seed_memory_mb": float(memory_mb)}
    if peak_mb > float(memory_mb):
        raise SystemExit(
            f"rt_seed refused: the calibration cut (streamed, E962) holds "
            f"~{CUT_PEAK_BYTES_PER_LISTING // 1024} kB an advert at its peak, so the "
            f"{int(scope_listings):,} adverts the walk listed would peak at ~{peak_mb:,.0f} MiB, "
            f"over SEED_MEMORY_MB {float(memory_mb):,.0f} (half the runner's 16 GiB): the "
            "runner would kill the seed mid-cut and strand autodedup.rt_lease for "
            f"{SEED_LEASE_TTL_S:,} s. Narrow rt_scope. Nothing was written.")
    return report


def _population(conn: Any, hashes: list[int]) -> dict[int, int]:
    """E91's population of `hashes` (sorted): ONE statement, because `public.images` has no
    index on `phash` (D8) and a statement a slice would be a sequential scan a slice. Its rows
    are read off the cursor one at a time rather than fetched into a list first (E962: ~2.3M
    rows for the 323k-advert re-seed, ~280 MB of tuples next to the dict they fill); libpq
    still holds the whole result while they are read, ~64-72 B a row (~150 MB at 2.3M)."""
    if not hashes:
        return {}
    with conn.cursor() as cur:
        cur.execute(COHORT_PHASH_POP_SQL, {"hashes": hashes})
        return {int(row[0]): int(row[1]) for row in cur}


def cut_calibration(conn: Any, settings: Settings, model_version: str | None,
                    generation: str = GENERATION, *, chunk: int = CUT_CHUNK,
                    deadline: float | None = None, reserve_s: float = 0.0,
                    stopping: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Cut the generation's calibration from the database, over what its scope snapshot holds.

    Only the cohort statistics are cut (E70's price deciles, exploded keys, token and attribute
    document frequencies, pin population and certifying codes) plus E91's pHash population; the
    band, the thresholds, `store_floor`, `catalog_df` and the model are engine settings and never
    live. The listings are read through `SqlFacts` — the pass's own facts — so the calibration
    and the pass cannot read two different corpora (the parity gate's whole question, answered
    by construction), and the population is ONE aggregate over `public.images` for the scope's
    hashes, the statement the export always ran. Written together with the calibration row, so
    the switch is atomic inside the caller's transaction. A cut is not a generation (E915):
    stored decisions keep the digest they were taken under and are re-decided only when a feed
    re-claims their listing. Under a `deadline` the reads stop `reserve_s` before it, so the
    build and the writes keep that much of it (E944)."""
    clock = time.perf_counter()
    read_by = None if deadline is None else deadline - reserve_s
    # Sorted here as well as by the statement: the stream folds its slices in ascending id
    # (`CalibrationCut.fold`), the order the oracle adds them in.
    ids = sorted(int(row[0]) for row in _rows(conn, RT_CUT_SCOPE_IDS_SQL,
                                              {"generation": generation}))
    if not ids:
        raise CalibrationRefusal(
            f"the scope snapshot of generation {generation!r} holds no listing — nothing to "
            "cut a calibration over. Refusing rather than freezing an empty one.")
    hashes: set[int] = set()
    for start in range(0, len(ids), chunk):
        if read_by is not None:
            _cut_bound(conn, read_by, stopping)
        hashes |= {int(row[0]) for row in _rows(conn, RT_CUT_HASHES_SQL,
                                                {"ids": ids[start:start + chunk]})}
    n_hashes = len(hashes)
    _cut_bound(conn, read_by, stopping)
    # The set goes before the statement runs (~84 B a hash): only its sorted list is bound.
    wanted = sorted(hashes)
    del hashes
    population = _population(conn, wanted)
    del wanted
    facts = SqlFacts(conn, population=population, stopping=stopping)
    # E962: each chunk's facts and fingerprints are folded into the cut and dropped before the
    # next chunk is read, so the cut holds counts, never the scope (`calibration_cut`).
    cut = CalibrationCut(settings)
    for start in range(0, len(ids), chunk):
        if read_by is not None:
            _cut_bound(conn, read_by, stopping)
        # Fingerprints read no CLIP vector, so the cut skips the read that would move ~2 kB per
        # photograph for nothing.
        read = facts.facts(ids[start:start + chunk], clip=False)
        cut.fold({listing_id: build_fingerprint(listing, images, settings)
                  for listing_id, (listing, images) in read.items()},
                 {listing_id: listing for listing_id, (listing, _images) in read.items()})
        del read
    calibration = cut.build(generation)
    del cut
    if deadline is not None:
        _cut_bound(conn, deadline, stopping)
    order = sorted(population)
    for start in range(0, len(order), POP_CHUNK):
        _exec_many(conn, RT_PHASH_POP_WRITE_SQL, [
            {"phash": phash, "n_listings": population[phash]}
            for phash in order[start:start + POP_CHUNK]])
    # E963: zlib over the canonical JSON in a bytea, so no jsonb ceiling (256 MiB) binds a cut.
    _exec(conn, RT_CALIBRATION_WRITE_SQL, {
        "generation": generation, "digest": calibration.digest(),
        "n_listings": calibration.n_listings, "payload_zlib": encode_payload(calibration),
        "settings": json.dumps(settings.to_dict(), sort_keys=True),
        "model_version": model_version})
    return {"digest": calibration.digest(), "n_listings": calibration.n_listings,
            "scope_listings": len(ids), "hashes": n_hashes,
            "hashes_measured": len(population), "statements": facts.statements + 4,
            "seconds": round(time.perf_counter() - clock, 3)}


def _measure_rate(claimed: int, elapsed_s: float, previous: float,
                  bound: Mapping[str, Any]) -> tuple[float | None, bool]:
    """The rate a pass stores (E98's blend, E951's doubling) and whether the doubling won."""
    # A claim the clock cut is a measurement at any size: without that, a rate halved to a
    # one-listing claim would never be measured again.
    limit = int(bound.get("limit") or 0)
    saturated = bound.get("bound_by") == "time" and limit > 0 and claimed >= limit
    if elapsed_s <= 0 or (claimed < PASS_RATE_MIN_CLAIM and not saturated):
        return None, False
    start = max(1e-6, previous)
    blended = PASS_RATE_ALPHA * (claimed / float(elapsed_s)) + (1.0 - PASS_RATE_ALPHA) * start
    if saturated and elapsed_s < RAMP_HEADROOM * PASS_DEADLINE_S and 2.0 * start > blended:
        return round(2.0 * start, 6), True
    return round(blended, 6), False


def _engine_since(conn: Any, generation: str, recorded: Any, digest: str) -> str:
    """Since when the lane has run under `digest` (E960): the recorded instant while the digest
    is the recorded one, else the database's now, written in a commit of its own (the clock
    `rt_fp.updated_at` is stamped by). A deploy overlapping a pass of the previous engine can
    leave this a pass early or late; the next pass's digest check settles it."""
    if isinstance(recorded, Mapping) and recorded.get("digest") == digest and recorded.get(
            "since"):
        return str(recorded["since"])
    return str(_rows(conn, RT_ENGINE_WRITE_SQL, {
        "key": engine_key(generation), "digest": digest,
        "updated_by": f"{LANE_NAME}:engine"})[0][0])


def _stale_budget(conn: Any, generation: str, record: Mapping[str, Any], why: str) -> None:
    _exec(conn, RT_SETTING_WRITE_SQL, {
        "key": reconcile.budget_key(generation), "value": json.dumps(dict(record)),
        "updated_by": f"{LANE_NAME}:{why}"})


def _stale_claims(conn: Any, generation: str, control: Mapping[str, Any],
                  handed: Sequence[Sequence[int]], bootstrap: bool, store: "SqlStore",
                  engine_since: str) -> tuple[list[int], int, int]:
    """What this pass re-claims for the groups the reconcile handed on (E960), chosen BEFORE the
    pass and stamped in a commit of its own (`rt_redecide_tried`), so a re-claim the pair budget
    refuses or the deadline cuts is not tried again for `TRIED_BACKOFF_HOURS` and never holds
    the head of the queue. None while no reconcile follows the pass (the build, a seed of
    another version, a closed apply scope) or while the reject ledger holds no row under this
    store's key (after a deploy that moved it, every neighbour's reject would be re-scored).
    The count is bounded like the rate (`rt_redecide_budget`): written in flight ahead of the
    pass, a count a pass finds still in flight means the last pass that re-claimed died, so this
    one offers none and halves the budget. Returns the adverts, how many of the queue's adverts
    are stale, and the budget."""
    record = control.get(reconcile.budget_key(generation))
    record = dict(record) if isinstance(record, Mapping) else {}
    budget = max(1, int(record.get("budget") or reconcile.STALE_FLOOR))
    if record.get("inflight"):
        budget = max(1, int(record["inflight"]) // 2)
        _stale_budget(conn, generation, {"budget": budget}, "redecide_halved")
        return [], 0, budget
    if not handed or bootstrap or not seed_current(control, generation):
        return [], 0, budget
    if reconcile.read_scope(conn)[0] is None or not store.ledger_keyed():
        return [], 0, budget
    ages = reconcile.refreshed(conn, generation, [i for group in handed for i in group],
                               fresh_hours=reconcile.FRESH_DECISION_HOURS,
                               engine_since=engine_since)
    tried = control.get(reconcile.tried_key(generation))
    taken, stamps = reconcile.select_claims(handed, ages,
                                            tried if isinstance(tried, Mapping) else {},
                                            datetime.now(timezone.utc), budget)
    _exec(conn, RT_SETTING_WRITE_SQL, {
        "key": reconcile.tried_key(generation), "value": json.dumps(stamps),
        "updated_by": f"{LANE_NAME}:redecide"})
    if taken:
        _stale_budget(conn, generation, {"budget": budget, "inflight": len(taken)},
                      "redecide_ahead")
    return taken, sum(1 for _at, stale in ages.values() if stale), budget


def _write_rate(conn: Any, generation: str, rate: float, why: str) -> None:
    _exec(conn, RT_SETTING_WRITE_SQL, {
        "key": pass_rate_key(generation), "value": json.dumps(round(max(1e-6, rate), 6)),
        "updated_by": f"{LANE_NAME}:{why}"})


def _write_claim_cap(conn: Any, generation: str, record: Mapping[str, Any], why: str) -> None:
    _exec(conn, RT_SETTING_WRITE_SQL, {
        "key": claim_cap_key(generation), "value": json.dumps(dict(record)),
        "updated_by": f"{LANE_NAME}:{why}"})


def _fit_claim_cap(conn: Any, generation: str, stored: Any, limit_mb: float | None
                   ) -> tuple[dict[str, Any], bool]:
    """The cap row as stored, fitted to this container's memory limit in a commit of its own,
    and whether that limit grew by `CLAIM_CAP_LIMIT_GROWTH` over the row's (E948b)."""
    cap = read_claim_cap(stored)
    if limit_mb is None:
        return cap, False
    stamped = cap["limit_mb"] is None
    if stamped:
        # Only E948 wrote rows without the key, and it halved only in the 8 GB container; any
        # other row without a limit (a seed's, one halved while none was read) takes this one.
        e948 = _halved(cap) and isinstance(stored, Mapping) and "limit_mb" not in stored
        cap = {**cap, "limit_mb": E948_LIMIT_MB if e948 else limit_mb}
    if limit_mb < cap["limit_mb"] * (1.0 + CLAIM_CAP_LIMIT_GROWTH):
        if stamped:
            _write_claim_cap(conn, generation, cap, "limit_stamped")
        return cap, False
    if not _halved(cap):
        # Nothing to reset, but the row takes this limit: a death under it must halve.
        cap = {**cap, "limit_mb": limit_mb}
        _write_claim_cap(conn, generation, cap, "limit_stamped")
        return cap, True
    reset = full_claim_cap(f"memory limit grew {cap['limit_mb']:,.0f} → {limit_mb:,.0f} MB at "
                           f"{_utc_second(time.time())}", limit_mb)
    _write_claim_cap(conn, generation, reset, "limit_grew")
    LOG.warning("AUTODEDUP: %s; the claim cap reset from %s to %s (E948b)",
                reset["reason"], cap["cap"], reset["cap"])
    return reset, True


def _release_predecessor(conn: Any, generation: str, cap: dict[str, Any], booted_epoch: int,
                         limit_mb: float | None, *, halve: bool = True,
                         re_cutting: str | None = None
                         ) -> tuple[str | None, str | None, dict[str, Any]]:
    """The live lease a dead predecessor of this process left, released with the cap halved in
    ONE commit of their own and logged at once, before a pass that may die the same way: the
    released holder, None, the halved cap. At the cap's floor it is only named and left to its
    TTL (`CLAIM_CAP_FLOOR`): None, that holder, the cap as it was. Without `halve` (the limit
    grew since the predecessor ran, E948b) it is released and logged, and the cap kept; so it
    is when the released holder is `re_cutting`, the one a re-cut stamped (E944): it died of the
    cut, not of its claim."""
    host, pid = socket.gethostname(), os.getpid()
    if int(cap["cap"]) <= CLAIM_CAP_FLOOR:
        return None, rt_lease.predecessor(conn, host, pid, booted_epoch), cap
    with _transaction(conn):
        died = rt_lease.release_predecessor(conn, host, pid, booted_epoch)
        if died is None:
            return None, None, cap
        before = cap
        halve = halve and died != re_cutting
        if halve:
            cap = halve_claim_cap(before, died, booted_epoch, limit_mb)
            _write_claim_cap(conn, generation, cap, "halved")
    LOG.warning("AUTODEDUP: a predecessor of this container died holding autodedup.rt_lease: "
                "%s; released, and the claim cap %s", died,
                f"halved from {before['cap']} to {cap['cap']} (E948)" if halve else
                f"kept at {cap['cap']}: it died inside a re-cut (E944)" if died == re_cutting else
                f"kept at {cap['cap']}: it ran under a smaller memory limit (E948b)")
    return died, None, cap


def pass_holder() -> str:
    """The name a pass takes the lease under: host, process, second. The worker makes it
    before the pass so its SIGTERM path can release exactly that row (E941), and the next
    process of the same container reads all three back (E948)."""
    return f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"


def _pass_clock(stopping: Callable[[], bool]) -> Callable[[], float]:
    """The reconcile's clock (E941): the real one, and +inf once the worker is shutting down,
    so the reconcile stops before its next group as it does when the pass's time is spent."""
    return lambda: math.inf if stopping() else time.perf_counter()


def peak_rss_mb() -> float | None:
    """The process's peak resident memory in MiB, read at the end of a pass (E941):
    `getrusage(RUSAGE_SELF).ru_maxrss`, which Linux reports in KiB (macOS in bytes). It is a
    high-water mark of the WHOLE process since it started — on the worker, every lane's — so
    a pass that grew it shows, and one that did not repeats the last reading. None where the
    platform has no getrusage."""
    if resource is None:
        return None
    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return round(peak / (1_048_576.0 if sys.platform == "darwin" else 1024.0), 1)


def memo_entries() -> dict[str, int]:
    """The entries the engine's process-level memos of what it read hold now (E949): the readers
    cached on a body, by module, E282's shingle sets, and the token hashes and SimHash lanes."""
    return {**body_cache_entries(), "shingles": len(indistinguishable._SHINGLE_MEMO),
            "tokens": len(normalize._token_hashes) + len(normalize._token_lanes)}


def forget_bodies() -> None:
    """Empty every memo `memo_entries` counts (E949), so no reading of a body or a token is
    carried into the next pass. Each is a pure function of its arguments: emptied, a reading
    is recomputed to the same value, so no decision changes."""
    clear_body_caches()
    indistinguishable._SHINGLE_MEMO.clear()
    normalize._token_hashes.clear()
    normalize._token_lanes.clear()


def pass_memory() -> dict[str, Any]:
    """The process's memory at a pass's end: its high-water mark, what it holds now, and what
    the body memos hold just before the pass's end empties them (E949)."""
    return {"peak_rss_mb": peak_rss_mb(), "rss_mb": rss_mb(), "memo_entries": memo_entries()}


def _put_back_rate(conn: Any, generation: str, rate_per_s: float,
                   original: BaseException) -> None:
    """The rate a refused pass halved ahead (E941), back as it was: a refusal is not the
    claim's size. Best effort — a failure is noted on `original`, never raised in its place."""
    try:
        _write_rate(conn, generation, rate_per_s, "restored")
    except Exception as exc:  # noqa: BLE001 — the refusal is the signal
        original.add_note(f"restoring {pass_rate_key(generation)} after the refusal failed "
                          f"({type(exc).__name__}: {exc}); the next pass claims half")


def _after_raise(fresh_conn: Callable[[], Any] | None, conn: Any, generation: str,
                 rate_per_s: float, holder: str, original: BaseException, *,
                 halve: bool = True) -> None:
    """What a pass that RAISED still owes, on ONE connection: E913's halving (E930) and its
    lease's release (E931). Through `fresh_conn` when the caller gave one — the pass's own
    connection may be the one the server terminated, and a lease that connection cannot
    release sits until its TTL, skipping every pass of the next ~35 min as "leased" — else on
    `conn`. The release tries the pass's own connection first, the fresh one only when that
    fails and new ones from `fresh_conn` only when a database restart took both, so a live pass
    releases the way every other end of a run does. Best effort:
    a failure is noted on `original` rather than replacing it — the raise is the signal.
    `halve=False` once the worker is shutting down (E941): the release still runs, and the
    rate row is left as it stands, the half written ahead of the pass."""
    fresh: Any = None
    try:
        try:
            if fresh_conn is not None:
                fresh = fresh_conn()
            if halve:
                _write_rate(fresh if fresh is not None else conn, generation,
                            rate_per_s / 2.0, "halved")
        except Exception as exc:  # noqa: BLE001 — best effort
            original.add_note(f"halving {pass_rate_key(generation)} after the raise also "
                              f"failed ({type(exc).__name__}: {exc}); the next pass claims "
                              "the half written ahead of it")
        rt_lease.release_after(conn, holder, original, fallback=fresh, connect=fresh_conn)
    finally:
        close = getattr(fresh, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


def run_incremental(conn_factory: Callable[[], Any], *,
                    deadline_s: float | None = None,
                    fresh_conn: Callable[[], Any] | None = None,
                    holder: str | None = None,
                    stopping: Callable[[], bool] | None = None,
                    booted_epoch: int | None = None,
                    memory_limit_mb: float | None = None) -> dict[str, Any]:
    """One bounded pass of THE lane, then its reconcile, under one lease (E914, A9).

    The worker's `autodedup` lane is the only caller and runs it only while its interval is
    above 0. The pass decides, groups and commits; then, outside the bootstrap phase and inside
    what is left of its time, the reconcile turns the groups it re-clustered into production
    merges; then, when the pHash population has drifted below `COVERAGE_FLOOR`, the calibration
    is due: the next tick re-cuts it instead of passing, under the same lease and a deadline of
    its own (`run_recut`, E944), and a pass reads its payload once a digest (`read_calibration`).
    The rate is halved BEFORE the pass (E941), in a write of its own that commits
    before the pass's transaction opens, so a pass the system kills — a deploy, a restart, the
    kernel's OOM killer — has already slowed the next claim; a pass that committed puts the
    previous rate back before its reconcile and overwrites it with its measurement after it
    (doubled when it had room, E951; an idle pass, too small to measure, leaves the previous
    rate), and a refused one puts the previous rate back too. A pass past its deadline rolls
    back and keeps the halving (E913); so does a pass that RAISED, which writes it again through
    `fresh_conn` — a factory for a NEW connection, since its own may be dead — or on its own
    connection when the caller gives none (E930). Every release of the pass's lease falls back
    to new connections from it too, when a database restart took the pass's own.

    `holder` names the lease the pass takes (`pass_holder()` when the caller names none) and
    `stopping` says the caller is shutting down (E941): the worker's SIGTERM path releases
    THAT holder's lease on a fresh connection at once (after cancelling a statement the pass is
    running), so a pass that sees `stopping` takes no lease, gives back one it just took, stops
    at its next deadline checkpoint (`PassStopped`, rolled back on its own connection) and
    returns at once so the `finally` releases its lease, skips the reconcile (or stops it
    before its next group) and the re-cut (or stops it at its next checkpoint), and writes no
    rate or watermark row — nor after a reconcile or re-cut its fence stopped. Every
    transaction the lane writes in — the pass's, each reconcile group's, the re-cut's — ends
    with `rt_lease.hold`: one whose lease was released under it rolls back instead of
    committing beside the next holder. Not fenced: the reconcile's planning writes (its sweep
    cursor, its skipped-group rows), which a signal landing between the pass's commit and the
    first group lets through.

    `booted_epoch` is the worker process's boot second (E948): right before the take, the live
    lease a dead predecessor of this process left (this hostname and pid, an earlier second) is
    released and the claim cap halved, in one commit of their own, and logged; at the cap's
    floor that lease is only named (`predecessor_kept`) and left to its TTL. The limit every
    feed's share is cut from is at most the cap, which only a seed or a bigger container
    raises again (E948b): `memory_limit_mb` is the container's memory limit (None, or 0 and
    below: unknown), and right before that release (and the take), in a commit of its own, a
    cap row whose recorded limit it exceeds by `CLAIM_CAP_LIMIT_GROWTH` is reset to
    `max_listings` and logged (a cap already there only takes the new limit), and the release
    then halves nothing: the predecessor fitted the row before its own take, so it ran under
    the smaller limit. A row that records none is stamped with the limit this pass reads, or
    measured from `E948_LIMIT_MB` if E948 halved it (no `limit_mb` key at all); a halving
    records the limit as `halve_claim_cap` says.

    Whatever ends it, the pass empties the process-level memos of what it read (E949,
    `forget_bodies`), after its lease is released; a summary reads them first (`memo_entries`).
    A raised pass also lets go of the raise it noted (`original`) before it leaves: the raise's
    traceback holds this frame, so keeping it made a cycle, and every local of the pass stayed
    alive after the lane loop dropped the raise, until the next full collection."""
    deadline_s = float(PASS_DEADLINE_S if deadline_s is None else deadline_s)
    started = time.perf_counter()
    deadline = started + deadline_s
    generation = GENERATION
    holder = holder or pass_holder()
    shutting_down: Callable[[], bool] = stopping or (lambda: False)
    conn = conn_factory()
    leased = False
    predecessor: str | None = None
    kept: str | None = None
    original: BaseException | None = None
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539, 596, 604 or "
                             "616 not applied")
        if not _rows(conn, RT_CALIBRATION_PRESENT_SQL, {"generation": generation}):
            return {"skipped": "unseeded",
                    "reason": (f"generation {generation!r} has no calibration — seed it with "
                               "`gh workflow run autodedup.yml -f mode=rt_seed` first"),
                    "generation": generation, "spent_usd": 0.0}
        control = lane_settings(conn, [recut_key(generation),
                                       scope_setting_key(generation),
                                       bootstrap_setting_key(generation),
                                       pass_rate_key(generation),
                                       seed_version_key(generation),
                                       claim_cap_key(generation),
                                       engine_key(generation),
                                       reconcile.redecide_key(generation),
                                       reconcile.tried_key(generation),
                                       reconcile.budget_key(generation)])
        try:
            scope = pass_scope(read_scope_setting(control, generation))
            parents = resolve_scope_parents(conn, scope)
            districts = resolve_scope_districts(conn, scope, parents)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        bootstrap = setting_flag(control.get(bootstrap_setting_key(generation)))
        rate_per_s = max(1e-6, _number(control.get(pass_rate_key(generation)),
                                       PASS_RATE_PER_S))
        try:
            storage = storage_guard(conn, generation, scope)
        except StorageRefusal as exc:
            raise SystemExit(str(exc)) from exc
        if shutting_down():
            return {"skipped": STOPPING, "reason": "the worker is shutting down",
                    "spent_usd": 0.0}
        # E948b, before the release: a limit grown since the row was last fitted proves the
        # dead predecessor ran under the smaller one, so the release then halves nothing.
        limit_mb = _known_limit_mb(memory_limit_mb)
        cap, grew = _fit_claim_cap(conn, generation, control.get(claim_cap_key(generation)),
                                   limit_mb)
        if booted_epoch is not None:
            # E944: a holder that died inside a re-cut died of the cut, not of its claim.
            stamped = control.get(recut_key(generation))
            predecessor, kept, cap = _release_predecessor(
                conn, generation, cap, booted_epoch, limit_mb, halve=not grew,
                re_cutting=(stamped.get("cutting") if isinstance(stamped, Mapping) else None))
        if not take_lease(conn, holder):
            held_by = ("another writer holds autodedup.rt_lease" if kept is None else
                       "a dead predecessor of this container holds autodedup.rt_lease, left to "
                       f"its TTL at the claim cap's floor ({CLAIM_CAP_FLOOR}; E948)")
            return {"skipped": "leased", "reason": f"{held_by}: {rt_lease.describe(conn)}",
                    "predecessor_released": predecessor, "predecessor_kept": kept,
                    "spent_usd": 0.0}
        leased = True
        if shutting_down():
            # The shutdown began while the lease was being taken: its release may have run
            # before the take, so nothing else would end this one before the process does.
            release_lease(conn, holder)
            leased = False
            return {"skipped": STOPPING, "reason": "the worker is shutting down",
                    "predecessor_released": predecessor, "spent_usd": 0.0}
        row, calibration = read_calibration(conn, generation)
        settings, model = pass_config(row[4], row[5], generation)
        # E944: read again under the lease — a re-cut stopped while this tick waited for it has
        # put its row back since `control` was read.
        recut = lane_settings(conn, [recut_key(generation)]).get(recut_key(generation))
        cutting = isinstance(recut, Mapping) and bool(recut.get("cutting"))
        recut = recut_died(conn, generation, holder, recut, row[6])
        died = recut["last"] if cutting else None
        # The scope as it now stands: the last cut's count, or the store's fingerprinted adverts
        # when the scope has grown since.
        scope_listings = max(int(row[2] or 0),
                             int((storage.get("rows") or {}).get("rt_fp") or 0))
        if (recut_unfit(bootstrap, scope_listings, recut, row[6]) is None
                and recut_due(recut, row[6]) and _recut_age_ok(recut, row[6])):
            # E944: the tick a pass found the population drifted re-cuts instead of passing.
            return {"skipped": "recut",
                    "reason": "the pHash population fell below the floor: the calibration is "
                              "re-cut in this tick, the next one passes",
                    "recut": run_recut(conn, settings, model.version, generation, holder=holder,
                                       before=str(row[1]), recut=recut, deadline=deadline,
                                       stopping=stopping),
                    "calibration": {"built_age_days": age_days(row[6])},
                    "generation": generation, "predecessor_released": predecessor,
                    "claim_cap": cap, "spent_usd": 0.0, **pass_memory()}
        store = SqlStore(conn, generation, calibration_digest=calibration.digest(),
                         engine_digest=engine_digest(settings, model),
                         store_floor=settings.store_floor, model_version=model.version)
        facts = SqlFacts(conn, deadline=deadline, stopping=stopping)
        engine_since = _engine_since(conn, generation, control.get(engine_key(generation)),
                                     store.engine_digest)
        handed = reconcile.handed_groups(control.get(reconcile.redecide_key(generation)))
        stale_claims, stale_waiting, stale_budget = _stale_claims(
            conn, generation, control, handed, bootstrap, store, engine_since)
        work = SqlWork(conn, scope, generation, parents=parents, districts=districts,
                       bootstrap=bootstrap,
                       pass_budget_s=(BOOTSTRAP_BUDGET_FRACTION * PASS_DEADLINE_S if bootstrap
                                      else PASS_BUDGET_S),
                       rate_per_s=rate_per_s, claim_cap=int(cap["cap"]),
                       stale_claims=stale_claims, stale_waiting=stale_waiting,
                       stale_budget=stale_budget)
        result = None
        stopped = ""
        # E941: halved AHEAD, outside the transaction (`db.connect` is autocommit), so it stands
        # whatever ends this pass. E930 halved only on a raise the pass lived to see; a pass the
        # system killed (a deploy, a restart, OOM) wrote nothing, and once its lease expired the
        # next pass claimed the identical batch at the identical size.
        _write_rate(conn, generation, rate_per_s / 2.0, "halved_ahead")
        try:
            with _transaction(conn):
                # The three bounds are the transaction's first statements and LOCAL to it
                # (W9d-4): over the transaction-mode pooler a session-level guard may belong to
                # a backend this transaction never runs on.
                _exec(conn, RT_STATEMENT_GUARD_SQL,
                      {"statement_timeout_ms": STATEMENT_TIMEOUT_MS})
                _exec(conn, RT_LOCK_GUARD_SQL, {"lock_timeout_ms": LOCK_TIMEOUT_MS})
                _exec(conn, RT_IDLE_GUARD_SQL, {"idle_timeout_ms": IDLE_TIMEOUT_MS})
                pass_now = time.time()
                result = run_pass_bounded(
                    store, facts, work, settings, model, calibration, limits=PASS_LIMITS,
                    generation=generation, now=pass_now,
                    hold=EvidenceHold(pass_now, EVIDENCE_HORIZON_HOURS * 3600.0),
                    deadline=deadline, stopping=stopping)
                if result.aborted:
                    # Nothing this pass wrote survives a refusal, and no cursor moved.
                    raise _Refused()
                # The phase ends by ITSELF, in the transaction that empties the backlog.
                if bootstrap and work.bootstrap_done:
                    _exec(conn, RT_SETTING_WRITE_SQL, {
                        "key": bootstrap_setting_key(generation), "value": json.dumps(False),
                        "updated_by": f"{LANE_NAME}:bootstrap_done"})
                # E941, the fence: the last statement before the commit. A lease released
                # under the pass (the worker's shutdown) ends it here, rolled back.
                rt_lease.hold(conn, holder)
        except rt_lease.LeaseLost as exc:
            # The transaction rolled back: nothing written, no cursor moved. Someone else may
            # hold the lease by now, so the pass writes nothing more — no rate (the half
            # written ahead stands), no watermark, no release of a row that is not its own.
            leased = False
            return {"counts": {}, "aborted": LEASE_LOST, "reason": str(exc)[:300],
                    "reconcile": {"skipped": f"pass_{LEASE_LOST}"}, "generation": generation,
                    "predecessor_released": predecessor, "claim_cap": cap,
                    "spent_usd": 0.0, **pass_memory()}
        except _Refused:
            stopped = result.aborted if result is not None else "refused"
        except PassStopped:
            # E941: the worker is shutting down — the pass stopped at a checkpoint and rolled
            # back on its own live connection, so its row locks are free now, not at SIGKILL.
            stopped = STOPPING
        except PassDeadline:
            stopped = "deadline"
        except RetireRefusal as exc:
            # The transaction rolled back on the way out: nothing written, no cursor moved. A
            # refusal never halves (E930): it repeats until the operator acts, and halving it
            # every tick would wedge the claim at one advert — so the rate halved ahead goes back.
            if not shutting_down():
                _put_back_rate(conn, generation, rate_per_s, exc)
            raise SystemExit(str(exc)) from exc
        except Exception as exc:
            # E930: the pass RAISED (a backend the server terminated, a cancelled statement, a
            # Python error). It rolled back and moved no cursor, so the next pass would claim
            # the IDENTICAL batch at the identical size and fail the same way for ever
            # (2026-10-02: six passes, the same 500 adverts). Halve the rate as E913 does —
            # E941 already wrote the same half ahead of the pass; this is that write again, on
            # a live connection — and release the lease there too (E931): on a dead connection
            # the `finally` below could not, and the lease then sat until its TTL (2,400 s), so
            # the next passes were skipped "leased" for up to ~35 min. Settled here on both
            # counts — attempted on the pass's connection, then the fresh one, then new ones,
            # and noted on the raise when none could — so the `finally` releases nothing twice.
            _after_raise(fresh_conn, conn, generation, rate_per_s, holder, exc,
                         halve=not shutting_down())
            leased = False
            raise
        if stopped == STOPPING:
            # E941: rolled back for the shutdown — return at once, so the `finally` releases
            # the lease on the pass's own connection with no reads in between.
            return {"counts": {}, "aborted": STOPPING,
                    "reconcile": {"skipped": f"pass_{STOPPING}"}, "generation": generation,
                    "predecessor_released": predecessor, "claim_cap": cap,
                    "spent_usd": 0.0, **pass_memory()}
        if stopped and not shutting_down():
            # E913: the next pass claims half as much (the half E941 wrote ahead, named).
            _write_rate(conn, generation, rate_per_s / 2.0, "halved")
        if not stopped and result is not None and not shutting_down():
            # E941: the pass committed, so its claim did not kill it — the half written ahead
            # goes back NOW, before the reconcile, whose planning runs outside the pass's
            # statement guards: a raise there would otherwise leave the half standing, and
            # eight such passes take the claim to one advert. The measurement below, once the
            # reconcile has run, still overwrites it.
            _write_rate(conn, generation, rate_per_s, "restored")
        if stale_claims and not shutting_down() and (
                stopped == "pair_budget" or (not stopped and result is not None)):
            # E960: the pass carrying the re-claims did not die. Committed WITH them (its first
            # attempt), the budget is whole again; refused or retried without them, it stands.
            committed = not stopped and result is not None and result.attempts == 1
            _stale_budget(conn, generation,
                          {"budget": reconcile.STALE_FLOOR if committed else stale_budget},
                          "redecide_committed" if committed else "redecide_refused")
        summary: dict[str, Any] = (result.to_json() if result is not None
                                   else {"counts": {}, "aborted": stopped})
        summary["aborted"] = stopped
        # A9: the reconcile, after the pass committed and under the same lease. Off during
        # the build (a half-built store is missing groups), off after a pass that rolled back,
        # and off over a generation no seed of this design built (the 09-21 `rt` predates F2:
        # its groups are not the ones this build would draw).
        if stopped:
            summary["reconcile"] = {"skipped": f"pass_{stopped}"}
        elif shutting_down():
            # E941: the shutdown may have released the lease already; merges wait for the
            # next worker's pass, which re-plans them.
            summary["reconcile"] = {"skipped": STOPPING}
        elif not seed_current(control, generation):
            summary["reconcile"] = {
                "skipped": SEED_MISMATCH,
                "reason": (f"autodedup.settings {seed_version_key(generation)} is "
                           f"{control.get(seed_version_key(generation))!r}, not "
                           f"{SEED_VERSION!r}: re-seed with `mode=rt_seed "
                           "args=settings=w29,model=w6_gold,fresh=true` before it merges")}
        elif bootstrap:
            summary["reconcile"] = {"skipped": "bootstrap"}
        else:
            reconcile_started = time.perf_counter()
            summary["reconcile"] = reconcile.run(
                conn, generation, result.cluster_keys if result is not None else [],
                run_id=f"{reconcile.RUN_PREFIX}{holder}", deadline=deadline,
                blocks=[block.key for block in work.enter_blocks],
                clock=_pass_clock(shutting_down),
                # E941: every group's transaction ends with the pass's lease check, so a lease
                # released after the pass committed stops the merges at the next group.
                fence=lambda group_conn: rt_lease.hold(group_conn, holder),
                engine_since=engine_since, waiting=handed)
            # What the reconcile cost inside the pass, so G2's rate is read with it in (B3).
            summary["reconcile"]["seconds"] = round(time.perf_counter() - reconcile_started, 3)
        elapsed = time.perf_counter() - started
        # E941: a reconcile whose fence found the lease gone means the lane is someone else's
        # now — the pass writes nothing more, as after the shutdown signal.
        lost = (summary.get("reconcile") or {}).get("stopped") == LEASE_LOST

        def owns_the_lane() -> bool:
            return not lost and not shutting_down()

        doubled = False
        if not stopped and result is not None and owns_the_lane():
            # The pass's measurement, reconcile included, blended with the rate it started
            # from (or doubled, E951), replaces the rate put back above; a pass that claimed
            # too little to measure itself (an idle one) leaves that rate. Not once the lane is
            # not its own: the next holder's rows are not its to write.
            measured, doubled = _measure_rate(len(result.claimed), elapsed, rate_per_s,
                                              work.claim_bound)
            if measured is not None:
                _write_rate(conn, generation, measured, "rate")
        coverage = (None if not facts.images_with_phash else round(
            1.0 - facts.images_unmeasured / float(facts.images_with_phash), 6))
        summary["population"] = {
            "phash_pop_rows": phash_pop_rows(conn),
            "images_with_phash": facts.images_with_phash,
            "images_unmeasured": facts.images_unmeasured,
            "hashes_unmeasured": len(facts.hashes_unmeasured),
            "coverage": coverage,
            "floor": COVERAGE_FLOOR,
        }
        if died is not None:
            summary["recut"] = died
        # A10: the population has drifted — the next tick re-cuts the calibration (E944), so
        # no pass spends its own time on a cut that grows with the scope; in a build, or over a
        # scope no tick could cut, the row says why instead and the drift is measured again in
        # RECUT_MIN_AGE_H.
        if (not stopped and owns_the_lane() and coverage is not None
                and coverage < COVERAGE_FLOOR
                and facts.images_with_phash >= RECUT_MIN_IMAGES
                and not recut_due(recut, row[6]) and _recut_age_ok(recut, row[6])):
            unfit = recut_unfit(bootstrap, scope_listings, recut, row[6])
            value = {**(recut if isinstance(recut, Mapping) else {}),
                     "due": None if unfit else _iso(row[6])}
            if unfit:
                at = datetime.now(timezone.utc).isoformat()
                summary["recut"] = value["last"] = {
                    "skipped": unfit, "scope_listings": scope_listings, "at": at}
                value["tried_at"] = at
            with suppress(rt_lease.LeaseLost):
                _write_recut(conn, generation, holder, value,
                             "recut_unfit" if unfit else "recut_due")
            summary["population"]["recut_due"] = unfit is None
        summary["fact_reads"] = facts.reads
        summary["statements"] = store.statements + facts.statements + work.statements
        summary["settings"] = settings.to_dict()
        summary["model_version"] = model.version
        summary["store_rows"] = int(
            _rows(conn, RT_FP_COUNT_SQL, {"generation": generation})[0][0])
        summary["scope"] = scope.as_json()
        summary["windows"] = dict(work.windows)
        summary["enter_scan"] = dict(work.enter_scan)
        # E952: the pair budget beside the clock's and the cap's: a refusal (`aborted`
        # "pair_budget", `counts.attempts`) is read against it.
        summary["claim_bound"] = {**work.claim_bound, "max_pairs": PASS_LIMITS.max_pairs}
        summary["rate_doubled"] = doubled
        summary["bootstrap"] = {
            "active": bool(bootstrap),
            "backlog": work.bootstrap_backlog,
            "ended_this_pass": bool(bootstrap and work.bootstrap_done and not stopped),
        }
        summary["evidence"] = {
            **dict(work.evidence),
            "held_this_pass": result.held if result is not None else 0,
            "released_this_pass": result.released if result is not None else 0,
            "redecided": result.redecided if result is not None else 0,
        }
        summary["calibration"] = {"digest": calibration.digest(), "built_at": _iso(row[6]),
                                  "built_age_days": age_days(row[6])}
        # E943: the reject ledger's engine key; a pass after it moved decides every reject anew.
        summary["engine_digest"] = store.engine_digest
        summary["retention"] = {"store_floor": settings.store_floor,
                                "pairs_retained": store.pairs_retained,
                                "pairs_evicted": store.pairs_evicted}
        summary["deadline_s"] = float(deadline_s)
        summary["elapsed_s"] = round(elapsed, 3)
        after_bytes = schema_bytes(conn)
        summary["storage"] = {
            **storage,
            "schema_mb_after": round(after_bytes / 1_048_576.0, 2),
            "pass_growth_mb": round(
                (after_bytes / 1_048_576.0) - float(storage["schema_mb"]), 3),
        }
        if owns_the_lane():
            record_storage(conn, generation, after_bytes)
        summary["predecessor_released"] = predecessor
        summary["claim_cap"] = cap
        summary.update(pass_memory())
        return summary
    except BaseException as exc:
        original = exc
        raise
    finally:
        try:
            if leased:
                rt_lease.release_after(conn, holder, original, connect=fresh_conn)
        finally:
            # E949: the raise's traceback holds this frame, so `original` made a cycle; and
            # the memos go after the lease, which the next holder may be waiting on.
            original = None
            forget_bodies()
            close = getattr(conn, "close", None)
            if callable(close):
                close()


RT_SEED_ARGS: frozenset[str] = frozenset({"settings", "model", SCOPE_SETTING, "fresh",
                                          rt_lease.RELEASE_ARG})


def run_rt_seed(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
) -> dict[str, Any]:
    """`--mode rt_seed`: (re)build THE generation from the database (A10, E912).

    It names the scorer (`settings`, `model`) and the scope (`rt_scope`, else the row the
    previous seed wrote, else the trial blocks), walks every scope block to its end (E940),
    cuts the calibration and the pHash population from the database, starts the cursors at
    TODAY and opens the build phase — every in-scope listing then arrives through the passes, the
    one path the replay proof covers (E98). A generation already seeded is rebuilt only with
    `fresh=true`, which first empties it (E97) inside the same transaction; production merges
    are never touched. It takes the lane's lease and refuses while a pass holds it — stop the
    worker's lane first (interval 0).

    THE ORDER (E916), every step before the transaction read-only:
      1. refuse an unknown arg, an absent store, a seeded generation without `fresh=true`,
         and a scope that does not resolve;
      2. the storage guard. With `fresh=true` it reads the schema size AND what this
         generation's rows hold of every table step 5 empties, and refuses only when the
         PROJECTED size (schema minus those rows) is over MAX_SCHEMA_MB — re-measuring after
         the DELETE would not do, because a DELETE does not shrink `pg_total_relation_size`
         (the space is reused by the rebuild once VACUUM frees it; the file keeps its size).
         Without `fresh=true` it checks the schema as it stands, exactly as a pass does;
      3. release a named stale lease, if asked;
      4. take the lease (refuse while a pass holds it);
      5. ONE transaction: the reset (fresh only), the block walk, the calibration cut, the
         SECOND storage check (E941: the schema as it now stands, less what a fresh reset
         freed, plus `BUILD_BYTES_PER_LISTING` for every listing in scope — what the build
         writes), the cursors and the control rows. A refusal inside it rolls every row back,
         the reset's included."""
    unknown = sorted(set(args) - RT_SEED_ARGS)
    if unknown:
        raise SystemExit(f"unknown arg(s) {', '.join(unknown)}; allowed: "
                         f"{', '.join(sorted(RT_SEED_ARGS))}")
    settings, model, settings_name, model_name = named_config(args, what="rt_seed")
    fresh = str(args.get("fresh") or "").strip().lower() == "true"
    generation = GENERATION
    holder = f"rt_seed:{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
    conn = conn_factory()
    leased = False
    original: BaseException | None = None
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539, 596, 604 or "
                             "616 not applied")
        existing = _rows(conn, RT_CALIBRATION_PRESENT_SQL, {"generation": generation})
        if existing and not fresh:
            raise SystemExit(
                f"generation {generation!r} is already seeded (calibration "
                f"{str(existing[0][1])!r}, cut {existing[0][2]}) — the calibration re-cuts "
                "itself when the pHash population drifts; a new scorer or scope is a REBUILD: "
                "pass fresh=true. Nothing was written.")
        control = lane_settings(conn, [scope_setting_key(generation)])
        try:
            scope = resolve_scope(args.get(SCOPE_SETTING),
                                  read_scope_setting(control, generation))
            # Proved at SEED time: a quarter or an okres the register cannot place is a scope
            # whose entrant sweep could never walk it (W9d-3, E961).
            parents = resolve_scope_parents(conn, scope)
            districts = resolve_scope_districts(conn, scope, parents)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        try:
            # E916: a fresh seed is budgeted on what the schema holds WITHOUT the rows it is
            # about to delete; any other seed on the schema as it stands.
            storage = storage_guard(conn, generation, scope, replacing=fresh)
        except StorageRefusal as exc:
            raise SystemExit(str(exc)) from exc
        released = (rt_lease.release_stale(conn, str(args[rt_lease.RELEASE_ARG]).strip())
                    if str(args.get(rt_lease.RELEASE_ARG) or "").strip() else None)
        if not take_lease(conn, holder, ttl=SEED_LEASE_TTL_S):
            raise SystemExit(
                f"rt_seed refused: autodedup.rt_lease is {rt_lease.describe(conn)}. Set "
                "app_settings.realtime_autodedup_interval_seconds to 0, wait for the lease to "
                "clear, and seed again. Nothing was written.")
        leased = True
        reset: dict[str, int] = {}
        with _transaction(conn):
            if fresh:
                reset = reset_generation(conn, generation, holder=holder)
            # E940: EVERY block of the scope, each read to its end (a page a statement) and under
            # no daily cap — the cut, A10's re-cut refusal above RECUT_MAX_LISTINGS and E941's
            # second storage check are all taken over what the walk listed, so a block cut to
            # its first page, or a block the cap left for tomorrow, would bias the one and
            # under-count the other for the generation's life. The build starts listed whole.
            work = SqlWork(conn, scope, generation, parents=parents, districts=districts,
                           bootstrap=True, enter_slice=ENTER_SLICE)
            if work.enter_blocks:
                work.refresh_scope_ids(0, seed=True)
            # E940: what the walk listed decides two refusals, and both run BEFORE the cut — the
            # seed's hour (~11.5 ms an advert) — so neither spends it first. The count is the
            # cut's own: the snapshot's distinct adverts.
            scope_listings = len(_rows(conn, RT_CUT_SCOPE_IDS_SQL, {"generation": generation}))
            # E941: the first check saw the schema before this seed wrote a row. The walk has
            # now written the snapshot; the cut will write the pHash population and the build
            # BUILD_BYTES_PER_LISTING for every listing in scope: refuse here, inside the
            # transaction, so the reset and the walk roll back.
            try:
                build_storage = storage_guard(
                    conn, generation, scope, build_listings=scope_listings,
                    replaced_bytes=int(storage["replaced_bytes"]) if fresh else None)
            except StorageRefusal as exc:
                raise SystemExit(str(exc)) from exc
            memory = seed_memory(scope_listings)
            try:
                cut = cut_calibration(conn, settings, model.version, generation)
            except CalibrationRefusal as exc:
                raise SystemExit(f"{scope.label()!r}: {exc}") from exc
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
            for name in (CURSOR_REVIVE, CURSOR_SCOPE, reconcile.CURSOR):
                _exec(conn, RT_CURSOR_SET_SQL, {"name": name, "last_listing_id": 0})
            _exec(conn, RT_CURSOR_WRITE_SQL, {"name": CURSOR_ENTER, "last_listing_id": 0,
                                              "last_snapshot_id": 0, "watermark": None})
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": scope_setting_key(generation), "value": json.dumps(scope.as_json()),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": bootstrap_setting_key(generation), "value": json.dumps(True),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            # The one row that lets the reconcile merge from this generation (and the review
            # pages default to it) once the build ends: written by the seed, never by hand.
            _exec(conn, RT_SETTING_WRITE_SQL, {
                "key": seed_version_key(generation), "value": json.dumps(SEED_VERSION),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            # A new build measures its own rate from the conservative start (review B3): the
            # 09-21 row still read 0.585503, so G2 would have "passed" before any W5 pass ran.
            _write_rate(conn, generation, PASS_RATE_PER_S, "rt_seed")
            # E948: and claims the whole `max_listings` until a death of its own halves it. No
            # `limit_mb`: the seed runs outside the worker's container, whose first pass stamps
            # its limit (E948b).
            _write_claim_cap(conn, generation, full_claim_cap("rt_seed"), "rt_seed")
        summary = {
            "generation": generation,
            "calibration": cut,
            "settings_name": settings_name,
            "model_name": model_name,
            "model_version": model.version,
            "cursors": {CURSOR_NEW: int(seeded[0]), CURSOR_CHANGED: int(seeded[1]),
                        CURSOR_FLIPPED: str(seeded[2])},
            "fresh": bool(fresh),
            "reset": reset,
            "bootstrap": True,
            "seed_version": SEED_VERSION,
            "pass_rate_per_s": PASS_RATE_PER_S,
            "claim_cap": PASS_LIMITS.max_listings,
            "scope": scope.as_json(),
            "enter_scan": dict(work.enter_scan),
            "storage": storage,
            "storage_build": build_storage,
            # E940: the cut's projected peak against SEED_MEMORY_MB, and what the seed's
            # process actually peaked at — the receipt E962's bound is measured from.
            "seed_memory": memory,
            "peak_rss_mb": peak_rss_mb(),
            "lease_released": released,
            "spent_usd": 0.0,
        }
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "rt_seed.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return summary
    except BaseException as exc:
        original = exc
        raise
    finally:
        try:
            if leased:
                rt_lease.release_after(conn, holder, original, connect=conn_factory)
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
