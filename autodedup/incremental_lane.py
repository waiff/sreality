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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Mapping, Sequence

from autodedup.dataset import Image, Listing, Location
from autodedup.export import DEFAULT_CLIP_MODEL, encode_clip
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
    RT_FACTS_SQL,
    RT_FLIPPED_LISTINGS_SQL,
    RT_FP_COUNT_SQL,
    RT_FP_DELETE_SQL,
    RT_FP_READ_SQL,
    RT_FP_UPSERT_SQL,
    RT_IDLE_GUARD_SQL,
    RT_IMAGE_CLIP_SQL,
    RT_IMAGE_TAGS_SQL,
    RT_IMAGES_SQL,
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
    RT_SEED_CURSORS_SQL,
    RT_SETTING_SQL,
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
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL

LANE_NAME: str = "autodedup_realtime"
# Longer than the workflow's own 25-minute timeout: a lease that expires while its holder is
# still running invites a second pass to write over the first.
LEASE_TTL_S: int = 2100
CURSOR_NEW: str = "rt_new"
CURSOR_CHANGED: str = "rt_changed"
CURSOR_FLIPPED: str = "rt_flipped"
CURSOR_REVIVE: str = "rt_revive"
ENV_FLAG: str = "AUTODEDUP_REALTIME_ENABLED"
DB_FLAG: str = "realtime_enabled"

# How long a row must have existed before the lane will claim it (E68). Portal writes commit in
# seconds; a transaction still open after five minutes would have to be a stuck one, and the
# straggler sweep catches even that.
SETTLE_LAG_S: int = 300
# How far back the anti-join looks for a row that committed after the cursor passed its id.
STRAGGLER_WINDOW: int = 5000
# One round-robin slice of the revive sweep. 434k inactive rows at 20k a pass is a full cycle
# every ~22 passes (~3.7 h at the `*/10` cadence), which is the lane's revival latency.
REVIVE_SLICE: int = 20000
STATEMENT_TIMEOUT_MS: int = 120_000
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

    Three things are buffered rather than written statement by statement (E69): the fingerprint
    rows and postings of one pass, the census cells, and the lookups already answered. Measured:
    a whole 24-listing cohort decided in one pass costs **14 statements** and a pass over an
    unchanged corpus costs **4**, where the per-listing spelling cost ~17,700 for a 166-listing
    claim."""

    def __init__(self, conn: Any, generation: str = GENERATION) -> None:
        self.conn = conn
        self.generation = generation
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
        } for row in rows])

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

    The gallery is assembled exactly as the export lane assembles it — pHash, the corpus-wide
    population, the CLIP tag pairs (logical first, then the fine anchor when it differs) and
    the float16 vector — because a feature that reads a thinner image than the cohort pass did
    is a decision the replay proof does not cover. `broker_key` is the one field a listing row
    cannot serve: it is the export's SALTED key (E28), so the lane reads it from the
    fingerprint mirror it wrote itself rather than re-deriving an identity here."""

    def __init__(self, conn: Any, clip_model: str = DEFAULT_CLIP_MODEL) -> None:
        self.conn = conn
        self.clip_model = clip_model
        self.reads = 0
        self.statements = 0

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        listings: dict[int, Listing] = {}
        self.statements += 2
        for row in _rows(self.conn, RT_FACTS_SQL, {"ids": wanted}):
            listings[int(row[0])] = _listing_of(row)
        galleries: dict[int, list[Image]] = {}
        image_ids: list[int] = []
        for row in _rows(self.conn, RT_IMAGES_SQL, {"ids": wanted}):
            image_id = int(row[1])
            image_ids.append(image_id)
            galleries.setdefault(int(row[0]), []).append(
                Image(image_id=image_id, listing_id=int(row[0]),
                      seq=int(row[2]) if row[2] is not None else None,
                      phash=int(row[3]) if row[3] is not None else None,
                      pop=int(row[4]) if row[4] is not None else None))
        tags: dict[int, list[tuple[str, float | None]]] = {}
        clips: dict[int, str] = {}
        if image_ids:
            self.statements += 2
            for row in _rows(self.conn, RT_IMAGE_TAGS_SQL,
                             {"ids": image_ids, "model": self.clip_model}):
                bucket = tags.setdefault(int(row[0]), [])
                score = float(row[3]) if row[3] is not None else None
                logical, fine = row[2], row[1]
                if logical:
                    bucket.append((str(logical), score))
                if fine and fine != logical:
                    bucket.append((str(fine), score))
            for row in _rows(self.conn, RT_IMAGE_CLIP_SQL,
                             {"ids": image_ids, "model": self.clip_model}):
                clips[int(row[0])] = encode_clip(row[1])
        for bucket in galleries.values():
            for image in bucket:
                image.tags = tags.get(image.image_id, [])
                image.clip = clips.get(image.image_id)
        self.reads += len(listings)
        return {i: (listing, galleries.get(i, [])) for i, listing in listings.items()}


