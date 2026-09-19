"""`mode=score` — run the engine over one exported cohort and PERSIST the result (§8, §13).

The pass in one sentence: download a finished `export` run's cohort artifact, run the whole
engine over it in-process, and write what it decided into `autodedup.pairs`,
`autodedup.clusters`, `autodedup.cluster_members` and `autodedup.cluster_conflicts` under one
`autodedup.runs` row — so the W5 validation UI reads a table instead of an artifact nobody
can filter.

Three things this lane owns, each paid for elsewhere in this repo:

  * A GENERATION is rebuilt WHOLE, in ONE transaction. Clustering is union-find: identities
    are reassigned every pass, so a cluster that lost a member is not an update of anything.
    The generation's `clusters`/`cluster_members`/`cluster_conflicts` rows are deleted and
    re-inserted together, never merged into, and a pass that dies mid-rebuild leaves the
    previous generation intact rather than clusters without members. `pairs` is the exception
    — it is keyed on the pair itself, is latest-wins by design, and carries verdict/judgement
    joins that must survive a re-score.
  * The run row is OPENED BEFORE THE ENGINE and closed on both paths. The engine phase is the
    long one and the one that dies (a bad artifact, an OOM), so a row that appeared only
    afterwards would leave exactly that failure invisible — `/autodedup/stats` would keep
    reporting the previous success as the last score run. A dead pass leaves `status='failed'`
    plus the scrubbed error text, not a `running` row nobody can tell from a lane at work.
  * Nothing is applied (ruling D4). `clusters.status` is always `proposed`, `property_id` and
    `pairs.applied_merge_group` are never written, and no `public.*` relation is touched.

The engine itself lives in `autodedup.harness`; this module is the plumbing around it — the
artifact download (shared with the judge lane), the argument surface, and the writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from autodedup import features, harness
from autodedup.dataset import load
from autodedup.judge_lane import COHORT_FILE, download_cohort
from autodedup.model import LogisticModel, hand_initialised
from autodedup.score_sql import (
    CLUSTER_CONFLICT_INSERT_SQL,
    CLUSTER_CONFLICTS_DELETE_SQL,
    CLUSTER_INSERT_SQL,
    CLUSTER_MEMBER_INSERT_SQL,
    CLUSTER_MEMBERS_DELETE_SQL,
    CLUSTERS_DELETE_SQL,
    JUDGED_EDGES_SQL,
    MUST_NOT_LINK_SQL,
    PAIR_CLUSTER_ORPHAN_CLEAR_SQL,
    PAIR_UPSERT_SQL,
    RUN_FINISH_SQL,
    RUN_START_SQL,
    STORE_PRESENT_SQL,
)
from autodedup.settings import Settings

RUN_FILE: str = "run.json"
SUMMARY_FILE: str = "score.json"
DEFAULT_GENERATION: str = "g1"
CLUSTER_STATUS: str = "proposed"

# `autodedup.pairs.feature_version` is a smallint stamped on every row so a later pass can tell
# which feature vocabulary produced a score. The number belongs to the VOCABULARY, so it is
# imported, never restated here: a second constant would let a stored v1 vector and a v2 vector
# carry the same stamp the day FEATURE_ORDER changes shape.
FEATURE_VERSION: int = features.FEATURE_VERSION

# One prepared plan, many rows: psycopg pipelines an executemany. The chunk bounds the
# PARAMETER BATCH a single execute carries (a 43k-pair cohort in one call is megabytes of
# bound parameters), not the transaction — the writes still land together.
CHUNK: int = 1_000

# `--args settings=sweep_a` resolves inside the repo, never off the wire: a lane input is an
# operator string and a bare path would make `settings=/etc/passwd` a readable file.
SETTINGS_DIR: Path = Path(__file__).resolve().parent / "settings"
MODELS_DIR: Path = Path(__file__).resolve().parent / "models"

# `autodedup.pairs.families` is a bitmask, not an array: the UI filters on "has image evidence"
# across millions of rows, and `families & 32 > 0` is an index-friendly predicate where
# `'IMG' = any(families)` is not. The layout is fixed by the W5 contract; today's engine can
# only ever set five of the seven bits — `features.EVIDENCE_FAMILIES` is
# (IMG, TXT, LOC, BRK, ATTR), so PRICE and TIME are RESERVED and a UI filter on either of them
# would match nothing until a feature family claims the bit.
FAMILY_BITS: dict[str, int] = {
    "ATTR": 1,
    "PRICE": 2,
    "TXT": 4,
    "BRK": 8,
    "LOC": 16,
    "IMG": 32,
    "TIME": 64,
}

# `decide.Decision.zone` has four values; `autodedup.pairs.zone` has three (migration 528's
# CHECK). A guard veto IS a rejection — it is the strongest one there is — and the reason it
# was rejected survives in `guard_veto` and `decision`, so the fourth value folds into
# `reject` rather than failing an insert on a store_floor of 0.
ZONE_OF: dict[str, str] = {"merge": "merge", "band": "band", "reject": "reject",
                          "veto": "reject"}

# Ruling: a pair whose galleries are mostly developer catalogue stock carries a photo warning
# on its cluster, because that is exactly the evidence the K-A/K-B certificates over-fire on.
SHARED_PHOTO_RATIO: float = 0.80

# `fingerprint.block_key_of`'s grain letters: c = část obce (a quarter), o = obec (a town).
BLOCK_GRAINS: frozenset[str] = frozenset({"c", "o"})

# The stored `[value, present]` reader is the harness's (E12) — one definition, not two.
# `feature_value` is the public name it should have; the private one is the fallback until
# the harness grows it, so a promotion there needs no change here.
feature_value: Callable[[dict[str, Any], str], float | None] = getattr(
    harness, "feature_value", harness._feat
)


# --- arguments -------------------------------------------------------------------------


@dataclass(slots=True)
class ScoreArgs:
    export_run: str
    cohort: str | None
    settings: str | None
    model: str | None
    generation: str
    store_floor: float | None


ARG_KEYS: tuple[str, ...] = (
    "export_run", "cohort", "settings", "model", "generation", "store_floor",
)


def repo_path(raw: str, base: Path, suffix: str = ".json") -> Path:
    """`sweep_a` or `sweep_a.json` -> `<base>/sweep_a.json`, refusing anything outside `base`."""
    name = raw if raw.endswith(suffix) else f"{raw}{suffix}"
    resolved = (base / name).resolve()
    if base.resolve() not in resolved.parents:
        raise SystemExit(f"{raw!r} must name a file inside {base.name}/")
    if not resolved.is_file():
        raise SystemExit(f"no such file: {base.name}/{name}")
    return resolved


def parse_args(args: dict[str, str]) -> ScoreArgs:
    unknown = sorted(set(args) - set(ARG_KEYS))
    if unknown:
        raise SystemExit(
            f"unknown score arg(s) {', '.join(unknown)}; known: {', '.join(ARG_KEYS)}"
        )
    cohort = (args.get("cohort") or "").strip() or None
    export_run = (args.get("export_run") or "").strip()
    if not export_run and not cohort:
        raise SystemExit("export_run (the finished `export` lane run id) is required")
    if export_run and not export_run.isdigit():
        raise SystemExit(f"export_run must be a GitHub run id, got {export_run!r}")

    generation = (args.get("generation") or "").strip() or DEFAULT_GENERATION
    raw_floor = (args.get("store_floor") or "").strip()
    store_floor: float | None = None
    if raw_floor:
        try:
            store_floor = float(raw_floor)
        except ValueError as exc:
            raise SystemExit(f"store_floor must be a number, got {raw_floor!r}") from exc

    return ScoreArgs(
        export_run=export_run,
        cohort=cohort,
        settings=(args.get("settings") or "").strip() or None,
        model=(args.get("model") or "").strip() or None,
        generation=generation,
        store_floor=store_floor,
    )


def load_settings(parsed: ScoreArgs) -> Settings:
    """The swept row, plus the one dial the lane may override. `replace` re-validates."""
    settings = Settings.from_json(repo_path(parsed.settings, SETTINGS_DIR)) \
        if parsed.settings else Settings()
    if parsed.store_floor is None:
        return settings
    return replace(settings, store_floor=parsed.store_floor)


def load_model(parsed: ScoreArgs) -> LogisticModel:
    if not parsed.model:
        return hand_initialised()
    path = repo_path(parsed.model, MODELS_DIR)
    return LogisticModel.from_json(json.loads(path.read_text(encoding="utf-8")))


def fingerprint_of(settings: Settings, model: LogisticModel, generation: str) -> str:
    """What made this pass reproducible, as one digest: settings row + model + generation.

    Two runs with the same fingerprint scored the same cohort the same way — which is what
    makes a re-score of an unchanged configuration a no-op worth recognising."""
    payload = json.dumps(
        {
            "settings": settings.to_dict(),
            "model": model.to_json(),
            "generation": generation,
            "feature_version": FEATURE_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- row shaping -------------------------------------------------------------------------


def families_bitmask(names: Iterable[str]) -> int:
    mask = 0
    for name in names:
        mask |= FAMILY_BITS.get(str(name), 0)
    return mask


def present_features(row: dict[str, Any]) -> dict[str, list[Any]]:
    """Only what the pair actually HAS. An absent feature is unknown, never zero (E12), and a
    row that spells out all 44 absences costs more jsonb than the evidence it carries."""
    out: dict[str, list[Any]] = {}
    for name, entry in (row.get("feats") or {}).items():
        if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not entry[1]:
            continue
        try:
            out[str(name)] = [float(entry[0]), True]
        except (TypeError, ValueError):
            continue
    return out


def storable(row: dict[str, Any], store_floor: float) -> bool:
    """What lands in `autodedup.pairs`: the whole band and merge zone whatever it scored, plus
    the reject tail at or above the floor. The harness applies the same predicate when it
    writes the artifact, so this is the contract stated rather than re-derived."""
    zone = str(row.get("zone") or "")
    if zone in ("merge", "band"):
        return True
    try:
        return float(row.get("score") or 0.0) >= store_floor
    except (TypeError, ValueError):
        return False


def membership_of(clusters: dict[str, Any]) -> dict[int, int]:
    """listing id -> cluster key, from `clusters.json`'s `{key: [members]}`."""
    out: dict[int, int] = {}
    for key, members in (clusters.get("clusters") or {}).items():
        for listing_id in members or ():
            out[int(listing_id)] = int(key)
    return out


