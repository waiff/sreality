"""What `autodedup.pairs.score` can hold — and why the clustering depends on it (E115).

`cluster.edge_rank` orders a component's merge edges `(certificate first, -score, lo, hi)`, so
the score is not only a report: it is a SORT KEY, and the sort decides which union a
non-transitive invariant (`floor_spread`, the size cap, must-not-link) gets to refuse. The
batch lane ranks the float64 `Decision`s in the process that computed them; the real-time lane
ranks `Decision`s rebuilt from stored rows. The two agree only while the store keeps the
number the ranking reads.

Until migration 541 it did not. The column was `real` — float4, 24 bits — and on the W9l
comparison **130 distinct float4 values carried 4,918 merge edges**, 4,800 of them in a bucket
holding more than one float64 score: for 97.6% of the edges `-score` tied and `edge_rank` fell
through to `(lo, hi)`. The same decisions clustered to 903 groups in memory and 900 through the
store, with 74/71 member sets one-sided (M175, M176). Nothing in the in-memory replay could see
it, because `MemoryStore` kept the `PairRow` verbatim — which is the other half of the repair:
the twin narrows exactly as the column does, so the proof covers the store path rather than
stopping at it.

One constant says which type the column is, and `MemoryStore`, the replay and the tests all
read it here.
"""

from __future__ import annotations

import struct
from typing import Any, Mapping

# The declared type of `autodedup.pairs.score`, as migration 541 leaves it. The rail that keeps
# this honest is `tests/autodedup/test_store_precision.py`, which reads the migrations.
PAIR_SCORE_SQL_TYPE: str = "double precision"

# The relative resolution of each type at 1.0 — what one unit in the last place costs there.
# float4 carries 24 bits of significand, float8 carries 53.
_EPS: dict[str, float] = {"real": 2.0 ** -23, "double precision": 2.0 ** -52}


def store_eps(sql_type: str = PAIR_SCORE_SQL_TYPE) -> float:
    """The relative resolution the store keeps at a score near 1.0."""
    try:
        return _EPS[sql_type]
    except KeyError:
        raise ValueError(f"unknown score column type {sql_type!r}") from None


def narrow(score: float, sql_type: str = PAIR_SCORE_SQL_TYPE) -> float:
    """`score` as the column would hand it back — the identity under `double precision`.

    A pass that writes a float64 into a `real` column reads back the nearest float4, which is
    what `struct.pack('f', ...)` does, so a store round trip is spelled once here rather than
    imagined at each call site."""
    if sql_type == "double precision":
        return float(score)
    if sql_type != "real":
        raise ValueError(f"unknown score column type {sql_type!r}")
    return float(struct.unpack("f", struct.pack("f", float(score)))[0])


def storable(row: Mapping[str, Any], store_floor: float) -> bool:
    """What every store keeps of a decided pair: the merge and band zones whatever they scored,
    any row that carries evidence (E61's designator veto names its two units there), and the
    reject tail at or above `store_floor`. ONE predicate for the harness, the score lane and the
    real-time store, because the D43 cluster relation reads its feature slots off exactly these
    rows (F2): a store that kept a different set would cluster a different relation."""
    if str(row.get("zone") or "") in ("merge", "band") or row.get("evidence"):
        return True
    try:
        return float(row.get("score") or 0.0) >= store_floor
    except (TypeError, ValueError):
        return False


def is_lossless(sql_type: str = PAIR_SCORE_SQL_TYPE) -> bool:
    """Can the column carry the float64 the engine ranks on? Only `double precision` can."""
    return sql_type == "double precision"
