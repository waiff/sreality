"""`--mode rt_equivalence` — READ-ONLY. Is the LIVE store the batch engine's store? (E99)

The check the replay-equivalence proof cannot give. Replay feeds the incremental path from an
export ARTIFACT and compares it with the batch engine run over that same artifact, in one
process, from an EMPTY store: it proves the MECHANISM — same decisions, same clusters, same
edge order — and it has proved it four times. It says nothing about the rows a real generation
actually holds, and three times now that was exactly where the defect was: W9f's first live
pass issued ZERO K-C against the batch generation's 1,771 (E90), the 2026-09-20 re-seed left
15,923 pairs of a superseded scorer in place (E97), and W9l found that the store could not
carry the number the clustering ranks on (E114).

So this mode reads BOTH stores and diffs them. Given a live generation and a batch generation
scored on the SAME export with the same settings and model, inside the live generation's own
scope, it compares the pairs, the clusters and the store itself — and **attributes every
difference to a named cause**, because a difference nobody can name is the only kind worth
failing on (E119).

  * **Defects** (E120) are asymmetries in the instrument or the store rather than in the
    engine: a column one lane writes and the other never does, two generations built under
    different clock rules, a score column that cannot hold the number `cluster.edge_rank`
    ranks on. Each one makes the comparison lie, so each one fails the verdict by itself.
  * **`score_not_from_vector`** (E121) is the defect at PAIR grain, and it is asked of every
    row this mode reads a vector for, on BOTH sides, before anything is attributed:
    `decide_pair` sets `score = model.predict_proba(feats)` in every branch but the veto, so
    a stored score its own stored vector does not reproduce was not written by this engine and
    NO cause may excuse it. Without it the attribution below reads green over a real decision
    bug that happens to ride on a drifted listing — demonstrated, twice, on the shipped code.
  * **Shared pairs** that differ are attributed by reading WHICH FEATURES moved:
    `drifted_since_export` (the clock features moved and the live value is the one that
    matches the facts as they stand now), `calibration_cohort` (the corpus-frequency features
    moved, which two differently-cut calibrations always do), `clock_anchor` (the two
    generations do not agree what the window's end is) — and `unexplained`.
  * **One-sided pairs** keep their own causes, now reported PER SIDE: `scope`,
    `not_in_live_store`, `arrival_after_export`, `retention`, `store_floor`, `unexplained`.
  * **Clusters** are compared component by component: a component whose two sides hold
    DIFFERENT merge edges is `upstream_pair` (the pair grain already accounted for it), one
    holding a listing the batch cohort never had is `arrival`, and one where both sides hold
    the SAME edges and partition them differently is `unexplained` — which is the shape E114
    wore, and the reason this attribution exists at all.

The verdict fails on `unexplained` and on defects. It does not fail on a difference that has
been named and evidenced, because a check that cannot tell drift from a bug is a description.

It writes NOTHING — not a `public` row, not an `autodedup` row, not an `iterations` row. Its
deliverable is `out/rt_equivalence.json`.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from autodedup.features import (
    CALIBRATION_FEATURES,
    CLOCK_FEATURES,
    ClockFacts,
    WindowRule,
    clock_features,
)
from autodedup.harness import model_of_version
from autodedup.incremental import EVIDENCE_HOLD_REASON, GENERATION
from autodedup.judge_lane import download_cohort
from autodedup.incremental_lane import (
    EVIDENCE_HORIZON_HOURS,
    SCOPE_SETTING,
    lane_settings,
    read_scope_setting,
    resolve_scope_parents,
    scope_setting_key,
)
from autodedup.incremental_scope import ScopeError, resolve_scope
from autodedup.incremental_sql import (
    RT_CALIBRATION_READ_SQL,
    RT_EQUIV_BATCH_SETTINGS_SQL,
    RT_EQUIV_CLOCK_FACTS_SQL,
    RT_EQUIV_EXPORT_WINDOW_SQL,
    RT_EQUIV_HOLDS_SQL,
    RT_EQUIV_MEMBERS_SQL,
    RT_EQUIV_PAIR_FEATURES_SQL,
    RT_EQUIV_PAIRS_SQL,
    RT_EQUIV_SCOPE_IDS_SQL,
    RT_EQUIV_SCORE_TYPE_SQL,
    RT_KNOWN_SQL,
    RT_SCOPE_BLOCK_SQL,
)
from autodedup.store_score import PAIR_SCORE_SQL_TYPE, is_lossless, narrow, store_eps

EQUIVALENCE_FILE: str = "rt_equivalence.json"
MAX_EXAMPLES: int = 8
# The largest score movement tolerated on an UNEXPLAINED pair. An attributed one is allowed to
# move as far as its cause carries it: the 2026-09-21 comparison's largest was 0.3924, and all
# of it was eleven listings whose delisting self-healed between the export and the pass (M173).
SCORE_ONLY_MAX: float = 0.05
# What "the same score" means. The two sides compute the same features with the same model in
# the same process-local float64, so a difference above this is a difference in the INPUTS, not
# in the arithmetic — which is the whole point of the comparison.
SCORE_TOL: float = 1e-6
# ...but never below the STORE's own resolution. `tol = 1e-6` is ~16 float4 ULPs, and on the
# 2026-09-21 comparison 121 shared pairs already differed by more than 0 and at most the
# tolerance (M178): the counter sat on a knife edge. The effective tolerance is therefore
# raised to this many units in the score column's last place, and the report says which was
# used. Under `double precision` the floor is ~7e-15 and `SCORE_TOL` wins.
TOL_STORE_ULPS: int = 32
# The store floor the comparison falls back to when the generation's calibration row carries no
# settings blob. It is the shipped default and it is only ever a fallback: a floor read from the
# row is the floor the generation was actually written under.
DEFAULT_STORE_FLOOR: float = 0.02
# How many listing ids one `public.listings` read carries.
FACTS_CHUNK: int = 5_000
# The decision fields a pair is compared on. `certificate` is one of them because
# `cluster.edge_rank` reads it FIRST (migration 539), which is also why a lane that does not
# write it is a defect rather than a disagreement.
DECISION_FIELDS: tuple[str, ...] = ("zone", "certificate", "guard_veto")
# How a decision string names the certificate it carried. `decide` writes
# `certificate:K-C[:...]`, and E63 can re-promote a certified pair under a `context_rule:`
# reason — which is why the string is the WITNESS that a column should have been written and
# never the definition of what it should hold.
CERTIFICATE_PREFIX: str = "certificate:"
# E121. The one relation between a stored pair's own columns that holds BY CONSTRUCTION:
# `decide._decide_layers` sets `score = model.predict_proba(feats)` in every branch but the two
# vetoes, and `apply_context_rule` carries the score through untouched. So a stored score its
# own stored vector does not reproduce is a defect of that ROW, whatever else moved on the
# pair. Measured before it was shipped: 46,688 non-veto pairs of the export cohort re-scored
# from their vectors, max gap 0.0 — exact, so this costs no false positives (M182).
SCORE_DEFECT: str = "score_not_from_vector"
# The rows a re-score cannot be ASKED of. Each one is counted and named in the report rather
# than skipped, because an exemption nobody counts is the hole this rule exists to close:
#   `guard_veto`        a veto writes 0.0 and never consults the model (`pair_veto`, E61)
#   `no_feature_vector` the store kept no vector — the upsert coalesces, so a probe-only
#                       update leaves the vector the decision was taken on, and a row that
#                       was never scored under this build has none at all
#   `no_model_version`  the row does not NAME the scorer it was decided by (the shape E97
#                       wore: 15,923 rows with `model_version` NULL), and inventing one for it
#                       would be the instrument deciding what the store failed to record
#   `model_unavailable` the row names a model this build does not carry
SCORE_EXEMPT: tuple[str, ...] = (
    "guard_veto", "no_feature_vector", "no_model_version", "model_unavailable")
# E918. The cause a shared pair carries when the live side is HELD for complete photo evidence
# (E908) and the decision it holds IS the batch's: a hold, not a decision, and the safe way
# round — the lane has decided exactly what the batch decided and is waiting to act on it.
EVIDENCE_HOLD: str = "evidence_hold"
# The two decision fields a hold rewrites. `incremental` writes the band and no certificate
# while it waits and keeps what it decided in `pairs.evidence.held_*`, so a release is a
# re-decision and not a reconstruction — and so the instrument can read what is being held.
HOLD_FIELDS: frozenset[str] = frozenset({"zone", "certificate"})
# The cap a hold may run to: the lane's own horizon, measured from the youngest endpoint whose
# gallery was still incomplete — the one the release arm (`RT_EVIDENCE_RELEASE_SQL`) keeps it
# for. A hold past it is one the lane should have released, and it is `unexplained` again.
HOLD_CAP_S: float = EVIDENCE_HORIZON_HOURS * 3600.0
_UNREAD: object = object()


def _rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))
        return list(cur.fetchall())


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


class Pair:
    """One stored pair row, from either side, in the three fields a decision IS."""

    __slots__ = ("lo", "hi", "score", "zone", "certificate", "decision", "guard_veto",
                 "families", "model_version", "features")

    def __init__(self, row: Sequence[Any]) -> None:
        self.lo = int(row[0])
        self.hi = int(row[1])
        self.score = _float(row[2])
        self.zone = None if row[3] is None else str(row[3])
        self.certificate = None if row[4] is None else str(row[4])
        self.decision = None if row[5] is None else str(row[5])
        self.guard_veto = None if row[6] is None else str(row[6])
        self.families = None if row[7] is None else int(row[7])
        self.model_version = None if row[8] is None else str(row[8])
        # Filled only for the pairs that differ (`read_features`): the vector is ~60 keys and
        # the question it answers is only ever asked about a few thousand pairs.
        self.features: dict[str, float | None] | None = None

    @property
    def key(self) -> tuple[int, int]:
        return (self.lo, self.hi)

    def to_json(self) -> dict[str, Any]:
        return {"listing_lo": self.lo, "listing_hi": self.hi, "score": self.score,
                "zone": self.zone, "certificate": self.certificate,
                "decision": self.decision, "guard_veto": self.guard_veto,
                "families": self.families, "model_version": self.model_version}


def read_pairs(conn: Any, generation: str) -> dict[tuple[int, int], Pair]:
    return {pair.key: pair for pair in
            (Pair(row) for row in _rows(conn, RT_EQUIV_PAIRS_SQL,
                                        {"generation": generation}))}


def read_features(conn: Any, generation: str, keys: Sequence[tuple[int, int]],
                  into: Mapping[tuple[int, int], Pair]) -> int:
    """Hang the stored feature vector on the pairs named by `keys`. Returns the statements run.

    The store keeps the score lane's own `{name: [value, present]}` shape (E12), and an ABSENT
    feature is carried as absent rather than as a zero — so `None` here means "this side had
    nothing to compare", which is itself a difference worth naming."""
    if not keys:
        return 0
    statements = 0
    for start in range(0, len(keys), FACTS_CHUNK):
        chunk = keys[start:start + FACTS_CHUNK]
        statements += 1
        for row in _rows(conn, RT_EQUIV_PAIR_FEATURES_SQL, {
            "generation": generation,
            "los": [key[0] for key in chunk],
            "his": [key[1] for key in chunk],
        }):
            pair = into.get((int(row[0]), int(row[1])))
            if pair is not None and row[2] is not None:
                # A row the store kept no vector for stays `None`, which is `no_feature_vector`
                # and not an empty vector: "nothing was stored" and "nothing was present" are
                # different findings.
                pair.features = _feature_vector(row[2])
    return statements