def pair_params(
    row: dict[str, Any], membership: dict[int, int], model_version: str
) -> dict[str, Any]:
    # `autodedup_pairs_order_ck` refuses lo >= hi, and one reversed row aborts the whole
    # chunk it travels in; `decide` orders its pairs today, so this is the cheap belt.
    lo, hi = sorted((int(row["lo"]), int(row["hi"])))
    cluster_lo, cluster_hi = membership.get(lo), membership.get(hi)
    features = present_features(row)
    return {
        "listing_lo": lo,
        "listing_hi": hi,
        "probes": sorted(str(probe) for probe in (row.get("probes") or ())),
        "families": families_bitmask(row.get("families") or ()),
        "features": json.dumps(features, ensure_ascii=False, sort_keys=True),
        "score": float(row.get("score") or 0.0),
        "zone": ZONE_OF.get(str(row.get("zone") or ""), "reject"),
        "decision": str(row.get("reason") or row.get("decision") or "") or None,
        "guard_veto": row.get("veto") or None,
        # A pair is a cluster's EDGE only when both of its sides landed in that cluster; a
        # merge edge the invariants refused sits across two clusters and carries neither.
        "cluster_key": cluster_lo if cluster_lo is not None and cluster_lo == cluster_hi
        else None,
        "feature_version": FEATURE_VERSION,
        "model_version": model_version,
    }


