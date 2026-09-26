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

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from autodedup.features import (
    CALIBRATION_FEATURES,
    CLOCK_FEATURES,
    ClockFacts,
    WindowRule,
    clock_features,
)
from autodedup.harness import model_of_version
from autodedup.incremental import GENERATION
from autodedup.incremental_lane import (
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


def scope_listings(conn: Any, generation: str, scope: Any) -> tuple[set[int], str]:
    """The live generation's own membership, and where it was read from.

    `autodedup.rt_scope_ids` is the snapshot the entrant feed claims out of, so it is the
    generation's OWN answer to "what does my scope hold" and it costs `public` nothing. A
    generation whose snapshot is empty — one seeded and never passed — has to fall back to the
    block walk, and the report says which it used, because a comparison scoped by one and a
    build scoped by the other would be two different questions."""
    ids = {int(row[0]) for row in _rows(conn, RT_EQUIV_SCOPE_IDS_SQL,
                                        {"generation": generation})}
    if ids:
        return ids, "rt_scope_ids"
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
        ids.update(int(row[0]) for row in _rows(conn, RT_SCOPE_BLOCK_SQL, {
            "obec": obec, "cast_obce": cast_obce, "limit": 1_000_000}))
    return ids, "listing_location"


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


def batch_settings(conn: Any, generation: str) -> tuple[dict[str, Any] | None, Any]:
    """The settings the batch generation's newest successful score pass recorded, and when
    that pass finished."""
    rows = _rows(conn, RT_EQUIV_BATCH_SETTINGS_SQL, {"generation": generation})
    if not rows or rows[0][0] is None:
        return None, (rows[0][2] if rows else None)
    blob = rows[0][0]
    return (dict(blob) if isinstance(blob, Mapping) else json.loads(blob or "{}"),
            rows[0][2])


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


def cause_of(pair: Pair, *, side: str, in_scope: set[int], known: set[int],
             seen: Mapping[int, Any], exported_at: Any, store_floor: float) -> str:
    """Why exactly one side holds this pair — or `unexplained`, which fails the verdict.

    The order is the order of AUTHORITY. A pair whose endpoint the scope does not hold was
    never this generation's to decide; one whose endpoint the live store has no fingerprint for
    has not been built yet; one whose endpoint arrived after the export could not have been in
    the batch cohort at all; and only then do the two storage rules — the floor and what is
    still sitting in the store under an older floor — get to explain anything."""
    if not ({pair.lo, pair.hi} <= in_scope):
        return "scope"
    if not ({pair.lo, pair.hi} <= known):
        return "not_in_live_store"
    if exported_at is not None and side == "live":
        for listing_id in (pair.lo, pair.hi):
            stamp = seen.get(listing_id)
            if stamp is not None and stamp > exported_at:
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
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
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
        in_scope, scope_source = scope_listings(conn, generation, scope)
        score_type = store_score_type(conn)
        batch_blob, batch_finished = batch_settings(conn, batch)
        # The batch cohort's cut: the export time when the dispatch names it, else the batch
        # pass's own finish — LATER than the export, so an arrival between the two reads as
        # unexplained rather than excused (the instrument fails closed without the stamp).
        exported_raw = str(args.get("exported_at") or "").strip() or batch_finished
        live_pairs = read_pairs(conn, generation)
        batch_pairs = read_pairs(conn, batch)
        live_clusters = read_clusters(conn, generation)
        batch_clusters = read_clusters(conn, batch)

        # The effective tolerance is never finer than the store's own resolution (M178).
        tol = max(asked_tol, TOL_STORE_ULPS * store_eps(score_type)
                  if score_type in ("real", "double precision") else asked_tol)
        asymmetry = asymmetric_fields(live_pairs, batch_pairs)
        asymmetric = sorted({str(entry["field"]) for entry in asymmetry})

        only_live = sorted(set(live_pairs) - set(batch_pairs))
        only_batch = sorted(set(batch_pairs) - set(live_pairs))
        both = sorted(set(live_pairs) & set(batch_pairs))
        differing_keys = [key for key in both
                          if differences(live_pairs[key], batch_pairs[key], tol)]
        # The nine this mode always runs: the calibration, the control settings, the scope
        # snapshot, the score column's type, the batch pass's settings, two pair reads and two
        # membership reads. Everything after it is bounded by what actually differs.
        statements = 9
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

    exported_at = None
    if exported_raw is not None and seen:
        # The baseline's stamp is TEXT in a settings row and the live column is a timestamp, so
        # one of the two has to be converted — and it is the stamp, once, rather than every row.
        sample = next(iter(seen.values()), None)
        exported_at = _as_stamp(exported_raw, sample)

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
    for key in differing_keys:
        live, batch_pair = live_pairs[key], batch_pairs[key]
        moved = differences(live, batch_pair, tol)
        features = moved_features(live, batch_pair, tol)
        cause = shared_cause(live, batch_pair, moved, features, asymmetric=asymmetric,
                             one_clock=one_clock, facts=facts, settings=clock_settings,
                             tol=tol)
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
            cause = cause_of(store[key], side=side, in_scope=in_scope, known=known, seen=seen,
                             exported_at=exported_at, store_floor=store_floor)
            causes[side][cause] = causes[side].get(cause, 0) + 1
            if cause == "unexplained" and len(unexplained) < MAX_EXAMPLES:
                unexplained.append({"side": side, **store[key].to_json()})
    one_sided = {cause: causes["live"].get(cause, 0) + causes["batch"].get(cause, 0)
                 for cause in set(causes["live"]) | set(causes["batch"])}

    clusters = _attribute_clusters(cluster_diff, seen, exported_at, known)
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
            "shared_unexplained_examples": shared_unexplained,
            "differing_examples": differing_examples,
            "only_live": len(only_live),
            "only_batch": len(only_batch),
            "causes": one_sided,
            "causes_by_side": causes,
            "unexplained_examples": unexplained,
        },
        "clusters": clusters,
        "verdict": {"ok": not reasons, "reasons": reasons},
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