def _feature_vector(raw: Any) -> dict[str, float | None]:
    blob = raw if isinstance(raw, Mapping) else json.loads(raw or "{}")
    out: dict[str, float | None] = {}
    for name, entry in blob.items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out[str(name)] = _float(entry[0]) if entry[1] else None
        else:
            out[str(name)] = _float(entry)
    return out


def read_clusters(conn: Any, generation: str) -> dict[int, frozenset[int]]:
    """`cluster_key -> members`, from the membership table both lanes write."""
    out: dict[int, set[int]] = {}
    for row in _rows(conn, RT_EQUIV_MEMBERS_SQL, {"generation": generation}):
        out.setdefault(int(row[0]), set()).add(int(row[1]))
    return {key: frozenset(members) for key, members in out.items()}


def scope_listings(conn: Any, generation: str,
                   scope: Any) -> tuple[set[int], dict[int, Any], str]:
    """The live generation's own membership, when each listing's location put it there, and
    where it was read from.

    `autodedup.rt_scope_ids` is the snapshot the entrant feed claims out of, so it is the
    generation's OWN answer to "what does my scope hold" and it costs `public` nothing. A
    generation whose snapshot is empty — one seeded and never passed — has to fall back to the
    block walk, and the report says which it used, because a comparison scoped by one and a
    build scoped by the other would be two different questions."""
    ids: set[int] = set()
    resolved: dict[int, Any] = {}
    for row in _rows(conn, RT_EQUIV_SCOPE_IDS_SQL, {"generation": generation}):
        ids.add(int(row[0]))
        _keep_latest(resolved, int(row[0]), row[1])
    if ids:
        return ids, resolved, "rt_scope_ids"
    if scope.whole_corpus:
        raise SystemExit(
            "this generation's scope is the whole corpus and its membership snapshot is "
            "empty — there is nothing bounded to compare inside")
    # The one path that reads `public` for membership, and only for a generation no pass has
    # walked yet. A quarter block is keyed on its PARENT obec, exactly as the entrant sweep
    # keys it, through the same register lookup (W9d-3).
    parents = resolve_scope_parents(conn, scope)
    for block in scope.blocks:
        obec = block.code if block.grain == "obec" else parents.get(block.code)
        cast_obce = block.code if block.grain == "cast_obce" else None
        if obec is None:
            continue
        for row in _rows(conn, RT_SCOPE_BLOCK_SQL, {
                "obec": obec, "cast_obce": cast_obce, "limit": 1_000_000}):
            ids.add(int(row[0]))
            _keep_latest(resolved, int(row[0]), row[1])
    return ids, resolved, "listing_location"


def _keep_latest(into: dict[int, Any], listing_id: int, stamp: Any) -> None:
    """A listing two blocks hold is read twice; its location has one `resolved_at`, but the
    later of two readings is never the wrong one to keep."""
    parsed = _to_stamp(stamp)
    if parsed is not None and (into.get(listing_id) is None or parsed > into[listing_id]):
        into[listing_id] = parsed


def known_listings(conn: Any, generation: str, ids: Sequence[int]) -> set[int]:
    """Which of `ids` this generation has a fingerprint row for."""
    if not ids:
        return set()
    return {int(row[0]) for row in _rows(conn, RT_KNOWN_SQL,
                                         {"generation": generation, "ids": list(ids)})}


