"""Local CLI over the cohort artifact: read it, run the lane's own pass on it, evaluate a run.

    python3 -m autodedup.harness stats out/cohort.jsonl.gz [--json out/summary.json]
    python3 -m autodedup.harness sample out/cohort.jsonl.gz --block turnov --n 5
    python3 -m autodedup.harness run out/cohort.jsonl.gz --out runs/r1 --settings w31 --model w6_gold
    python3 -m autodedup.harness pair out/cohort.jsonl.gz 101 102 --settings w31 --model w6_gold
    python3 -m autodedup.harness evaluate runs/r1 labels/ [--base runs/r0] [--reference truth16/]
    python3 -m autodedup.harness fit runs/r1 --operator-labels operator_labels.jsonl --out runs/r1/fit

No database, no network, no secrets: the artifact is the whole input, and it carries no PII by
contract (PROGRAM.md E28). `run` is `run_pass` over a MemoryStore (SW1): one decision path for the
lane, the harness and every report. `evaluate` reads a run against the rulings; it never decides
anything but the ruled pairs a run never stored.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup import evaluate as evaluation
from autodedup import revocation, seals
from autodedup.dataset import (
    CATALOG_POP_MIN,
    UNASSIGNED_BLOCK,
    Dataset,
    Image,
    Listing,
    load,
)
from autodedup.decide import CERTIFICATES, ZONES, Decision, decide_pair
from autodedup.evaluate import (
    CALIBRATION_AUTO,
    FIT_MAX_ITER,
    FIT_METHODS,
    L2_GRID,
    fit_model,
    split_groups,
    write_report,
)
from autodedup.features import FEATURE_ORDER, pair_features
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.hazard_context import ContextIndex
from autodedup.incremental import (
    EVIDENCE_HOLD_REASON,
    Calibration,
    EvidenceHold,
    Keyer,
    Limits,
    PairRow,
    PassResult,
    context_for,
    run_pass_bounded,
)
from autodedup.incremental_store import CohortFacts, MemoryStore, Schedule
from autodedup.labels import (
    TIER_PRECEDENCE,
    WEIGHT_OPERATOR,
    all_labels_by_tier,
    label_pairs,
    load_all_judgements,
    load_all_operator_labels,
    operator_label_pairs,
)
from autodedup.model import CALIBRATION_METHODS, LogisticModel, hand_initialised
from autodedup.settings import Settings
from autodedup.store_score import storable

PAIRS_FILE: str = "pairs.jsonl.gz"
RUN_FILE: str = "run.json"
CLUSTERS_FILE: str = "clusters.json"
MODEL_FILE: str = "model.json"
SPLIT_MAP_FILE: str = "split_map.json"
FIT_STEM: str = "fit"
SAMPLE_SEED: int = 20260916

SHARE_ROWS: tuple[tuple[str, str], ...] = (
    ("active_share", "active"),
    ("with_area", "area"),
    ("with_disposition", "disposition"),
    ("with_floor", "floor"),
    ("with_broker_key", "broker_key"),
    ("with_street_key", "street_key"),
    ("with_ruian_adm_kod", "ruian_adm_kod"),
    ("with_point", "coord pin"),
    ("with_any_clip", ">=1 CLIP vector"),
    ("with_any_phash", ">=1 pHash"),
)


def _pct(value: Any) -> str:
    return f"{float(value) * 100:5.1f}%" if isinstance(value, (int, float)) else "     -"


def _counts(counts: dict[str, Any], limit: int = 12) -> str:
    items = list(counts.items())[:limit]
    tail = " …" if len(counts) > limit else ""
    return ", ".join(f"{name} {n}" for name, n in items) + tail


def _curve(curve: dict[str, Any]) -> str:
    return ", ".join(f">={df} {_pct(share).strip()}" for df, share in curve.items())


def _print_block(key: str, stats: dict[str, Any], out: Any) -> None:
    label = stats.get("label") or key
    grain = stats.get("grain") or "?"
    code = stats.get("code")
    print(f"\n{key}  [{grain} {code if code is not None else '-'}]  {label}", file=out)
    print("-" * 72, file=out)
    print(f"  {'listings':<22}{stats.get('n_listings', 0)}", file=out)
    if not stats.get("n_listings"):
        print("  (no listings in this block)", file=out)
        return
    print(f"  {'images':<22}{stats.get('n_images', 0)}"
          f"  (mean {stats.get('images_per_listing_mean', 0.0):.2f} /"
          f" median {stats.get('images_per_listing_median', 0.0):.1f} per listing)", file=out)
    for field_name, label_text in SHARE_ROWS:
        print(f"  {label_text:<22}{_pct(stats.get(field_name))}", file=out)
    pop_min = stats.get("catalog_pop_min", CATALOG_POP_MIN)
    catalog = stats.get("catalog_image_share")
    row = f"images pop>={pop_min}"
    if catalog is None:
        print(f"  {row:<22}     -  UNMEASURED: every pHash-bearing image has pop=0,"
              f" so the exporter's population probe did not run (catalog subtraction is blind)",
              file=out)
    else:
        unmeasured = int(stats.get("catalog_pop_unmeasured", 0))
        print(f"  {row:<22}{_pct(catalog)}"
              f"   [{_curve(stats.get('catalog_share_curve', {}))}]"
              + (f"  ({unmeasured} pHash images unmeasured)" if unmeasured else ""), file=out)
    print(f"  {'sources':<22}{_counts(stats.get('sources', {}))}", file=out)
    print(f"  {'category_main':<22}{_counts(stats.get('category_main', {}))}", file=out)
    print(f"  {'category_type':<22}{_counts(stats.get('category_type', {}))}", file=out)
    print(f"  {'price-hist depth':<22}"
          f"{_counts(stats.get('price_history_depth', {}), limit=13)}", file=out)


def _print_integrity(dataset: Dataset, summary: dict[str, Any], out: Any) -> None:
    integrity = dataset.integrity
    blocked = sum(int(stats.get("n_listings", 0)) for stats in summary.values())
    total = len(dataset.listings)
    bad_clip = dataset.clip_payload_errors()
    unmeasured = dataset.unmeasured_pop_images()
    print("\nintegrity", file=out)
    print("-" * 72, file=out)
    mark = "ok" if blocked == total else "MISMATCH"
    print(f"  {'listings in blocks':<26}{blocked} of {total}  {mark}", file=out)
    print(f"  {'duplicate listing ids':<26}{integrity.duplicate_listing_ids}", file=out)
    print(f"  {'duplicate image ids':<26}{integrity.duplicate_image_ids}", file=out)
    print(f"  {'images without a listing':<26}{integrity.orphan_images}", file=out)
    print(f"  {'bad CLIP payloads':<26}{len(bad_clip)}"
          + (f"  e.g. {bad_clip[:5]}" if bad_clip else ""), file=out)
    print(f"  {'pHash images with pop=0':<26}{unmeasured}", file=out)
    print(f"  {'skipped record types':<26}"
          f"{_counts(integrity.skipped_types) or 'none'}", file=out)


def _image_line(image: Image) -> str:
    tags = ", ".join(f"{name}={score:.2f}" if isinstance(score, float) else str(name)
                     for name, score in image.tags[:3])
    return (f"      #{image.seq if image.seq is not None else '-'} id={image.image_id}"
            f" phash={image.phash} pop={image.pop}"
            f" clip={'yes' if image.clip else 'no'}"
            f" path={image.storage_path or '-'}"
            + (f" tags[{tags}]" if tags else ""))


def _print_listing(listing: Listing, images: list[Image], out: Any) -> None:
    loc = listing.location
    print(f"\n  listing {listing.id}  {listing.source or '?'}"
          f"/{listing.source_id_native or '?'}  block={listing.block}", file=out)
    print(f"    {listing.category_main or '?'}/{listing.category_type or '?'}"
          f"/{listing.subtype or '-'}  dispo={listing.disposition or '-'}"
          f"  area={listing.area_m2}  floor={listing.floor}/{listing.total_floors}"
          f"  price={listing.price}", file=out)
    if listing.attrs:
        print(f"    attrs {json.dumps(listing.attrs, ensure_ascii=False, sort_keys=True)}",
              file=out)
    print(f"    active={listing.is_active}  first_seen={listing.first_seen_at}"
          f"  last_seen={listing.last_seen_at}  inactive_at={listing.inactive_at}", file=out)
    print(f"    broker_key={listing.broker_key}  identity={listing.broker_identity_id}"
          f"  firm={listing.broker_firm_id}", file=out)
    print(f"    loc obec={loc.obec_name or '-'}({loc.obec_kod})"
          f" cast={loc.cast_obce_name or '-'}({loc.cast_obce_kod})"
          f" gran={loc.granularity or '-'}/{loc.granularity_rank}"
          f" street={loc.street_key or '-'} hn={loc.house_number or '-'}"
          f" cp={loc.house_number_cp or '-'}/co={loc.house_number_co or '-'}"
          f" ruian={loc.ruian_adm_kod} pin=({loc.lat},{loc.lon})"
          f" +-{loc.uncertainty_radius_m}m", file=out)
    print(f"    url={listing.source_url or '-'}", file=out)
    if listing.price_history:
        path = " -> ".join(f"{when}:{price}" for when, price in listing.price_history)
        print(f"    price path  {path}", file=out)
    if listing.description:
        text = " ".join(listing.description.split())
        print(f"    desc({len(listing.description)}ch)  {text[:200]}", file=out)
    print(f"    images {len(images)}", file=out)
    for image in images[:8]:
        print(_image_line(image), file=out)


def cmd_stats(args: argparse.Namespace, out: Any) -> int:
    dataset = load(args.artifact)
    summary = dataset.summary(pop_min=args.catalog_df)
    meta = dataset.meta
    print(f"cohort {args.artifact}", file=out)
    print(f"  exported_at={meta.exported_at}  generator_version={meta.generator_version}", file=out)
    if meta.counts:
        print(f"  counts  {json.dumps(meta.counts, sort_keys=True)}", file=out)
    for key, stats in summary.items():
        _print_block(key, stats, out)
    _print_integrity(dataset, summary, out)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {target}", file=out)
    return 0


def cmd_sample(args: argparse.Namespace, out: Any) -> int:
    dataset = load(args.artifact)
    if args.block not in dataset.block_keys():
        print(f"unknown block {args.block!r}; known: {', '.join(dataset.block_keys()) or '(none)'}",
              file=sys.stderr)
        return 1
    listings = dataset.block(args.block)[: max(args.n, 0)]
    print(f"block {args.block}: showing {len(listings)} of "
          f"{len(dataset.block(args.block))} listings", file=out)
    for listing in listings:
        _print_listing(listing, dataset.images(listing.id), out)
    return 0


# --- naming a settings row or a model, the way every lane names them --------------------
#
# `--args settings=w8` resolves inside the repo, never off the wire: a lane input is an
# operator string and a bare path would make `settings=/etc/passwd` a readable file. This
# lived in `score_lane` until W9g, when the real-time lane was found running `Settings()` and
# the uncalibrated prior because it took a PATH — so `settings=w8` named no file it could
# read and the recipe that shipped passed nothing at all (E90a). One definition (E12).
SETTINGS_DIR: Path = Path(__file__).resolve().parent / "settings"
MODELS_DIR: Path = Path(__file__).resolve().parent / "models"
# The two words that NAME the uncalibrated defaults, so choosing them is a choice a dispatch
# can be read to have made rather than the silence of an empty argument.
DEFAULT_SETTINGS_NAME: str = "default"
PRIOR_MODEL_NAME: str = "prior"


def repo_path(raw: str, base: Path, suffix: str = ".json") -> Path:
    """`sweep_a` or `sweep_a.json` -> `<base>/sweep_a.json`, refusing anything outside `base`.

    A dispatch that spells the whole repo-relative path (`autodedup/settings/w8.json`, which is
    how the recipes in git history spell it) names the same file and is accepted as such — the
    prefix is stripped, never followed, so what can be read is still only what is inside
    `base`."""
    name = raw.strip()
    for prefix in (f"autodedup/{base.name}/", f"{base.name}/", f"./{base.name}/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name if name.endswith(suffix) else f"{name}{suffix}"
    resolved = (base / name).resolve()
    if base.resolve() not in resolved.parents:
        raise SystemExit(f"{raw!r} must name a file inside {base.name}/")
    if not resolved.is_file():
        raise SystemExit(f"no such file: {base.name}/{name}")
    return resolved


def named_settings(name: str | None) -> Settings:
    """A settings row BY NAME (`w8`), or the uncalibrated defaults when the name says so."""
    if not name or name == DEFAULT_SETTINGS_NAME:
        return Settings()
    return Settings.from_json(repo_path(name, SETTINGS_DIR))


def named_model(name: str | None) -> LogisticModel:
    """A model BY NAME (`w6_gold`), or the hand-initialised prior when the name says so.

    A model file whose own `version` is not the name it was loaded under is refused: the
    version is what every stored row is stamped with, so the two must be one string."""
    if not name or name == PRIOR_MODEL_NAME:
        return hand_initialised()
    model = LogisticModel.from_json(
        json.loads(repo_path(name, MODELS_DIR).read_text(encoding="utf-8")))
    if model.version and model.version != name:
        raise SystemExit(
            f"models/{name}.json carries version {model.version!r} — a model is stamped on "
            "every row it decides, so the file and its version must be one name")
    return model


def model_of_version(version: str | None) -> LogisticModel:
    """The model a STORED `model_version` names — the prior's own version resolves to the
    prior, anything else to `models/<version>.json`."""
    if not version or str(version) == hand_initialised().version:
        return hand_initialised()
    return named_model(str(version))


def model_name_of(model: LogisticModel) -> str:
    """The name a model is dispatched under: its own version, or the prior's word."""
    version = getattr(model, "version", None)
    return PRIOR_MODEL_NAME if (not version or version == hand_initialised().version) \
        else str(version)