def block_key_of(keys: Sequence[str]) -> tuple[int | None, str | None]:
    """`["c490245"]` -> `(490245, "c")`. The engine's block key is grain-PREFIXED text so that a
    část code can never collide numerically with an obec code (`fingerprint.block_key_of`),
    while `clusters.block_key` is a bigint — so the letter is stored beside the code in
    `block_grain` (migration 529) instead of being dropped, which would have merged the two
    grains into one filter value. A cluster spanning two blocks stores neither."""
    if len(keys) != 1:
        return None, None
    raw = str(keys[0])
    grain, digits = raw[:1], raw[1:]
    if grain not in BLOCK_GRAINS or not digits.isdigit():
        return None, None
    return int(digits), grain


def _single(values: Sequence[Any]) -> Any | None:
    """A cluster attribute is the one value every member agrees on, or nothing at all."""
    return values[0] if len(values) == 1 else None


def _max_int(values: Iterable[float | None]) -> int | None:
    present = [value for value in values if value is not None]
    return int(round(max(present))) if present else None


def edges_by_cluster(
    rows: Sequence[dict[str, Any]], membership: dict[int, int]
) -> dict[int, list[dict[str, Any]]]:
    """The accepted merge edges INSIDE each cluster — every cluster statistic reads these."""
    edges: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("zone")) != "merge":
            continue
        lo, hi = int(row["lo"]), int(row["hi"])
        key = membership.get(lo)
        if key is None or key != membership.get(hi):
            continue
        edges.setdefault(key, []).append(row)
    return edges