def clock_facts(conn: Any,
                ids: Sequence[int]) -> tuple[dict[int, Any], dict[int, ClockFacts], int]:
    """`first_seen_at` (the arrival cause) and the four facts every clock feature reads.

    Returns the statement count as well, because this mode's promise is that it is cheap and
    read-only, and a promise nobody counts is a promise."""
    seen: dict[int, Any] = {}
    facts: dict[int, ClockFacts] = {}
    statements = 0
    wanted = sorted({int(i) for i in ids})
    for start in range(0, len(wanted), FACTS_CHUNK):
        statements += 1
        for row in _rows(conn, RT_EQUIV_CLOCK_FACTS_SQL,
                         {"ids": wanted[start:start + FACTS_CHUNK]}):
            listing_id = int(row[0])
            seen[listing_id] = row[1]
            facts[listing_id] = ClockFacts(
                first_seen_at=_iso(row[1]), last_seen_at=_iso(row[2]),
                inactive_at=_iso(row[3]), is_active=bool(row[4]),
            )
    return seen, facts, statements


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def store_score_type(conn: Any) -> str:
    """The declared type of `autodedup.pairs.score`, or the shipped one if nothing answers."""
    rows = _rows(conn, RT_EQUIV_SCORE_TYPE_SQL)
    if not rows or rows[0][0] is None:
        return PAIR_SCORE_SQL_TYPE
    return str(rows[0][0]).strip().lower()


def batch_settings(conn: Any,
                   generation: str) -> tuple[dict[str, Any] | None, Any, str | None]:
    """The settings the batch generation's newest successful score pass recorded, when that
    pass finished, and the export run it was scored on."""
    rows = _rows(conn, RT_EQUIV_BATCH_SETTINGS_SQL, {"generation": generation})
    if not rows:
        return None, None, None
    row = rows[0]
    export_run = None if len(row) < 4 or row[3] is None else str(row[3]).strip() or None
    if row[0] is None:
        return None, row[2], export_run
    blob = row[0]
    return (dict(blob) if isinstance(blob, Mapping) else json.loads(blob or "{}"),
            row[2], export_run)


def export_window(conn: Any, export_run: str | None) -> tuple[Any, Any] | None:
    """`(started_at, finished_at)` of the export run's own ledger row, or None when the batch
    pass names no export run or the export left no row (a ledger failure never fails a lane)."""
    if not export_run or not export_run.isdigit():
        return None
    rows = _rows(conn, RT_EQUIV_EXPORT_WINDOW_SQL, {"run_id": int(export_run)})
    if not rows or rows[0][0] is None:
        return None
    return rows[0][0], rows[0][1]


@dataclass(frozen=True, slots=True)
class Hold:
    """One live pair HELD for complete photo evidence (E908), as the store keeps it.

    `held_zone` / `held_certificate` are what the pass DECIDED and did not act on. `age_s` is
    how long ago this generation first decided the youngest endpoint whose gallery was still
    incomplete — the endpoint the lane's release arm keeps the hold for — or None when no
    endpoint is incomplete or one cannot be aged, and then the lane owes a release."""

    held_zone: str | None
    held_certificate: str | None
    age_s: float | None

    @property
    def alive(self) -> bool:
        return self.age_s is not None and self.age_s < HOLD_CAP_S


def read_holds(conn: Any, generation: str) -> dict[tuple[int, int], Hold]:
    """Every pair the live generation holds for evidence, keyed like the pair reads.

    A hold is alive exactly when the lane's release arm would NOT release it: some endpoint was
    decided with an incomplete gallery and first decided inside the horizon. An endpoint whose
    first decision carries no stamp cannot be aged, and a hold the instrument cannot age is not
    one it will excuse — the SQL arm keeps such a hold for ever, which is the defect."""
    out: dict[tuple[int, int], Hold] = {}
    for row in _rows(conn, RT_EQUIV_HOLDS_SQL,
                     {"generation": generation, "reason": EVIDENCE_HOLD_REASON}):
        ages = [float(age) for complete, age in ((row[4], row[5]), (row[6], row[7]))
                if complete is False and age is not None]
        out[(int(row[0]), int(row[1]))] = Hold(
            held_zone=None if row[2] is None else str(row[2]),
            held_certificate=str(row[3]) if row[3] else None,
            age_s=min(ages) if ages else None,
        )
    return out


def as_held(pair: Pair, hold: Hold) -> Pair:
    """The row the pass decided and did not write: the stored row with the held decision put
    back. The score, the vector and the veto are the stored ones — a hold moves neither."""
    held = Pair([pair.lo, pair.hi, pair.score, hold.held_zone or "merge",
                 hold.held_certificate, pair.decision, pair.guard_veto, pair.families,
                 pair.model_version])
    held.features = pair.features
    return held


def differences(left: Pair, right: Pair, tol: float) -> list[str]:
    """What moved between two rows for the same pair. Ordered so the report's counters read
    decision-first: a zone that moved is a different answer, a score that moved by 1e-5 is a
    different input to the same answer."""
    moved: list[str] = []
    for field in DECISION_FIELDS:
        if getattr(left, field) != getattr(right, field):
            moved.append(field)
    if (left.score is None) != (right.score is None):
        moved.append("score")
    elif left.score is not None and right.score is not None \
            and abs(left.score - right.score) > tol:
        moved.append("score")
    return moved


def moved_features(left: Pair, right: Pair, tol: float) -> list[str]:
    """Which feature slots hold a different value on the two sides.

    A slot ABSENT on one side and present on the other counts: the vector is what the model
    was handed, so "this side had nothing to compare" is a different input, not a missing
    report."""
    if left.features is None or right.features is None:
        return []
    moved: list[str] = []
    for name in sorted(set(left.features) | set(right.features)):
        a, b = left.features.get(name), right.features.get(name)
        if a is None or b is None:
            if a is not b:
                moved.append(name)
        elif abs(a - b) > tol:
            moved.append(name)
    return moved


def _as_feats(pair: Pair) -> dict[str, tuple[float, bool]]:
    """The stored vector in the shape the model reads it.

    An absent feature is `(0.0, False)` and a missing key is the same thing to
    `LogisticModel.score`, so the store's present-only vector (`score_lane.present_features`)
    reconstructs the model's input exactly — absent is unknown, never a zero (E12)."""
    return {name: (0.0, False) if value is None else (float(value), True)
            for name, value in (pair.features or {}).items()}


def _narrowed(value: float, score_type: str) -> float:
    """`value` as the score column would hand it back, or unchanged for a column this build
    does not know how to narrow — the comparison must not invent a rounding it cannot name."""
    try:
        return narrow(float(value), score_type)
    except ValueError:
        return float(value)


def score_from_vector(pair: Pair, *, score_type: str, tol: float,
                      models: dict[str, Any]) -> tuple[str, float | None, float | None]:
    """Does this row's stored score follow from its OWN stored vector? (E121)

    `("ok" | one of SCORE_EXEMPT | SCORE_DEFECT, recomputed, gap)`. The row names its scorer
    (`model_version`) and `model_of_version` resolves it the way every lane does — one
    definition (E12) — and the recomputation is then narrowed through the store's own column,
    so a `real` store is compared as a `real` store instead of being passed by a tolerance
    wide enough to hide what `real` did to it (E115)."""
    if pair.guard_veto is not None:
        return "guard_veto", None, None
    if pair.features is None:
        return "no_feature_vector", None, None
    if pair.model_version is None:
        return "no_model_version", None, None
    model = models.get(pair.model_version, _UNREAD)
    if model is _UNREAD:
        try:
            model = model_of_version(pair.model_version)
        except (SystemExit, OSError, ValueError):
            model = None
        models[pair.model_version] = model
    if model is None:
        return "model_unavailable", None, None
    recomputed = _narrowed(model.predict_proba(_as_feats(pair)), score_type)
    if pair.score is None:
        return SCORE_DEFECT, recomputed, None
    gap = abs(recomputed - _narrowed(pair.score, score_type))
    # The "ok" branch is the one that has to be EARNED (`gap <= tol`) rather than the defect
    # branch (`gap > tol`): a NaN compares False both ways, and a stored NaN reading "ok" is
    # exactly the silent pass this rule exists to stop.
    return ("ok" if gap <= tol else SCORE_DEFECT), recomputed, gap