CROSS_BLOCK: str = "(cross-block)"


def pair_block(la: Listing, lb: Listing) -> str:
    """The cohort block a pair belongs to — K4/K5 are location-free, so a pair can straddle two."""
    if la.block != lb.block:
        return CROSS_BLOCK
    return la.block or UNASSIGNED_BLOCK


def pair_block_key(fa: Fingerprint, fb: Fingerprint) -> str:
    """The engine's own blocking grain (obec / část), which is finer than the cohort block."""
    if fa.block_key and fa.block_key == fb.block_key:
        return fa.block_key
    return CROSS_BLOCK


def source_pair(fa: Fingerprint, fb: Fingerprint) -> str:
    return "|".join(sorted((fa.source or "?", fb.source or "?")))


def _zone_counter() -> dict[str, int]:
    return {zone: 0 for zone in ZONES}


def load_must_not_link(path: str | None) -> frozenset[tuple[int, int]]:
    """E27's permanent negatives — or E910's must-links, in the same two shapes — lo<hi.

    Two shapes, because the batch build has to be able to read the one the labels lane
    actually uploads (N4): a JSON list of `[lo, hi]`, or the lane's `must_not_link.jsonl`
    artifact — one `{"listing_lo": …, "listing_hi": …}` object per line.
    """
    if not path:
        return frozenset()
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return frozenset(
            (int(min(row["listing_lo"], row["listing_hi"])),
             int(max(row["listing_lo"], row["listing_hi"])))
            for row in rows
        )
    return frozenset(
        (int(min(lo, hi)), int(max(lo, hi)))
        for lo, hi in (tuple(row) for row in json.loads(text))
    )