def medoid_of(members: Sequence[int], edges: Sequence[dict[str, Any]]) -> int | None:
    """The most central member: the one carrying the most accepted-edge weight (E36's
    attachment point). Ties break on the smallest id, so the value is reproducible."""
    if not members:
        return None
    weight: dict[int, float] = {listing_id: 0.0 for listing_id in members}
    for edge in edges:
        score = float(edge.get("score") or 0.0)
        for side in ("lo", "hi"):
            listing_id = int(edge[side])
            if listing_id in weight:
                weight[listing_id] += score
    return min(members, key=lambda listing_id: (-weight[listing_id], listing_id))


def joined_via(
    members: Sequence[int], edges: Sequence[dict[str, Any]]
) -> dict[int, tuple[int, int] | None]:
    """Each member's strongest incident edge. `cluster_pairs` does not export the union ORDER,
    so this is the best reconstruction of "how did this listing get in" the artifact allows —
    and it is the edge the reviewer wants to see first either way."""
    best: dict[int, tuple[int, int] | None] = {listing_id: None for listing_id in members}
    ranked = sorted(
        edges,
        key=lambda edge: (0 if edge.get("certificate") else 1,
                          -float(edge.get("score") or 0.0), int(edge["lo"]), int(edge["hi"])),
    )
    for edge in ranked:
        lo, hi = int(edge["lo"]), int(edge["hi"])
        for listing_id in (lo, hi):
            if listing_id in best and best[listing_id] is None:
                best[listing_id] = (lo, hi)
    return best


def cluster_params(
    row: dict[str, Any],
    edges: Sequence[dict[str, Any]],
    judged: set[tuple[int, int]],
    model_version: str,
    generation: str,
) -> dict[str, Any]:
    members = [int(listing_id) for listing_id in row.get("members") or ()]
    block_key, block_grain = block_key_of(row.get("block_key") or ())
    return {
        "cluster_key": int(row["cluster_key"]),
        "generation": generation,
        "size": int(row.get("size") or len(members)),
        "block_key": block_key,
        "block_grain": block_grain,
        "cat_group": _single(row.get("cat_group") or ()),
        "category_main": _single(row.get("category_main") or ()),
        "category_type": _single(row.get("category_type") or ()),
        "area_min": row.get("area_min"),
        "area_max": row.get("area_max"),
        "sources": sorted(str(source) for source in (row.get("sources") or ())),
        "medoid_listing_id": medoid_of(members, edges),
        "min_edge_score": row.get("min_edge_score"),
        "mean_edge_score": row.get("mean_edge_score"),
        "n_judged_edges": sum(
            1 for edge in edges if (int(edge["lo"]), int(edge["hi"])) in judged
        ),
        "n_certificate_edges": int(row.get("n_certificate_edges") or 0),
        "evidence_families": families_bitmask(row.get("evidence_families") or ()),
        "max_gap_days": _max_int(feature_value(edge, "gap_days") for edge in edges),
        "shared_photo_warning": any(
            (feature_value(edge, "catalog_ratio_max") or 0.0) >= SHARED_PHOTO_RATIO
            for edge in edges
        ),
        "status": CLUSTER_STATUS,
        "model_version": model_version,
        "feature_version": FEATURE_VERSION,
    }


