"""The in-memory twins of the real-time lane's three inputs — the harness's substrate (SW1).

`MemoryStore` implements `incremental.Store`, `CohortFacts` the `FactSource` and `Schedule` the
`WorkSource`, so `harness run` drives the SAME `run_pass` production does: only the verbs that
touch Postgres differ, and a twin that drifts from its protocol fails to import.

It is NOT the only store the proof covers: `tests/autodedup/test_incremental_sqlstore.py`
replays the same cohort through `incremental_lane.SqlStore` against a Postgres fake and asserts
the two reach the same state, because every one of W9's four blocking defects lived in the SQL
adapter and none of them could be seen from here.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from dataclasses import replace

from autodedup.dataset import Dataset, Image, Listing
from autodedup.hazard_context import ContextStamp, address_block_key, category_group
from autodedup.incremental import (
    SET_CAP,
    CellRow,
    FpRow,
    PairRow,
    WorkItem,
    key_token,
)
from autodedup.store_score import PAIR_SCORE_SQL_TYPE, narrow


def shape_token(listing: Listing) -> str:
    """The `_shape_count` grain, flattened to a token so the capped set can hold it."""
    area = listing.area_m2
    bucket = None if area is None or area <= 0 else round(float(area), 1)
    return key_token((listing.disposition, bucket))


class MemoryStore:
    def __init__(self, now: float | None = None,
                 score_sql_type: str = PAIR_SCORE_SQL_TYPE) -> None:
        # The twin's clock, for `first_decided_at` (E92). Production reads the server's; a
        # replay that never sets one leaves the stamp NULL, which is what keeps the plain
        # replay byte-for-byte what it was before the evidence horizon existed.
        self.now = now
        # E116: the twin narrows a score exactly as `autodedup.pairs.score` does, so the
        # replay proof covers the STORE rather than an idealisation of it. Under the shipped
        # `double precision` this is the identity; passing `real` reproduces the pre-541
        # column, which is how W9m's regression arm shows the proof would now catch E114.
        self.score_sql_type = score_sql_type
        self.postings: dict[tuple[str, str], list[int]] = {}
        self.keys: dict[int, list[tuple[str, str]]] = {}
        self.fp: dict[int, FpRow] = {}
        self.pairs: dict[tuple[int, int], PairRow] = {}
        self.clusters: dict[int, list[int]] = {}
        self.cluster_row: dict[int, dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.mnl: set[tuple[int, int]] = set()
        self.ml: set[tuple[int, int]] = set()
        # G4 (E920): the listings a ruling changed since the last pass names — the SQL store's
        # `rt_rulings` cursor. Whoever changes a ruling adds its listings; `flush` (the end of
        # the pass that read them) consumes what was read.
        self.ruled: set[int] = set()
        self._ruled_read: set[int] = set()
        self.cell: dict[tuple[str, str], CellRow] = {}
        self._merge_adj: dict[int, set[int]] = {}

    # ---------------------------------------------------------------- postings and rows
    def lookup(self, probe: str, token: str) -> list[int]:
        return list(self.postings.get((probe, token), ()))

    def lookup_many(self, keys: Sequence[tuple[str, str]]
                    ) -> dict[tuple[str, str], list[int]]:
        return {key: list(self.postings[key]) for key in keys if key in self.postings}

    def put_listing(self, listing_id: int, row: FpRow,
                    keys: Sequence[tuple[str, str]]) -> None:
        self._drop_keys(listing_id)
        # `first_decided_at` is the STORE's to stamp and never a pass's to fabricate, exactly
        # as `RT_FP_UPSERT_SQL` coalesces it server-side: a refresh keeps the first stamp.
        held = self.fp.get(listing_id)
        stamped = (held.first_decided_at if held is not None and held.first_decided_at
                   is not None else row.first_decided_at)
        if stamped is None:
            stamped = self.now
        self.fp[listing_id] = replace(row, first_decided_at=stamped)
        self.keys[listing_id] = list(keys)
        for key in keys:
            bucket = self.postings.setdefault(key, [])
            # Postings stay ascending: the cohort pass posts in sorted listing-id order and the
            # fan-out cap fills in that order, so an out-of-order insert would change retrieval.
            position = len(bucket)
            while position and bucket[position - 1] > listing_id:
                position -= 1
            bucket.insert(position, listing_id)

    def _drop_keys(self, listing_id: int) -> None:
        for key in self.keys.pop(listing_id, ()):
            bucket = self.postings.get(key)
            if bucket and listing_id in bucket:
                bucket.remove(listing_id)
                if not bucket:
                    self.postings.pop(key, None)

    def drop_listing(self, listing_id: int) -> None:
        self._drop_keys(listing_id)
        self.fp.pop(listing_id, None)
        for pair in [p for p in self.pairs if listing_id in p]:
            self.delete_pairs([pair])

    def keys_many(self, ids: Iterable[int]) -> dict[int, list[tuple[str, str]]]:
        return {i: list(self.keys.get(i, ())) for i in ids}

    def rows(self, ids: Iterable[int]) -> dict[int, FpRow]:
        return {i: self.fp[i] for i in ids if i in self.fp}

    def known(self, ids: Iterable[int]) -> set[int]:
        return {i for i in ids if i in self.fp}

    # ------------------------------------------------------------------------ pair grain
    def pairs_touching(self, ids: Iterable[int]) -> dict[tuple[int, int], PairRow]:
        wanted = set(ids)
        return {key: row for key, row in self.pairs.items()
                if key[0] in wanted or key[1] in wanted}

    def pairs_within(self, members: Iterable[int]) -> list[PairRow]:
        inside = set(members)
        return [row for key, row in sorted(self.pairs.items())
                if key[0] in inside and key[1] in inside]

    def merge_neighbours(self, ids: Iterable[int]) -> dict[int, set[int]]:
        return {i: set(self._merge_adj.get(i, ())) for i in ids}

    def upsert_pairs(self, rows: Sequence[PairRow]) -> None:
        for row in rows:
            stored = replace(row, score=narrow(row.score, self.score_sql_type))
            self.pairs[(stored.lo, stored.hi)] = stored
            self._reindex(stored)

    def delete_pairs(self, keys: Sequence[tuple[int, int]]) -> None:
        for key in keys:
            row = self.pairs.pop(key, None)
            if row is not None:
                self._unlink(row.lo, row.hi)

    def _reindex(self, row: PairRow) -> None:
        if row.zone == "merge":
            self._merge_adj.setdefault(row.lo, set()).add(row.hi)
            self._merge_adj.setdefault(row.hi, set()).add(row.lo)
        else:
            self._unlink(row.lo, row.hi)

    def _unlink(self, lo: int, hi: int) -> None:
        self._merge_adj.get(lo, set()).discard(hi)
        self._merge_adj.get(hi, set()).discard(lo)

    # --------------------------------------------------------------------- cluster grain
    def clusters_touching(self, members: Iterable[int]) -> dict[int, list[int]]:
        inside = set(members)
        return {key: list(rows) for key, rows in self.clusters.items()
                if inside & set(rows)}

    def write_clusters(self, drop_keys: Sequence[int], rows: Sequence[Mapping[str, Any]],
                       conflicts: Sequence[Mapping[str, Any]]) -> None:
        for key in drop_keys:
            self.clusters.pop(key, None)
            self.cluster_row.pop(key, None)
        for row in rows:
            key = int(row["cluster_key"])
            self.clusters[key] = list(row["members"])
            self.cluster_row[key] = dict(row)
        # `RT_CONFLICT_DROP_SQL`, exactly: a conflict goes only when BOTH its ends are members
        # of a re-written cluster row. One with a singleton end is re-inserted beside itself on
        # the next re-cluster — the live defect the twin must show, not hide (SW1 review).
        touched = {int(i) for row in rows for i in row["members"]}
        self.conflicts = [c for c in self.conflicts
                          if not (int(c["lo"]) in touched and int(c["hi"]) in touched)]
        # `RT_CLOSURE_CONFLICT_APPEND_SQL`: a dissolved closure (E926) is re-read every pass
        # its contradiction stands, so it is appended only when it differs from the newest
        # record of its generation sharing an advert with it.
        for conflict in map(dict, conflicts):
            if conflict.get("must_link") and conflict == next(
                    (held for held in reversed(self.conflicts)
                     if held.get("must_link") and held["generation"] == conflict["generation"]
                     and set(held["members"]) & set(conflict["members"])), None):
                continue
            self.conflicts.append(conflict)

    def must_not_link(self) -> set[tuple[int, int]]:
        return set(self.mnl)

    def must_link(self) -> set[tuple[int, int]]:
        return set(self.ml)

    def rulings_changed(self) -> set[int]:
        self._ruled_read = set(self.ruled)
        return set(self.ruled)

    # ---------------------------------------------------------------------- live census
    def cells(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], CellRow]:
        return {key: self.cell[key] for key in keys if key in self.cell}

    def bump_cell(self, listing: Listing) -> None:
        key = (address_block_key(listing), category_group(listing))
        row = self.cell.setdefault(key, CellRow(key[0], key[1]))
        row.n_listings += 1
        for bucket, value in (
            (row.shapes, shape_token(listing)),
            (row.brokers, listing.broker_key),
            (row.source_ids, listing.source_id_native),
        ):
            if not value or value in bucket:
                continue
            if len(bucket) >= SET_CAP:
                row.capped = True
                continue
            bucket.append(value)

    def unbump_cell(self, cell: tuple[str, str]) -> None:
        row = self.cell.get(tuple(cell))
        if row is not None and row.n_listings > 0:
            row.n_listings -= 1

    def stamped_merges(self, blocks: Sequence[str]
                       ) -> list[tuple[int, int, ContextStamp, str, bool]]:
        wanted = set(blocks)
        out: list[tuple[int, int, ContextStamp, str, bool]] = []
        for row in self.pairs.values():
            if row.zone != "merge":
                continue
            block = str(row.context.get("block") or "")
            if block not in wanted:
                continue
            stamp = ContextStamp.from_evidence(row.evidence)
            if stamp is None:
                continue
            out.append((row.lo, row.hi, stamp, block, bool(row.certificate)))
        return out

    def flush(self) -> None:
        """Nothing is buffered here — the SQL store's write buffer is what this verb is for —
        but the pass that read the changed rulings has now honoured them (G4)."""
        self.ruled -= self._ruled_read
        self._ruled_read = set()


class CohortFacts:
    """The FactSource over an exported cohort. A `withheld` listing's photographs are handed over
    unprocessed — `phash`, `pop`, `clip` and `tags` stripped — which is the gallery ~80 % of
    arrivals are first decided on (E92/E93); emptying `withheld` is the producers arriving."""

    def __init__(self, ds: Dataset, withheld: Iterable[int] = ()) -> None:
        self.ds = ds
        self.withheld = frozenset(withheld)

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        out: dict[int, tuple[Listing, list[Image]]] = {}
        for listing_id in ids:
            listing = self.ds.listings.get(listing_id)
            if listing is None:
                continue
            images = self.ds.images(listing_id)
            if listing_id in self.withheld:
                images = [replace(image, phash=None, pop=None, clip=None, tags=[])
                          for image in images]
            out[listing_id] = (listing, images)
        return out


class Schedule:
    """The WorkSource: a fixed claim order instead of the watermark feeds. A claim advances
    nothing and a commit advances over exactly what it is handed, so a pass the pair budget
    refused (E75) re-claims the same listings, as production's feeds do. `redecide` is the
    evidence sweep (E92): the same ids handed back to be re-decided whatever their digest."""

    def __init__(self, order: Sequence[int], redecide: bool = False) -> None:
        self.order = list(order)
        self.redecide = redecide
        self.cursor = 0

    def claim(self, limit: int) -> list[WorkItem]:
        feed = "evidence" if self.redecide else "new"
        return [WorkItem(listing_id, feed, None, position, redecide=self.redecide)
                for position, listing_id in enumerate(
                    self.order[self.cursor:self.cursor + limit], start=self.cursor + 1)]

    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]:
        positions = [int(item.cursor) for item in done if item.cursor is not None]
        if positions:
            self.cursor = max(positions)
        return {"done": self.cursor, "total": len(self.order)}

    def exhausted(self) -> bool:
        return self.cursor >= len(self.order)