def score_self_consistency(keys: Sequence[tuple[int, int]], live: Mapping[Any, Pair],
                           batch: Mapping[Any, Pair], *, score_type: str,
                           tol: float) -> dict[str, Any]:
    """E121 over every row the instrument reads a vector for, on BOTH sides.

    That population is the shared pairs that DIFFER — the rows an attribution would otherwise
    excuse — and the report says how many were checked and how many were exempt, because a
    rule whose denominator nobody prints can be passing on nothing at all."""
    models: dict[str, Any] = {}
    exempt: dict[str, int] = {}
    defects = {"live": 0, "batch": 0}
    examples: list[dict[str, Any]] = []
    checked = 0
    max_gap = 0.0
    for key in keys:
        for side, store in (("live", live), ("batch", batch)):
            pair = store.get(key)
            if pair is None:
                continue
            status, recomputed, gap = score_from_vector(
                pair, score_type=score_type, tol=tol, models=models)
            if status in SCORE_EXEMPT:
                exempt[status] = exempt.get(status, 0) + 1
                continue
            checked += 1
            if gap is not None:
                max_gap = max(max_gap, gap)
            if status != SCORE_DEFECT:
                continue
            defects[side] += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append({"side": side, "listing_lo": pair.lo, "listing_hi": pair.hi,
                                 "model_version": pair.model_version, "stored": pair.score,
                                 "recomputed": recomputed, "gap": gap})
    return {"rows": 2 * len(keys), "checked": checked, "exempt": exempt,
            "defects": defects, "defective": defects["live"] + defects["batch"],
            "max_gap": max_gap, "tolerance": tol, "score_column": score_type,
            "models_unavailable": sorted(name for name, model in models.items()
                                         if model is None),
            "examples": examples}


def asymmetric_fields(live: Mapping[Any, Pair],
                      batch: Mapping[Any, Pair]) -> list[dict[str, Any]]:
    """Decision fields one store WRITES and the other never does (E120, M171).

    Evidenced rather than inferred from an absence: a generation that simply merged nothing
    certified holds no certificates either, and calling THAT a defect would fail every honest
    comparison. The signature of the real thing is a side whose `decision` STRINGS name a
    certificate while the column holds none on every row — which is exactly what
    `rt_base_w13` looked like: 0 of 16,751 rows carrying the column, 3,866 of them naming one
    in `decision` (M171). The column is the definition (D41) and the string is the witness."""
    out: list[dict[str, Any]] = []
    for name, store in (("live", live), ("batch", batch)):
        column = sum(1 for pair in store.values() if pair.certificate is not None)
        named = sum(1 for pair in store.values()
                    if pair.decision and CERTIFICATE_PREFIX in pair.decision)
        if column == 0 and named:
            out.append({"field": "certificate", "side": name, "rows_naming_one": named,
                        "rows_carrying_one": 0})
    return out


def arrival_of(listing_id: int, *, cut: datetime | None, seen: Mapping[int, Any],
               resolved: Mapping[int, Any], batch_held: Collection[int],
               cohort: Collection[int] | None) -> str | None:
    """Which clock says `listing_id` reached the scope after the batch cohort was cut, or None.

    The export's cohort is `listings` joined to `listing_location` by block, read as the export
    STARTS (`cut`). A listing is missing from it for one of two reasons: it did not exist yet
    (`first_seen_at` after the cut), or its location — what puts it in a block — had not been
    written yet (the scope snapshot's `resolved_at` after the cut, the lane's own arrival event
    for the snapshot feed).

    Membership decides; the clocks only say WHY. A listing the batch generation holds any row
    for (a pair at any zone, a cluster membership) was in its cohort by construction, and one
    the export artifact carries (`cohort`) was in it by record — neither is ever an arrival,
    whatever its clocks say. `resolved_at` is rewritten by every re-resolve, so it is read
    ONLY against the artifact: a listing the cohort provably lacks whose location was written
    after the cut moved in; one it lacks whose clocks both predate the cut was never missing
    for that reason, and the scope and the export simply disagree. Without the artifact only
    `first_seen_at` can prove an absence — a listing that did not exist at the cut."""
    if cut is None or listing_id in batch_held:
        return None
    if cohort is not None and listing_id in cohort:
        return None
    clocks = (("first_seen_at", seen), ("resolved_at", resolved)) if cohort is not None \
        else (("first_seen_at", seen),)
    for clock, stamps in clocks:
        stamp = _to_stamp(stamps.get(listing_id))
        if stamp is not None and stamp > cut:
            return clock
    return None


def cohort_listing_ids(path: Path) -> set[int]:
    """The listing ids an export artifact carries.

    The artifact's order is a contract (`export.py`, pinned by `test_export.py`): `meta`, then
    every `listing`, then every `image` — so the read stops at the first image and never
    decompresses the part of the file that is 90 % of its bytes."""
    ids: set[int] = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record.get("t")
            if kind == "image":
                break
            if kind == "listing":
                ids.add(int(record["id"]))
    return ids