def conflict_params(
    clusters: dict[str, Any], membership: dict[int, int], generation: str
) -> list[dict[str, Any]]:
    """Refused unions and refused bridges — §8 calls these the highest-value rows in the UI,
    because a conflict means the engine found strong evidence in both directions."""
    rows: list[dict[str, Any]] = []
    for conflict in clusters.get("conflicts") or ():
        lo, hi = sorted((int(conflict["lo"]), int(conflict["hi"])))
        rows.append({
            "kind": "invariant",
            "cluster_key_a": membership.get(lo),
            "cluster_key_b": membership.get(hi),
            "listing_lo": lo,
            "listing_hi": hi,
            "invariant": str(conflict.get("invariant") or "") or None,
            "detail": json.dumps(
                {
                    "generation": generation,
                    "score": conflict.get("score"),
                    "certificate": conflict.get("certificate"),
                    "families": conflict.get("families") or [],
                    "members": conflict.get("members") or [],
                },
                ensure_ascii=False, sort_keys=True,
            ),
        })
    for bridge in clusters.get("bridges") or ():
        # E57: an APPLIED bridge is a union, not a conflict — it has no evidence in two
        # directions left to show.
        if bridge.get("applied"):
            continue
        lo, hi = sorted((int(bridge["lo"]), int(bridge["hi"])))
        rows.append({
            "kind": "bridge",
            "cluster_key_a": bridge.get("left_cluster"),
            "cluster_key_b": bridge.get("right_cluster"),
            "listing_lo": lo,
            "listing_hi": hi,
            "invariant": None,
            "detail": json.dumps(
                {
                    "generation": generation,
                    "score": bridge.get("score"),
                    "certificate": bridge.get("certificate"),
                    "families": bridge.get("families") or [],
                    "left_members": bridge.get("left_members") or [],
                    "right_members": bridge.get("right_members") or [],
                },
                ensure_ascii=False, sort_keys=True,
            ),
        })
    return rows


# --- the store ---------------------------------------------------------------------------