# --- `harness run`: the lane's own pass over the cohort (SW1, ADD06) -------------------------
#
# The withheld-photos arm's clock (E92/E93). Nothing here is a wall clock: the first decision is
# INSIDE the horizon (so the hold fires) and the last one is outside it (so it expires).
WITHHELD_T0: float = 1_780_000_000.0
WITHHELD_HORIZON_S: float = 48 * 3600.0


def _drain(store: MemoryStore, facts: CohortFacts, work: Schedule, settings: Settings,
           model: LogisticModel, calibration: Calibration, limits: Limits,
           hold: EvidenceHold | None) -> list[PassResult]:
    passes: list[PassResult] = []
    while not work.exhausted():
        before = work.cursor
        passes.append(run_pass_bounded(store, facts, work, settings, model, calibration,
                                       limits=limits, hold=hold))
        if work.cursor == before:
            raise SystemExit(f"pair budget refused {passes[-1].wanted_pairs} pairs at "
                             f"max_pairs={limits.max_pairs}")
    return passes


def calibrated(dataset: Dataset, settings: Settings
               ) -> tuple[dict[int, Fingerprint], Calibration]:
    """The lane's calibration core (`cut_calibration`: fingerprints off the facts, then
    `Calibration.build`) over the whole cohort."""
    fps = {i: build_fingerprint(listing, images, settings)
           for i, (listing, images) in CohortFacts(dataset).facts(sorted(dataset.listings)).items()}
    return fps, Calibration.build(fps, dataset.listings, settings)