def _listing_of(row: Sequence[Any]) -> Listing:
    return Listing(
        id=int(row[0]), block="", source=row[1], source_id_native=row[2], source_url=row[3],
        category_main=row[4], category_type=row[5], subtype=row[6], disposition=row[7],
        area_m2=float(row[8]) if row[8] is not None else None,
        floor=int(row[9]) if row[9] is not None else None,
        total_floors=int(row[10]) if row[10] is not None else None,
        price=float(row[11]) if row[11] is not None else None,
        description=row[12],
        first_seen_at=row[13].isoformat() if row[13] is not None else None,
        last_seen_at=row[14].isoformat() if row[14] is not None else None,
        inactive_at=row[15].isoformat() if row[15] is not None else None,
        is_active=bool(row[16]),
        broker_key=None,
        broker_identity_id=int(row[17]) if row[17] is not None else None,
        broker_firm_id=int(row[18]) if row[18] is not None else None,
        location=Location(
            obec_kod=int(row[19]) if row[19] is not None else None,
            cast_obce_kod=int(row[20]) if row[20] is not None else None,
            granularity=row[21],
            lat=float(row[22]) if row[22] is not None else None,
            lon=float(row[23]) if row[23] is not None else None,
            street_key=row[24], house_number=row[25], house_number_cp=row[26],
            house_number_co=row[27], psc=row[28],
            ruian_adm_kod=int(row[29]) if row[29] is not None else None,
            country_code=row[30], country_status=row[31],
        ),
    )


class SqlWork:
    """The four bounded watermark feeds, and the rule that a cursor moves only over what a pass
    actually decided.

    Every feed is settle-lagged and paged on its FULL key (E68), because neither of the two
    things that look like watermarks here is monotone on its own: `id` is assigned at INSERT
    (so overlapping batch transactions commit out of order) and `inactive_at` is the
    transaction timestamp (so a `mark_inactive` batch shares one stamp — 189 live tie groups
    are larger than one pass's share, the largest 5,534 rows)."""

    def __init__(self, conn: Any, generation: str = GENERATION,
                 lag: int = SETTLE_LAG_S, straggler_window: int = STRAGGLER_WINDOW,
                 revive_slice: int = REVIVE_SLICE) -> None:
        self.conn = conn
        self.generation = generation
        self.lag = lag
        self.straggler_window = straggler_window
        self.revive_slice = revive_slice
        self.statements = 0
        self._revive_next: int | None = None

    def _query(self, sql: str, params: Mapping[str, Any]) -> list[tuple]:
        self.statements += 1
        return _rows(self.conn, sql, params)

    def cursors(self) -> dict[str, tuple[int, int, Any]]:
        out = {name: (0, 0, _EPOCH) for name in
               (CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, CURSOR_REVIVE)}
        for row in self._query(RT_CURSOR_READ_SQL, {
                "names": [CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, CURSOR_REVIVE]}):
            out[str(row[0])] = (int(row[1] or 0), int(row[2] or 0), row[3] or _EPOCH)
        return out

    def claim(self, limit: int) -> list[WorkItem]:
        cursors = self.cursors()
        share = max(1, limit // 4)
        items: list[WorkItem] = []

        after_new = cursors[CURSOR_NEW][0]
        # The straggler sweep FIRST: a row that committed after the cursor passed its id is
        # older work than anything the forward feed is about to hand over. It carries no
        # cursor value — it is BEHIND the watermark by definition.
        if after_new and self.straggler_window:
            for row in self._query(RT_NEW_STRAGGLERS_SQL, {
                    "after_id": after_new, "window": self.straggler_window,
                    "generation": self.generation, "limit": share}):
                items.append(WorkItem(int(row[0]), "straggler", _epoch(row[1]), None))
        for row in self._query(RT_NEW_LISTINGS_SQL, {
                "after_id": after_new, "lag": self.lag, "limit": share}):
            items.append(WorkItem(int(row[0]), "new", _epoch(row[1]), int(row[0])))
        for row in self._query(RT_CHANGED_LISTINGS_SQL, {
                "after_id": cursors[CURSOR_CHANGED][1], "lag": self.lag, "limit": share}):
            items.append(WorkItem(int(row[1]), "changed", _epoch(row[2]), int(row[0])))
        for row in self._query(RT_FLIPPED_LISTINGS_SQL, {
                "after": cursors[CURSOR_FLIPPED][2], "after_id": cursors[CURSOR_FLIPPED][0],
                "lag": self.lag, "limit": share}):
            items.append(WorkItem(int(row[0]), "flipped", _epoch(row[1]),
                                  (row[1], int(row[0]))))

        if self.revive_slice:
            after_revive = cursors[CURSOR_REVIVE][0]
            rows = self._query(RT_REVIVED_SQL, {
                "generation": self.generation, "after_id": after_revive,
                "limit": self.revive_slice})
            slice_max, slice_size, revived = (
                (int(rows[0][0]), int(rows[0][1]), list(rows[0][2] or ()))
                if rows else (after_revive, 0, []))
            # A short slice is the end of the sweep, so the cursor wraps and the next pass
            # starts the cycle again.
            self._revive_next = 0 if slice_size < self.revive_slice else slice_max
            for listing_id in revived:
                items.append(WorkItem(int(listing_id), "revived", None, None))
        return items

    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]:
        """Advance each feed to the maximum of what THIS pass decided, and no further."""
        by_feed: dict[str, list[Any]] = {}
        for item in done:
            if item.cursor is not None:
                by_feed.setdefault(item.feed, []).append(item.cursor)
        out: dict[str, Any] = {}
        if by_feed.get("new"):
            value = int(max(by_feed["new"]))
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_NEW, "last_listing_id": value,
                "last_snapshot_id": None, "watermark": None})
            out[CURSOR_NEW] = value
        if by_feed.get("changed"):
            value = int(max(by_feed["changed"]))
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_CHANGED, "last_listing_id": None,
                "last_snapshot_id": value, "watermark": None})
            out[CURSOR_CHANGED] = value
        if by_feed.get("flipped"):
            stamp, listing_id = max(by_feed["flipped"])
            self.statements += 1
            _exec(self.conn, RT_CURSOR_WRITE_SQL, {
                "name": CURSOR_FLIPPED, "last_listing_id": int(listing_id),
                "last_snapshot_id": None, "watermark": stamp})
            out[CURSOR_FLIPPED] = [str(stamp), int(listing_id)]
        if self._revive_next is not None:
            self.statements += 1
            _exec(self.conn, RT_CURSOR_SET_SQL, {
                "name": CURSOR_REVIVE, "last_listing_id": int(self._revive_next)})
            out[CURSOR_REVIVE] = int(self._revive_next)
            self._revive_next = None
        return out


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
    """One pass, one transaction (E70). `db.connect` is autocommit, so without this a crash
    between the cluster DELETE and its INSERT loses those clusters permanently — the re-claim
    finds matching digests, skips, and never rebuilds them."""
    opener = getattr(conn, "transaction", None)
    return opener() if callable(opener) else nullcontext()