def batch_cohort(
    cohort_path: str | None, export_run: str | None,
    fetch: Callable[[str, Path], Path] = download_cohort,
) -> tuple[set[int] | None, str | None, str | None]:
    """The batch cohort's listing set: `(ids, where it came from, why it could not be read)`.

    The batch pass stores its pairs and its clusters and NOT the listings it was handed, so the
    set is read from the export artifact it was scored on — a local `cohort=<path>`, else the
    `export_run` its own run row names, downloaded into a temporary directory that is gone
    before the report is written (the lane uploads `out/`, and a 280 MB cohort is not a report).
    A failure is reported and never raised: without the set the arrival cause falls back to
    `first_seen_at`, which is the conservative side."""
    try:
        if cohort_path:
            return cohort_listing_ids(Path(cohort_path)), "cohort", None
        if not export_run or not export_run.isdigit():
            return None, None, "the batch pass names no export run"
        with tempfile.TemporaryDirectory(prefix="rt_equivalence_") as tmp:
            return (cohort_listing_ids(fetch(export_run, Path(tmp))),
                    f"export_run:{export_run}", None)
    except (SystemExit, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"[:400]


def cause_of(pair: Pair, *, side: str, in_scope: set[int], known: set[int],
             arrived: Collection[int], store_floor: float) -> str:
    """Why exactly one side holds this pair — or `unexplained`, which fails the verdict.

    The order is the order of AUTHORITY. A pair whose endpoint the scope does not hold was
    never this generation's to decide; one whose endpoint the live store has no fingerprint for
    has not been built yet; one whose endpoint arrived after the export could not have been in
    the batch cohort at all (`arrival_of`); and only then do the two storage rules — the floor
    and what is still sitting in the store under an older floor — get to explain anything.

    A HOLD (E908) is not a one-sided cause: a pair the live side holds for evidence and the
    batch side holds nothing for is a pair the lane DECIDED to merge and the batch did not keep,
    and waiting to act on that decision does not explain it."""
    if not ({pair.lo, pair.hi} <= in_scope):
        return "scope"
    if not ({pair.lo, pair.hi} <= known):
        return "not_in_live_store"
    if side == "live" and ({pair.lo, pair.hi} & set(arrived)):
        return "arrival_after_export"
    score = pair.score
    if score is not None and score < store_floor:
        # In the store under a floor it no longer clears: `storable` keeps the merge and band
        # zones whatever they score and the reject tail only at or above the floor, and a row
        # is evicted when something re-scores it, not when the floor moves.
        return "retention"
    if pair.zone == "reject":
        return "store_floor"
    return "unexplained"


def shared_cause(
    live: Pair, batch: Pair, moved: Sequence[str], features: Sequence[str], *,
    asymmetric: Sequence[str], one_clock: bool | None, facts: Mapping[int, ClockFacts],
    settings: WindowRule, tol: float,
) -> str:
    """Why BOTH stores hold this pair and disagree about it — or `unexplained` (E119).

    Authority again. A field nobody wrote on one side explains itself and nothing else, so it
    is stripped first; a disagreement left over with an IDENTICAL feature vector is the
    sharpest finding this instrument can make, because the same inputs reached two answers;
    and the two legitimate reasons the inputs can differ — the facts moved after the export,
    and the two calibrations were cut over different cohorts — have to be EVIDENCED before
    they are allowed to explain anything."""
    real = [field for field in moved if field not in set(asymmetric)]
    if not real:
        return "store_write_asymmetry"
    if live.features is None or batch.features is None:
        return "no_feature_vector"
    if not features:
        # Same vector, same model, different answer. Nothing legitimate does this.
        return "unexplained"
    clock = [name for name in features if name in CLOCK_FEATURES]
    calibration = [name for name in features if name in CALIBRATION_FEATURES]
    if len(clock) + len(calibration) != len(features):
        return "unexplained"
    if clock:
        if one_clock is False:
            return "clock_anchor"
        if not _drift_evidenced(live, batch, clock, facts, settings, tol):
            return "unexplained"
        return "drifted_since_export"
    return "calibration_cohort"


def hold_cause(live: Pair, batch: Pair, features: Sequence[str], hold: Hold | None, *,
               asymmetric: Sequence[str], one_clock: bool | None,
               facts: Mapping[int, ClockFacts], settings: WindowRule, tol: float) -> str:
    """Why a shared pair differs when the LIVE side is held for evidence (E908, E918).

    The hold explains a difference only when three things are true: the store still holds it
    for a reason the lane recognises (an endpoint inside the 48 h horizon with an incomplete
    gallery — past that the lane owes a release, and the pair is `unexplained` again); the
    decision it holds IS the batch's — zone and certificate, read from `evidence.held_*`; and
    whatever ELSE differs has a cause of its own, attributed exactly as an unheld pair's is. A
    held merge the batch does not make is a disagreement the hold is merely postponing."""
    if hold is None or not hold.alive:
        return "unexplained"
    held = as_held(live, hold)
    moved = differences(held, batch, tol)
    if not moved:
        return EVIDENCE_HOLD
    rest = shared_cause(held, batch, moved, features, asymmetric=asymmetric,
                        one_clock=one_clock, facts=facts, settings=settings, tol=tol)
    return rest if rest in UNNAMED else EVIDENCE_HOLD


def _drift_evidenced(live: Pair, batch: Pair, clock: Sequence[str],
                     facts: Mapping[int, ClockFacts], settings: WindowRule,
                     tol: float) -> bool:
    """Is the LIVE value the one that matches the facts as they stand now?

    The live generation read `public.listings` hours after the export the batch pass was
    scored on, so a clock feature that moved is drift exactly when the newer reading is the
    one closer to today's facts. Recomputed through `features.clock_features` — the engine's
    own function — so the instrument cannot invent a second definition of the window."""
    a, b = facts.get(live.lo), facts.get(live.hi)
    if a is None or b is None or live.features is None or batch.features is None:
        return False
    now = clock_features(a, b, settings)
    closer = False
    for name in clock:
        entry = now.get(name)
        value = None if entry is None or not entry[1] else float(entry[0])
        held, was = live.features.get(name), batch.features.get(name)
        if value is None or held is None or was is None:
            return False
        if abs(held - value) > abs(was - value) + tol:
            return False
        if abs(held - value) + tol < abs(was - value):
            closer = True
    return closer


def run_equivalence(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path, *,
    fetch_cohort: Callable[[str, Path], Path] = download_cohort,
) -> dict[str, Any]:
    """`--mode rt_equivalence`: the live store against a batch generation. Writes nothing."""
    generation = str(args.get("generation") or "").strip() or GENERATION
    batch = str(args.get("batch") or "").strip()
    if not batch:
        raise SystemExit(
            "batch=<generation> is required — this mode compares the LIVE store with a batch "
            "generation scored on the same export under the same settings and model, and "
            "there is no default that could be right")
    if batch == generation:
        raise SystemExit(f"batch and generation are both {batch!r} — a generation is trivially "
                         "equivalent to itself")
    asked_tol = float(str(args.get("tol") or SCORE_TOL))

    conn = conn_factory()
    try:
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
        if not rows:
            raise SystemExit(
                f"generation {generation!r} has no frozen calibration — this mode compares the "
                "store a SEEDED generation holds, and an unseeded one holds nothing")
        settings_blob = rows[0][5]
        live_settings = (settings_blob if isinstance(settings_blob, dict)
                         else json.loads(settings_blob or "{}"))
        store_floor = float(live_settings.get("store_floor") or DEFAULT_STORE_FLOOR)
        live_model = None if rows[0][6] is None else str(rows[0][6])
        control = lane_settings(conn, [scope_setting_key(generation)])
        try:
            scope = resolve_scope(None, read_scope_setting(control, generation))
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        in_scope, resolved, scope_source = scope_listings(conn, generation, scope)
        score_type = store_score_type(conn)
        batch_blob, batch_finished, export_run = batch_settings(conn, batch)
        window = export_window(conn, export_run)
        exported_raw = str(args.get("exported_at") or "").strip() or None
        live_pairs = read_pairs(conn, generation)
        batch_pairs = read_pairs(conn, batch)
        live_clusters = read_clusters(conn, generation)
        batch_clusters = read_clusters(conn, batch)
        holds = read_holds(conn, generation)

        # The effective tolerance is never finer than the store's own resolution (M178).
        tol = max(asked_tol, TOL_STORE_ULPS * store_eps(score_type)
                  if score_type in ("real", "double precision") else asked_tol)
        asymmetry = asymmetric_fields(live_pairs, batch_pairs)
        asymmetric = sorted({str(entry["field"]) for entry in asymmetry})

        only_live = sorted(set(live_pairs) - set(batch_pairs))
        only_batch = sorted(set(batch_pairs) - set(live_pairs))
        both = sorted(set(live_pairs) & set(batch_pairs))
        # A held pair differs when its STORED row does, and also when the decision it holds
        # does: a band row both sides agree on can be hiding a merge the batch never made.
        differing_keys = [key for key in both
                          if differences(live_pairs[key], batch_pairs[key], tol)
                          or _held_moved(live_pairs[key], batch_pairs[key], holds, tol)]
        # The ten this mode always runs: the calibration, the control settings, the scope
        # snapshot, the score column's type, the batch pass's settings, two pair reads, two
        # membership reads and the live side's holds — plus the export's ledger row when the
        # batch pass names its export. Everything after it is bounded by what actually differs.
        statements = 10 + (1 if export_run and export_run.isdigit() else 0)
        statements += read_features(conn, generation, differing_keys, live_pairs)
        statements += read_features(conn, batch, differing_keys, batch_pairs)

        cluster_diff = _cluster_components(live_clusters, batch_clusters, in_scope,
                                           live_pairs, batch_pairs)
        endpoints = {i for key in only_live + only_batch for i in key}
        endpoints |= {i for key in differing_keys for i in key}
        endpoints |= cluster_diff["listings"]
        known = known_listings(conn, generation, sorted(endpoints))
        seen, facts, fact_statements = (clock_facts(conn, sorted(endpoints)) if endpoints
                                        else ({}, {}, 0))
        statements += (1 if endpoints else 0) + fact_statements
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    cut, cut_source = _cohort_cut(window, exported_raw, batch_finished)
    cohort, cohort_source, cohort_error = batch_cohort(
        str(args.get("cohort") or "").strip() or None, export_run, fetch_cohort)
    batch_held = {i for key in batch_pairs for i in key}
    batch_held |= {i for members in batch_clusters.values() for i in members}
    arrived: dict[int, str] = {}
    for listing_id in endpoints:
        clock = arrival_of(listing_id, cut=cut, seen=seen, resolved=resolved,
                           batch_held=batch_held, cohort=cohort)
        if clock is not None:
            arrived[listing_id] = clock

    live_clock = live_settings.get("live_window_from_sighting")
    batch_clock = (batch_blob or {}).get("live_window_from_sighting")
    one_clock = None if (live_clock is None or batch_clock is None) \
        else bool(live_clock) == bool(batch_clock)
    clock_settings = WindowRule(live_window_from_sighting=bool(live_clock))

    by_field: dict[str, int] = {}
    differing_examples: list[dict[str, Any]] = []
    shared_causes: dict[str, int] = {}
    shared_unexplained: list[dict[str, Any]] = []
    decisions_moved = 0
    score_only = 0
    score_only_max = 0.0
    unexplained_score_max = 0.0
    held_shared: list[dict[str, Any]] = []
    for key in differing_keys:
        live, batch_pair = live_pairs[key], batch_pairs[key]
        moved = differences(live, batch_pair, tol)
        features = moved_features(live, batch_pair, tol)
        attribution = {"asymmetric": asymmetric, "one_clock": one_clock, "facts": facts,
                       "settings": clock_settings, "tol": tol}
        if live.decision == EVIDENCE_HOLD_REASON:
            hold = holds.get(key)
            cause = hold_cause(live, batch_pair, features, hold, **attribution)
            if not moved and hold is not None:
                # The stored rows agree and the HELD decision does not: name what moved as the
                # hold's, so the counters never read a band row as a decision that moved.
                moved = [f"held_{field}"
                         for field in differences(as_held(live, hold), batch_pair, tol)]
            held_shared.append({"key": key, "cause": cause, "hold": hold})
        else:
            cause = shared_cause(live, batch_pair, moved, features, **attribution)
        shared_causes[cause] = shared_causes.get(cause, 0) + 1
        if moved == ["score"]:
            score_only += 1
            a, b = live.score, batch_pair.score
            if a is not None and b is not None:
                score_only_max = max(score_only_max, abs(a - b))
                if cause in UNNAMED:
                    unexplained_score_max = max(unexplained_score_max, abs(a - b))
        else:
            decisions_moved += 1
        for field in moved:
            by_field[field] = by_field.get(field, 0) + 1
        if cause in UNNAMED and len(shared_unexplained) < MAX_EXAMPLES:
            shared_unexplained.append({"cause": cause, "moved": moved,
                                       "moved_features": features,
                                       "live": live.to_json(),
                                       "batch": batch_pair.to_json()})
        if len(differing_examples) < MAX_EXAMPLES:
            differing_examples.append({
                "listing_lo": key[0], "listing_hi": key[1], "moved": moved,
                "moved_features": features, "cause": cause,
                "live": live.to_json(), "batch": batch_pair.to_json()})

    causes: dict[str, dict[str, int]] = {"live": {}, "batch": {}}
    unexplained: list[dict[str, Any]] = []
    for side, keys, store in (("live", only_live, live_pairs),
                              ("batch", only_batch, batch_pairs)):
        for key in keys:
            cause = cause_of(store[key], side=side, in_scope=in_scope, known=known,
                             arrived=arrived, store_floor=store_floor)
            causes[side][cause] = causes[side].get(cause, 0) + 1
            if cause == "unexplained" and len(unexplained) < MAX_EXAMPLES:
                unexplained.append({"side": side, **store[key].to_json()})
    one_sided = {cause: causes["live"].get(cause, 0) + causes["batch"].get(cause, 0)
                 for cause in set(causes["live"]) | set(causes["batch"])}

    held_edges = {entry["key"] for entry in held_shared if entry["cause"] == EVIDENCE_HOLD}
    clusters = _attribute_clusters(cluster_diff, arrived, known, held_edges)
    evidence_hold = _hold_report(held_shared, holds, only_live, live_pairs)
    consistency = score_self_consistency(differing_keys, live_pairs, batch_pairs,
                                         score_type=score_type, tol=tol)
    defects = _defects(asymmetry, one_clock, score_type, live_clock, batch_clock)
    defects.extend(_score_defects(consistency))
    live_versions = sorted({p.model_version for p in live_pairs.values()
                            if p.model_version is not None})
    batch_versions = sorted({p.model_version for p in batch_pairs.values()
                             if p.model_version is not None})

    reasons: list[str] = []
    if not both:
        reasons.append("no pair is stored on both sides, so nothing was compared")
    for defect in defects:
        reasons.append(defect["reason"])
    unnamed_shared = sum(shared_causes.get(cause, 0) for cause in UNNAMED)
    if unnamed_shared:
        reasons.append(f"{unnamed_shared} of {len(both)} shared pairs differ with no cause")
    if unexplained_score_max > SCORE_ONLY_MAX:
        reasons.append(f"an unattributed score-only difference of {unexplained_score_max:.4f} "
                       f"exceeds {SCORE_ONLY_MAX} with the decision unchanged")
    if evidence_hold["unexplained"]:
        reasons.append(
            f"{evidence_hold['unexplained']} of the {evidence_hold['shared']} held shared pairs "
            "are not excused by their hold — past the "
            f"{EVIDENCE_HORIZON_HOURS:.0f} h evidence cap, not ageable, or holding a decision "
            "the batch did not take")
    if one_sided.get("unexplained"):
        reasons.append(f"{one_sided['unexplained']} one-sided pairs have no cause")
    if clusters["component_causes"].get("unexplained"):
        reasons.append(f"{clusters['component_causes']['unexplained']} cluster components "
                       "hold the same merge edges on both sides and partition them differently")
    if one_sided.get("not_in_live_store"):
        reasons.append(f"{one_sided['not_in_live_store']} one-sided pairs touch a listing the "
                       "live store has never fingerprinted — the build is not finished")
    if live_versions and batch_versions and live_versions != batch_versions:
        reasons.append(f"model_version {live_versions} against {batch_versions}")
    notes: list[str] = []
    if evidence_hold["explained"]:
        notes.append(
            f"{evidence_hold['explained']} shared pairs are HELD for complete photo evidence "
            f"(E908) — a hold, not a decision; the oldest has waited "
            f"{evidence_hold['oldest_h']:.1f} h of the {EVIDENCE_HORIZON_HOURS:.0f} h cap")
    if arrived:
        notes.append(f"{len(arrived)} listings reached the scope after the batch cohort's cut "
                     f"({_stamp(cut)}, from {cut_source})")

    report: dict[str, Any] = {
        "generation": generation,
        "batch": batch,
        "scope": scope.as_json(),
        "scope_source": scope_source,
        "scope_listings": len(in_scope),
        "store_floor": store_floor,
        "score_tolerance": {"asked": asked_tol, "used": tol,
                            "store_column": score_type,
                            "store_eps": store_eps(score_type)
                            if score_type in ("real", "double precision") else None},
        "clock": {"live": live_clock, "batch": batch_clock, "one_definition": one_clock},
        "exported_at": _stamp(exported_raw),
        "export_window": {
            "export_run": export_run,
            "started_at": _stamp(window[0]) if window else None,
            "finished_at": _stamp(window[1]) if window else None,
            "cut": _stamp(cut),
            "cut_source": cut_source,
        },
        "arrivals": {
            **_arrival_report(arrived, seen, resolved),
            "cohort": {
                "source": cohort_source,
                "listings": None if cohort is None else len(cohort),
                # In the scope and NOT in the cohort the batch was scored on. Arrivals are a
                # handful; hundreds here is a scope the export never covered.
                "scope_absent": None if cohort is None else len(in_scope - cohort),
                "error": cohort_error,
            },
        },
        "model_version": {"calibration": live_model, "live_pairs": live_versions,
                          "batch_pairs": batch_versions},
        "defects": defects,
        "pairs": {
            "live": len(live_pairs),
            "batch": len(batch_pairs),
            "both": len(both),
            "identical": not differing_keys,
            "differing": len(differing_keys),
            "decisions_moved": decisions_moved,
            "score_only": score_only,
            "score_only_max": round(score_only_max, 6),
            "score_only_max_unattributed": round(unexplained_score_max, 6),
            "by_field": by_field,
            "score_self_consistency": consistency,
            "shared_causes": shared_causes,
            "evidence_hold": evidence_hold,
            "shared_unexplained_examples": shared_unexplained,
            "differing_examples": differing_examples,
            "only_live": len(only_live),
            "only_batch": len(only_batch),
            "causes": one_sided,
            "causes_by_side": causes,
            "unexplained_examples": unexplained,
        },
        "clusters": clusters,
        "verdict": {"ok": not reasons, "reasons": reasons, "notes": notes},
        "statements": statements,
        "spent_usd": 0.0,
    }
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / EQUIVALENCE_FILE).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return report