def _chunks(rows: Sequence[dict[str, Any]], size: int = CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _fetchall(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or ())


def _fetchone(conn: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _value(row: Any, key: str) -> Any:
    if row is None:
        return None
    return row[0] if isinstance(row, (list, tuple)) else row.get(key)


def store_ready(conn: Any) -> bool:
    """Unlike the progress ledger, an absent store is FATAL here — see `run_score`."""
    if conn is None:
        return False
    try:
        return bool(_value(_fetchone(conn, STORE_PRESENT_SQL), "present"))
    except Exception:  # noqa: BLE001 — an unreadable probe reads as "not ready"
        return False


def _execute(conn: Any, sql: str, params: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def insert_chunked(conn: Any, sql: str, rows: Sequence[dict[str, Any]]) -> int:
    """Rows in bounded batches, inside whatever transaction the caller opened."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        for chunk in _chunks(rows):
            cur.executemany(sql, chunk)
    return len(rows)


def execute_many(conn: Any, sql: str, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.transaction():
        return insert_chunked(conn, sql, rows)


def start_run(conn: Any, params: dict[str, Any]) -> int | None:
    return _int_or_none(_value(_fetchone(conn, RUN_START_SQL, params), "id"))


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def finish_run(
    conn: Any,
    run_id: int | None,
    *,
    status: str,
    cohort: Any = None,
    stats: Any = None,
    error: str | None = None,
) -> None:
    if run_id is None:
        return
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(RUN_FINISH_SQL, {
                "id": int(run_id),
                "status": status,
                "cohort": _json_or_none(cohort),
                "stats": _json_or_none(stats),
                "error": error,
            })


def _scrub(text: str) -> str:
    """`lane.scrub`, imported late: `lane` imports THIS module at import time."""
    from autodedup.lane import scrub

    return scrub(text)


def fail_run(conn: Any, run_id: int | None, exc: BaseException) -> None:
    """Bookkeeping never replaces the failure it records: the likeliest reason a write died is
    a dead connection, and that would make this UPDATE raise over the real exception."""
    try:
        finish_run(conn, run_id, status="failed",
                   error=_scrub(f"{type(exc).__name__}: {exc}")[:2000])
    except Exception:  # noqa: BLE001 — the original exception is the result
        pass


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def read_must_not_link(conn: Any) -> frozenset[tuple[int, int]]:
    """E27's permanent negatives: an operator's `different` verdict binds the NEXT pass."""
    rows = _fetchall(conn, MUST_NOT_LINK_SQL)
    pairs: set[tuple[int, int]] = set()
    for row in rows:
        lo, hi = (row[0], row[1]) if isinstance(row, (list, tuple)) else (
            row.get("listing_lo"), row.get("listing_hi")
        )
        pairs.add((int(min(lo, hi)), int(max(lo, hi))))
    return frozenset(pairs)


def read_judged_edges(
    conn: Any, edges: Sequence[tuple[int, int]]
) -> set[tuple[int, int]]:
    if not edges:
        return set()
    rows = _fetchall(conn, JUDGED_EDGES_SQL, {
        "los": [lo for lo, _ in edges],
        "his": [hi for _, hi in edges],
    })
    judged: set[tuple[int, int]] = set()
    for row in rows:
        lo, hi = (row[0], row[1]) if isinstance(row, (list, tuple)) else (
            row.get("listing_lo"), row.get("listing_hi")
        )
        judged.add((int(lo), int(hi)))
    return judged


def persist(
    conn: Any,
    *,
    rows: Sequence[dict[str, Any]],
    clusters: dict[str, Any],
    generation: str,
    model_version: str,
    store_floor: float,
) -> dict[str, int]:
    """Pairs, then the generation rebuilt whole. Returns what each relation received."""
    membership = membership_of(clusters)
    stored = [row for row in rows if storable(row, store_floor)]
    pairs = [pair_params(row, membership, model_version) for row in stored]
    n_pairs = execute_many(conn, PAIR_UPSERT_SQL, pairs)

    edges = edges_by_cluster(rows, membership)
    all_edges = sorted(
        {(int(edge["lo"]), int(edge["hi"])) for bucket in edges.values() for edge in bucket}
    )
    judged = read_judged_edges(conn, all_edges)

    cluster_rows: list[dict[str, Any]] = []
    member_rows: list[dict[str, Any]] = []
    for row in clusters.get("rows") or ():
        key = int(row["cluster_key"])
        bucket = edges.get(key, [])
        cluster_rows.append(cluster_params(row, bucket, judged, model_version, generation))
        members = [int(listing_id) for listing_id in row.get("members") or ()]
        via = joined_via(members, bucket)
        for listing_id in members:
            edge = via.get(listing_id)
            member_rows.append({
                "cluster_key": key,
                "listing_id": listing_id,
                "joined_via_lo": edge[0] if edge else None,
                "joined_via_hi": edge[1] if edge else None,
            })
    conflicts = conflict_params(clusters, membership, generation)
    keys = sorted({int(row["cluster_key"]) for row in cluster_rows})

    # The sweep and the rebuild are ONE transaction: half a generation — clusters without
    # members, or a deleted generation never re-inserted — is what the UI would render.
    scope = {"generation": generation, "keys": keys}
    with conn.transaction():
        _execute(conn, CLUSTER_MEMBERS_DELETE_SQL, scope)
        _execute(conn, CLUSTER_CONFLICTS_DELETE_SQL, {"generation": generation})
        _execute(conn, CLUSTERS_DELETE_SQL, scope)
        n_clusters = insert_chunked(conn, CLUSTER_INSERT_SQL, cluster_rows)
        n_members = insert_chunked(conn, CLUSTER_MEMBER_INSERT_SQL, member_rows)
        n_conflicts = insert_chunked(conn, CLUSTER_CONFLICT_INSERT_SQL, conflicts)
        _execute(conn, PAIR_CLUSTER_ORPHAN_CLEAR_SQL)
    return {
        "pairs_upserted": n_pairs,
        "clusters": n_clusters,
        "cluster_members": n_members,
        "cluster_conflicts": n_conflicts,
        "judged_edges": len(judged),
    }


def _close(conn: Any) -> None:
    close = getattr(conn, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — a closed-connection error is not a result
            pass


# --- mode entry --------------------------------------------------------------------------


def run_score(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    parsed = parse_args(args)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings(parsed)
    model = load_model(parsed)

    cohort_path = (
        Path(parsed.cohort)
        if parsed.cohort
        else download_cohort(parsed.export_run, out_dir / "artifact")
    )
    if not cohort_path.is_file():
        raise SystemExit(f"no cohort artifact at {cohort_path}")

    conn = conn_factory()
    try:
        if not store_ready(conn):
            raise SystemExit(
                "autodedup.runs/pairs/clusters are absent — apply migration 528 before "
                "scoring; this lane's deliverable IS the stored pass"
            )
        # E27/E33: read FIRST, because it is an input to the clustering this row will record.
        must_not_link = read_must_not_link(conn)

        run_id = start_run(conn, {
            "fingerprint": fingerprint_of(settings, model, parsed.generation),
            "params": json.dumps({
                "generation": parsed.generation,
                "artifact": str(cohort_path),
                "export_run": parsed.export_run or None,
                "settings": settings.to_dict(),
                "settings_path": parsed.settings,
                "model_path": parsed.model,
                "model_version": model.version,
                "feature_version": FEATURE_VERSION,
                "n_must_not_link": len(must_not_link),
            }, ensure_ascii=False, sort_keys=True, default=str),
        })

        try:
            dataset = load(cohort_path)
            engine = harness.run_engine(dataset, settings, model, out_dir, must_not_link)
            engine["artifact"] = str(cohort_path)
            engine["generation"] = parsed.generation
            (out_dir / RUN_FILE).write_text(
                json.dumps(engine, indent=2, sort_keys=True), encoding="utf-8"
            )
            rows = harness.read_pairs(out_dir)
            clusters = json.loads(
                (out_dir / harness.CLUSTERS_FILE).read_text(encoding="utf-8")
            )
            counts = persist(
                conn,
                rows=rows,
                clusters=clusters,
                generation=parsed.generation,
                model_version=model.version,
                store_floor=settings.store_floor,
            )
            cohort = {
                "artifact": str(cohort_path),
                "export_run": parsed.export_run or None,
                "blocks": [
                    {"key": block.key, "grain": block.grain, "code": block.code,
                     "label": block.label}
                    for block in dataset.meta.blocks
                ],
                "n_listings": engine.get("n_listings"),
                "n_images": engine.get("n_images"),
            }
        except Exception as exc:  # noqa: BLE001 — a dead pass must not stay `running`
            fail_run(conn, run_id, exc)
            raise

        counts.update({
            "pairs_scored": engine.get("pairs_scored"),
            "pairs_stored_in_artifact": engine.get("pairs_stored"),
            # What the engine scored and never stored: the reject tail below the floor.
            "pairs_below_floor": max(
                int(engine.get("pairs_scored") or 0) - int(counts["pairs_upserted"]), 0
            ),
            **{f"zone_{zone}": count for zone, count in (engine.get("zones") or {}).items()},
            **{f"certificate_{name}": count
               for name, count in (engine.get("certificates") or {}).items()},
        })
        summary: dict[str, Any] = {
            "generation": parsed.generation,
            "export_run": parsed.export_run or None,
            "cohort": str(cohort_path),
            "run_id": run_id,
            "model_version": model.version,
            "feature_version": FEATURE_VERSION,
            "store_floor": settings.store_floor,
            "n_must_not_link": len(must_not_link),
            "counts": counts,
            "timings": engine.get("timings") or {},
            "band_width": engine.get("band_width"),
            "clusters_stats": engine.get("clusters") or {},
        }
        finish_run(conn, run_id, status="success", cohort=cohort, stats=engine)
    finally:
        _close(conn)

    (out_dir / SUMMARY_FILE).write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    return summary