def run(
    dataset: Dataset,
    settings: Settings,
    model: LogisticModel,
    out_dir: Path,
    must_not_link: frozenset[tuple[int, int]] = frozenset(),
    must_link: frozenset[tuple[int, int]] = frozenset(),
    withhold_photos: bool = False,
) -> dict[str, Any]:
    """`run_pass` over a MemoryStore, the whole cohort claimed as the scope, written as the three
    run artifacts. The lane's path: its calibration core over the cohort, its claim order
    (`RT_SCOPE_ENTRANTS_SQL`: ascending id) and its pass limits. `must_link` / `must_not_link`
    are the operator's rulings, bound as the lane binds them; left empty the run is blind.

    `withhold_photos` is the E92/E93 arm: every listing is first decided with its photographs
    unprocessed under the evidence hold, then the producers land and the sweep re-decides, then
    the horizon passes. The final state must be the plain run's; `withheld_photos` says what the
    hold held on the way."""
    clock = time.perf_counter()
    fps, calibration = calibrated(dataset, settings)
    facts = CohortFacts(dataset)
    store = MemoryStore(now=WITHHELD_T0 if withhold_photos else None)
    store.mnl, store.ml = set(must_not_link), set(must_link)
    order = sorted(dataset.listings)
    hold = EvidenceHold(WITHHELD_T0, WITHHELD_HORIZON_S) if withhold_photos else None
    facts.withheld = frozenset(order if withhold_photos else ())
    passes = _drain(store, facts, Schedule(order), settings, model, calibration, Limits(), hold)
    withheld: dict[str, Any] = {}
    if withhold_photos:
        withheld["held_on_first_decision"] = sum(p.held for p in passes)
        facts.withheld = frozenset()
        passes += _drain(store, facts, Schedule(order, redecide=True), settings, model,
                         calibration, Limits(), hold)
        late = EvidenceHold(WITHHELD_T0 + WITHHELD_HORIZON_S + 3600.0, WITHHELD_HORIZON_S)
        passes += _drain(store, facts, Schedule(order, redecide=True), settings, model,
                         calibration, Limits(), late)
        withheld.update({"held_total": sum(p.held for p in passes),
                         "released_total": sum(p.released for p in passes),
                         "still_held": sum(1 for row in store.pairs.values()
                                           if row.reason == EVIDENCE_HOLD_REASON)})
    decide_s = time.perf_counter() - clock
    summary = write_run(store, dataset, fps, Keyer(settings, calibration), settings, out_dir)
    summary.update({
        "model_version": model.version,
        "model_fit": dict(model.fit_report),
        "calibration_digest": calibration.digest(),
        "claim": Limits().max_listings,
        "passes": len(passes),
        "decisions": sum(p.pairs_scored for p in passes),
        "must_not_link": {**summary["must_not_link"], "loaded": len(must_not_link)},
        "must_link": {**summary["must_link"], "loaded": len(must_link)},
        "timings": {"decide_s": decide_s, "write_s": time.perf_counter() - clock - decide_s},
        **({"withheld_photos": withheld} if withhold_photos else {}),
    })
    return summary


def _pair_line(row: PairRow, fps: Mapping[int, Fingerprint], dataset: Dataset,
               exploded: Callable[[int], set[str]]) -> str:
    lo, hi = row.lo, row.hi
    fa, fb = fps[lo], fps[hi]
    line = row.decision().to_json()
    line.update({
        "context": row.context,
        "block": pair_block(dataset.listings[lo], dataset.listings[hi]),
        "block_key": pair_block_key(fa, fb),
        "source_pair": source_pair(fa, fb),
        "cross_source": fa.source != fb.source,
        "probes": sorted(row.probes),
        "exploded": sorted(exploded(lo) | exploded(hi)),
        "feats": {name: [value, present] for name, (value, present) in (row.feats or {}).items()},
    })
    return json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n"


