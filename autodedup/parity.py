"""`--mode rt_parity` — READ-ONLY. Are the live lane's facts the batch engine's facts?

W9f's first live pass scored 36,940 pairs against generation `rt` and issued 1,024 K-B, 75
K-R and ZERO K-C, while the batch generation `g6` over the same cohort issued 1,771 K-C; 5,888
of the 7,878 pairs both generations hold differ in score by more than 0.01. The
replay-equivalence proof (44,724 of 44,724) could not see any of it, because replay feeds the
incremental path from the EXPORT ARTIFACT while the live lane reads its facts through
`incremental_lane.SqlFacts` (E77 caught one instance of this class already: no attrs, no price
history, no `broker_key`).

So this mode holds everything but the facts constant. For a seeded, source-stratified sample of
listings present BOTH in an export run's cohort artifact and in the live database, it builds the
same `Listing`/`Image` pair twice — once from the artifact, once through `SqlFacts` — and diffs
them field by field; then it scores the same sampled pairs from both fact sets under ONE
settings/model/calibration and reports every feature and decision that moved. A listing whose
content changed after the export is genuine drift, not a defect, so it is reported by itself and
kept out of the stable counters.

It writes NOTHING — not to `public`, not to `autodedup` (it is absent from `lane.ITERATION_META`
on purpose, so not even an `iterations` row is filed). Its deliverable is `out/parity.json`.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from autodedup.dataset import Dataset, Image, Listing, load
from autodedup.decide import decide_pair
from autodedup.features import FEATURE_VERSION, pair_features
from autodedup.fingerprint import Fingerprint, build_fingerprint
# The instrument NAMES its scorer the way every other lane does (E83a): `settings=w8`
# was read as a path here and found no file, which is how a lane ends up running the
# uncalibrated prior while its operator believes it is running w8.
from autodedup.harness import named_model as load_model
from autodedup.harness import named_settings as load_settings
from autodedup.hazard_context import ContextIndex
from autodedup.incremental import GENERATION, Calibration, Keyer, context_for
from autodedup.incremental_lane import SqlFacts
from autodedup.incremental_sql import (
    RT_CALIBRATION_READ_SQL,
    RT_PARITY_CHANGE_SQL,
    RT_PHASH_POP_COUNT_SQL,
)
from autodedup.parity_digest import (
    baseline,
    compare,
    image_view,
    listing_view,
    stratified_sample,
)
from autodedup.judge_lane import COHORT_FILE, download_cohort
from autodedup.settings import Settings

PARITY_FILE = "parity.json"
MAX_EXAMPLES = 5
FLOAT_TOL = 1e-9

# The change stamp of a listing's CONTENT — the GATE's definition, imported rather than
# restated, so the instrument and the rail that runs every pass read drift the same way.
PARITY_CHANGE_SQL = RT_PARITY_CHANGE_SQL

# E70 freezes the corpus-wide pHash population in this table and `SqlFacts` joins every image
# against it. How many rows it actually holds is the first thing the report needs to say.
PARITY_PHASH_POP_SQL = RT_PHASH_POP_COUNT_SQL

# Presence for the WHOLE cohort in one statement: the sample is drawn from what is still there,
# and the count of what is not is a headline of its own. Full facts are then read for the
# sample alone rather than for 4,887 listings nobody asked to compare.
PARITY_PRESENT_SQL = """
select l.id as id
from listings l
where l.id = any(%(ids)s::bigint[])
"""


@dataclass(slots=True, frozen=True)
class ParityArgs:
    export_run: str
    cohort: str
    n: int
    pairs: int
    seed: int
    generation: str
    settings: str | None
    model: str | None
    population: str


def parse_args(args: Mapping[str, str]) -> ParityArgs:
    export_run = str(args.get("export_run") or "").strip()
    cohort = str(args.get("cohort") or "").strip()
    if not export_run and not cohort:
        raise SystemExit("export_run (the finished `export` lane run id) is required")
    if export_run and not export_run.isdigit():
        raise SystemExit(f"export_run must be a GitHub run id, got {export_run!r}")
    return ParityArgs(
        export_run=export_run,
        cohort=cohort,
        n=_positive(args, "n", 200),
        pairs=_positive(args, "pairs", 400),
        seed=int(str(args.get("seed") or 1)),
        generation=str(args.get("generation") or "").strip() or GENERATION,
        settings=str(args.get("settings") or "").strip() or None,
        model=str(args.get("model") or "").strip() or None,
        population=_population_source(args),
    )


POPULATION_SOURCES: tuple[str, ...] = ("frozen", "artifact")


def _population_source(args: Mapping[str, str]) -> str:
    """Which pHash population the LIVE side reads. `frozen` is what the lane really does —
    `autodedup.phash_pop`, and an absent hash is unknown. `artifact` reads the cohort's own
    counts instead, which is exactly what `rt_seed` writes into that table, so the operator can
    measure a seed's effect on the certificates BEFORE seeding anything (E84)."""
    raw = str(args.get("population") or "frozen").strip().lower()
    if raw not in POPULATION_SOURCES:
        raise SystemExit(f"population must be one of {', '.join(POPULATION_SOURCES)}, "
                         f"got {raw!r}")
    return raw