def _as_stamp(raw: Any, like: Any) -> Any:
    """The baseline's `exported_at` in whatever type the live column hands back."""
    if hasattr(raw, "isoformat") or like is None:
        return raw
    parse = getattr(type(like), "fromisoformat", None)
    if parse is None or not isinstance(raw, str):
        return None
    try:
        return parse(raw)
    except ValueError:
        return None


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


def _attribute_clusters(diff: Mapping[str, Any], seen: Mapping[int, Any], exported_at: Any,
                        known: set[int]) -> dict[str, Any]:
    """Name every cluster component's cause, in the order of AUTHORITY (E119).

    `arrival` first: a listing the batch cohort never held cannot be clustered the same way on
    both sides, and it would otherwise show up as a difference in the edge set and be filed
    under the pair grain that legitimately explains it. Then `upstream_pair`: the two sides
    hold different merge edges, so the pair grain has already accounted for this component and
    counting it twice would double-report one cause. What is LEFT — the same edges, a
    different partition — is the finding, and it is the exact shape E114 wore."""
    left, right = diff["left"], diff["right"]
    only_live = sorted(left.keys() - right.keys(), key=sorted)
    only_batch = sorted(right.keys() - left.keys(), key=sorted)
    shared = left.keys() & right.keys()
    keys_identical = all(sorted(left[members]) == sorted(right[members]) for members in shared)

    causes: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for component in diff["components"]:
        members = set(component["listings"])
        arrived = [i for i in sorted(members)
                   if i not in known
                   or (exported_at is not None and seen.get(i) is not None
                       and seen[i] > exported_at)]
        if arrived:
            cause = "arrival"
        elif component["live_edges"] != component["batch_edges"]:
            cause = "upstream_pair"
        else:
            cause = "unexplained"
        causes[cause] = causes.get(cause, 0) + 1
        if len(examples) < MAX_EXAMPLES:
            examples.append({"cause": cause, **component,
                             **({"arrived": arrived} if arrived else {})})
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