def write_run(store: MemoryStore, dataset: Dataset, fps: Mapping[int, Fingerprint],
              keyer: Keyer, settings: Settings, out_dir: Path) -> dict[str, Any]:
    """The store's final state as `pairs.jsonl.gz` (the stored rows, key order), `clusters.json`
    and the summary `run.json` carries — every count read off the decided rows, none rebuilt."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[int, set[str]] = {}

    def exploded(listing_id: int) -> set[str]:
        if listing_id not in cache:
            cache[listing_id] = keyer.exploded_probes(fps[listing_id])
        return cache[listing_id]

    zones = _zone_counter()
    certificates = {name: 0 for name in CERTIFICATES}
    reasons: dict[str, int] = {}
    families: dict[str, int] = {}
    stored = 0
    with gzip.open(out_dir / PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for key in sorted(store.pairs):
            row = store.pairs[key]
            zones[row.zone] += 1
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
            if row.certificate:
                certificates[row.certificate] += 1
            for name in row.families:
                families[name] = families.get(name, 0) + 1
            if storable({"zone": row.zone, "score": row.score, "evidence": row.evidence},
                        settings.store_floor):
                stored += 1
                handle.write(_pair_line(row, fps, dataset, exploded))
    clusters = {key: sorted(members) for key, members in sorted(store.clusters.items())}
    conflicts = [{k: v for k, v in c.items() if k not in ("kind", "generation")}
                 for c in store.conflicts if c.get("kind") == "invariant"]
    bridges = [{k: v for k, v in c.items() if k not in ("kind", "generation")}
               for c in store.conflicts if c.get("kind") == "bridge"]
    of = {i: key for key, members in clusters.items() for i in members}
    together = lambda pairs: sum(1 for lo, hi in pairs  # noqa: E731
                                 if of.get(lo) is not None and of.get(lo) == of.get(hi))
    sizes = [len(members) for members in clusters.values()]
    stats = {
        "n_merge_edges": zones["merge"],
        "n_edges_refused": len(conflicts),
        "n_bridges_refused": len(bridges),
        "n_clusters": len(clusters),
        "n_clustered_listings": sum(sizes),
        "size_histogram": {str(size): sizes.count(size) for size in sorted(set(sizes))},
        "max_size": max(sizes) if sizes else 0,
        "mean_size": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "conflicts_by_invariant": dict(sorted(
            (name, sum(1 for c in conflicts if c.get("invariant") == name))
            for name in {str(c.get("invariant")) for c in conflicts})),
    }
    (out_dir / CLUSTERS_FILE).write_text(json.dumps({
        "clusters": {str(key): members for key, members in clusters.items()},
        "rows": [store.cluster_row[key] for key in clusters],
        "conflicts": conflicts, "bridges": bridges, "stats": stats,
    }, indent=2, sort_keys=True), encoding="utf-8")
    scored = len(store.pairs)
    vetoed = {key for key, row in store.pairs.items() if row.veto == UNIT_DESIGNATOR_VETO}
    return {
        "n_listings": len(dataset.listings),
        "n_images": sum(len(bucket) for bucket in dataset.images_by_listing.values()),
        "settings": settings.to_dict(),
        "feature_order": list(FEATURE_ORDER),
        "pairs_scored": scored,
        "pairs_stored": stored,
        "zones": zones,
        "band_width": (zones["band"] / scored) if scored else 0.0,
        "certificates": certificates,
        "reasons": dict(sorted(reasons.items())),
        "evidence_families": dict(sorted(families.items())),
        "clusters": stats,
        "must_not_link": {"together": together(store.mnl), "unit_designator_veto": len(vetoed)},
        "must_link": {"not_together": len(store.ml) - together(store.ml)},
    }


def cmd_run(args: argparse.Namespace, out: Any) -> int:
    settings = named_settings(args.settings)
    model = named_model(args.model)
    clock = time.perf_counter()
    dataset = load(args.artifact)
    load_seconds = time.perf_counter() - clock
    out_dir = Path(args.out)
    summary = run(dataset, settings, model, out_dir,
                  load_must_not_link(args.must_not_link), load_must_not_link(args.must_link),
                  withhold_photos=args.withhold_photos)
    summary["artifact"] = str(args.artifact)
    summary["timings"]["load_s"] = load_seconds
    (out_dir / RUN_FILE).write_text(json.dumps(summary, indent=2, sort_keys=True),
                                    encoding="utf-8")
    zones = summary["zones"]
    print(f"run {args.artifact} -> {out_dir}  calibration {summary['calibration_digest']}",
          file=out)
    print(f"  listings {summary['n_listings']}  pairs {summary['pairs_scored']}"
          f"  stored {summary['pairs_stored']}  passes {summary['passes']}", file=out)
    print(f"  merge {zones['merge']}  band {zones['band']}  reject {zones['reject']}"
          f"  veto {zones['veto']}  certificates {summary['certificates']}", file=out)
    print(f"  clusters {summary['clusters']['n_clusters']}"
          f"  refused unions {summary['clusters']['n_edges_refused']}", file=out)
    if "withheld_photos" in summary:
        print(f"  withheld photos {json.dumps(summary['withheld_photos'], sort_keys=True)}",
              file=out)
    print(f"  timings {json.dumps({k: round(v, 2) for k, v in summary['timings'].items()})}",
          file=out)
    return 0


def _side(fp: Fingerprint, listing: Listing) -> list[tuple[str, str]]:
    loc = listing.location
    return [
        ("listing", str(fp.listing_id)),
        ("source", f"{fp.source}/{listing.source_id_native}"),
        ("block", fp.block_key),
        ("category", f"{fp.category_main}/{fp.category_type}"),
        ("disposition", str(fp.disposition)),
        ("area_m2", str(fp.area_m2)),
        ("floor", f"{fp.floor}/{fp.total_floors}"),
        ("price", str(fp.price)),
        ("broker_key", (fp.broker_key or "-")[:12]),
        ("street", f"{fp.street_key or '-'} {fp.house_number or '-'}"),
        ("ruian", str(fp.ruian_adm_kod)),
        ("pin", f"{fp.pin_key or '-'} r={loc.uncertainty_radius_m}"),
        ("images", f"{fp.n_images} (non-catalog {len(fp.image_hashes)})"),
        ("window", f"{fp.first_seen_at} .. {fp.last_seen_at or fp.inactive_at}"),
        ("desc", " ".join((listing.description or "").split())[:60]),
    ]


def pair_probes(keyer: Keyer, fa: Fingerprint, fb: Fingerprint) -> list[str]:
    """The blocking probes that pair two adverts: a key one probes and the other is indexed
    under, neither exploded — empty when retrieval never pairs them."""
    return sorted({probe for x, y in ((fa, fb), (fb, fa))
                   for probe, token in set(keyer.probe_keys(x)) & set(keyer.index_keys(y))
                   if not keyer.is_exploded(probe, token)})


def cmd_pair(args: argparse.Namespace, out: Any) -> int:
    settings = named_settings(args.settings)
    model = named_model(args.model)
    dataset = load(args.artifact)
    missing = [listing_id for listing_id in (args.lo, args.hi) if listing_id not in dataset.listings]
    if missing:
        print(f"unknown listing id(s): {missing}", file=sys.stderr)
        return 1
    lo, hi = sorted((args.lo, args.hi))
    fps, calibration = calibrated(dataset, settings)
    row = decide_explicit(dataset, settings, model, [(lo, hi)], (fps, calibration))[(lo, hi)]
    decision = Decision(lo, hi, row["zone"], row["score"], set(row["families"]),
                        row["certificate"], row["veto"], row["reason"])
    feats = {name: (value, present) for name, (value, present) in row["feats"].items()}
    fa, fb, la, lb = fps[lo], fps[hi], dataset.listings[lo], dataset.listings[hi]
    probes = pair_probes(Keyer(settings, calibration), fa, fb)

    left = _side(fa, la)
    right = _side(fb, lb)
    print(f"pair {lo} x {hi}", file=out)
    print("-" * 96, file=out)
    for (label, value_a), (_, value_b) in zip(left, right):
        print(f"  {label:<12}{value_a[:38]:<40}{value_b[:38]}", file=out)
    print(f"\n  probes      {', '.join(probes) or '(not a candidate pair)'}", file=out)
    print(f"  zone        {decision.zone}  score {decision.score:.4f}"
          f"  certificate {decision.certificate or '-'}  veto {decision.veto or '-'}", file=out)
    print(f"  reason      {decision.reason}", file=out)
    print(f"  families    {', '.join(sorted(decision.families)) or '(none)'}", file=out)
    print("\n  features (present only)", file=out)
    for name in FEATURE_ORDER:
        value, present = feats[name]
        if present:
            print(f"    {name:<26}{value: .4f}", file=out)
    print("\n  top log-odds contributions", file=out)
    contributions = model.contributions(feats)
    for name, value in sorted(contributions.items(), key=lambda kv: -abs(kv[1]))[:12]:
        if abs(value) > 1e-9:
            print(f"    {name:<30}{value: .3f}", file=out)
    return 0


def feature_value(row: dict[str, Any], name: str) -> float | None:
    """One `[value, present]` feature off a stored pair row — absent reads as None (E12)."""
    entry = (row.get("feats") or {}).get(name)
    if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not entry[1]:
        return None
    try:
        return float(entry[0])
    except (TypeError, ValueError):
        return None


def read_pairs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / PAIRS_FILE
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# --- W3: labels in, evaluation and a fitted model out ---------------------------------


def _add_operator_label_args(command: argparse.ArgumentParser) -> None:
    """The operator tier's flags, read by `fit`."""
    command.add_argument("--operator-labels", action="append", default=None,
                         help="operator_labels.jsonl from the `labels` lane; repeatable."
                              " The operator is the TOP tier — it outranks gold")
    command.add_argument("--exclude-implied", action="store_true",
                         help="drop operator labels implied by a confirmed group and keep only"
                              " the pairs the operator ruled explicitly")
    command.add_argument("--implied-weight", type=float, default=None,
                         help="weight for implied operator labels (default: 1.0, the same as"
                              " an explicit one); 0 excludes them from the fit's arithmetic"
                              " while leaving them in the reports")
    command.add_argument("--exclude-browse-merge", action="store_true",
                         help="drop the pair labels a Browse merge wrote (source browse_merge,"
                              " E299) and keep the pairs the operator ruled one by one")
    command.add_argument("--browse-merge-weight", type=float, default=None,
                         help="weight for browse_merge operator labels (default: 1.0)")