def _positive(args: Mapping[str, str], key: str, fallback: int) -> int:
    raw = str(args.get(key) or "").strip()
    if not raw:
        return fallback
    if not raw.isdigit() or int(raw) <= 0:
        raise SystemExit(f"{key} must be a positive integer, got {raw!r}")
    return int(raw)


# --- field views --------------------------------------------------------------------------
#
# `listing_view` and `image_view` live in `parity_digest` beside the three digests the GATE is
# built from, so the instrument's field-by-field diff and the gate's short digest are a view of
# the same facts rather than two opinions about what a listing is (E12).


def equal(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return abs(left - right) <= FLOAT_TOL
    if isinstance(left, dict) and isinstance(right, dict):
        return sorted(left) == sorted(right) and all(
            equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _short(value: Any) -> Any:
    """Examples travel in the report, so a 700-char description or a 512-d CLIP payload is cut
    to its head plus its length — enough to see WHICH value it is, never the whole body."""
    if isinstance(value, str) and len(value) > 60:
        return f"{value[:60]}…(+{len(value) - 60})"
    if isinstance(value, (list, dict)) and len(value) > 6:
        return f"<{type(value).__name__} of {len(value)}>"
    return value


class FieldDiff:
    """One field's tally, split by whether the listing drifted since the export."""

    def __init__(self) -> None:
        self.compared = 0
        self.differ = 0
        self.differ_stable = 0
        self.examples: list[dict[str, Any]] = []

    def add(self, key: Any, artifact: Any, live: Any, *, stable: bool) -> None:
        self.compared += 1
        if equal(artifact, live):
            return
        self.differ += 1
        if stable:
            self.differ_stable += 1
        if len(self.examples) < MAX_EXAMPLES:
            self.examples.append({"key": key, "artifact": _short(artifact),
                                  "live": _short(live), "stable": stable})

    def to_json(self) -> dict[str, Any]:
        return {"compared": self.compared, "differ": self.differ,
                "differ_stable": self.differ_stable, "examples": self.examples}


# What an image field IS decides how a difference in it reads. `seq` and `storage_path` are
# stored facts of the gallery; `phash`, `clip` and `tags` are producer output that a re-run of
# the pHash or CLIP job moves under a frozen calibration without any listing changing; `pop` is
# the frozen cohort statistic the lane joins against (E83).
IMAGE_FIELD_CLASS: dict[str, str] = {
    "image.seq": "stored", "image.storage_path": "stored",
    "image.phash": "producer", "image.clip": "producer", "image.clip_present": "producer",
    "image.tags": "producer", "image.tags_len": "producer",
    "image.pop": "frozen_statistic",
}


def _classes(table: Mapping[str, FieldDiff]) -> dict[str, dict[str, int]]:
    """The image diff rolled up by what the field is, so "the corpus moved" and "the lane reads
    a different value for an unchanged image" are never read as one number."""
    out: dict[str, dict[str, int]] = {}
    for name, diff in table.items():
        if not diff.differ:
            continue
        bucket = out.setdefault(IMAGE_FIELD_CLASS.get(name, "other"),
                                {"fields": 0, "differ": 0, "differ_stable": 0})
        bucket["fields"] += 1
        bucket["differ"] += diff.differ
        bucket["differ_stable"] += diff.differ_stable
    return dict(sorted(out.items()))


def _tally(table: Mapping[str, FieldDiff]) -> dict[str, Any]:
    """Only the fields that actually moved, worst first — a report of 60 zeroes hides its news."""
    moved = {name: diff for name, diff in table.items() if diff.differ}
    return {name: moved[name].to_json()
            for name in sorted(moved, key=lambda k: (-moved[k].differ, k))}


# --- the sample ----------------------------------------------------------------------------


class Keys:
    """Every listing's probe and index keys, taken ONCE. A 200-id sample is 19,900 candidate
    pairs and a fingerprint carries ~17 keys, so recomputing them per pair is the difference
    between a second and a minute."""

    def __init__(self, keyer: Keyer, fps: Mapping[int, Fingerprint]) -> None:
        self.keyer = keyer
        self.probe = {i: set(keyer.probe_keys(fp)) for i, fp in fps.items()}
        self.index = {i: set(keyer.index_keys(fp)) for i, fp in fps.items()}

    def probes(self, lo: int, hi: int) -> set[str]:
        """The probes under which either side retrieves the other — `retrieve`'s attribution,
        taken symmetrically so the set does not depend on which id came first."""
        hits = (self.probe.get(lo, set()) & self.index.get(hi, set())) | (
            self.probe.get(hi, set()) & self.index.get(lo, set())
        )
        return {probe for probe, token in hits if not self.keyer.is_exploded(probe, token)}


def sample_pairs(
    keys: Keys, fps: Mapping[int, Fingerprint], ids: Sequence[int], k: int, seed: int
) -> list[tuple[int, int]]:
    """`k` pairs the engine would really look at: both sides in the sample and at least one
    probe key shared, so the draw is not a cloud of unrelated adverts whose every feature is
    trivially absent. Same-block pairs top the list up when the probes run out."""
    rng = random.Random(seed + 1)
    ordered = sorted(i for i in ids if i in fps)
    probed: list[tuple[int, int]] = []
    same_block: list[tuple[int, int]] = []
    for index, lo in enumerate(ordered):
        for hi in ordered[index + 1:]:
            if keys.probes(lo, hi):
                probed.append((lo, hi))
            elif fps[lo].block_key and fps[lo].block_key == fps[hi].block_key:
                same_block.append((lo, hi))
    if len(probed) > k:
        return sorted(rng.sample(probed, k))
    out = list(probed)
    if same_block:
        out.extend(rng.sample(same_block, min(max(0, k - len(out)), len(same_block))))
    return sorted(set(out))


# --- one side of the comparison ---------------------------------------------------------


@dataclass(slots=True)
class Side:
    listings: dict[int, Listing]
    images: dict[int, list[Image]]
    fps: dict[int, Fingerprint]

    @classmethod
    def build(
        cls,
        listings: Mapping[int, Listing],
        images: Mapping[int, Sequence[Image]],
        settings: Settings,
    ) -> "Side":
        kept = {i: listings[i] for i in sorted(listings)}
        gallery = {i: list(images.get(i, ())) for i in kept}
        return cls(
            listings=kept,
            images=gallery,
            fps={i: build_fingerprint(kept[i], gallery[i], settings) for i in kept},
        )


def _rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))
        return list(cur.fetchall())


