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
from autodedup.harness import load_model, load_settings
from autodedup.hazard_context import ContextStamp, address_block_key, category_group
from autodedup.incremental import (
    GENERATION,
    Calibration,
    CellRow,
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
    RT_FP_COUNT_SQL,
    RT_FP_DELETE_SQL,
    RT_FP_READ_SQL,
    RT_FP_UPSERT_SQL,
    RT_IDLE_GUARD_SQL,
    RT_LOCK_GUARD_SQL,
    RT_PHASH_POP_SQL,
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
    RT_SCOPE_ENTER_SQL,
    RT_SCOPE_PARENT_OBEC_SQL,
    RT_SCOPE_DRIFT_SQL,
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
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL

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
ENV_FLAG: str = "AUTODEDUP_REALTIME_ENABLED"
DB_FLAG: str = "realtime_enabled"
SCOPE_SETTING: str = "rt_scope"
BUDGET_SETTING: str = "rt_max_schema_mb"
RETIRE_SETTING: str = "rt_max_retire_fraction"
RESCOPE_ARG: str = "rt_rescope"
STORAGE_WATERMARK: str = "rt_storage_last"

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
# One round-robin slice of the ENTRANT sweep (W9d-3), over ONE of the scope's blocks a pass. The
# trial scope's largest block is Praha-Vysočany at 1,439 rows, so a slice covers a block whole
# and the sweep cycles the scope every len(blocks) passes — 30 minutes at the `*/10` cadence.
ENTER_SLICE: int = 20000
# The share of the store one pass's drift sweep may retire before the lane STOPS instead (W9d-1).
# A geocode correction moves a listing or two; a scope that has gone wrong moves everything, and
# the difference between those two is the only thing standing between a hand-edited settings row
# and a store that has to be re-seeded. Data, not a constant: `rt_max_retire_fraction`.
MAX_RETIRE_FRACTION: float = 0.05
# The storage budget, in megabytes of schema `autodedup` (pg_total_relation_size, indexes and
# TOAST included). The schema is ~148 MB today and the operator pays for it; a lane that has
# not been watched for a week must not be able to double it.
MAX_SCHEMA_MB: float = 400.0
STATEMENT_TIMEOUT_MS: int = 120_000
LOCK_TIMEOUT_MS: int = 5_000
IDLE_TIMEOUT_MS: int = 300_000
_EPOCH: str = "epoch"


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
                 store_floor: float = 0.02) -> None:
        self.conn = conn
        self.generation = generation
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
            "calibration_digest": row.evidence.get("_calibration"),
            "feature_version": FEATURE_VERSION,
            "model_version": row.evidence.get("_model"),
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
    read from `autodedup.phash_pop`, never recounted."""

    def __init__(self, conn: Any, clip_model: str = DEFAULT_CLIP_MODEL) -> None:
        self.conn = conn
        self.clip_model = clip_model
        self.reads = 0
        self.statements = 0

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
        # Frozen, never recounted (E70) — and absent from the table means a population of 0
        # for that hash, not an unknown, because the cohort lane writes every hash it saw.
        population = {int(row[0]): int(row[1]) for row in
                      _rows(self.conn, RT_PHASH_POP_SQL, {"hashes": hashes})} if hashes else {}
        self.statements += 1
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
                 max_retire_fraction: float = MAX_RETIRE_FRACTION) -> None:
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
        self.parents = dict(parents or {})
        self.enter_blocks = _enter_blocks(scope, self.parents)
        self.retired_refused = 0
        self.statements = 0
        self.windows: dict[str, int] = {}
        self._pending: dict[str, Any] = {}
        self._claimed: dict[str, set[int]] = {}

    def _query(self, sql: str, params: Mapping[str, Any]) -> list[tuple]:
        self.statements += 1
        return _rows(self.conn, sql, params)

    def cursors(self) -> dict[str, tuple[int, int, Any]]:
        names = [CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, CURSOR_REVIVE, CURSOR_SCOPE,
                 CURSOR_ENTER]
        out = {name: (0, 0, _EPOCH) for name in names}
        for row in self._query(RT_CURSOR_READ_SQL, {"names": names}):
            out[str(row[0])] = (int(row[1] or 0), int(row[2] or 0), row[3] or _EPOCH)
        return out

    def claim(self, limit: int) -> list[WorkItem]:
        cursors = self.cursors()
        share = max(1, limit // 5)
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
            self._guard_retirement(departed)
            self._pending[CURSOR_SCOPE] = 0 if slice_size < self.drift_slice else slice_max
            for listing_id in departed[:share]:
                items.append(WorkItem(int(listing_id), "drifted", None, None, retire=True))

        # The sixth feed (W9d-3): the rows the scope has started holding and no cursor has ever
        # seen, because `listing_location` is written after the listing is.
        if self.enter_slice and self.enter_blocks:
            index = cursors[CURSOR_ENTER][1] % len(self.enter_blocks)
            after_enter = cursors[CURSOR_ENTER][0]
            block = self.enter_blocks[index]
            rows = self._query(RT_SCOPE_ENTER_SQL, {
                "generation": self.generation, "obec": block.obec,
                "cast_obce": block.cast_obce, "after_id": after_enter,
                "limit": self.enter_slice})
            slice_max, slice_size, entered = (
                (int(rows[0][0]), int(rows[0][1]), list(rows[0][2] or ()))
                if rows else (after_enter, 0, []))
            self.windows["entered"] = slice_size
            taken = [int(value) for value in entered[:share]]
            if len(entered) > len(taken):
                # Cut like a forward feed: stop at the last row this pass took.
                self._pending[CURSOR_ENTER] = (taken[-1], index)
            elif slice_size < self.enter_slice:
                # The block is walked out — move to the next one and start it from the top.
                self._pending[CURSOR_ENTER] = (0, (index + 1) % len(self.enter_blocks))
            else:
                self._pending[CURSOR_ENTER] = (slice_max, index)
            for listing_id in taken:
                items.append(WorkItem(listing_id, "entered", None, None))
        for item in items:
            self._claimed.setdefault(item.feed, set()).add(item.listing_id)
        return items

    def _guard_retirement(self, departed: Sequence[Any]) -> None:
        """Refuse a drift sweep that is not drift (W9d-1).

        The sweep reads the scope as a PREDICATE, so a scope that holds nothing — a settings
        row hand-written as `","`, a rescope that lost its blocks — reports the whole store as
        departed and the lane grinds it away a share at a time, silently, until a re-seed is
        the only recovery. Two rails, both independent of how the scope was parsed: a scope
        with no blocks retires nothing at all, and a sweep that wants more than
        `rt_max_retire_fraction` of the generation's rows in ONE pass window stops the lane
        loudly with the cursor unmoved, because that is drift's shape in no corpus."""
        if not departed:
            return
        if not self.scope.whole_corpus and not self.scope.blocks:
            raise RetireRefusal(
                f"the drift sweep would retire {len(departed)} listings under an EMPTY scope — "
                "refusing. Nothing was written and no cursor moved.")
        store_rows = int(_rows(self.conn, RT_FP_COUNT_SQL,
                               {"generation": self.generation})[0][0] or 0)
        self.statements += 1
        allowed = max(1, int(self.max_retire_fraction * store_rows))
        if len(departed) > allowed:
            self.retired_refused = len(departed)
            raise RetireRefusal(
                f"the drift sweep would retire {len(departed)} of the generation's "
                f"{store_rows} listings in one pass, over the {self.max_retire_fraction:.0%} "
                f"{RETIRE_SETTING} rail ({allowed}) — refusing. That is a scope that has gone "
                "wrong, not a geocode correction. Nothing was written and no cursor moved; "
                f"check autodedup.settings {SCOPE_SETTING!r}.")

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
                           ("drifted", CURSOR_SCOPE), ("entered", CURSOR_ENTER)):
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
        for name in (CURSOR_REVIVE, CURSOR_SCOPE):
            if name in pending:
                self.statements += 1
                _exec(self.conn, RT_CURSOR_SET_SQL, {
                    "name": name, "last_listing_id": int(pending[name])})
                out[name] = int(pending[name])
        if CURSOR_ENTER in pending:
            after_id, block = pending[CURSOR_ENTER]
            self.statements += 1
            # Both halves written whole: the sweep WRAPS to 0 inside a block and rolls the
            # block pointer over, and a coalescing write could never take either back to 0.
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
    schema HAS), plus the quarter code when the block is a quarter (a filter, not an index)."""

    obec: int
    cast_obce: int | None = None


def _enter_blocks(scope: Scope, parents: Mapping[int, int]) -> tuple[EnterBlock, ...]:
    if scope.whole_corpus:
        return ()        # nothing can enter a scope that already holds everything
    blocks: list[EnterBlock] = []
    for code in scope.obec_codes:
        blocks.append(EnterBlock(int(code)))
    for code in scope.cast_obce_codes:
        parent = parents.get(int(code))
        # A quarter with no parent is refused by `resolve_scope_parents` before a pass is
        # built, so here it can only be a caller that resolved none: sweep what CAN be swept
        # rather than refusing a second time in the wrong place.
        if parent is not None:
            blocks.append(EnterBlock(int(parent), int(code)))
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
    if isinstance(value, Mapping):
        value = value.get("value", value.get("mb"))
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(fallback)


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

    settings = load_settings(args.get("settings") or None)
    model = load_model(args.get("model") or None)
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
        control = lane_settings(conn, [SCOPE_SETTING, BUDGET_SETTING, RETIRE_SETTING])
        try:
            # The PERSISTED scope is the generation's, and a dispatch argument is a re-scope
            # the operator has to ask for by name (W9d-2).
            scope, rescoped = resolve_pass_scope(
                args.get(SCOPE_SETTING), control.get(SCOPE_SETTING),
                rescope=str(args.get(RESCOPE_ARG) or "").strip().lower() == "true")
            parents = resolve_scope_parents(conn, scope)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        max_schema_mb = _setting_number(
            args.get(BUDGET_SETTING, control.get(BUDGET_SETTING)), MAX_SCHEMA_MB)
        max_retire_fraction = _setting_number(
            args.get(RETIRE_SETTING, control.get(RETIRE_SETTING)), MAX_RETIRE_FRACTION)
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
        store = SqlStore(conn, generation, store_floor=settings.store_floor)
        facts = SqlFacts(conn)
        work = SqlWork(conn, scope, generation,
                       lag=int(args.get("settle_lag") or SETTLE_LAG_S),
                       straggler_window=int(args.get("straggler_window") or STRAGGLER_WINDOW),
                       revive_slice=int(args.get("revive_slice") or REVIVE_SLICE),
                       window=int(args.get("feed_window") or FEED_WINDOW),
                       drift_slice=int(args.get("drift_slice") or DRIFT_SLICE),
                       enter_slice=int(args.get("enter_slice") or ENTER_SLICE),
                       parents=parents, max_retire_fraction=max_retire_fraction)
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
                        "key": SCOPE_SETTING, "value": json.dumps(scope.as_json()),
                        "updated_by": f"{LANE_NAME}:rescope"})
                    _exec(conn, RT_CURSOR_WRITE_SQL, {
                        "name": CURSOR_ENTER, "last_listing_id": 0,
                        "last_snapshot_id": 0, "watermark": None})
                result = run_pass_bounded(store, facts, work, settings, model, calibration,
                                          limits=limits, generation=generation,
                                          now=time.time())
                if result.aborted:
                    # Nothing this pass wrote survives a refusal, and no cursor moved.
                    raise _Refused()
        except _Refused:
            pass
        except RetireRefusal as exc:
            # The transaction rolled back on the way out: nothing written, no cursor moved.
            raise SystemExit(str(exc)) from exc
        summary = result.to_json() if result is not None else {"aborted": "unknown"}
        summary["fact_reads"] = facts.reads
        summary["statements"] = store.statements + facts.statements + work.statements
        summary["calibration_n_listings"] = int(rows[0][2] or 0)
        summary["store_rows"] = int(
            _rows(conn, RT_FP_COUNT_SQL, {"generation": generation})[0][0])
        summary["scope"] = scope.as_json()
        summary["rescoped"] = bool(rescoped)
        summary["windows"] = dict(work.windows)
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

    Read-only against `public` like the pass itself, and behind the same dark switch."""
    from autodedup.dataset import load
    from autodedup.fingerprint import build_all

    if not env_enabled():
        return {"skipped": "dark", "reason": f"{ENV_FLAG} is not true", "spent_usd": 0.0}
    artifact = (args.get("artifact") or "").strip()
    if not artifact:
        raise SystemExit("rt_seed needs artifact=<cohort.jsonl.gz>")
    generation = (args.get("generation") or "").strip() or GENERATION
    settings = load_settings(args.get("settings") or None)
    model = load_model(args.get("model") or None)
    backfill = str(args.get("backfill") or "").strip().lower() == "true"

    conn = conn_factory()
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539 not applied")
        control = lane_settings(conn, [SCOPE_SETTING, BUDGET_SETTING])
        try:
            scope = resolve_scope(args.get(SCOPE_SETTING), control.get(SCOPE_SETTING))
            # Proved at SEED time rather than at the first pass: a quarter the register cannot
            # place is a scope whose entrant sweep could never walk it (W9d-3).
            resolve_scope_parents(conn, scope)
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        try:
            storage = storage_guard(conn, generation, scope, _setting_number(
                args.get(BUDGET_SETTING, control.get(BUDGET_SETTING)), MAX_SCHEMA_MB))
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
        with _transaction(conn):
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
                "key": SCOPE_SETTING, "value": json.dumps(scope.as_json()),
                "updated_by": f"{LANE_NAME}:rt_seed"})
            if backfill:
                store = SqlStore(conn, generation, store_floor=settings.store_floor)
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
                              bool(listing.is_active)),
                        keyer.index_keys(fp))
                    store.bump_cell(listing)
                    written += 1
                store.flush()
        summary = {
            "generation": generation,
            "calibration_digest": calibration.digest(),
            "calibration_n_listings": calibration.n_listings,
            "cursors": {CURSOR_NEW: int(seeded[0]), CURSOR_CHANGED: int(seeded[1]),
                        CURSOR_FLIPPED: str(seeded[2]), CURSOR_REVIVE: 0, CURSOR_SCOPE: 0},
            "backfilled": written,
            "scope": scope.as_json(),
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