def judgement_paths(args: argparse.Namespace) -> list[str] | None:
    """The `--judgements` files, or None when there is no label source at all.

    The operator is a tier in its own right (`labels.OPERATOR_TIER`), so a judgements file is no
    longer the only way to put a label on a pair — but SOME source has to be named, or there is
    nothing to measure against."""
    paths = list(getattr(args, "judgements", None) or ())
    if not paths and not list(getattr(args, "operator_labels", None) or ()):
        print("pass --judgements and/or --operator-labels: nothing to label with",
              file=sys.stderr)
        return None
    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        print(f"no such judgements file(s): {missing}", file=sys.stderr)
        return None
    return paths


def operator_tier(args: argparse.Namespace) -> dict[Any, Any]:
    """The operator tier as the flags ask for it, or empty when no artifact was given."""
    paths = list(getattr(args, "operator_labels", None) or ())
    if not paths:
        return {}
    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        raise SystemExit(f"no such operator-labels file(s): {missing}")
    rows = load_all_operator_labels(paths)
    weight = getattr(args, "implied_weight", None)
    merge_weight = getattr(args, "browse_merge_weight", None)
    return operator_label_pairs(
        rows,
        include_implied=not getattr(args, "exclude_implied", False),
        implied_weight=(WEIGHT_OPERATOR if weight is None else float(weight)),
        include_browse_merge=not getattr(args, "exclude_browse_merge", False),
        browse_merge_weight=(WEIGHT_OPERATOR if merge_weight is None else float(merge_weight)),
    )


def load_labels(
    paths: Sequence[str],
    precedence: Sequence[str],
    operator: Mapping[Any, Any] | None = None,
) -> tuple[Any, Any, Any]:
    judgements = load_all_judgements(paths)
    return (
        judgements,
        label_pairs(judgements, precedence=precedence, operator=operator),
        all_labels_by_tier(judgements, operator=operator),
    )