# The shared-pair causes that are NOT a cause: each one fails the verdict.
UNNAMED: frozenset[str] = frozenset({"unexplained", "no_feature_vector"})


def _defects(asymmetry: Sequence[Mapping[str, Any]], one_clock: bool | None, score_type: str,
             live_clock: Any, batch_clock: Any) -> list[dict[str, Any]]:
    """The asymmetries that make the comparison itself lie (E120).

    Each one is reported as a defect rather than as a disagreement, because each one is a
    property of the store or of the instrument — no re-run of the engine changes it, and a
    verdict that counted them as engine differences is the verdict W9l had to take apart."""
    out: list[dict[str, Any]] = []
    for entry in asymmetry:
        out.append({
            "defect": "store_write_asymmetry",
            **dict(entry),
            "reason": f"the {entry['side']} generation names a certificate in `decision` on "
                      f"{entry['rows_naming_one']} rows and carries `pairs.certificate` on "
                      "none — the column cannot be compared until both lanes write it",
        })
    if one_clock is False:
        out.append({
            "defect": "clock_anchor",
            "live_window_from_sighting": {"live": live_clock, "batch": batch_clock},
            "reason": "the two generations do not agree what the end of a listing's live "
                      "window is, so every co-live feature is two different measurements",
        })
    if not is_lossless(score_type):
        out.append({
            "defect": "store_score_precision",
            "column": score_type,
            "reason": f"`autodedup.pairs.score` is `{score_type}` and `cluster.edge_rank` "
                      "ranks on it, so a generation is not re-clusterable from its own rows "
                      "(migration 541)",
        })
    return out


