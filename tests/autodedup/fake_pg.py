"""A Postgres stand-in for the real-time lane's statements — the rail W9's verification asked for.

Every one of W9's four blocking defects lived in `incremental_lane.SqlStore` and none of them
could be seen from the in-memory twin: the fingerprint row was never written, the certificate
and the evidence families were dropped on read-back, the families bitmask was written as a
count, and E64's rail queried with an empty id array. A recording fake would have caught none
of them either — what catches them is a fake that STORES what the adapter writes and SERVES it
back in the column order the adapter reads, so a cohort can be replayed through the SQL path
and compared with the same cohort replayed through the twin.

It is deliberately dumb: it dispatches on the SQL constant itself (never on a parse), so a
statement this file does not know raises rather than silently answering nothing, and a
constant that changes shape breaks here before it breaks in production. Where the real schema
has a constraint the lane must not violate — `zone` in three values, `listing_lo < listing_hi`,
`families` a bitmask, `certificate` one of four codes — the fake ENFORCES it, because that is
the cheapest place to find out.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from autodedup import export_sql as E
from autodedup import incremental_sql as S
from autodedup import apply_sql as A
from autodedup.decide import CERTIFICATES
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL
from autodedup.score_lane import CLUSTER_INSERT_SQL, CLUSTER_MEMBER_INSERT_SQL


def _jsonb(value: Any) -> Any:
    """psycopg hands a jsonb column back as a parsed object, not as text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class FakePg:
    """The tables the lane writes, plus the three `public` feeds it reads."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        self.fp_key: set[tuple[str, str, str, int]] = set()
        self.rt_fp: dict[tuple[str, int], dict[str, Any]] = {}
        self.pairs: dict[tuple[str, int, int], dict[str, Any]] = {}
        self.clusters: dict[tuple[str, int], dict[str, Any]] = {}
        self.cluster_members: set[tuple[str, int, int]] = set()
        self.cluster_conflicts: list[dict[str, Any]] = []
        self.cells: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.cursors: dict[str, dict[str, Any]] = {}
        self.calibration: dict[str, dict[str, Any]] = {}
        self.lease: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}
        self.mnl: set[tuple[int, int]] = set()
        self.ml: set[tuple[int, int]] = set()
        # `public.app_settings`: the apply scope row the reconcile reads (A9). Absent = closed.
        self.app_settings: dict[str, Any] = {}
        # `public`, read-only: what the four feeds page over AND what the fact source reads.
        # The rows carry whatever column a statement asks for, so a listing row here is the
        # same dict the feeds and `COHORT_LISTINGS_SQL` both read.
        self.listings: dict[int, dict[str, Any]] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.locations: dict[int, dict[str, Any]] = {}
        self.image_rows: list[dict[str, Any]] = []
        self.clip_tags: list[dict[str, Any]] = []
        self.clip_vectors: dict[int, str] = {}
        self.phash_pop: dict[int, int] = {}
        self.statements: list[str] = []
        # Which statements the pass issued INSIDE its transaction. `set_config(..., true)` is
        # only a guard where the transaction can see it (W9d-4), and over the transaction-mode
        # pooler that is the only place it survives.
        self.statements_in_tx: list[str] = []
        self.in_transaction = False
        # `public.ruian_admin_units`: cast_obce code -> parent obec code (W9d-3).
        self.admin_parents: dict[int, int] = {}
        # The scope's membership snapshot and the two ledgers W9e rails the lane with: the
        # entrant cadence's scan log and the rolling-day retirement log.
        self.scope_ids: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.scope_scans: list[dict[str, Any]] = []
        self.retire_events: list[dict[str, Any]] = []
        self.transactions = 0
        self.rolled_back = 0
        # What `pg_total_relation_size` over schema `autodedup` answers — the storage guard's
        # one input (E79). Tests move it to put the lane over budget.
        self.schema_bytes = 64 * 1_048_576
        # `pg_total_relation_size` of each table a fresh seed empties (E916): what the seed's
        # projection apportions by the generation's share of the rows. Absent = 0 bytes.
        self.table_bytes: dict[str, int] = {}
        # The declared type of `autodedup.pairs.score` (migration 541) and the settings each
        # batch generation's score pass recorded — what `rt_equivalence` reads to say whether
        # the store can carry the number the clustering ranks on, and whether the two sides
        # ran the same clock.
        self.score_column_type = "double precision"
        self.score_runs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ psycopg surface
    def cursor(self) -> "_Cursor":
        return _Cursor(self)

    def transaction(self) -> "_Tx":
        return _Tx(self)

    def close(self) -> None:
        return None

    def snapshot(self) -> dict[str, Any]:
        """Everything a rollback has to put back."""
        return {
            "fp_key": set(self.fp_key),
            "rt_fp": {k: dict(v) for k, v in self.rt_fp.items()},
            "pairs": {k: dict(v) for k, v in self.pairs.items()},
            "clusters": {k: dict(v) for k, v in self.clusters.items()},
            "cluster_members": set(self.cluster_members),
            "cluster_conflicts": [dict(row) for row in self.cluster_conflicts],
            "cells": {k: dict(v) for k, v in self.cells.items()},
            "cursors": {k: dict(v) for k, v in self.cursors.items()},
            # The seed takes the lease and the clean reset (E97) runs inside its transaction, so
            # a refusal has to put the lease row back the way Postgres would.
            "lease": {k: dict(v) for k, v in self.lease.items()},
            "scope_ids": {k: dict(v) for k, v in self.scope_ids.items()},
            "scope_scans": [dict(row) for row in self.scope_scans],
            "retire_events": [dict(row) for row in self.retire_events],
            # The seed writes these three INSIDE its transaction (the frozen population, the
            # calibration and the control rows), so a refusal has to put them back too.
            "phash_pop": dict(self.phash_pop),
            "calibration": {k: dict(v) for k, v in self.calibration.items()},
            "settings": dict(self.settings),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.fp_key = state["fp_key"]
        self.rt_fp = state["rt_fp"]
        self.pairs = state["pairs"]
        self.clusters = state["clusters"]
        self.cluster_members = state["cluster_members"]
        self.cluster_conflicts = state["cluster_conflicts"]
        self.cells = state["cells"]
        self.cursors = state["cursors"]
        self.lease = state["lease"]
        self.scope_ids = state["scope_ids"]
        self.scope_scans = state["scope_scans"]
        self.retire_events = state["retire_events"]
        self.phash_pop = state["phash_pop"]
        self.calibration = state["calibration"]
        self.settings = state["settings"]


class _Tx:
    def __init__(self, conn: FakePg) -> None:
        self.conn = conn
        self.state: dict[str, Any] | None = None

    def __enter__(self) -> "_Tx":
        self.conn.transactions += 1
        self.conn.in_transaction = True
        self.state = self.conn.snapshot()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.conn.in_transaction = False
        if exc_type is not None and self.state is not None:
            self.conn.restore(self.state)
            self.conn.rolled_back += 1
        return False


class _Cursor:
    def __init__(self, conn: FakePg) -> None:
        self.conn = conn
        self.rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def fetchall(self) -> list[tuple]:
        return list(self.rows)

    def executemany(self, sql: str, seq: Sequence[Mapping[str, Any]]) -> None:
        for params in seq:
            self.execute(sql, params)

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        self.conn.statements.append(sql)
        if self.conn.in_transaction:
            self.conn.statements_in_tx.append(sql)
        self.rows = _dispatch(self.conn, sql, dict(params or {}))
        # psycopg exposes the column names, and the fact source reads rows as dicts through
        # them. Every export statement aliases every column, so the SELECT list IS the names.
        self.description = [(name,) for name in _aliases(sql)]


def _aliases(sql: str) -> list[str]:
    return re.findall(r"\bAS\s+(\w+)", sql, re.I)


def _in_scope(db: FakePg, listing_id: Any, p: Mapping[str, Any]) -> bool:
    """The scope predicate the feeds spell in SQL: `listing_location` by primary key, then the
    two code lists. `all_scope` holds everything, a location row included or not."""
    if p.get("all_scope"):
        return True
    location = db.locations.get(int(listing_id)) or {}
    obec = location.get("obec_kod")
    cast_obce = location.get("cast_obce_kod")
    return ((obec is not None and obec in set(p.get("obec") or ()))
            or (cast_obce is not None and cast_obce in set(p.get("cast_obce") or ())))


def _dispatch(db: FakePg, sql: str, p: Mapping[str, Any]) -> list[tuple]:  # noqa: C901
    gen = str(p.get("generation") or "")

    if sql == S.RT_STORE_PRESENT_SQL:
        return [(True,)]
    if sql == S.RT_SCHEMA_SIZE_SQL:
        return [(int(db.schema_bytes),)]
    if sql == S.RT_ROW_CENSUS_SQL:
        return [("fp_key", sum(1 for row in db.fp_key if row[0] == gen)),
                ("rt_fp", sum(1 for g, _i in db.rt_fp if g == gen)),
                ("pairs", sum(1 for g, _lo, _hi in db.pairs if g == gen)),
                ("rt_block_cell", sum(1 for g, _k, _c in db.cells if g == gen))]
    if sql == S.RT_GENERATION_BYTES_SQL:
        held = {
            "pairs": [key[0] for key in db.pairs],
            "cluster_members": [row[0] for row in db.cluster_members],
            "cluster_conflicts": [(_jsonb(row.get("detail")) or {}).get("generation")
                                  for row in db.cluster_conflicts],
            "clusters": [key[0] for key in db.clusters],
            "rt_fp": [key[0] for key in db.rt_fp],
            "fp_key": [row[0] for row in db.fp_key],
            "rt_block_cell": [key[0] for key in db.cells],
            "rt_scope_ids": [key[0] for key in db.scope_ids],
            "rt_scope_scan": [row["generation"] for row in db.scope_scans],
            "rt_retire_event": [row["generation"] for row in db.retire_events],
        }
        return [(name, int(db.table_bytes.get(name, 0)), len(gens),
                 sum(1 for g in gens if g == gen)) for name, gens in held.items()]
    if sql == S.RT_ROW_ESTIMATE_SQL:
        return [("fp_key", len(db.fp_key)), ("rt_fp", len(db.rt_fp)),
                ("pairs", len(db.pairs)), ("rt_block_cell", len(db.cells))]
    if sql == S.RT_SETTINGS_MANY_SQL:
        return [(key, db.settings[key]) for key in p["keys"] if key in db.settings]
    if sql == S.RT_SETTING_WRITE_SQL:
        db.settings[str(p["key"])] = _jsonb(p["value"])
        return []
    if sql in (S.RT_STATEMENT_GUARD_SQL, S.RT_LOCK_GUARD_SQL, S.RT_IDLE_GUARD_SQL):
        return [("set",)]

    # ---------------------------------------------------------------- lease
    if sql == S.RT_LEASE_TAKE_SQL:
        held = db.lease.get(p["name"])
        if held and held["expires_at"] > db.now:
            return []
        db.lease[p["name"]] = {"holder": p["holder"],
                               "expires_at": db.now + timedelta(seconds=int(p["ttl"]))}
        return [(p["holder"],)]
    if sql == S.RT_LEASE_RELEASE_SQL:
        held = db.lease.get(p["name"])
        if held and held["holder"] == p["holder"]:
            held["expires_at"] = db.now
        return []
    if sql == S.RT_LEASE_READ_SQL:
        held = db.lease.get(p["name"])
        return ([(held["holder"], held.get("taken_at"), held["expires_at"],
                  held["expires_at"] > db.now)] if held else [])

    # ---------------------------------------------------------------- postings
    if sql == S.RT_LOOKUP_MANY_SQL:
        wanted = set(zip(p["probes"], p["tokens"]))
        return sorted((probe, token, listing_id)
                      for g, probe, token, listing_id in db.fp_key
                      if g == gen and (probe, token) in wanted)
    if sql == S.RT_KEYS_MANY_SQL:
        ids = set(p["ids"])
        return sorted((listing_id, probe, token)
                      for g, probe, token, listing_id in db.fp_key
                      if g == gen and listing_id in ids)
    if sql == S.RT_KEY_DELETE_SQL:
        ids = set(p["ids"])
        db.fp_key = {row for row in db.fp_key
                     if not (row[0] == gen and row[3] in ids)}
        return []
    if sql == S.RT_KEY_INSERT_SQL:
        for probe, token, listing_id in zip(p["probes"], p["tokens"], p["listing_ids"]):
            db.fp_key.add((gen, str(probe), str(token), int(listing_id)))
        return []

    # ---------------------------------------------------------------- fingerprint rows
    if sql == S.RT_FP_UPSERT_SQL:
        key = (gen, int(p["listing_id"]))
        # `first_decided_at` is COALESCED server-side (migration 540): a refresh keeps the
        # first stamp. The fake enforces that, because a lane that restamped it would hold a
        # merge for ever and nothing else here could see it.
        held = db.rt_fp.get(key) or {}
        db.rt_fp[key] = {
            "category_main": p["category_main"], "category_type": p["category_type"],
            "area_m2": p["area_m2"], "disposition": p["disposition"], "floor": p["floor"],
            "fp_digest": p["fp_digest"], "cell_key": p["cell_key"],
            "cell_group": p["cell_group"], "is_active": p["is_active"],
            "ev_images": p["ev_images"], "ev_phash": p["ev_phash"],
            "ev_clip": p["ev_clip"], "ev_tags": p["ev_tags"],
            "ev_complete": p["ev_complete"],
            "first_decided_at": held.get("first_decided_at") or db.now,
        }
        return []
    if sql == S.RT_FP_DELETE_SQL:
        for listing_id in p["ids"]:
            db.rt_fp.pop((gen, int(listing_id)), None)
        return []
    if sql == S.RT_FP_READ_SQL:
        out = []
        for listing_id in p["ids"]:
            row = db.rt_fp.get((gen, int(listing_id)))
            if row is None:
                continue
            stamp = row.get("first_decided_at")
            out.append((int(listing_id), row["category_main"], row["category_type"],
                        row["area_m2"], row["disposition"], row["floor"], row["fp_digest"],
                        row["cell_key"], row["cell_group"], row["is_active"],
                        row.get("ev_images"), row.get("ev_phash"), row.get("ev_clip"),
                        row.get("ev_tags"),
                        stamp.timestamp() if hasattr(stamp, "timestamp") else stamp))
        return out
    if sql == S.RT_KNOWN_SQL:
        return [(int(i),) for i in p["ids"] if (gen, int(i)) in db.rt_fp]
    if sql == S.RT_FP_COUNT_SQL:
        return [(sum(1 for g, _i in db.rt_fp if g == gen),)]

    # ---------------------------------------------------------------- pairs
    if sql in (S.RT_PAIRS_TOUCHING_SQL, S.RT_PAIRS_WITHIN_SQL, S.RT_STAMPED_MERGES_SQL):
        rows = []
        for (g, lo, hi), row in sorted(db.pairs.items()):
            if g != gen:
                continue
            if sql == S.RT_PAIRS_TOUCHING_SQL:
                ids = set(p["ids"])
                if lo not in ids and hi not in ids:
                    continue
            elif sql == S.RT_PAIRS_WITHIN_SQL:
                ids = set(p["ids"])
                if lo not in ids or hi not in ids:
                    continue
            else:
                if row["zone"] != "merge":
                    continue
                block = (row["context"] or {}).get("block")
                if block not in set(p["blocks"]):
                    continue
            vector = row.get("features") or {}
            rows.append((lo, hi, list(row["probes"]), row["from_lo"], row["from_hi"],
                         row["score"], row["zone"], row["decision"], row["guard_veto"],
                         row["families"], row["certificate"], row["evidence"],
                         row["context"], row["fp_lo"], row["fp_hi"],
                         {name: vector[name] for name in FEATURE_SLOTS if name in vector}))
        return rows
    if sql == S.RT_MERGE_NEIGHBOURS_SQL:
        ids = set(p["ids"])
        out = []
        for (g, lo, hi), row in db.pairs.items():
            if g != gen or row["zone"] != "merge":
                continue
            if lo in ids:
                out.append((lo, hi))
            if hi in ids:
                out.append((hi, lo))
        return out
    if sql == S.RT_PAIR_UPSERT_SQL:
        lo, hi = int(p["listing_lo"]), int(p["listing_hi"])
        assert lo < hi, "autodedup_pairs_order_ck refuses lo >= hi"
        assert p["zone"] in ("merge", "band", "reject"), p["zone"]
        assert isinstance(p["families"], int) and 0 <= p["families"] < 128, p["families"]
        assert p["certificate"] in (None, *CERTIFICATES), p["certificate"]
        previous = db.pairs.get((gen, lo, hi)) or {}
        db.pairs[(gen, lo, hi)] = {
            "probes": list(p["probes"]), "from_lo": p["from_lo"], "from_hi": p["from_hi"],
            "families": int(p["families"]), "certificate": p["certificate"],
            "features": _jsonb(p["features"]), "fp_lo": p["fp_lo"], "fp_hi": p["fp_hi"],
            "score": float(p["score"]), "zone": p["zone"], "decision": p["decision"],
            "guard_veto": p["guard_veto"], "evidence": _jsonb(p["evidence"]),
            "context": _jsonb(p["context"]),
            "calibration_digest": p["calibration_digest"],
            "feature_version": p["feature_version"], "model_version": p["model_version"],
            "cluster_key": previous.get("cluster_key"),
        }
        return []
    if sql == S.RT_PAIR_DELETE_SQL:
        db.pairs.pop((gen, int(p["listing_lo"]), int(p["listing_hi"])), None)
        return []
    if sql == S.RT_PAIR_CLUSTER_SQL:
        ids = set(p["ids"])
        for (g, lo, hi), row in db.pairs.items():
            if g == gen and lo in ids and hi in ids:
                row["cluster_key"] = int(p["cluster_key"])
        return []
    if sql == S.RT_PAIR_UNCLUSTER_SQL:
        keys = set(p["keys"])
        for (g, _lo, _hi), row in db.pairs.items():
            if g == gen and row.get("cluster_key") in keys:
                row["cluster_key"] = None
        return []

    # ---------------------------------------------------------------- clusters
    if sql == S.RT_CLUSTERS_TOUCHING_SQL:
        ids = set(p["ids"])
        keys = {key for g, key, listing_id in db.cluster_members
                if g == gen and listing_id in ids}
        return sorted((key, listing_id) for g, key, listing_id in db.cluster_members
                      if g == gen and key in keys)
    if sql == S.RT_CLUSTER_DROP_SQL:
        for key in p["keys"]:
            db.clusters.pop((gen, int(key)), None)
        return []
    if sql == S.RT_CLUSTER_MEMBERS_DROP_SQL:
        keys = set(int(k) for k in p["keys"])
        db.cluster_members = {row for row in db.cluster_members
                              if not (row[0] == gen and row[1] in keys)}
        return []
    if sql == S.RT_CONFLICT_DROP_SQL:
        ids = set(p["ids"])
        db.cluster_conflicts = [
            row for row in db.cluster_conflicts
            if not ((row["detail"] or {}).get("generation") == gen
                    and row["listing_lo"] in ids and row["listing_hi"] in ids)]
        return []
    if sql == CLUSTER_INSERT_SQL:
        db.clusters[(gen, int(p["cluster_key"]))] = dict(p)
        return []
    if sql == CLUSTER_MEMBER_INSERT_SQL:
        db.cluster_members.add((gen, int(p["cluster_key"]), int(p["listing_id"])))
        return []
    if sql == CLUSTER_CONFLICT_INSERT_SQL:
        row = dict(p)
        row["detail"] = _jsonb(p["detail"])
        db.cluster_conflicts.append(row)
        return []

    # ---------------------------------------------------------------- census cells
    if sql == S.RT_CELL_READ_SQL:
        keys = set(p["keys"])
        return [(key, group, row["n_listings"], row["shapes"], row["brokers"],
                 row["source_ids"], row["capped"])
                for (g, key, group), row in sorted(db.cells.items())
                if g == gen and key in keys]
    if sql == S.RT_CELL_UPSERT_SQL:
        db.cells[(gen, str(p["cell_key"]), str(p["category_group"]))] = {
            "n_listings": int(p["n_listings"]), "shapes": _jsonb(p["shapes"]),
            "brokers": _jsonb(p["brokers"]), "source_ids": _jsonb(p["source_ids"]),
            "capped": bool(p["capped"])}
        return []

    # ------------------------------------------------- the export's own fact statements
    if sql == E.COHORT_LISTINGS_SQL:
        names = _aliases(sql)
        return [tuple(db.listings[i].get(name) if name != "id" else i for name in names)
                for i in sorted(p["ids"]) if i in db.listings]
    if sql == E.COHORT_LOCATION_SQL:
        names = _aliases(sql)
        return [tuple(db.locations[i].get(name) if name != "listing_id" else i
                      for name in names)
                for i in sorted(p["ids"]) if i in db.locations]
    if sql == E.COHORT_PRICE_HISTORY_SQL:
        names = _aliases(sql)
        wanted = set(p["ids"])
        rows = [row for row in db.snapshots
                if row.get("listing_id") in wanted and row.get("price_czk") is not None]
        rows.sort(key=lambda r: (r["listing_id"], r.get("scraped_at") or db.now))
        return [tuple(row.get(name) for name in names) for row in rows]
    if sql == E.COHORT_IMAGES_SQL:
        names = _aliases(sql)
        wanted = set(p["ids"])
        rows = [row for row in db.image_rows if row.get("listing_id") in wanted]
        rows.sort(key=lambda r: (r["listing_id"], r.get("sequence") is None,
                                 r.get("sequence") or 0, r["image_id"]))
        return [tuple(row.get(name) for name in names) for row in rows]
    if sql == E.COHORT_CLIP_SQL:
        wanted = set(p["ids"])
        return [(image_id, vector) for image_id, vector in sorted(db.clip_vectors.items())
                if image_id in wanted]
    if sql == E.COHORT_CLIP_TAGS_SQL:
        names = _aliases(sql)
        wanted = set(p["ids"])
        return [tuple(row.get(name) for name in names) for row in db.clip_tags
                if row.get("image_id") in wanted and row.get("model", p["model"]) == p["model"]]
    if sql == S.RT_EVIDENCE_CANDIDATES_SQL:
        horizon = timedelta(hours=int(p["horizon_hours"]))
        out = []
        for (g, listing_id), row in sorted(db.rt_fp.items()):
            if g != gen or listing_id <= int(p["after_id"]):
                continue
            stamp = row.get("first_decided_at")
            if not (row.get("ev_complete") is not True or stamp is None
                    or stamp > db.now - horizon):
                continue
            out.append((int(listing_id), row.get("ev_images"), row.get("ev_phash"),
                        row.get("ev_clip"), row.get("ev_tags")))
        return out[:int(p["limit"])]
    if sql == S.RT_EVIDENCE_PROBE_SQL:
        wanted = set(int(i) for i in p["ids"])
        counts: dict[int, list[int]] = {}
        for row in db.image_rows:
            listing_id = int(row.get("listing_id") or 0)
            if listing_id not in wanted:
                continue
            bucket = counts.setdefault(listing_id, [0, 0, 0])
            bucket[0] += 1
            bucket[1] += int(row.get("phash") is not None)
            bucket[2] += int(row.get("clip_tagged_at") is not None)
        return [(listing_id, *counts[listing_id]) for listing_id in sorted(counts)]
    if sql == S.RT_EVIDENCE_RELEASE_SQL:
        horizon = timedelta(hours=int(p["horizon_hours"]))

        def _pending(listing_id: int) -> bool:
            row = db.rt_fp.get((gen, int(listing_id))) or {}
            stamp = row.get("first_decided_at")
            return bool(row.get("ev_complete") is False
                        and stamp is not None and stamp > db.now - horizon)

        out = []
        for (g, lo, hi), row in sorted(db.pairs.items()):
            if g != gen or row.get("decision") != p["reason"]:
                continue
            if _pending(lo) or _pending(hi):
                continue
            out.append((lo, hi))
        return out[:int(p["limit"])]
    if sql == S.RT_EVIDENCE_HELD_COUNT_SQL:
        return [(sum(1 for (g, _lo, _hi), row in db.pairs.items()
                     if g == gen and row.get("decision") == p["reason"]),)]
    if sql == S.RT_PHASH_POP_SQL:
        wanted = set(p["hashes"])
        return sorted((h, n) for h, n in db.phash_pop.items() if h in wanted)
    if sql == S.RT_PHASH_POP_COUNT_SQL:
        return [(len(db.phash_pop),)]
    if sql == S.RT_PHASH_POP_WRITE_SQL:
        db.phash_pop[int(p["phash"])] = int(p["n_listings"])
        return []

    # --------------------------------------------- A10: the calibration cut (E912)
    if sql == S.RT_CUT_SCOPE_IDS_SQL:
        return sorted({(listing_id,) for (g, _block, listing_id) in db.scope_ids if g == gen})
    if sql == S.RT_CUT_HASHES_SQL:
        wanted = set(int(i) for i in p["ids"])
        return sorted({(int(row["phash"]),) for row in db.image_rows
                       if row.get("listing_id") in wanted and row.get("phash") is not None})
    if sql == E.COHORT_PHASH_POP_SQL:
        wanted = set(int(h) for h in p["hashes"])
        carriers: dict[int, set[int]] = {}
        for row in db.image_rows:
            if row.get("phash") is not None and int(row["phash"]) in wanted:
                carriers.setdefault(int(row["phash"]), set()).add(int(row["listing_id"]))
        return [tuple([phash, len(ids)]) for phash, ids in sorted(carriers.items())]

    # --------------------------------------------- the reconcile's scope row (A9)
    if sql == A.SETTING_SQL:
        key = str(p["key"])
        return [(db.app_settings[key],)] if key in db.app_settings else []

    if sql == S.RT_MUST_NOT_LINK_SQL:
        return sorted(db.mnl)
    if sql == S.RT_MUST_LINK_SQL:
        return sorted(db.ml)

    # ---------------------------------------------------------------- the clean reset (E97)
    #
    # Each statement counts what it deleted, because the seed's receipt says so per table. The
    # fake enforces the one thing the reset promises: the predicate is THIS generation, so a
    # statement that lost its `where` would show up here as another generation going missing.
    if sql == S.RT_FRESH_PAIRS_SQL:
        gone = [key for key in db.pairs if key[0] == gen]
        for key in gone:
            db.pairs.pop(key)
        return [(len(gone),)]
    if sql == S.RT_FRESH_CLUSTER_MEMBERS_SQL:
        gone = [row for row in db.cluster_members if row[0] == gen]
        db.cluster_members = {row for row in db.cluster_members if row[0] != gen}
        return [(len(gone),)]
    if sql == S.RT_FRESH_CLUSTERS_SQL:
        gone = [key for key in db.clusters if key[0] == gen]
        for key in gone:
            db.clusters.pop(key)
        return [(len(gone),)]
    if sql == S.RT_FRESH_CLUSTER_CONFLICTS_SQL:
        keep = [row for row in db.cluster_conflicts
                if (_jsonb(row.get("detail")) or {}).get("generation") != gen]
        gone = len(db.cluster_conflicts) - len(keep)
        db.cluster_conflicts[:] = keep
        return [(gone,)]
    if sql == S.RT_FRESH_RT_FP_SQL:
        gone = [key for key in db.rt_fp if key[0] == gen]
        for key in gone:
            db.rt_fp.pop(key)
        return [(len(gone),)]
    if sql == S.RT_FRESH_FP_KEY_SQL:
        gone = [row for row in db.fp_key if row[0] == gen]
        db.fp_key = {row for row in db.fp_key if row[0] != gen}
        return [(len(gone),)]
    if sql == S.RT_FRESH_BLOCK_CELL_SQL:
        gone = [key for key in db.cells if key[0] == gen]
        for key in gone:
            db.cells.pop(key)
        return [(len(gone),)]
    if sql == S.RT_FRESH_SCOPE_IDS_SQL:
        gone = [key for key in db.scope_ids if key[0] == gen]
        for key in gone:
            db.scope_ids.pop(key)
        return [(len(gone),)]
    if sql == S.RT_FRESH_SCOPE_SCAN_SQL:
        keep = [row for row in db.scope_scans if row["generation"] != gen]
        gone = len(db.scope_scans) - len(keep)
        db.scope_scans[:] = keep
        return [(gone,)]
    if sql == S.RT_FRESH_RETIRE_EVENT_SQL:
        keep = [row for row in db.retire_events if row["generation"] != gen]
        gone = len(db.retire_events) - len(keep)
        db.retire_events[:] = keep
        return [(gone,)]
    if sql == S.RT_FRESH_CURSORS_SQL:
        names = {str(name) for name in p["names"]}
        gone = [name for name in db.cursors if name in names]
        for name in gone:
            db.cursors.pop(name)
        return [(len(gone),)]
    if sql == S.RT_FRESH_LEASE_SQL:
        held = db.lease.get(str(p["name"]))
        if held is None or held["holder"] == p["holder"]:
            return [(0,)]
        db.lease.pop(str(p["name"]))
        return [(1,)]

    # ---------------------------------------------------------------- live equivalence (E99)
    if sql == S.RT_EQUIV_PAIRS_SQL:
        return [(lo, hi, row["score"], row["zone"], row["certificate"], row["decision"],
                 row["guard_veto"], row["families"], row.get("model_version"))
                for (g, lo, hi), row in sorted(db.pairs.items()) if g == gen]
    if sql == S.RT_EQUIV_MEMBERS_SQL:
        return sorted((key, listing_id) for g, key, listing_id in db.cluster_members
                      if g == gen)
    if sql == S.RT_EQUIV_SCOPE_IDS_SQL:
        return sorted((listing_id,) for (g, _block, listing_id) in db.scope_ids if g == gen)
    if sql == S.RT_EQUIV_CLOCK_FACTS_SQL:
        return [(int(i), row.get("first_seen_at"), row.get("last_seen_at"),
                 row.get("inactive_at"), bool(row.get("is_active", True)))
                for i in sorted(p["ids"])
                for row in [db.listings.get(int(i))] if row is not None]
    if sql == S.RT_EQUIV_PAIR_FEATURES_SQL:
        wanted = set(zip([int(v) for v in p["los"]], [int(v) for v in p["his"]]))
        return [(lo, hi, _jsonb(row.get("features")))
                for (g, lo, hi), row in sorted(db.pairs.items())
                if g == gen and (lo, hi) in wanted]
    if sql == S.RT_EQUIV_SCORE_TYPE_SQL:
        return [(db.score_column_type, 24 if db.score_column_type == "real" else 53)]
    if sql == S.RT_EQUIV_BATCH_SETTINGS_SQL:
        row = db.score_runs.get(gen)
        return [] if row is None else [(_jsonb(row.get("settings")),
                                        row.get("model_version"), db.now)]

    # ---------------------------------------------------------------- calibration
    if sql == S.RT_CALIBRATION_PRESENT_SQL:
        row = db.calibration.get(gen)
        return [] if row is None else [(gen, row["digest"], db.now)]
    if sql == S.RT_CALIBRATION_READ_SQL:
        row = db.calibration.get(gen)
        return [] if row is None else [(gen, row["digest"], row["n_listings"],
                                        _jsonb(row["payload"]), row["artifact_url"],
                                        _jsonb(row["settings"]), row["model_version"],
                                        db.now)]
    if sql == S.RT_CALIBRATION_WRITE_SQL:
        db.calibration[gen] = dict(p)
        return []

    # ---------------------------------------------------------------- cursors
    if sql == S.RT_CURSOR_READ_SQL:
        return [(name, row.get("last_listing_id"), row.get("last_snapshot_id"),
                 row.get("watermark"))
                for name, row in sorted(db.cursors.items()) if name in set(p["names"])]
    if sql == S.RT_CURSOR_WRITE_SQL:
        row = db.cursors.setdefault(str(p["name"]), {})
        for column, key in (("last_listing_id", "last_listing_id"),
                            ("last_snapshot_id", "last_snapshot_id"),
                            ("watermark", "watermark")):
            if p.get(key) is not None:
                row[column] = p[key]
        return []
    if sql == S.RT_CURSOR_SET_SQL:
        db.cursors[str(p["name"])] = {"last_listing_id": int(p["last_listing_id"])}
        return []
    if sql == S.RT_SEED_CURSORS_SQL:
        listing_ids = [int(i) for i in db.listings] or [0]
        flips = [(row["inactive_at"], i) for i, row in db.listings.items()
                 if row.get("inactive_at")]
        snapshot_ids = [int(row["id"]) for row in db.snapshots] or [0]
        watermark, watermark_id = max(flips) if flips else (db.now, 0)
        return [(max(listing_ids), max(snapshot_ids), watermark, watermark_id)]

    # ---------------------------------------------------------------- the public feeds
    if sql == S.RT_NEW_LISTINGS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        win = [(i, row["first_seen_at"]) for i, row in sorted(db.listings.items())
               if i > int(p["after_id"])
               and (row["first_seen_at"] is None or row["first_seen_at"] <= cut)
               ][:int(p["window"])]
        keep = [(i, stamp) for i, stamp in win if _in_scope(db, i, p)]
        return [(max((i for i, _s in win), default=int(p["after_id"])), len(win),
                 [i for i, _s in keep], [stamp for _i, stamp in keep])]
    if sql == S.RT_NEW_STRAGGLERS_SQL:
        # The last N ROWS by id, never an id range (W9d-3).
        win = sorted((i for i in db.listings if i <= int(p["after_id"])),
                     reverse=True)[:int(p["window"])]
        rows = [(i, db.listings[i]["first_seen_at"]) for i in sorted(win)
                if (gen, i) not in db.rt_fp and _in_scope(db, i, p)]
        return rows[:int(p["limit"])]
    if sql == S.RT_CHANGED_LISTINGS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        win = [(row["id"], row["listing_id"], row["scraped_at"])
               for row in sorted(db.snapshots, key=lambda r: r["id"])
               if row["id"] > int(p["after_id"]) and row["listing_id"] is not None
               and (row["scraped_at"] is None or row["scraped_at"] <= cut)
               ][:int(p["window"])]
        keep = [row for row in win if _in_scope(db, row[1], p)]
        return [(max((row[0] for row in win), default=int(p["after_id"])), len(win),
                 [row[1] for row in keep], [row[2] for row in keep],
                 [row[0] for row in keep])]
    if sql == S.RT_FLIPPED_LISTINGS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        after = p["after"]
        after_key = (datetime.min.replace(tzinfo=timezone.utc)
                     if after == "epoch" else after, int(p["after_id"]))
        win = sorted((row["inactive_at"], i) for i, row in db.listings.items()
                     if row.get("inactive_at") and row["inactive_at"] <= cut
                     and (row["inactive_at"], i) > after_key)[:int(p["window"])]
        keep = [(stamp, i) for stamp, i in win if _in_scope(db, i, p)]
        end_stamp, end_id = win[-1] if win else (None, 0)
        return [(end_stamp, end_id, len(win),
                 [i for _s, i in keep], [stamp for stamp, _i in keep])]
    # The fifth feed: this generation's own rows the scope no longer holds (E79).
    if sql == S.RT_SCOPE_DRIFT_SQL:
        slice_ids = sorted(i for g, i in db.rt_fp
                           if g == gen and i > int(p["after_id"]))[:int(p["limit"])]
        departed = [i for i in slice_ids if not _in_scope(db, i, p)]
        return [(max(slice_ids) if slice_ids else int(p["after_id"]),
                 len(slice_ids), departed)]
    # The sixth feed (W9d-3), as W9e pays for it (R3): the wide block walk on `public` is a
    # cadence-driven REFRESH of the snapshot, and an ordinary pass claims out of the snapshot.
    if sql == S.RT_SCOPE_BLOCK_SQL:
        obec, cast_obce = p["obec"], p.get("cast_obce")
        found = sorted(
            listing_id for listing_id, place in db.locations.items()
            if place.get("obec_kod") == obec
            and (cast_obce is None or place.get("cast_obce_kod") == cast_obce))
        return [(i, db.locations[i].get("resolved_at")) for i in found[:int(p["limit"])]]
    if sql == S.RT_SCOPE_IDS_WRITE_SQL:
        for listing_id, resolved in zip(p["listing_ids"], p["resolved"]):
            # The column is `timestamptz`, so psycopg reads it back as a datetime however the
            # INSERT spelled it.
            db.scope_ids[(gen, str(p["block_key"]), int(listing_id))] = {
                "resolved_at": (datetime.fromisoformat(resolved)
                                if isinstance(resolved, str) else resolved),
                "refreshed_at": db.now}
        return []
    if sql == S.RT_SCOPE_IDS_PRUNE_SQL:
        keep = set(int(i) for i in p["listing_ids"])
        db.scope_ids = {key: row for key, row in db.scope_ids.items()
                        if not (key[0] == gen and key[1] == str(p["block_key"])
                                and key[2] not in keep)}
        return []
    if sql == S.RT_SCOPE_IDS_DELETE_SQL:
        gone = set(int(i) for i in p["ids"])
        db.scope_ids = {key: row for key, row in db.scope_ids.items()
                        if not (key[0] == gen and key[2] in gone)}
        return []
    if sql == S.RT_SCOPE_IDS_PRUNE_BLOCKS_SQL:
        keep = set(str(b) for b in p["block_keys"])
        db.scope_ids = {key: row for key, row in db.scope_ids.items()
                        if not (key[0] == gen and key[1] not in keep)}
        return []
    if sql == S.RT_SCOPE_ENTRANTS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        rows = sorted((listing_id, row["resolved_at"])
                      for (g, _block, listing_id), row in db.scope_ids.items()
                      if g == gen and listing_id > int(p["after_id"])
                      and (row["resolved_at"] is None or row["resolved_at"] <= cut)
                      and (gen, listing_id) not in db.rt_fp)
        return rows[:int(p["limit"])]
    if sql == S.RT_SCOPE_SCAN_STATE_SQL:
        out: dict[str, list[Any]] = {}
        window = db.now - timedelta(hours=int(p["hours"]))
        for row in db.scope_scans:
            if row["generation"] != gen:
                continue
            state = out.setdefault(row["block_key"], [None, 0])
            last = state[0]
            if last is None or row["scanned_at"] > last:
                state[0] = row["scanned_at"]
            if row["scanned_at"] > window:
                state[1] += 1
        return [(block, (db.now - last).total_seconds(), scans)
                for block, (last, scans) in sorted(out.items())]
    if sql == S.RT_SCOPE_SCAN_SEEN_SQL:
        return sorted({(row["block_key"],) for row in db.scope_scans
                       if row["generation"] == gen})
    if sql == S.RT_SCOPE_BACKLOG_SQL:
        return [(sum(1 for (g, _block, listing_id) in db.scope_ids
                     if g == gen and (gen, listing_id) not in db.rt_fp),)]
    if sql == S.RT_SCOPE_SCAN_WRITE_SQL:
        db.scope_scans.append({"generation": gen, "block_key": str(p["block_key"]),
                               "scanned_at": db.now, "rows_found": int(p["rows_found"]),
                               "elapsed_ms": p["elapsed_ms"]})
        return []
    # The rolling-day retirement window (W9e/R2).
    if sql == S.RT_RETIRE_WINDOW_SQL:
        window = db.now - timedelta(hours=int(p["hours"]))
        rows = sorted((row["retired_at"], row["store_rows"], row["n_retired"])
                      for row in db.retire_events
                      if row["generation"] == gen and row["retired_at"] > window)
        return [(sum(row[2] for row in rows), rows[0][1] if rows else None)]
    if sql == S.RT_RETIRE_EVENT_WRITE_SQL:
        db.retire_events.append({"generation": gen, "retired_at": db.now,
                                 "n_retired": int(p["n_retired"]),
                                 "store_rows": int(p["store_rows"])})
        return []
    if sql == S.RT_SCOPE_PARENT_OBEC_SQL:
        return [(int(code), int(db.admin_parents[int(code)])) for code in p["codes"]
                if int(code) in db.admin_parents]
    if sql == S.RT_REVIVED_SQL:
        slice_ids = sorted(i for g, i in db.rt_fp
                           if g == gen and not db.rt_fp[(g, i)]["is_active"]
                           and i > int(p["after_id"]))[:int(p["limit"])]
        revived = [i for i in slice_ids if db.listings.get(i, {}).get("is_active")
                   and _in_scope(db, i, p)]
        return [(max(slice_ids) if slice_ids else int(p["after_id"]),
                 len(slice_ids), revived)]

    raise AssertionError(f"FakePg does not know this statement:\n{sql}")