def decide_explicit(
    dataset: Dataset, settings: Settings, model: LogisticModel, keys: Iterable[tuple[int, int]],
    cohort: tuple[dict[int, Fingerprint], Calibration] | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Pairs decided as an explicit input set — the ruled pairs a run never stored, or one pair
    asked about by id (ADD07) — through the lane's calibration and `decide_pair`, and never added
    to a group. The census is the whole cohort's, which is the run's final one."""
    wanted = sorted({(min(lo, hi), max(lo, hi)) for lo, hi in keys
                     if lo != hi and lo in dataset.listings and hi in dataset.listings})
    if not wanted:
        return {}
    fps, calibration = cohort or calibrated(dataset, settings)
    ctx = context_for(calibration, settings, fps, dataset.listings)
    census = ContextIndex.build(dataset.listings, dataset.images_by_listing)
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for lo, hi in wanted:
        fa, fb, la, lb = fps[lo], fps[hi], dataset.listings[lo], dataset.listings[hi]
        feats = pair_features(fa, fb, la, lb, dataset.images(lo), dataset.images(hi), ctx,
                              settings)
        decision = decide_pair(fa, fb, la, lb, feats, (), model, settings, census)
        out[(lo, hi)] = {**decision.to_json(),
                         "feats": {name: [v, p] for name, (v, p) in feats.items()}}
    return out


def cmd_evaluate(args: argparse.Namespace, out: Any) -> int:
    """M1-M5 of one run against one rulings file, and with `--base` the D83 read lists."""
    run_dir = Path(args.run_dir)
    summary = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    artifact = args.artifact or summary.get("artifact")
    if not artifact or not Path(artifact).is_file():
        print(f"no cohort artifact {artifact!r}: pass --artifact", file=sys.stderr)
        return 1
    dataset = load(artifact)
    run_view = evaluation.read_run(run_dir)
    rulings = evaluation.read_rulings(args.rulings)
    ids = set(dataset.listings)
    unstored = [key for key in rulings
                if key[0] in ids and key[1] in ids and key not in run_view.rows]
    # Only the ruled pairs the run never stored are DECIDED here, and only under the run's own
    # engine: a run.json this checkout cannot parse (an arm's branch-only dials) or a model it
    # does not carry skips them, counted and named — never decided with this checkout's rule.
    # Everything else (M1-M3, M5, the D83 lists) reads the stored rows and never the settings.
    explicit: dict[tuple[int, int], dict[str, Any]] = {}
    skipped: dict[str, Any] | None = None
    try:
        engine = (Settings.from_dict(summary["settings"]),
                  model_of_version(summary.get("model_version")))
    except (ValueError, SystemExit) as exc:
        skipped = {"pairs": len(unstored), "reason": str(exc),
                   "to_decide": "run `harness evaluate` from the arm's own checkout"}
    else:
        explicit = decide_explicit(dataset, *engine, unstored)
    report: dict[str, Any] = {
        "run": str(run_dir), "rulings": str(args.rulings), "artifact": str(artifact),
        "explicit_pairs": len(explicit),
        **({"explicit_skipped": skipped} if skipped else {}),
        "metrics": evaluation.measure(run_view, rulings, ids, explicit),
        # E111: E110 names the event that revokes it, so every evaluation COUNTS it.
        "revocation": revocation.check_rows(run_view.rows.values(), rulings).to_json(),
    }
    if args.base:
        report["d83"] = evaluation.read_lists(
            evaluation.read_run(args.base), run_view, rulings,
            evaluation.read_reference(args.reference))
        report["base"] = str(args.base)
    target = Path(args.out or run_dir) / "evaluate.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    metrics = report["metrics"]
    print(f"evaluate {run_dir}  rulings {args.rulings}  ({metrics['M5_coverage']['verdict']})",
          file=out)
    for name in ("M1_precision", "M2_recall", "M3_purity", "M5_coverage"):
        print(f"  {name:<14}{json.dumps(metrics[name], sort_keys=True)}", file=out)
    print("  " + revocation.check_rows(run_view.rows.values(), rulings).line(), file=out)
    if skipped:
        print(f"  explicit: skipped {skipped['pairs']} ruled pair(s): {skipped['reason']}",
              file=out)
    if "d83" in report:
        counts = {k: v for k, v in report["d83"]["counts"].items() if not isinstance(v, dict)}
        print(f"  D83 vs {args.base}  {json.dumps(counts, sort_keys=True)}", file=out)
    print(f"wrote {target}", file=out)
    return 0


def split_seed_from_map(value: str | None, given: int, out: Any) -> int:
    """A committed map carries the seed that partitions it, so a holdout is never re-randomised.

    `split_of` hashes `<seed>:<group>`: one map under two seeds is two different holdouts. When
    the resolved map records a seed and the caller left `--seed` at its default, the FILE wins
    and says so; an explicit `--seed` that disagrees is an override, and it is printed as one."""
    if not value:
        return given
    try:
        recorded = seals.read_seed(seals.resolve(value))
    except (FileNotFoundError, ValueError):
        return given
    if recorded is None or recorded == given:
        return given
    if given == SAMPLE_SEED:
        print(f"split map records seed {recorded}; using it rather than the default {given}",
              file=out)
        return recorded
    print(f"warning: --seed {given} overrides the seed {recorded} the split map records; the "
          "holdout is not the one that map names", file=sys.stderr)
    return given


def parse_l2_grid(raw: str | None) -> tuple[float, ...] | None:
    """`--l2-grid` as given, `default` as the W4c grid, absent as no sweep at all."""
    if raw is None:
        return None
    if raw == "default":
        return L2_GRID
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise SystemExit("--l2-grid needs at least one penalty")
    try:
        return tuple(float(item) for item in values)
    except ValueError as exc:
        raise SystemExit(f"--l2-grid must be comma-separated numbers: {raw!r}") from exc


def cmd_fit(args: argparse.Namespace, out: Any) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / PAIRS_FILE).is_file():
        print(f"no {PAIRS_FILE} in {run_dir}", file=sys.stderr)
        return 1
    paths = judgement_paths(args)
    if paths is None:
        return 1
    operator = operator_tier(args)
    _judgements, labels, _ = load_labels(paths, args.precedence, operator)
    rows = read_pairs(run_dir)
    if args.split_map:
        try:
            groups = seals.read_map(seals.resolve(args.split_map))
        except (FileNotFoundError, ValueError) as exc:
            print(f"unusable --split-map: {exc}", file=sys.stderr)
            return 1
    else:
        groups = split_groups(rows, labels)
    seed = split_seed_from_map(args.split_map, args.seed, out)
    try:
        model, report = fit_model(
            rows, labels, seed=seed, version=args.version,
            epochs=args.epochs, method=args.method, max_iter=args.max_iter,
            tol=args.tol, l2=args.l2, l2_grid=parse_l2_grid(args.l2_grid),
            calibration=args.calibration,
            weight_cap=args.weight_cap, split_map=groups,
        )
    except ValueError as exc:
        print(f"fit failed: {exc}", file=sys.stderr)
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / MODEL_FILE
    model_path.write_text(
        json.dumps(model.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    # The split map is written beside the model so a challenger can be scored on THIS seal
    # rather than on whatever components its own merge edges happen to form (§9's feedback loop).
    seals.write_map(out_dir / SPLIT_MAP_FILE, groups, seed=seed)
    fit_seal = str(report.sections.get("split", {}).get("seal", {}).get("sha256") or "")
    json_path, markdown_path = write_report(report, out_dir / FIT_STEM)
    print(f"fit {run_dir}  judgements {', '.join(paths) or '(none)'}", file=out)
    print(f"  labels {len(labels)} over {len(rows)} stored pairs", file=out)
    print("", file=out)
    for line in report.headline():
        print(line, file=out)
    print(f"\nwrote {model_path}, {out_dir / SPLIT_MAP_FILE}, {json_path} and {markdown_path}",
          file=out)
    # A seal that lives only in a scratch directory is a seal the next wave cannot re-measure
    # on (W6 lost two that way), so every fit says how to make this one durable.
    if fit_seal and not seals.committed(fit_seal):
        print(f"  seal {fit_seal[:12]} is NOT committed — keep the holdout reproducible with:",
              file=out)
        print(f"    cp {out_dir / SPLIT_MAP_FILE} {seals.path_for(fit_seal)}", file=out)
    print(f"  activate with: run <artifact> --model {model_path}", file=out)
    if not report.sections["convergence"].get("converged_flag"):
        # The l2 advice points the OPPOSITE way per method: gradient descent stalls on a stiff
        # penalty, Newton stalls when the penalty is too weak to identify the design at all.
        knob = ("--max-iter (or raise --l2)" if args.method == "irls"
                else "--epochs (or lower --l2)")
        print(f"fit did not converge: raise {knob} before trusting the weights", file=sys.stderr)
        if args.require_convergence:
            return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodedup.harness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="per-block summary of the cohort artifact")
    stats.add_argument("artifact", help="path to cohort.jsonl.gz")
    stats.add_argument("--json", default=None, help="also write the summary as JSON here")
    stats.add_argument("--catalog-df", type=int, default=CATALOG_POP_MIN,
                       help="pHash population at which an image counts as catalog stock (E9/M9)")
    stats.set_defaults(func=cmd_stats)

    sample = sub.add_parser("sample", help="print a few listings from one block")
    sample.add_argument("artifact", help="path to cohort.jsonl.gz")
    sample.add_argument("--block", required=True, help="block key, e.g. turnov")
    sample.add_argument("--n", type=int, default=5, help="how many listings to print")
    sample.set_defaults(func=cmd_sample)

    run = sub.add_parser("run", help="the lane's run_pass over the cohort, in a MemoryStore")
    run.add_argument("artifact", help="path to cohort.jsonl.gz")
    run.add_argument("--out", required=True, help="directory for run.json / pairs.jsonl.gz / clusters.json")
    run.add_argument("--settings", default=None, help="a settings row by name (w31)")
    run.add_argument("--model", default=None, help="a model by name (w6_gold); default the prior")
    run.add_argument("--must-not-link", default=None,
                     help="the operator's `different` rulings (must_not_link.jsonl or [lo, hi])")
    run.add_argument("--must-link", default=None,
                     help="the operator's `same` rulings (must_link.jsonl or [lo, hi])")
    run.add_argument("--withhold-photos", action="store_true",
                     help="decide first with every photograph unprocessed, under the hold (E93)")
    run.set_defaults(func=cmd_run)

    pair = sub.add_parser("pair", help="side-by-side evidence for one pair")
    pair.add_argument("artifact", help="path to cohort.jsonl.gz")
    pair.add_argument("lo", type=int, help="listing id")
    pair.add_argument("hi", type=int, help="listing id")
    pair.add_argument("--settings", default=None, help="a settings row by name (w31)")
    pair.add_argument("--model", default=None, help="a model by name (w6_gold); default the prior")
    pair.set_defaults(func=cmd_pair)

    evaluate_parser = sub.add_parser(
        "evaluate", help="M1-M5 of a run against the rulings, and the D83 lists vs --base")
    evaluate_parser.add_argument("run_dir", help="a directory written by `run`")
    evaluate_parser.add_argument("rulings", help="the labels lane's export directory")
    evaluate_parser.add_argument("--base", default=None,
                                 help="the reference run directory the D83 lists read against")
    evaluate_parser.add_argument("--reference", default=None,
                                 help="truth16's CD/CN directory, a triage input (K40)")
    evaluate_parser.add_argument("--artifact", default=None,
                                 help="the cohort.jsonl.gz (default: the run.json's own)")
    evaluate_parser.add_argument("--out", default=None, help="directory for evaluate.json")
    evaluate_parser.set_defaults(func=cmd_evaluate)

    fit = sub.add_parser("fit", help="fit and calibrate a model from labels (PROGRAM.md §6)")
    fit.add_argument("run_dir", help="a directory written by `run`")
    fit.add_argument("--judgements", action="append", default=None,
                     help="judgements.jsonl files; repeatable (tiers merged by --precedence)."
                          " Optional when --operator-labels is given")
    fit.add_argument("--out", required=True, help="directory for the report files")
    fit.add_argument("--precedence", action="append", default=None,
                     help="tier precedence, highest first; repeatable"
                          " (default: operator, gold, vision, text)")
    _add_operator_label_args(fit)
    fit.set_defaults(func=cmd_fit, precedence_default=TIER_PRECEDENCE)
    fit.add_argument("--seed", type=int, default=SAMPLE_SEED,
                     help="deterministic 60/20/20 cluster-split seed")
    fit.add_argument("--version", default=None, help="model version string to stamp")
    fit.add_argument("--method", choices=list(FIT_METHODS), default=FIT_METHODS[0],
                     help="optimiser: irls (Newton, the default) or gd (gradient descent)")
    fit.add_argument("--epochs", type=int, default=3000,
                     help="gradient-descent epoch ceiling (--method gd)")
    fit.add_argument("--max-iter", type=int, default=FIT_MAX_ITER,
                     help="Newton iteration ceiling (--method irls)")
    fit.add_argument("--tol", type=float, default=None,
                     help="convergence tolerance: max|delta| for irls, mean|grad| for gd"
                          " (default: the method's own)")
    fit.add_argument("--l2", type=float, default=1e-3, help="L2 penalty on the weights")
    fit.add_argument("--l2-grid", nargs="?", const="default", default=None,
                     help="sweep L2 and keep the lowest validation log loss: a comma-separated "
                          f"list, or bare for the W4c grid {','.join(f'{v:g}' for v in L2_GRID)}")
    fit.add_argument("--calibration", choices=[CALIBRATION_AUTO, *CALIBRATION_METHODS],
                     default=CALIBRATION_AUTO,
                     help="calibration map; `auto` picks the lower out-of-fold validation ECE")
    fit.add_argument("--weight-cap", type=float, default=None,
                     help="cap on label weight x stratum weight"
                          " (default: 10x the median weight)")
    fit.add_argument("--split-map", default=None,
                     help="a split_map.json from an earlier fit, or a committed seal under"
                          " autodedup/splits, so a challenger is scored on the incumbent's"
                          " sealed split")
    fit.add_argument("--require-convergence", action="store_true",
                     help="exit 1 when the fit hits its iteration ceiling short of tolerance")
    return parser


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    stream = out if out is not None else sys.stdout
    if getattr(args, "precedence", None) is None and hasattr(args, "precedence_default"):
        args.precedence = list(args.precedence_default)
    artifact = getattr(args, "artifact", None)
    if artifact is not None and not Path(artifact).is_file():
        print(f"no such artifact: {artifact}", file=sys.stderr)
        return 1
    return int(args.func(args, stream))


if __name__ == "__main__":
    raise SystemExit(main())
