"""`--mode rt_equivalence` — READ-ONLY. Is the LIVE store the batch engine's store? (E99)

The check the replay-equivalence proof cannot give. Replay feeds the incremental path from an
export ARTIFACT and compares it with the batch engine run over that same artifact, in one
process, from an EMPTY store: it proves the MECHANISM — same decisions, same clusters, same
edge order — and it has proved it four times (44,724 of 44,724 pairs, 849 of 849 clusters). It
says nothing about the rows a real generation actually holds, and twice now that was exactly
where the defect was: W9f's first live pass issued ZERO K-C against the batch generation's
1,771 (E90), and the 2026-09-20 re-seed left 15,923 pairs of a superseded scorer in place
because `reseed=true` re-cuts the calibration and deletes nothing (E97).

So this mode reads BOTH stores and diffs them. Given a live generation and a batch generation
scored on the SAME export with the same settings and model, inside the live generation's own
scope, it compares:

  * the pairs both sides hold — zone, certificate and score (within `tol`, default 1e-6);
  * the pairs only one side holds, each attributed to a CAUSE the two engines legitimately
    differ by (a scope the batch cohort is wider than, a listing the live build has not
    reached, an arrival after the export, the store floor, retention) — and anything left over
    is `unexplained`, which is what makes the verdict a verdict rather than a description;
  * the clusters, by member set and by key, trimmed to the scope on both sides.

It writes NOTHING — not a `public` row, not an `autodedup` row, not an `iterations` row. Its
deliverable is `out/rt_equivalence.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from autodedup.incremental import GENERATION
from autodedup.incremental_lane import (
    SCOPE_SETTING,
    lane_settings,
    parity_baseline_key,
    read_scope_setting,
    resolve_scope_parents,
    scope_setting_key,
)
from autodedup.incremental_scope import ScopeError, resolve_scope
from autodedup.incremental_sql import (
    RT_CALIBRATION_READ_SQL,
    RT_EQUIV_FIRST_SEEN_SQL,
    RT_EQUIV_MEMBERS_SQL,
    RT_EQUIV_PAIRS_SQL,
    RT_EQUIV_SCOPE_IDS_SQL,
    RT_KNOWN_SQL,
    RT_SCOPE_BLOCK_SQL,
)

EQUIVALENCE_FILE: str = "rt_equivalence.json"
MAX_EXAMPLES: int = 8
# What "the same score" means. The two sides compute the same features with the same model in
# the same process-local float64, so a difference above this is a difference in the INPUTS, not
# in the arithmetic — which is the whole point of the comparison.
SCORE_TOL: float = 1e-6
# The store floor the comparison falls back to when the generation's calibration row carries no
# settings blob. It is the shipped default and it is only ever a fallback: a floor read from the
# row is the floor the generation was actually written under.
DEFAULT_STORE_FLOOR: float = 0.02
# How many listing ids one `first_seen_at` read carries. The arrival cause only ever asks about
# the endpoints of one-sided pairs, which on the trial scope is a few hundred.
FIRST_SEEN_CHUNK: int = 5_000


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
                 "families", "model_version")

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


def first_seen(conn: Any, ids: Sequence[int]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    wanted = sorted({int(i) for i in ids})
    for start in range(0, len(wanted), FIRST_SEEN_CHUNK):
        for row in _rows(conn, RT_EQUIV_FIRST_SEEN_SQL,
                         {"ids": wanted[start:start + FIRST_SEEN_CHUNK]}):
            out[int(row[0])] = row[1]
    return out


def differences(left: Pair, right: Pair, tol: float) -> list[str]:
    """What moved between two rows for the same pair. Ordered so the report's counters read
    decision-first: a zone that moved is a different answer, a score that moved by 1e-5 is a
    different input to the same answer."""
    moved: list[str] = []
    if left.zone != right.zone:
        moved.append("zone")
    if left.certificate != right.certificate:
        moved.append("certificate")
    if left.guard_veto != right.guard_veto:
        moved.append("guard_veto")
    if (left.score is None) != (right.score is None):
        moved.append("score")
    elif left.score is not None and right.score is not None \
            and abs(left.score - right.score) > tol:
        moved.append("score")
    return moved


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
    tol = float(str(args.get("tol") or SCORE_TOL))

    conn = conn_factory()
    try:
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
        if not rows:
            raise SystemExit(
                f"generation {generation!r} has no frozen calibration — this mode compares the "
                "store a SEEDED generation holds, and an unseeded one holds nothing")
        settings_blob = rows[0][5]
        settings_json = (settings_blob if isinstance(settings_blob, dict)
                         else json.loads(settings_blob or "{}"))
        store_floor = float(settings_json.get("store_floor") or DEFAULT_STORE_FLOOR)
        live_model = None if rows[0][6] is None else str(rows[0][6])
        control = lane_settings(conn, [scope_setting_key(generation), SCOPE_SETTING,
                                       parity_baseline_key(generation)])
        try:
            scope = resolve_scope(None, read_scope_setting(control, generation))
        except ScopeError as exc:
            raise SystemExit(f"{SCOPE_SETTING}: {exc}") from exc
        baseline = control.get(parity_baseline_key(generation))
        exported_raw = (baseline or {}).get("exported_at") if isinstance(baseline, Mapping) \
            else None
        in_scope, scope_source = scope_listings(conn, generation, scope)
        live_pairs = read_pairs(conn, generation)
        batch_pairs = read_pairs(conn, batch)
        live_clusters = read_clusters(conn, generation)
        batch_clusters = read_clusters(conn, batch)

        only_live = sorted(set(live_pairs) - set(batch_pairs))
        only_batch = sorted(set(batch_pairs) - set(live_pairs))
        endpoints = {i for key in only_live + only_batch for i in key}
        known = known_listings(conn, generation, sorted(endpoints))
        seen = first_seen(conn, sorted(endpoints)) if exported_raw else {}
        statements = 8 + (1 if endpoints else 0) + (1 if seen else 0)
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

    both = sorted(set(live_pairs) & set(batch_pairs))
    by_field: dict[str, int] = {}
    differing_examples: list[dict[str, Any]] = []
    differing = 0
    for key in both:
        moved = differences(live_pairs[key], batch_pairs[key], tol)
        if not moved:
            continue
        differing += 1
        for field in moved:
            by_field[field] = by_field.get(field, 0) + 1
        if len(differing_examples) < MAX_EXAMPLES:
            differing_examples.append({
                "listing_lo": key[0], "listing_hi": key[1], "moved": moved,
                "live": live_pairs[key].to_json(), "batch": batch_pairs[key].to_json()})

    causes: dict[str, int] = {}
    unexplained: list[dict[str, Any]] = []
    for side, keys, store in (("live", only_live, live_pairs),
                              ("batch", only_batch, batch_pairs)):
        for key in keys:
            cause = cause_of(store[key], side=side, in_scope=in_scope, known=known, seen=seen,
                             exported_at=exported_at, store_floor=store_floor)
            causes[cause] = causes.get(cause, 0) + 1
            if cause == "unexplained" and len(unexplained) < MAX_EXAMPLES:
                unexplained.append({"side": side, **store[key].to_json()})

    clusters = _compare_clusters(live_clusters, batch_clusters, in_scope)
    live_versions = sorted({p.model_version for p in live_pairs.values()
                            if p.model_version is not None})
    batch_versions = sorted({p.model_version for p in batch_pairs.values()
                             if p.model_version is not None})
    reasons: list[str] = []
    if differing:
        reasons.append(f"{differing} of {len(both)} shared pairs differ")
    if causes.get("unexplained"):
        reasons.append(f"{causes['unexplained']} one-sided pairs have no cause")
    if not clusters["member_sets_identical"]:
        reasons.append("cluster member sets differ inside the scope")
    if causes.get("not_in_live_store"):
        reasons.append(f"{causes['not_in_live_store']} one-sided pairs touch a listing the "
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
        "score_tolerance": tol,
        "exported_at": _stamp(exported_raw),
        "model_version": {"calibration": live_model, "live_pairs": live_versions,
                          "batch_pairs": batch_versions},
        "pairs": {
            "live": len(live_pairs),
            "batch": len(batch_pairs),
            "both": len(both),
            "identical": differing == 0,
            "differing": differing,
            "by_field": by_field,
            "differing_examples": differing_examples,
            "only_live": len(only_live),
            "only_batch": len(only_batch),
            "causes": causes,
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


def _compare_clusters(live: Mapping[int, frozenset[int]],
                      batch: Mapping[int, frozenset[int]],
                      in_scope: set[int]) -> dict[str, Any]:
    """Member sets first, keys second — and both trimmed to the scope.

    The batch cohort is wider than the live scope (it carries the assembled negative control
    and two whole towns of it), so a batch cluster is compared by the members the live scope
    could ever have held. A cluster trimmed to fewer than two members is not a cluster on
    either side and is dropped rather than counted as a difference."""
    def trim(source: Mapping[int, frozenset[int]]) -> dict[frozenset[int], list[int]]:
        out: dict[frozenset[int], list[int]] = {}
        for key, members in source.items():
            inside = frozenset(members & in_scope)
            if len(inside) < 2:
                continue
            out.setdefault(inside, []).append(int(key))
        return out

    left, right = trim(live), trim(batch)
    only_live = sorted(left.keys() - right.keys(), key=sorted)
    only_batch = sorted(right.keys() - left.keys(), key=sorted)
    shared = left.keys() & right.keys()
    keys_identical = all(sorted(left[members]) == sorted(right[members]) for members in shared)
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
    }