def _score_defects(consistency: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The pair-grain defect, one entry per side (E121).

    It is a DEFECT and not a shared cause on purpose: a cause answers "why do the two stores
    disagree about this pair", and this answers "this ROW is not a decision this engine took"
    — which is true of the row whether or not the other side holds anything at all, and which
    no amount of drift or calibration attribution on that pair is allowed to carry."""
    out: list[dict[str, Any]] = []
    for side in ("live", "batch"):
        rows = int(consistency["defects"][side])
        if not rows:
            continue
        out.append({
            "defect": SCORE_DEFECT, "side": side, "rows": rows,
            "checked": consistency["checked"], "max_gap": consistency["max_gap"],
            "reason": f"{rows} pair rows on the {side} side (of "
                      f"{consistency['checked']} rows re-scored in all) carry a score "
                      "their own stored feature vector does not reproduce (max gap "
                      f"{consistency['max_gap']:.6g} against a tolerance of "
                      f"{consistency['tolerance']:.6g}) — `decide_pair` sets the score from "
                      "the vector in every branch but the veto, so such a row was not written "
                      "by this engine and no cause attributed to that pair can excuse it",
        })
    return out


def _to_stamp(raw: Any) -> datetime | None:
    """A timestamp from a column or from a command line, always timezone-aware.

    `exported_at=2026-09-26T12:38Z` is text and `first_seen_at` is a `timestamptz`, and an aware
    and a naive datetime do not compare — so a stamp without a zone is read as UTC, the zone
    every stamp in this lane is written in."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        stamp = raw
    else:
        try:
            stamp = datetime.fromisoformat(str(raw).strip())
        except ValueError:
            return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _cohort_cut(window: tuple[Any, Any] | None, exported_raw: str | None,
                batch_finished: Any) -> tuple[datetime | None, str | None]:
    """The moment the batch cohort was cut, and where the instrument read it (E918).

    The export's ledger row first: the export lane reads its block ids as the mode STARTS, so
    the window's start is the cut — a listing first seen or first located after it cannot be in
    the cohort. The dispatch's `exported_at` only when the batch pass names no export run or the
    export left no row, and last the batch pass's own finish, which is LATER than the export and
    so fails closed: an arrival between the two reads as unexplained rather than excused."""
    if window is not None and _to_stamp(window[0]) is not None:
        return _to_stamp(window[0]), "export_ledger"
    if exported_raw and _to_stamp(exported_raw) is not None:
        return _to_stamp(exported_raw), "exported_at"
    if _to_stamp(batch_finished) is not None:
        return _to_stamp(batch_finished), "batch_finished"
    return None, None


def _held_moved(live: Pair, batch: Pair, holds: Mapping[tuple[int, int], Hold],
                tol: float) -> bool:
    """Does the decision a live HOLD keeps differ from the batch's, whatever the rows say?"""
    if live.decision != EVIDENCE_HOLD_REASON:
        return False
    hold = holds.get(live.key)
    return hold is not None and bool(differences(as_held(live, hold), batch, tol))


def _hold_report(held_shared: Sequence[Mapping[str, Any]],
                 holds: Mapping[tuple[int, int], Hold], only_live: Sequence[tuple[int, int]],
                 live_pairs: Mapping[tuple[int, int], Pair]) -> dict[str, Any]:
    """What the live side is HOLDING (E908), counted apart from what it decided.

    `explained` is the shared pairs whose only difference is the hold; `unexplained` the held
    shared pairs no hold excuses (past the cap, not ageable, or holding a decision the batch
    did not take). The oldest EXPLAINED hold's age is the number that says whether the photo
    producers are keeping up — a hold that ages past the cap is already `unexplained`."""
    explained = [entry for entry in held_shared if entry["cause"] == EVIDENCE_HOLD]
    refused = [entry for entry in held_shared if entry["cause"] in UNNAMED]
    ages = [entry["hold"].age_s for entry in explained
            if entry["hold"] is not None and entry["hold"].age_s is not None]
    examples: list[dict[str, Any]] = []
    for entry in refused[:MAX_EXAMPLES]:
        hold = entry["hold"]
        examples.append({
            "listing_lo": entry["key"][0], "listing_hi": entry["key"][1],
            "cause": entry["cause"],
            "held_zone": None if hold is None else hold.held_zone,
            "held_certificate": None if hold is None else hold.held_certificate,
            "age_h": None if hold is None or hold.age_s is None
            else round(hold.age_s / 3600.0, 2),
        })
    return {
        "reason": EVIDENCE_HOLD_REASON,
        "cap_h": EVIDENCE_HORIZON_HOURS,
        "live_rows": len(holds),
        "shared": len(held_shared),
        "explained": len(explained),
        "unexplained": len(refused),
        "only_live": sum(1 for key in only_live
                         if live_pairs[key].decision == EVIDENCE_HOLD_REASON),
        "oldest_h": round(max(ages) / 3600.0, 2) if ages else None,
        "unexplained_examples": examples,
    }


def _arrival_report(arrived: Mapping[int, str], seen: Mapping[int, Any],
                    resolved: Mapping[int, Any]) -> dict[str, Any]:
    """Which listings the instrument called arrivals, and by which clock — a count nobody can
    chase is a rumour, and this cause excuses pairs and components alike."""
    by_clock: dict[str, int] = {}
    for clock in arrived.values():
        by_clock[clock] = by_clock.get(clock, 0) + 1
    return {
        "listings": len(arrived),
        "by_clock": by_clock,
        "examples": [{"listing_id": listing_id, "clock": clock,
                      "first_seen_at": _stamp(seen.get(listing_id)),
                      "resolved_at": _stamp(resolved.get(listing_id))}
                     for listing_id, clock in sorted(arrived.items())[:MAX_EXAMPLES]],
    }


def _trim(source: Mapping[int, frozenset[int]],
          in_scope: set[int]) -> dict[frozenset[int], list[int]]:
    """Cluster member sets, trimmed to the scope.

    The batch cohort is wider than the live scope (it carries the assembled negative control
    and two whole towns of it), so a batch cluster is compared by the members the live scope
    could ever have held. A cluster trimmed to fewer than two members is not a cluster on
    either side and is dropped rather than counted as a difference."""
    out: dict[frozenset[int], list[int]] = {}
    for key, members in source.items():
        inside = frozenset(members & in_scope)
        if len(inside) < 2:
            continue
        out.setdefault(inside, []).append(int(key))
    return out


def _cluster_components(
    live: Mapping[int, frozenset[int]], batch: Mapping[int, frozenset[int]],
    in_scope: set[int], live_pairs: Mapping[tuple[int, int], Pair],
    batch_pairs: Mapping[tuple[int, int], Pair],
) -> dict[str, Any]:
    """Every listing whose member set differs, grouped into the components it differs in.

    A cluster comparison that only counts one-sided member sets cannot say WHY they differ,
    and W9l had to group 81 and 76 one-sided sets into 47 components by hand to find that 45
    of them held the SAME merge edges on both sides. That grouping is the unit of attribution
    here: a component is the closure of a differing listing under both sides' membership."""
    left, right = _trim(live, in_scope), _trim(batch, in_scope)
    of_listing_left = _membership(left)
    of_listing_right = _membership(right)
    # Indexed once per side: a component's edge set is then the size of the component, not of
    # the generation. A live scope holds tens of thousands of pairs and the 2026-09-21
    # comparison had 47 components to ask about.
    edges_left = _merge_index(live_pairs)
    edges_right = _merge_index(batch_pairs)
    differing = sorted(
        listing_id for listing_id in in_scope
        if of_listing_left.get(listing_id, frozenset({listing_id}))
        != of_listing_right.get(listing_id, frozenset({listing_id}))
    )
    seen: set[int] = set()
    components: list[dict[str, Any]] = []
    for listing_id in differing:
        if listing_id in seen:
            continue
        member_set = {listing_id}
        frontier = [listing_id]
        while frontier:
            current = frontier.pop()
            for side in (of_listing_left, of_listing_right):
                for neighbour in side.get(current, ()):  # type: ignore[arg-type]
                    if neighbour not in member_set:
                        member_set.add(neighbour)
                        frontier.append(neighbour)
        seen |= member_set
        components.append({
            "listings": sorted(member_set),
            "live": sorted(sorted(s) for s in _sets_over(of_listing_left, member_set)),
            "batch": sorted(sorted(s) for s in _sets_over(of_listing_right, member_set)),
            "live_edges": _merge_edges(edges_left, member_set),
            "batch_edges": _merge_edges(edges_right, member_set),
        })
    return {
        "left": left, "right": right, "components": components,
        "listings": seen,
    }


def _partition(sets: Sequence[Sequence[int]], drop: Collection[int]) -> list[list[int]]:
    """A component's partition with `drop` taken out of every set — empty sets gone."""
    return sorted(kept for kept in (sorted(set(members) - set(drop)) for members in sets)
                  if kept)


def _membership(trimmed: Mapping[frozenset[int], list[int]]) -> dict[int, frozenset[int]]:
    return {listing_id: members for members in trimmed for listing_id in members}


def _sets_over(of_listing: Mapping[int, frozenset[int]],
               members: set[int]) -> set[frozenset[int]]:
    return {of_listing.get(listing_id, frozenset({listing_id})) for listing_id in members}


def _merge_index(pairs: Mapping[tuple[int, int], Pair]) -> dict[int, set[int]]:
    """`listing -> the listings it holds a MERGE edge to`, both directions."""
    out: dict[int, set[int]] = {}
    for (lo, hi), pair in pairs.items():
        if pair.zone != "merge":
            continue
        out.setdefault(lo, set()).add(hi)
        out.setdefault(hi, set()).add(lo)
    return out


def _merge_edges(index: Mapping[int, set[int]], members: set[int]) -> list[list[int]]:
    return sorted([listing_id, other]
                  for listing_id in members
                  for other in index.get(listing_id, ())
                  if listing_id < other and other in members)


def _attribute_clusters(diff: Mapping[str, Any], arrived: Collection[int], known: set[int],
                        held_edges: Collection[tuple[int, int]]) -> dict[str, Any]:
    """Name every cluster component's cause (E119, E918).

    A component is explained by what moved its MERGE EDGES. When every edge one side holds and
    the other does not touches a listing that arrived after the batch cohort was cut (or one the
    live store has not fingerprinted), it is `arrival`; when every such edge is either that or
    a batch merge the live side is HOLDING for evidence (E908), it is `evidence_hold` — the
    group the lane will form once the photographs land. Any other moved edge makes it
    `upstream_pair`: the pair grain has already accounted for it, and counting it twice would
    double-report one cause. What is LEFT — the same edges, a different partition — is the
    finding, the exact shape E114 wore, unless taking the arrivals out of both partitions makes
    them equal (a listing the batch never had, joined by no edge of its own)."""
    left, right = diff["left"], diff["right"]
    only_live = sorted(left.keys() - right.keys(), key=sorted)
    only_batch = sorted(right.keys() - left.keys(), key=sorted)
    shared = left.keys() & right.keys()
    keys_identical = all(sorted(left[members]) == sorted(right[members]) for members in shared)
    held = {tuple(edge) for edge in held_edges}

    causes: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for component in diff["components"]:
        members = set(component["listings"])
        new = {i for i in members if i in arrived or i not in known}
        moved = ({tuple(edge) for edge in component["live_edges"]}
                 ^ {tuple(edge) for edge in component["batch_edges"]})
        held_here = sorted(edge for edge in moved if edge in held)
        rest = [edge for edge in moved if edge not in held and not (set(edge) & new)]
        if moved and not rest:
            cause = EVIDENCE_HOLD if held_here else "arrival"
        elif moved:
            cause = "upstream_pair"
        elif new and _partition(component["live"], new) == _partition(component["batch"], new):
            cause = "arrival"
        else:
            cause = "unexplained"
        causes[cause] = causes.get(cause, 0) + 1
        if len(examples) < MAX_EXAMPLES:
            examples.append({"cause": cause, **component,
                             **({"arrived": sorted(new)} if new else {}),
                             **({"held_edges": [list(edge) for edge in held_here]}
                                if held_here else {})})
    return {
        "live": len(left),
        "batch": len(right),
        "shared": len(shared),
        "member_sets_identical": not only_live and not only_batch,
        "keys_identical": bool(keys_identical),
        "only_live": len(only_live),
        "only_batch": len(only_batch),
        "only_live_examples": [sorted(members) for members in only_live[:MAX_EXAMPLES]],
        "only_batch_examples": [sorted(members) for members in only_batch[:MAX_EXAMPLES]],
        "components": len(diff["components"]),
        "component_listings": len(diff["listings"]),
        "component_causes": causes,
        "component_examples": examples,
    }
