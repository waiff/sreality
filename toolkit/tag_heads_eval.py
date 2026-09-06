"""Read-only diagnostics for a trained tag head: why did it get THAT one wrong?

Precision and recall say how often a head is wrong. They never say why, and "why"
is what a human needs before deciding a head is broken rather than the labels
being thin. So for each out-of-fold mistake this finds its nearest labeled
neighbor among the images that genuinely carry the class the head predicted, over
L2-normalized embeddings (faiss IndexFlatIP; on unit vectors inner product IS
cosine). A false positive whose nearest genuine positive is a near-identical photo
is a different problem from one that resembles nothing in the set.

THE BOUNDARY, stated once and enforced by shape. Nothing here may influence
training, precision/recall, or a threshold:

  * `diagnose_head` takes an ALREADY-TRAINED head and copies its metrics through
    verbatim; it recomputes nothing and can change nothing.
  * `HeadMetrics` is a frozen dataclass with a closed field list — a diagnostic
    cannot be attached to it even by accident.
  * the output lives in `EvalReport.diagnostics`, a separate field of a separate
    object, and no function in `toolkit.tag_heads` reads it.

faiss-cpu is the `analysis` extra (`pip install -e ".[analysis]"`) and is imported
lazily: a machine that only trains and scores never needs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from toolkit.tag_heads import DatasetSnapshot, HeadMetrics, OofPrediction, TrainedHead

DEFAULT_DIAGNOSTIC_LIMIT = 50


@dataclass(frozen=True)
class NeighborDiagnostic:
    """One mistake, and the labeled image that best explains it."""
    image_id: int
    listing_id: int
    label: int                  # what it truly is
    predicted: int              # what the head said
    kind: str                   # "false_positive" | "false_negative"
    score: float
    neighbor_image_id: int
    neighbor_listing_id: int
    neighbor_label: int
    similarity: float


@dataclass(frozen=True)
class EvalReport:
    """Final metrics, unchanged, plus analysis that is not part of them."""
    tag_id: int
    dataset_hash: str
    metrics: HeadMetrics
    diagnostics: tuple[NeighborDiagnostic, ...]


def mistakes(oof: Sequence[OofPrediction]) -> list[OofPrediction]:
    """Out-of-fold rows the head got wrong, worst first — a false positive the head
    was most confident about is the most informative one to look at."""
    wrong = [p for p in oof if p.predicted != p.label]
    wrong.sort(key=lambda p: abs(p.score - 0.5), reverse=True)
    return wrong


def _normalize(vec: Sequence[float]) -> list[float]:
    norm = sum(float(x) * float(x) for x in vec) ** 0.5
    if norm == 0.0:
        return [0.0] * len(vec)
    return [float(x) / norm for x in vec]


def nearest_opposite_neighbors(
    snapshot: DatasetSnapshot, wrong: Sequence[OofPrediction], *,
    limit: int = DEFAULT_DIAGNOSTIC_LIMIT,
) -> tuple[NeighborDiagnostic, ...]:
    """For each mistake, the closest image that genuinely IS what the head said.

    A false positive is matched against the true positives, a false negative
    against the true negatives — the pool whose true label equals the mistake's
    PREDICTED label, which is exactly the "what did it think it was looking at"
    question. The mistake itself is never its own neighbor.
    """
    if not wrong or not snapshot.rows:
        return ()
    import faiss  # noqa: PLC0415 — analysis-only extra, never a runtime import
    import numpy as np

    by_id = {r.image_id: r for r in snapshot.rows}
    out: list[NeighborDiagnostic] = []

    for target_label in (0, 1):
        batch = [p for p in wrong[:limit] if p.predicted == target_label]
        if not batch:
            continue
        pool = [r for r in snapshot.rows if r.label == target_label]
        if not pool:
            continue
        matrix = np.asarray([_normalize(r.embedding) for r in pool], dtype="float32")
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        queries = np.asarray(
            [_normalize(by_id[p.image_id].embedding) for p in batch], dtype="float32")
        # k=2 so a query that is itself in the pool can drop its own row; a
        # mistake predicted 1 whose true label is 1 does not exist by definition,
        # but the guard costs nothing and keeps the function honest if the caller
        # ever passes a non-mistake.
        k = 2 if len(pool) > 1 else 1
        sims, idx = index.search(queries, k)
        for p, sim_row, idx_row in zip(batch, sims, idx):
            for sim, j in zip(sim_row, idx_row):
                if j < 0:
                    continue
                neighbor = pool[int(j)]
                if neighbor.image_id == p.image_id:
                    continue
                out.append(NeighborDiagnostic(
                    image_id=p.image_id, listing_id=p.listing_id, label=p.label,
                    predicted=p.predicted,
                    kind="false_positive" if p.label == 0 else "false_negative",
                    score=p.score,
                    neighbor_image_id=neighbor.image_id,
                    neighbor_listing_id=neighbor.listing_id,
                    neighbor_label=neighbor.label,
                    similarity=float(sim),
                ))
                break

    out.sort(key=lambda d: d.similarity, reverse=True)
    return tuple(out)


def diagnose_head(
    head: TrainedHead, *, limit: int = DEFAULT_DIAGNOSTIC_LIMIT,
) -> EvalReport:
    """Attach neighbor diagnostics to a head whose numbers are already final.

    `head.metrics` is passed straight through — this function has no path by which
    it could recompute or adjust a metric, which is the point.
    """
    return EvalReport(
        tag_id=head.snapshot.tag_id,
        dataset_hash=head.snapshot.dataset_hash,
        metrics=head.metrics,
        diagnostics=nearest_opposite_neighbors(
            head.snapshot, mistakes(head.oof), limit=limit),
    )
