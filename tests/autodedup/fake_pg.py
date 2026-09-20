"""A Postgres stand-in for the real-time lane's own statements — the rail the W9 verification asked for.

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
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from autodedup import incremental_sql as S
from autodedup.decide import CERTIFICATES
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
        # `public`, read-only: what the four feeds page over.
        self.listings: dict[int, dict[str, Any]] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.transactions = 0
        self.rolled_back = 0

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


class _Tx:
    def __init__(self, conn: FakePg) -> None:
        self.conn = conn
        self.state: dict[str, Any] | None = None

    def __enter__(self) -> "_Tx":
        self.conn.transactions += 1
        self.state = self.conn.snapshot()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
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
        self.rows = _dispatch(self.conn, sql, dict(params or {}))


def _dispatch(db: FakePg, sql: str, p: Mapping[str, Any]) -> list[tuple]:  # noqa: C901
    gen = str(p.get("generation") or "")

    if sql == S.RT_STORE_PRESENT_SQL:
        return [(True,)]
    if sql in (S.RT_STATEMENT_GUARD_SQL, S.RT_IDLE_GUARD_SQL):
        return [("set",)]
    if sql == S.RT_SETTING_SQL:
        key = str(p["key"])
        return [(db.settings[key],)] if key in db.settings else []

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
        db.rt_fp[(gen, int(p["listing_id"]))] = {
            "category_main": p["category_main"], "category_type": p["category_type"],
            "area_m2": p["area_m2"], "disposition": p["disposition"], "floor": p["floor"],
            "fp_digest": p["fp_digest"], "cell_key": p["cell_key"],
            "cell_group": p["cell_group"], "is_active": p["is_active"],
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
            out.append((int(listing_id), row["category_main"], row["category_type"],
                        row["area_m2"], row["disposition"], row["floor"], row["fp_digest"],
                        row["cell_key"], row["cell_group"], row["is_active"]))
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
            rows.append((lo, hi, list(row["probes"]), row["from_lo"], row["from_hi"],
                         row["score"], row["zone"], row["decision"], row["guard_veto"],
                         row["families"], row["certificate"], row["evidence"],
                         row["context"], row["fp_lo"], row["fp_hi"]))
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

    if sql == S.RT_MUST_NOT_LINK_SQL:
        return sorted(db.mnl)

    # ---------------------------------------------------------------- calibration
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
        rows = [(i, row["first_seen_at"]) for i, row in sorted(db.listings.items())
                if i > int(p["after_id"])
                and (row["first_seen_at"] is None or row["first_seen_at"] <= cut)]
        return rows[:int(p["limit"])]
    if sql == S.RT_NEW_STRAGGLERS_SQL:
        low = int(p["after_id"]) - int(p["window"])
        rows = [(i, row["first_seen_at"]) for i, row in sorted(db.listings.items())
                if low < i <= int(p["after_id"]) and (gen, i) not in db.rt_fp]
        return rows[:int(p["limit"])]
    if sql == S.RT_CHANGED_LISTINGS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        rows = [(row["id"], row["listing_id"], row["scraped_at"])
                for row in sorted(db.snapshots, key=lambda r: r["id"])
                if row["id"] > int(p["after_id"]) and row["listing_id"] is not None
                and (row["scraped_at"] is None or row["scraped_at"] <= cut)]
        return rows[:int(p["limit"])]
    if sql == S.RT_FLIPPED_LISTINGS_SQL:
        cut = db.now - timedelta(seconds=int(p["lag"]))
        after = p["after"]
        after_key = (datetime.min.replace(tzinfo=timezone.utc)
                     if after == "epoch" else after, int(p["after_id"]))
        rows = sorted((row["inactive_at"], i) for i, row in db.listings.items()
                      if row.get("inactive_at") and row["inactive_at"] <= cut
                      and (row["inactive_at"], i) > after_key)
        return [(i, stamp) for stamp, i in rows[:int(p["limit"])]]
    if sql == S.RT_REVIVED_SQL:
        slice_ids = sorted(i for g, i in db.rt_fp
                           if g == gen and not db.rt_fp[(g, i)]["is_active"]
                           and i > int(p["after_id"]))[:int(p["limit"])]
        revived = [i for i in slice_ids if db.listings.get(i, {}).get("is_active")]
        return [(max(slice_ids) if slice_ids else int(p["after_id"]),
                 len(slice_ids), revived)]

    raise AssertionError(f"FakePg does not know this statement:\n{sql}")