class _Refused(Exception):
    """The pair budget refused this claim — roll the pass back and report it (E70)."""


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
        if not take_lease(conn, holder):
            return {"skipped": "leased", "reason": "another pass holds the lease",
                    "spent_usd": 0.0}
        leased = True
        _exec(conn, RT_STATEMENT_GUARD_SQL, {"statement_timeout_ms": STATEMENT_TIMEOUT_MS})
        _exec(conn, RT_IDLE_GUARD_SQL, {"idle_timeout_ms": IDLE_TIMEOUT_MS})
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
        if not rows:
            raise SystemExit(
                f"no frozen calibration for generation {generation!r} (E65) — seed it with "
                "`--mode rt_seed` before the lane runs")
        payload = rows[0][3]
        calibration = Calibration.from_json(
            payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        store = SqlStore(conn, generation)
        facts = SqlFacts(conn)
        work = SqlWork(conn, generation,
                       lag=int(args.get("settle_lag") or SETTLE_LAG_S),
                       straggler_window=int(args.get("straggler_window") or STRAGGLER_WINDOW),
                       revive_slice=int(args.get("revive_slice") or REVIVE_SLICE))
        result = None
        try:
            with _transaction(conn):
                result = run_pass_bounded(store, facts, work, settings, model, calibration,
                                          limits=limits, generation=generation,
                                          now=time.time())
                if result.aborted:
                    # Nothing this pass wrote survives a refusal, and no cursor moved.
                    raise _Refused()
        except _Refused:
            pass
        summary = result.to_json() if result is not None else {"aborted": "unknown"}
        summary["fact_reads"] = facts.reads
        summary["statements"] = store.statements + facts.statements + work.statements
        summary["calibration_n_listings"] = int(rows[0][2] or 0)
        summary["store_rows"] = int(
            _rows(conn, RT_FP_COUNT_SQL, {"generation": generation})[0][0])
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
    first live arrival is reached (E71). The seed writes the calibration from the same cohort
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

    ds = load(artifact)
    fps = build_all(ds, settings)
    calibration = Calibration.build(fps, ds.listings, settings, generation)
    conn = conn_factory()
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539 not applied")
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
            if backfill:
                store = SqlStore(conn, generation)
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
                        CURSOR_FLIPPED: str(seeded[2]), CURSOR_REVIVE: 0},
            "backfilled": written,
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