def as_datetime(value: Any) -> datetime | None:
    """A timestamp from either side as one comparable value. The artifact carries an ISO string
    and psycopg hands back a `datetime`, so a string comparison across the two would order
    `2026-09-17 12:00+00` against `2026-09-17T12:00:00+02:00` by spelling."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None


def drift_stamps(conn: Any, ids: Sequence[int]) -> dict[int, datetime]:
    """Each listing's newest content change, from `listing_snapshots` (rule #2 appends only on
    a content change, so the newest snapshot IS the last time the row really moved)."""
    out: dict[int, datetime] = {}
    for row in _rows(conn, PARITY_CHANGE_SQL, {"ids": list(ids)}):
        stamp = as_datetime(row[1])
        if stamp is not None:
            out[int(row[0])] = stamp
    return out


# --- the pass ------------------------------------------------------------------------------


def compare_facts(
    sample: Sequence[int],
    artifact: Side,
    live: Side,
    drifted: Mapping[int, str],
) -> dict[str, Any]:
    listing_diffs: dict[str, FieldDiff] = {}
    image_diffs: dict[str, FieldDiff] = {}
    counts = {"listings_compared": 0, "galleries_differing_in_size": 0,
              "images_only_artifact": 0, "images_only_live": 0, "images_compared": 0}
    gallery_examples: list[dict[str, Any]] = []
    for listing_id in sample:
        left, right = artifact.listings.get(listing_id), live.listings.get(listing_id)
        if left is None or right is None:
            continue
        stable = listing_id not in drifted
        counts["listings_compared"] += 1
        view_a, view_b = listing_view(left), listing_view(right)
        for name in sorted(set(view_a) | set(view_b)):
            listing_diffs.setdefault(name, FieldDiff()).add(
                listing_id, view_a.get(name), view_b.get(name), stable=stable)
        images_a = {img.image_id: img for img in artifact.images.get(listing_id, ())}
        images_b = {img.image_id: img for img in live.images.get(listing_id, ())}
        only_a, only_b = sorted(set(images_a) - set(images_b)), sorted(set(images_b) - set(images_a))
        counts["images_only_artifact"] += len(only_a)
        counts["images_only_live"] += len(only_b)
        if only_a or only_b:
            counts["galleries_differing_in_size"] += 1
            if len(gallery_examples) < MAX_EXAMPLES:
                gallery_examples.append({
                    "listing_id": listing_id, "stable": stable,
                    "n_artifact": len(images_a), "n_live": len(images_b),
                    "only_artifact": only_a[:5], "only_live": only_b[:5]})
        # Order is a fact too: `phash_gallery` caps at `phash_sample` in gallery order, so two
        # identical galleries in different orders are not the same input.
        listing_diffs.setdefault("image_order", FieldDiff()).add(
            listing_id,
            [img.image_id for img in artifact.images.get(listing_id, ())],
            [img.image_id for img in live.images.get(listing_id, ())],
            stable=stable)
        for image_id in sorted(set(images_a) & set(images_b)):
            counts["images_compared"] += 1
            fields_a = image_view(images_a[image_id])
            fields_b = image_view(images_b[image_id])
            for name in sorted(set(fields_a) | set(fields_b)):
                image_diffs.setdefault(f"image.{name}", FieldDiff()).add(
                    (listing_id, image_id), fields_a.get(name), fields_b.get(name),
                    stable=stable)
    return {
        "counts": counts,
        "gallery_examples": gallery_examples,
        "listing_fields": _tally(listing_diffs),
        "image_fields": _tally(image_diffs),
        "image_field_classes": _classes(image_diffs),
    }


def compare_pairs(
    pairs: Sequence[tuple[int, int]],
    artifact: Side,
    live: Side,
    calibration: Calibration,
    settings: Settings,
    model: Any,
    keys_artifact: Keys,
    keys_live: Keys,
) -> dict[str, Any]:
    """The same pairs scored from both fact sets under ONE settings/model/calibration, so every
    difference that survives is a difference in the FACTS."""
    ctx_a = context_for(calibration, settings, artifact.fps, artifact.listings)
    ctx_b = context_for(calibration, settings, live.fps, live.listings)
    census_a = ContextIndex.build(artifact.listings, artifact.images)
    census_b = ContextIndex.build(live.listings, live.images)
    feature_diffs: dict[str, FieldDiff] = {}
    deltas: dict[str, list[float]] = {}
    zone_moves: dict[str, int] = {}
    cert_moves: dict[str, int] = {}
    certificates: dict[str, dict[str, int]] = {"artifact": {}, "live": {}}
    scored = 0
    score_shift = 0
    examples: list[dict[str, Any]] = []
    for lo, hi in pairs:
        if lo not in artifact.fps or hi not in artifact.fps:
            continue
        if lo not in live.fps or hi not in live.fps:
            continue
        probes_a = keys_artifact.probes(lo, hi)
        probes_b = keys_live.probes(lo, hi)
        feats_a = pair_features(artifact.fps[lo], artifact.fps[hi], artifact.listings[lo],
                                artifact.listings[hi], artifact.images.get(lo, ()),
                                artifact.images.get(hi, ()), ctx_a, settings)
        feats_b = pair_features(live.fps[lo], live.fps[hi], live.listings[lo],
                                live.listings[hi], live.images.get(lo, ()),
                                live.images.get(hi, ()), ctx_b, settings)
        decision_a = decide_pair(artifact.fps[lo], artifact.fps[hi], artifact.listings[lo],
                                 artifact.listings[hi], feats_a, probes_a, model, settings,
                                 census_a)
        decision_b = decide_pair(live.fps[lo], live.fps[hi], live.listings[lo],
                                 live.listings[hi], feats_b, probes_b, model, settings,
                                 census_b)
        scored += 1
        feature_diffs.setdefault("pair.probes", FieldDiff()).add(
            (lo, hi), sorted(probes_a), sorted(probes_b), stable=True)
        for name in sorted(set(feats_a) | set(feats_b)):
            value_a, value_b = feats_a.get(name), feats_b.get(name)
            feature_diffs.setdefault(name, FieldDiff()).add(
                (lo, hi), value_a, value_b, stable=True)
            if value_a is not None and value_b is not None and value_a[1] and value_b[1]:
                deltas.setdefault(name, []).append(abs(value_a[0] - value_b[0]))
        for key, decision in (("artifact", decision_a), ("live", decision_b)):
            if decision.certificate:
                bucket = certificates[key]
                bucket[decision.certificate] = bucket.get(decision.certificate, 0) + 1
        if abs(decision_a.score - decision_b.score) > 0.01:
            score_shift += 1
        if decision_a.zone != decision_b.zone:
            move = f"{decision_a.zone}->{decision_b.zone}"
            zone_moves[move] = zone_moves.get(move, 0) + 1
        if decision_a.certificate != decision_b.certificate:
            move = f"{decision_a.certificate or '-'}->{decision_b.certificate or '-'}"
            cert_moves[move] = cert_moves.get(move, 0) + 1
            if len(examples) < MAX_EXAMPLES:
                examples.append({
                    "lo": lo, "hi": hi, "certificate": move,
                    "score_artifact": round(decision_a.score, 6),
                    "score_live": round(decision_b.score, 6),
                    "zone": f"{decision_a.zone}->{decision_b.zone}"})
    return {
        "scored": scored,
        "score_differs_gt_0_01": score_shift,
        "zone_moves": dict(sorted(zone_moves.items())),
        "certificate_moves": dict(sorted(cert_moves.items())),
        "certificates": {side: dict(sorted(bucket.items()))
                         for side, bucket in certificates.items()},
        "certificate_examples": examples,
        "features": _tally(feature_diffs),
        "mean_abs_delta": {
            name: round(sum(values) / len(values), 6)
            for name, values in sorted(deltas.items())
            if values and max(values) > FLOAT_TOL
        },
    }


def lane_config(conn: Any, generation: str, settings: Settings, model: Any) -> dict[str, Any]:
    """What the SEEDED generation was cut under, beside what this instrument scored with. The
    lane takes its settings and model from dispatch arguments, so a pass dispatched with none
    runs on `Settings()` and the uncalibrated `hand_initialised()` prior — which the frozen
    calibration does not pin and no pair row records."""
    rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
    if not rows:
        return {"generation": generation, "seeded": False}
    payload = rows[0][5]
    seeded = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    scoring = settings.to_dict()
    moved = {key: {"seeded": seeded.get(key), "instrument": scoring.get(key)}
             for key in sorted(set(seeded) | set(scoring))
             if not equal(seeded.get(key), scoring.get(key))}
    return {
        "generation": generation,
        "seeded": True,
        "digest": rows[0][1],
        "n_listings": int(rows[0][2] or 0),
        "seeded_model_version": rows[0][6],
        "instrument_model_version": getattr(model, "version", None),
        "settings_differences": moved,
    }


def run_parity(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
) -> dict[str, Any]:
    parsed = parse_args(args)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings(parsed.settings)
    model = load_model(parsed.model)

    cohort_path = (Path(parsed.cohort) if parsed.cohort
                   else download_cohort(parsed.export_run, out_dir / "artifact"))
    if not cohort_path.is_file():
        raise SystemExit(f"no cohort artifact at {cohort_path} ({COHORT_FILE})")
    dataset: Dataset = load(cohort_path)

    conn = conn_factory()
    try:
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": parsed.generation})
        if not rows:
            raise SystemExit(
                f"generation {parsed.generation!r} has no frozen calibration — this mode "
                "compares against the calibration the lane actually runs under")
        payload = rows[0][3]
        calibration = Calibration.from_json(
            payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        config = lane_config(conn, parsed.generation, settings, model)

        facts = SqlFacts(conn, population=(artifact_population(dataset)
                                           if parsed.population == "artifact" else None))
        # Presence for the whole cohort FIRST, in one statement, so the stratified draw is over
        # what is still there; the full fact read is then the sample's alone.
        cohort_ids = sorted(dataset.listings)
        present = {int(row[0]) for row in _rows(conn, PARITY_PRESENT_SQL, {"ids": cohort_ids})}
        sample = stratified_sample(dataset.listings, present, parsed.n, parsed.seed)
        live_facts: dict[int, tuple[Listing, list[Image]]] = {}
        for start in range(0, len(sample), 100):
            live_facts.update(facts.facts(sample[start:start + 100]))
        stamps = drift_stamps(conn, sample) if sample else {}
        exported_at = as_datetime(dataset.meta.exported_at)
        drifted = {i: stamp for i, stamp in stamps.items()
                   if exported_at is not None and stamp > exported_at}
        phash_pop_rows = int(_rows(conn, PARITY_PHASH_POP_SQL)[0][0])
        statements = facts.statements + 3
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    artifact_side = Side.build(
        {i: dataset.listings[i] for i in sample},
        {i: dataset.images(i) for i in sample},
        settings)
    live_side = Side.build(
        {i: live_facts[i][0] for i in sample if i in live_facts},
        {i: live_facts[i][1] for i in sample if i in live_facts},
        settings)
    keyer = Keyer(settings, calibration)
    keys_artifact = Keys(keyer, artifact_side.fps)
    keys_live = Keys(keyer, live_side.fps)

    pairs = sample_pairs(keys_artifact, artifact_side.fps, sample, parsed.pairs, parsed.seed)
    report: dict[str, Any] = {
        "export_run": parsed.export_run or None,
        "cohort": str(cohort_path),
        "exported_at": dataset.meta.exported_at,
        "generation": parsed.generation,
        "feature_version": FEATURE_VERSION,
        "calibration_digest": calibration.digest(),
        "settings_path": parsed.settings,
        "model_path": parsed.model,
        "model_version": getattr(model, "version", None),
        "lane_config": config,
        "phash_pop_rows": phash_pop_rows,
        "population_source": parsed.population,
        "statements": statements,
        "sample": {
            "requested": parsed.n,
            "cohort_listings": len(dataset.listings),
            "live_listings": len(present),
            "absent_live": len(dataset.listings) - len(present),
            "facts_read": len(live_facts),
            "sampled": len(sample),
            "by_source": _by_source(dataset, sample),
            "drifted": len(drifted),
            "drifted_examples": [{"listing_id": i, "last_change_at": drifted[i].isoformat()}
                                 for i in sorted(drifted)[:MAX_EXAMPLES]],
            "seed": parsed.seed,
        },
        # What the GATE would say, from the same two sides the diff above is taken from: the
        # seed refuses to cut a baseline when this is non-zero, and every pass refuses to run
        # on one (E84). Read-only, and worth having in the instrument's own report — it is the
        # answer to "will the re-seed be refused?" before anything is seeded.
        "gate": _gate_view(artifact_side, live_side, sample, drifted),
        "facts": compare_facts(sample, artifact_side, live_side, drifted),
        "pairs": {"requested": parsed.pairs, "drawn": len(pairs),
                  **compare_pairs(pairs, artifact_side, live_side, calibration, settings,
                                  model, keys_artifact, keys_live)},
    }
    (out_dir / PARITY_FILE).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    report["spent_usd"] = 0.0
    return report


def _gate_view(artifact: "Side", live: "Side", sample: Sequence[int],
               drifted: Mapping[int, Any]) -> dict[str, Any]:
    rows = baseline({i: artifact.listings[i] for i in sample if i in artifact.listings},
                    {i: artifact.images.get(i, []) for i in sample})
    report = compare(rows, live.listings, live.images, set(drifted))
    report["would_refuse"] = report["breaches"] > 0
    return report


def artifact_population(dataset: Dataset) -> dict[int, int]:
    """The cohort's own corpus-wide pHash counts — the rows `rt_seed` materialises."""
    out: dict[int, int] = {}
    for image in dataset.all_images():
        if image.phash is None or image.pop is None:
            continue
        out[int(image.phash)] = max(out.get(int(image.phash), 0), int(image.pop))
    return out


def _by_source(dataset: Dataset, sample: Sequence[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for listing_id in sample:
        listing = dataset.listings.get(listing_id)
        key = (listing.source if listing else None) or "(none)"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))
