"""Local CLI over the W1 cohort artifact — read it, summarise it, run the whole engine on it.

    python3 -m autodedup.harness stats out/cohort.jsonl.gz [--json out/summary.json]
    python3 -m autodedup.harness stats out/cohort.jsonl.gz --catalog-df 5
    python3 -m autodedup.harness sample out/cohort.jsonl.gz --block turnov --n 5
    python3 -m autodedup.harness run out/cohort.jsonl.gz --out runs/r1 [--settings s.json]
    python3 -m autodedup.harness pair out/cohort.jsonl.gz 101 102
    python3 -m autodedup.harness judge-sample runs/r1 --zone merge --n 400
    python3 -m autodedup.harness evaluate runs/r1 --judgements j.jsonl --out runs/r1/eval
    python3 -m autodedup.harness fit runs/r1 --judgements j.jsonl --out runs/r1/fit
    python3 -m autodedup.harness errors runs/r1 --judgements j.jsonl --top 5
    python3 -m autodedup.harness yardstick out/cohort.jsonl.gz runs/r1 --groups operator_merges.jsonl

No database, no network, no secrets: the artifact is the whole input. The artifact carries no
PII by contract (PROGRAM.md E28), so everything here is safe to print and to paste into a PR.

`stats` is also the artifact's only pre-W2 validator: it reconciles the per-block listing
counts against the cohort total and prints an integrity block, so a malformed payload or a
dropped record surfaces here rather than deep inside a feature pass.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup.blocking import build_index, generate_pairs
from autodedup.cluster import cluster_pairs, cluster_rows
from autodedup.d43 import relation_for
from autodedup.indistinguishable import FEATURE_SLOTS as D43_FEATURE_SLOTS
from autodedup.dataset import (
    CATALOG_POP_MIN,
    UNASSIGNED_BLOCK,
    Dataset,
    Image,
    Listing,
    load,
)
from autodedup.decide import CERTIFICATES, ZONES, Decision, decide_pair
from autodedup.development import holds as development_holds
from autodedup.family import refusals as family_guard_refusals
from autodedup.hazard_context import ContextIndex
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.evaluate import (
    CALIBRATION_AUTO,
    FIT_MAX_ITER,
    FIT_METHODS,
    L2_GRID,
    components_spanning_split,
    evaluate,
    fit_model,
    pooled_sample,
    rescore_rows,
    split_groups,
    write_report,
)
from autodedup import seals
from autodedup.features import (
    MIN_RARE_BLOCK_DOCS,
    RARE_TOKEN_CAP,
    FEATURE_ORDER,
    FeatureContext,
    pair_features,
)
from autodedup.fingerprint import Fingerprint, build_all
from autodedup import revocation
from autodedup.labels import (
    OPERATOR_TIER,
    TIER_PRECEDENCE,
    WEIGHT_OPERATOR,
    Sample,
    all_labels_by_tier,
    label_pairs,
    load_all_judgements,
    load_all_operator_labels,
    load_sample,
    operator_label_pairs,
    sample_from_judgements,
)
from autodedup.model import CALIBRATION_METHODS, LogisticModel, hand_initialised
from autodedup.settings import Settings
from autodedup.errors import DEFAULT_TOP, ERRORS_STEM, analyse
from autodedup.labels import SOURCE_BROWSE_MERGE
from autodedup.yardstick import DEFAULT_TOP as DEFAULT_YARDSTICK_TOP
from autodedup.yardstick import load_operator_pairs
from autodedup.yardstick import measure as measure_yardstick
from autodedup.yardstick import render_lines as render_yardstick
from autodedup.yardstick import write_report as write_yardstick

PAIRS_FILE: str = "pairs.jsonl.gz"
RUN_FILE: str = "run.json"
CLUSTERS_FILE: str = "clusters.json"
SAMPLE_FILE: str = "sample.json"
MODEL_FILE: str = "model.json"
SPLIT_MAP_FILE: str = "split_map.json"
EVAL_STEM: str = "eval"
FIT_STEM: str = "fit"
SAMPLE_SEED: int = 20260916
STRATUM_FLOOR: int = 5
JUDGE_STRATUM_FLOOR: int = 8
CATALOG_ONLY_STRATUM: str = "catalog-only"
CATALOG_ONLY_MIN: float = 0.90
SCORE_BUCKETS: int = 20

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


# --- the engine ----------------------------------------------------------------------


def load_settings(path: str | None) -> Settings:
    return Settings.from_json(path) if path else Settings()


def load_model(path: str | None) -> LogisticModel:
    if not path:
        return hand_initialised()
    return LogisticModel.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


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


def _bump(table: dict[str, dict[str, Any]], key: str, decision: Decision) -> None:
    row = table.setdefault(key, {"n": 0, **_zone_counter()})
    row["n"] += 1
    row[decision.zone] += 1


def _score_histogram(scores: Iterable[float]) -> dict[str, int]:
    buckets = [0] * SCORE_BUCKETS
    for score in scores:
        slot = min(SCORE_BUCKETS - 1, max(0, int(score * SCORE_BUCKETS)))
        buckets[slot] += 1
    return {f"{index / SCORE_BUCKETS:.2f}": count for index, count in enumerate(buckets)}


def load_must_not_link(path: str | None) -> frozenset[tuple[int, int]]:
    """E27's permanent negatives, normalised to lo<hi.

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


def _merge_pairs_file(
    part: Path,
    written: Sequence[tuple[int, int]],
    held: Sequence[tuple[tuple[int, int], str]],
    target: Path,
) -> None:
    """Merge the rows held back by E85 into the key order the artifact is read in.

    Both inputs are already sorted — the part file in the order the run decided pairs, the held
    rows by key — so one linear pass restores `pairs.jsonl.gz` to exactly the order a run
    without the guard writes. The part file never survives the run."""
    keys = list(held)
    keys.sort(key=lambda row: row[0])
    index = 0
    with gzip.open(part, "rt", encoding="utf-8") as source, \
            gzip.open(target, "wt", encoding="utf-8") as out:
        for key, line in zip(written, source, strict=True):
            while index < len(keys) and keys[index][0] < key:
                out.write(keys[index][1])
                index += 1
            out.write(line)
        for key, line in keys[index:]:
            out.write(line)
    part.unlink()


def run_engine(
    dataset: Dataset,
    settings: Settings,
    model: LogisticModel,
    out_dir: Path,
    must_not_link: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, Any]:
    """Fingerprint -> block -> feature -> decide -> cluster, writing the three run artifacts."""
    timings: dict[str, float] = {}
    clock = time.perf_counter()
    fps = build_all(dataset, settings)
    timings["fingerprints_s"] = time.perf_counter() - clock

    clock = time.perf_counter()
    pairs, blocking = generate_pairs(fps, settings)
    index = build_index(fps.values(), settings)
    exploded = {listing_id: sorted(index.exploded_probes(fp)) for listing_id, fp in fps.items()}
    blocking["listings_in_exploded_key"] = sum(1 for probes in exploded.values() if probes)
    timings["blocking_s"] = time.perf_counter() - clock

    clock = time.perf_counter()
    ctx = FeatureContext.build(fps, settings, dataset)
    ctx.index_attrs(fps, dataset.listings)
    # E63's refusing direction reads a census of the whole cohort, so it is built once here
    # beside the feature context rather than per pair.
    hazard = ContextIndex.build(dataset.listings, dataset.images_by_listing)
    timings["context_s"] = time.perf_counter() - clock

    out_dir.mkdir(parents=True, exist_ok=True)
    decisions: list[Decision] = []
    zones = _zone_counter()
    certificates = {name: 0 for name in CERTIFICATES}
    reasons: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    per_block: dict[str, dict[str, Any]] = {}
    per_source_pair: dict[str, dict[str, Any]] = {}
    scores: list[float] = []
    vetoed: set[tuple[int, int]] = set()
    # E132 reads three feature slots per pair and nothing else, so the cluster relation carries
    # those rather than the whole vector — 16k thin rows instead of 49k feature dictionaries.
    pair_slots: dict[tuple[int, int], dict[str, tuple[float, bool]]] = {}
    stored = 0

    clock = time.perf_counter()
    # E85: a family is a property of the PAIR SET, so a K-B pair cannot be finished until every
    # pair has been decided. Only the K-B rows are held back (1,600 of 49,000 on the g6 cohort),
    # and each carries the features its re-decision needs — the alternative, a second feature
    # pass, would double the only expensive phase of the run.
    deferred: list[dict[str, Any]] = []
    # ...and holding them back must not reorder anything downstream (W11 verification): the run
    # writes `pairs.jsonl.gz` in key order and clusters in edge order, so a deferred row is
    # merged back into its place rather than appended. `held` carries the finished K-B lines and
    # `written` the keys already on the part file, which is what makes the merge O(n) without a
    # second parse of every row.
    held: list[tuple[tuple[int, int], str]] = []
    written: list[tuple[int, int]] = []

    def finish(item: dict[str, Any], sink: Any) -> None:
        nonlocal stored
        decision, feats = item["decision"], item["feats"]
        lo, hi = item["lo"], item["hi"]
        decisions.append(decision)
        zones[decision.zone] += 1
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
        if decision.certificate:
            certificates[decision.certificate] += 1
        for name in decision.families:
            family_counts[name] = family_counts.get(name, 0) + 1
        scores.append(decision.score)
        _bump(per_block, item["block"], decision)
        _bump(per_source_pair, item["source_pair"], decision)
        if decision.veto == UNIT_DESIGNATOR_VETO:
            vetoed.add((lo, hi))
        # A vetoed row is stored although it scores nothing: E61 refuses on two STRINGS, and
        # the only way to adjudicate that refusal later is to read them off the row.
        if (decision.score >= settings.store_floor
                or decision.zone in ("merge", "band")
                or decision.evidence):
            stored += 1
            pair_slots[(lo, hi)] = {
                name: feats[name] for name in D43_FEATURE_SLOTS if name in feats
            }
            row = decision.to_json()
            if decision.certificate == "K-R":
                row.setdefault("evidence", {})["ref_codes"] = ",".join(ctx.shared_codes(lo, hi))
            row.update({
                # E63's census travels with the row so a re-simulation replays the census
                # the decision was taken under, never today's.
                "context": item["context"],
                "block": item["block"],
                "block_key": item["block_key"],
                "source_pair": item["source_pair"],
                "cross_source": item["cross_source"],
                "probes": sorted(item["probes"]),
                "exploded": sorted(set(exploded[lo]) | set(exploded[hi])),
                "feats": {
                    name: [value, present] for name, (value, present) in feats.items()
                },
            })
            sink((lo, hi), json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # E85 and E88 both decide a K-B row only once the whole pair set is known, so either one
    # sends the K-B rows down the deferred path. E86's ordering rail covers both.
    guard_on = settings.family_guard_mode != "off" or settings.development_hold_mode != "off"
    part_path = out_dir / (PAIRS_FILE + ".part" if guard_on else PAIRS_FILE)
    with gzip.open(part_path, "wt", encoding="utf-8") as handle:
        def to_file(key: tuple[int, int], line: str) -> None:
            written.append(key)
            handle.write(line)

        def to_memory(key: tuple[int, int], line: str) -> None:
            held.append((key, line))

        for (lo, hi) in sorted(pairs):
            probes = pairs[(lo, hi)]
            fa, fb = fps[lo], fps[hi]
            la, lb = dataset.listings[lo], dataset.listings[hi]
            feats = pair_features(
                fa, fb, la, lb, dataset.images(lo), dataset.images(hi), ctx, settings
            )
            decision = decide_pair(fa, fb, la, lb, feats, probes, model, settings, hazard)
            item = {
                "lo": lo, "hi": hi, "decision": decision, "feats": feats, "probes": probes,
                "fa": fa, "fb": fb, "la": la, "lb": lb,
                "block": pair_block(la, lb), "block_key": pair_block_key(fa, fb),
                "source_pair": source_pair(fa, fb), "cross_source": fa.source != fb.source,
                "context": hazard.pair_context(la, lb).to_json(),
            }
            if guard_on and decision.certificate == "K-B":
                deferred.append(item)
                continue
            finish(item, to_file)

        family_report: dict[str, Any] = {"mode": settings.family_guard_mode}
        hold_report: dict[str, Any] = {"mode": settings.development_hold_mode}
        if deferred:
            kb_decisions = [item["decision"] for item in deferred]
            refused, family_report = family_guard_refusals(
                kb_decisions, dataset.listings, settings
            )
            # E88: the hold reads the SAME pre-guard certificate set as E85 — both are
            # properties of the pair set, and reading one off the other's verdict would make
            # the family a function of the rule it is judging.
            dev_held, hold_report = development_holds(
                kb_decisions, dataset.listings, settings
            )
            for item in deferred:
                key = (item["lo"], item["hi"])
                clause, marker = refused.get(key), dev_held.get(key)
                if clause is not None or marker is not None:
                    item["decision"] = decide_pair(
                        item["fa"], item["fb"], item["la"], item["lb"], item["feats"],
                        item["probes"], model, settings, hazard, kb_refused=True,
                    )
                    if clause is not None:
                        item["decision"].reason = f"{item['decision'].reason}:kb_family:{clause}"
                        item["decision"].evidence["kb_family"] = clause
                    if marker is not None:
                        item["decision"].reason = (
                            f"{item['decision'].reason}:dev_hold:{marker}"
                        )
                        item["decision"].evidence["development_hold"] = marker
                finish(item, to_memory)

    if guard_on:
        _merge_pairs_file(part_path, written, held, out_dir / PAIRS_FILE)
    # The cohort pass decides in key order and the guard does not change what a pair is worth,
    # only when it is finished — so the edge order the clusterer sees stays the key order.
    decisions.sort(key=lambda decision: (decision.lo, decision.hi))
    timings["features_decide_s"] = time.perf_counter() - clock

    clock = time.perf_counter()
    # E61 refuses a UNION, not only an edge: two units of one building must not be joined
    # transitively through a third advert either, so the veto joins the must-not-link set.
    clusters = cluster_pairs(
        decisions, dataset.listings, fps, settings, frozenset(must_not_link) | vetoed,
        relation_for(settings, dataset.listings, pair_slots),
    )
    rows = cluster_rows(clusters, decisions, fps)
    timings["cluster_s"] = time.perf_counter() - clock

    (out_dir / CLUSTERS_FILE).write_text(
        json.dumps(
            {
                "clusters": {str(key): members for key, members in clusters.clusters.items()},
                "rows": rows,
                "conflicts": clusters.conflicts,
                "bridges": clusters.bridges,
                "stats": clusters.stats,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    scored = len(decisions)
    summary: dict[str, Any] = {
        "n_listings": len(dataset.listings),
        "n_images": sum(len(bucket) for bucket in dataset.images_by_listing.values()),
        "settings": settings.to_dict(),
        "model_version": model.version,
        "model_fit": dict(model.fit_report),
        "feature_order": list(FEATURE_ORDER),
        # The feature-module caps are not all settings fields, and a run artifact that cannot say
        # which caps produced it cannot be compared with the next one.
        "feature_params": {
            "clip_sample": int(settings.clip_sample),
            "phash_sample": int(settings.phash_sample),
            "min_rare_block_docs": MIN_RARE_BLOCK_DOCS,
            "rare_token_cap": RARE_TOKEN_CAP,
        },
        "blocking": blocking,
        "pairs_scored": scored,
        "pairs_stored": stored,
        "zones": zones,
        "guarded_at_blocking": blocking.get("guarded_pairs", {}),
        # E25: the band width IS the budget dial, so it is a headline number of every run.
        "band_width": (zones["band"] / scored) if scored else 0.0,
        "certificates": certificates,
        "reasons": dict(sorted(reasons.items())),
        "evidence_families": dict(sorted(family_counts.items())),
        "score_histogram": _score_histogram(scores),
        "per_block": {key: per_block[key] for key in sorted(per_block)},
        "per_source_pair": {key: per_source_pair[key] for key in sorted(per_source_pair)},
        "clusters": clusters.stats,
        # N4: g7 shipped `n_must_not_link = 45` and that was the E61 designator veto set alone —
        # the operator's own permanent negatives never reached the batch build, and the single
        # total could not say so. The split is now on the record of every run.
        "must_not_link": {
            "operator": len(frozenset(must_not_link) - vetoed),
            "unit_designator_veto": len(vetoed),
            "total": len(frozenset(must_not_link) | vetoed),
            "loaded": bool(must_not_link),
        },
        "family_guard": family_report,
        "development_hold": hold_report,
        "timings": timings,
    }
    return summary


def cmd_run(args: argparse.Namespace, out: Any) -> int:
    settings = load_settings(args.settings)
    model = load_model(args.model)
    clock = time.perf_counter()
    dataset = load(args.artifact)
    load_seconds = time.perf_counter() - clock
    out_dir = Path(args.out)
    must_not_link = load_must_not_link(getattr(args, "must_not_link", None))
    summary = run_engine(dataset, settings, model, out_dir, must_not_link)
    summary["artifact"] = str(args.artifact)
    summary["timings"]["load_s"] = load_seconds
    summary["timings"]["total_s"] = load_seconds + sum(
        value for key, value in summary["timings"].items() if key != "load_s"
    )
    (out_dir / RUN_FILE).write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    zones = summary["zones"]
    print(f"run {args.artifact} -> {out_dir}", file=out)
    print(f"  listings {summary['n_listings']}  pairs {summary['pairs_scored']}"
          f"  stored {summary['pairs_stored']}", file=out)
    print(f"  merge {zones['merge']}  band {zones['band']}  reject {zones['reject']}"
          f"  guarded {summary['blocking'].get('n_guarded_pairs', 0)}", file=out)
    print(f"  band width {summary['band_width']:.4f}"
          f"  certificates {summary['certificates']}", file=out)
    print(f"  clusters {summary['clusters']['n_clusters']}"
          f"  refused unions {summary['clusters']['n_edges_refused']}"
          f"  bridges {summary['clusters']['n_bridges_refused']}"
          f" (applied {summary['clusters'].get('n_bridges_applied', 0)})"
          f"  must-not-link {summary['clusters']['n_must_not_link']}", file=out)
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


def cmd_pair(args: argparse.Namespace, out: Any) -> int:
    settings = load_settings(args.settings)
    model = load_model(args.model)
    dataset = load(args.artifact)
    missing = [listing_id for listing_id in (args.lo, args.hi) if listing_id not in dataset.listings]
    if missing:
        print(f"unknown listing id(s): {missing}", file=sys.stderr)
        return 1
    fps = build_all(dataset, settings)
    ctx = FeatureContext.build(fps, settings, dataset)
    ctx.index_attrs(fps, dataset.listings)
    lo, hi = sorted((args.lo, args.hi))
    fa, fb = fps[lo], fps[hi]
    la, lb = dataset.listings[lo], dataset.listings[hi]
    pairs, _ = generate_pairs(fps, settings)
    probes = sorted(pairs.get((lo, hi), set()))
    feats = pair_features(fa, fb, la, lb, dataset.images(lo), dataset.images(hi), ctx, settings)
    decision = decide_pair(
        fa, fb, la, lb, feats, probes, model, settings,
        ContextIndex.build(dataset.listings, dataset.images_by_listing),
    )

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


def _stratum(row: dict[str, Any]) -> str:
    side = "cross" if row.get("cross_source") else "same"
    return f"{row.get('zone')}|{side}|{row.get('block') or '(none)'}"


def _certificate_class(row: dict[str, Any]) -> str:
    """Which layer decided the pair: a named certificate (E24), the model, or neither."""
    certificate = row.get("certificate")
    if certificate:
        return str(certificate)
    return "model" if row.get("zone") in ("merge", "band") else "none"


def _feat(row: dict[str, Any], name: str) -> float | None:
    """One `[value, present]` feature off a stored pair row — absent reads as None (E12)."""
    entry = (row.get("feats") or {}).get(name)
    if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not entry[1]:
        return None
    try:
        return float(entry[0])
    except (TypeError, ValueError):
        return None


def judge_stratum(row: dict[str, Any]) -> str:
    """W3's judge strata: zone x deciding layer x cohort block x same/cross source.

    Pairs whose image evidence is almost entirely catalogue stock are pulled out WHOLE rather
    than split across that grid: they are the developer-project class the judge exists to
    separate (PROGRAM.md §2), and proportional allocation over a four-way key would scatter
    them too thin for the resulting agreement number to mean anything."""
    ratio = _feat(row, "catalog_ratio_max")
    if ratio is not None and ratio >= CATALOG_ONLY_MIN:
        return CATALOG_ONLY_STRATUM
    side = "cross" if row.get("cross_source") else "same"
    return (f"{row.get('zone')}|{_certificate_class(row)}"
            f"|{row.get('block') or '(none)'}|{side}")


def _shuffle_key(seed: int, row: dict[str, Any]) -> str:
    payload = f"{seed}:{row.get('lo')}:{row.get('hi')}".encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def stratified_sample(
    rows: Sequence[dict[str, Any]],
    n: int,
    seed: int = SAMPLE_SEED,
    *,
    key_fn: Callable[[dict[str, Any]], str] = _stratum,
    floor: int = STRATUM_FLOOR,
) -> dict[str, Any]:
    """Proportional allocation with a floor of `floor` per stratum, deterministic order.

    The floor is honoured first — a stratum too small to judge is exactly the one W3 must see —
    so the selection can exceed `n` only when the floors alone already do."""
    strata: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault(key_fn(row), []).append(row)
    quotas: dict[str, int] = {
        name: min(floor, len(members)) for name, members in strata.items()
    }
    remaining = max(0, n - sum(quotas.values()))
    headroom = {key: len(members) - quotas[key] for key, members in strata.items()}
    total_headroom = sum(headroom.values())
    if remaining and total_headroom:
        shares = {
            key: remaining * space / total_headroom for key, space in headroom.items()
        }
        for key, share in shares.items():
            quotas[key] += min(headroom[key], int(share))
        leftovers = sorted(
            ((shares[key] - int(shares[key]), key) for key in shares), reverse=True
        )
        spare = remaining - sum(min(headroom[key], int(shares[key])) for key in shares)
        for _, key in leftovers:
            if spare <= 0:
                break
            if quotas[key] < len(strata[key]):
                quotas[key] += 1
                spare -= 1

    selected: list[dict[str, Any]] = []
    report: dict[str, dict[str, int]] = {}
    for key in sorted(strata):
        members = sorted(strata[key], key=lambda row: _shuffle_key(seed, row))
        take = members[: quotas[key]]
        selected.extend(take)
        report[key] = {"population": len(members), "selected": len(take)}
    selected.sort(key=lambda row: (row.get("lo", 0), row.get("hi", 0)))
    return {
        "seed": seed,
        "n_requested": n,
        "n_selected": len(selected),
        "n_strata": len(strata),
        "stratum_floor": floor,
        "strata": report,
        "pairs": selected,
    }


def sample_pairs(
    rows: Sequence[dict[str, Any]], n: int, seed: int = SAMPLE_SEED
) -> dict[str, Any]:
    """The judge lane's sample (PROGRAM.md §9): `judge_stratum` at a floor of 8.

    A pure function of (rows, n, seed), so the text and vision tiers of one seed judge the SAME
    pairs — which is the only way tier-vs-tier agreement (metric 8) measures the tiers rather
    than two different draws."""
    return stratified_sample(
        rows, n, seed, key_fn=judge_stratum, floor=JUDGE_STRATUM_FLOOR
    )


def read_pairs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / PAIRS_FILE
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def cmd_judge_sample(args: argparse.Namespace, out: Any) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / PAIRS_FILE).is_file():
        print(f"no {PAIRS_FILE} in {run_dir}", file=sys.stderr)
        return 1
    rows = read_pairs(run_dir)
    zones = tuple(args.zone or ())
    if zones:
        rows = [row for row in rows if row.get("zone") in zones]
    sample = stratified_sample(rows, args.n, args.seed)
    sample["run_dir"] = str(run_dir)
    sample["zones"] = list(zones)
    target = Path(args.out) if args.out else run_dir / "sample.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8")
    print(f"sampled {sample['n_selected']} pairs over {sample['n_strata']} strata"
          f" -> {target}", file=out)
    for key in sorted(sample["strata"]):
        row = sample["strata"][key]
        print(f"  {key:<44}{row['selected']:>5} of {row['population']}", file=out)
    return 0


# --- W3: labels in, evaluation and a fitted model out ---------------------------------


def run_settings(run_dir: Path, override: str | None) -> Settings:
    """The settings the pairs were SCORED under, unless a sweep file overrides them.

    Reading t_hi/t_lo off `run.json` matters: an evaluation run against today's defaults would
    silently re-zone yesterday's pairs and report a precision for a decision nobody made."""
    if override:
        return Settings.from_json(override)
    path = run_dir / RUN_FILE
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8")).get("settings")
        if isinstance(payload, dict):
            return Settings.from_dict(payload)
    return Settings()


def resolve_sample(
    paths: Sequence[str], explicit: Sequence[str] | str | None
) -> tuple[Sample | None, str]:
    """ONE DRAW PER JUDGEMENTS FILE — `--sample` repeated in the same order, else the
    `sample.json` the lane wrote beside each file — pooled into one design.

    Several judgement files are several draws, and a stratum's sampling rate belongs to the draw
    rather than to its name (`merge|K-C|jablonec|cross` was drawn 87 of 1,178 in the seed-1
    vision sample and 47 of 1,175 in the seed-2 one). Weighting them all by whichever file was
    passed first inflates every pair the other draws contributed, so the draws are pooled by
    `evaluate.pooled_sample`. A single `--sample` still covers every file — the pre-W4f
    behaviour — and passing several requires one per `--judgements`.

    Without any there are no per-stratum populations, so no number is cohort-level — the caller
    prints the reason rather than quietly reporting sample rates as cohort rates. An EXPLICIT
    sample that does not match its judgements is fatal; an auto-found one that does not match is
    dropped with a warning, because picking the neighbouring file was this function's guess."""
    given = [explicit] if isinstance(explicit, str) else list(explicit or ())
    if given and len(given) not in (1, len(paths)):
        raise ValueError(
            f"--sample given {len(given)} times for {len(paths)} --judgements: pass one per "
            f"judgements file, or exactly one for all of them"
        )
    overrides = (given * len(paths)) if len(given) == 1 else (given or [None] * len(paths))
    draws: list[Sample] = []
    notes: list[str] = []
    seen: set[str] = set()
    for path, override in zip(paths, overrides):
        candidate = Path(override) if override else Path(path).parent / SAMPLE_FILE
        if str(candidate) in seen:
            continue
        if not candidate.is_file():
            if override:
                raise ValueError(f"no such sample: {candidate}")
            continue
        try:
            draws.append(load_sample(candidate))
        except ValueError as exc:
            if override:
                raise
            print(f"ignoring {candidate}: {exc}", file=sys.stderr)
            continue
        seen.add(str(candidate))
        notes.append(str(candidate))
    if not draws:
        return None, "(none: HT weights fall back to 1.0)"
    if len(draws) == 1:
        return draws[0], notes[0]
    pooled = pooled_sample(draws)
    return pooled, f"{len(draws)} draws pooled [{pooled.stratum_fn}]: " + "; ".join(notes)


def _add_operator_label_args(command: argparse.ArgumentParser) -> None:
    """The operator tier's three flags, shared by `evaluate`, `fit` and `errors`."""
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


def cmd_evaluate(args: argparse.Namespace, out: Any) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / PAIRS_FILE).is_file():
        print(f"no {PAIRS_FILE} in {run_dir}", file=sys.stderr)
        return 1
    paths = judgement_paths(args)
    if paths is None:
        return 1
    operator = operator_tier(args)
    judgements, labels, per_tier = load_labels(paths, args.precedence, operator)
    if not labels:
        print("no usable labels in the judgements given", file=sys.stderr)
        return 1
    try:
        sample, sample_note = resolve_sample(paths, args.sample)
    except ValueError as exc:
        print(f"unusable --sample: {exc}", file=sys.stderr)
        return 1
    if sample is None and judgements:
        sample = sample_from_judgements(judgements)
    settings = run_settings(run_dir, args.settings)
    rows = read_pairs(run_dir)
    model_note = "run's own scores"
    expect_seal: str | None = None
    if args.model:
        path = Path(args.model)
        if not path.is_file():
            print(f"no such model: {path}", file=sys.stderr)
            return 1
        try:
            challenger = LogisticModel.from_json(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            print(f"unusable --model: {exc}", file=sys.stderr)
            return 1
        rows = rescore_rows(rows, challenger, settings)
        model_note = f"re-decided with {path} ({challenger.version})"
        expect_seal = ((challenger.provenance.get("seal") or {}).get("sha256") or None)
    # The split the model was FITTED on, not one re-derived from the merge edges of the run being
    # evaluated: those edges move with the model, so without this the "sealed" rows are a
    # different set here than they were at fit time and part of the holdout is training data.
    split_map = None
    if args.split_map:
        try:
            split_map = seals.read_map(seals.resolve(args.split_map))
        except (FileNotFoundError, ValueError) as exc:
            print(f"unusable --split-map: {exc}", file=sys.stderr)
            return 1
    elif expect_seal and seals.committed(expect_seal):
        # The map the model names is IN THE REPOSITORY, so the holdout needs no argument.
        split_map = seals.load(expect_seal)
        print(f"using the committed split map for seal {expect_seal[:12]}", file=out)
    elif expect_seal:
        lost = seals.LOST_SEALS.get(expect_seal)
        print(f"warning: --model carries seal {expect_seal[:12]} but no --split-map was given "
              f"and no map is committed for it"
              + (f" ({lost})" if lost else "")
              + "; the holdout below is re-derived from this run's own merge edges",
              file=sys.stderr)
        expect_seal = None
    seed = split_seed_from_map(args.split_map, args.seed, out)
    if seed == args.seed and expect_seal and split_map is not None and not args.split_map:
        recorded = seals.seed_for(expect_seal)
        if recorded is not None and args.seed == SAMPLE_SEED:
            seed = recorded
            print(f"the committed map records seed {recorded}; using it", file=out)
    if expect_seal and seals.spent(expect_seal):
        print(f"note: seal {expect_seal[:12]} is SPENT — {seals.spent(expect_seal)}", file=out)
    if split_map is not None:
        # E69: a map is cut from the components that existed when it was sealed, so a CHALLENGER
        # merging pairs the incumbent banded can put one cluster on both sides of the holdout.
        held = components_spanning_split(rows, split_map, seed)
        if not held["contained"]:
            print(
                f"warning: {held['n_spanning']} of this run's merge components span the holdout "
                f"({held['listings_in_spanning']} listings, {held['listings_absent_from_map']} "
                "of them absent from the map): the seal cannot adjudicate a cluster-grain claim "
                "about a run it does not contain (E69)",
                file=sys.stderr,
            )
    try:
        report = evaluate(rows, labels, sample, settings, by_tier=per_tier,
                          precedence=args.precedence, seed=seed,
                          split_map=split_map, expect_seal=expect_seal)
    except ValueError as exc:
        print(f"evaluate failed: {exc}", file=sys.stderr)
        return 1
    json_path, markdown_path = write_report(report, Path(args.out) / EVAL_STEM)
    print(f"evaluate {run_dir}  judgements {', '.join(paths) or '(none)'}", file=out)
    print(f"  labels {len(labels)} over {len(rows)} stored pairs   sample {sample_note}", file=out)
    print(f"  model {model_note}", file=out)
    # E111: E110 names the event that revokes it, so every evaluation COUNTS that event rather
    # than leaving it to be remembered. The check reports; it never decides.
    print("  " + revocation.check_rows(rows, per_tier.get(OPERATOR_TIER, {})).line(), file=out)
    print("", file=out)
    for line in report.headline():
        print(line, file=out)
    print(f"\nwrote {json_path} and {markdown_path}", file=out)
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
    judgements, labels, _ = load_labels(paths, args.precedence, operator)
    try:
        sample, sample_note = resolve_sample(paths, args.sample)
    except ValueError as exc:
        print(f"unusable --sample: {exc}", file=sys.stderr)
        return 1
    if sample is None and judgements:
        sample = sample_from_judgements(judgements)
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
            rows, labels, sample=sample, seed=seed, version=args.version,
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
    print(f"  labels {len(labels)} over {len(rows)} stored pairs   sample {sample_note}", file=out)
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

    run = sub.add_parser("run", help="blocking + features + decisions + clusters over a cohort")
    run.add_argument("artifact", help="path to cohort.jsonl.gz")
    run.add_argument("--out", required=True, help="directory for run.json / pairs.jsonl.gz / clusters.json")
    run.add_argument("--settings", default=None, help="Settings JSON (a sweep file)")
    run.add_argument("--model", default=None, help="model JSON; default is the hand priors")
    run.add_argument("--must-not-link", default=None,
                     help="JSON list of [lo, hi] pairs the clustering must never join (E27)")
    run.set_defaults(func=cmd_run)

    pair = sub.add_parser("pair", help="side-by-side evidence for one pair")
    pair.add_argument("artifact", help="path to cohort.jsonl.gz")
    pair.add_argument("lo", type=int, help="listing id")
    pair.add_argument("hi", type=int, help="listing id")
    pair.add_argument("--settings", default=None, help="Settings JSON")
    pair.add_argument("--model", default=None, help="model JSON; default is the hand priors")
    pair.set_defaults(func=cmd_pair)

    judge = sub.add_parser("judge-sample", help="stratified pair sample from a run directory (W3)")
    judge.add_argument("run_dir", help="a directory written by `run`")
    judge.add_argument("--n", type=int, default=400, help="target sample size")
    judge.add_argument("--out", default=None, help="where to write the sample JSON")
    judge.add_argument("--seed", type=int, default=SAMPLE_SEED, help="deterministic sample seed")
    judge.add_argument("--zone", action="append", choices=list(ZONES), default=None,
                       help="restrict the sample to this zone; repeatable"
                            " (D3's precision sample is `--zone merge --n 400`)")
    judge.set_defaults(func=cmd_judge_sample)

    for name, helptext, handler in (
        ("evaluate", "score a run against judge labels (PROGRAM.md §9)", cmd_evaluate),
        ("fit", "fit and calibrate a model from judge labels (PROGRAM.md §6)", cmd_fit),
    ):
        command = sub.add_parser(name, help=helptext)
        command.add_argument("run_dir", help="a directory written by `run`")
        command.add_argument("--judgements", action="append", default=None,
                             help="judgements.jsonl from the judge lane; repeatable"
                                  " (tiers are merged by --precedence). Optional when"
                                  " --operator-labels is given")
        command.add_argument("--sample", action="append", default=None,
                             help="sample.json carrying the per-stratum populations the"
                                  " Horvitz-Thompson weights need; repeatable, ONE PER"
                                  " --judgements in the same order (several files are several"
                                  " draws at different rates); one covers them all; default:"
                                  " the sample.json beside each --judgements")
        command.add_argument("--out", required=True, help="directory for the report files")
        command.add_argument("--precedence", action="append", default=None,
                             help="tier precedence, highest first; repeatable"
                                  " (default: operator, gold, vision, text)")
        _add_operator_label_args(command)
        command.set_defaults(func=handler, precedence_default=TIER_PRECEDENCE)
        command.add_argument("--seed", type=int, default=SAMPLE_SEED,
                             help="deterministic 60/20/20 cluster-split seed")
    fit = sub.choices["fit"]
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
    evaluate_parser = sub.choices["evaluate"]
    evaluate_parser.add_argument("--model", default=None,
                                 help="re-decide the stored pairs with this model JSON before "
                                      "evaluating (the hand prior's own scores are used without "
                                      "it)")
    evaluate_parser.add_argument(
        "--split-map", default=None,
        help="the split_map.json written beside the model being evaluated, or a committed "
             "seal under autodedup/splits, so the sealed split is the one the fit held out "
             "rather than one re-derived from this run's merge edges (a model whose seal IS "
             "committed needs no flag)",
    )
    evaluate_parser.add_argument("--settings", default=None,
                                 help="Settings JSON; default: the settings on run.json")

    # --- W4b: `errors` (autodedup/errors.py) -------------------------------------------
    errors_parser = sub.add_parser(
        "errors", help="false merges, false rejects and band composition, feature by feature"
    )
    errors_parser.add_argument("run_dir", help="a directory written by `run`")
    errors_parser.add_argument("--judgements", action="append", default=None,
                               help="judgements.jsonl from the judge lane; repeatable"
                                    " (tiers are merged by --precedence). Optional when"
                                    " --operator-labels is given")
    errors_parser.add_argument("--sample", action="append", default=None,
                               help="sample.json carrying the per-stratum populations the"
                                    " Horvitz-Thompson weights need; repeatable, one per"
                                    " --judgements in the same order; default: beside"
                                    " --judgements")
    errors_parser.add_argument("--out", default=None,
                               help="directory for errors.json / errors.md (default: run_dir)")
    errors_parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                               help="how many example pairs to list per error group")
    errors_parser.add_argument("--precedence", action="append", default=None,
                               help="tier precedence, highest first; repeatable"
                                    " (default: operator, gold, vision, text)")
    _add_operator_label_args(errors_parser)
    errors_parser.add_argument("--threshold", action="append", type=float, default=None,
                               help="score cut for the model-merge tables; repeatable"
                                    " (default: the run's own t_hi and the rungs above it)")
    errors_parser.set_defaults(func=cmd_errors, precedence_default=TIER_PRECEDENCE)

    # --- E299: `yardstick` (autodedup/yardstick.py) -------------------------------------
    yard = sub.add_parser(
        "yardstick",
        help="one generation measured against the operator's own Browse merges (E299)",
    )
    yard.add_argument("artifact", help="the cohort.jsonl.gz the generation was scored on")
    yard.add_argument("run", help="the generation: a run directory (pairs.jsonl.gz, clusters.json,"
                                  " run.json) or its pairs.jsonl.gz with --clusters")
    yard.add_argument("--groups", action="append", required=True,
                      help="operator_merges.jsonl from the labels lane, a JSON dump of"
                           " autodedup.operator_merges, or operator_labels.jsonl (its"
                           " browse_merge rows); repeatable")
    yard.add_argument("--clusters", default=None,
                      help="clusters.json (default: beside the pairs file)")
    yard.add_argument("--settings", default=None,
                      help="settings name (w29) or JSON path (default: the run.json's own)")
    yard.add_argument("--model", default=None,
                      help="model name (w6_gold) or JSON path (default: the run.json's"
                           " model_version)")
    yard.add_argument("--label-source", action="append", default=None,
                      help="operator_labels.jsonl sources to measure (default: browse_merge);"
                           " repeatable")
    yard.add_argument("--include-not-live", action="store_true",
                      help="also measure groups whose status is undone or withdrawn")
    yard.add_argument("--out", default=None,
                      help="directory for yardstick.json / yardstick.md (default: the run's)")
    yard.add_argument("--top", type=int, default=DEFAULT_YARDSTICK_TOP,
                      help="how many misses the printed list shows (the JSON holds all)")
    yard.set_defaults(func=cmd_yardstick)

    return parser


def _settings_arg(raw: str) -> Settings:
    """A path to a Settings JSON, else a name under autodedup/settings."""
    return Settings.from_json(raw) if Path(raw).is_file() else named_settings(raw)


def _model_arg(raw: str) -> LogisticModel:
    """A path to a model JSON, else a name under autodedup/models."""
    return load_model(raw) if Path(raw).is_file() else named_model(raw)


def cmd_yardstick(args: argparse.Namespace, out: Any) -> int:
    run = Path(args.run)
    run_dir = run if run.is_dir() else run.parent
    pairs_path = run / PAIRS_FILE if run.is_dir() else run
    clusters_path = Path(args.clusters) if args.clusters else run_dir / CLUSTERS_FILE
    for path in (pairs_path, clusters_path):
        if not path.is_file():
            print(f"no such file: {path}", file=sys.stderr)
            return 1
    missing = [path for path in args.groups if not Path(path).is_file()]
    if missing:
        print(f"no such operator-groups file(s): {missing}", file=sys.stderr)
        return 1
    run_json: dict[str, Any] = {}
    if (run_dir / RUN_FILE).is_file():
        run_json = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    settings = (_settings_arg(args.settings) if args.settings
                else run_settings(run_dir, None))
    model = (_model_arg(args.model) if args.model
             else model_of_version(run_json.get("model_version")))
    try:
        operator = load_operator_pairs(
            args.groups, label_sources=tuple(args.label_source or (SOURCE_BROWSE_MERGE,)),
            include_not_live=args.include_not_live,
        )
    except (ValueError, KeyError) as exc:
        print(f"unreadable operator groups: {exc}", file=sys.stderr)
        return 1
    clock = time.perf_counter()
    dataset = load(args.artifact)
    report = measure_yardstick(
        dataset, settings, model,
        pairs_path=pairs_path,
        clusters_payload=json.loads(clusters_path.read_text(encoding="utf-8")),
        operator=operator,
        inputs={
            "cohort": str(args.artifact), "pairs": str(pairs_path),
            "clusters": str(clusters_path), "groups": list(args.groups),
            "generation": run_json.get("generation"),
            "model_version": model.version,
            "settings": args.settings or "run.json",
        },
    )
    report["inputs"]["seconds"] = round(time.perf_counter() - clock, 1)
    json_path, text_path = write_yardstick(
        report, Path(args.out) if args.out else run_dir, args.top)
    for line in render_yardstick(report, args.top):
        print(line, file=out)
    print(f"\nwrote {json_path} and {text_path}", file=out)
    return 0


# --- W4b: the `errors` command (analysis lives in autodedup/errors.py) ---------------------


def cmd_errors(args: argparse.Namespace, out: Any) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / PAIRS_FILE).is_file():
        print(f"no {PAIRS_FILE} in {run_dir}", file=sys.stderr)
        return 1
    paths = judgement_paths(args)
    if paths is None:
        return 1
    operator = operator_tier(args)
    judgements, labels, _ = load_labels(paths, args.precedence, operator)
    if not labels:
        print("no usable labels in the judgements given", file=sys.stderr)
        return 1
    try:
        sample, sample_note = resolve_sample(paths, args.sample)
    except ValueError as exc:
        print(f"unusable --sample: {exc}", file=sys.stderr)
        return 1
    if sample is None and judgements:
        sample = sample_from_judgements(judgements)
    rows = read_pairs(run_dir)
    # The score ladder is anchored on the cut the pairs were ZONED under, so no rung is a
    # silent duplicate of the live merge set (see errors.threshold_ladder).
    t_hi = run_settings(run_dir, None).t_hi
    report = analyse(
        rows, labels, judgements, sample,
        top=args.top, t_hi=t_hi, thresholds=args.threshold,
    )
    out_dir = Path(args.out) if args.out else run_dir
    json_path, markdown_path = write_report(report, out_dir / ERRORS_STEM)
    print(f"errors {run_dir}  judgements {', '.join(paths) or '(none)'}", file=out)
    print(f"  labels {len(labels)} over {len(rows)} stored pairs   sample {sample_note}",
          file=out)
    print("", file=out)
    for line in report.headline():
        print(line, file=out)
    print(f"\nwrote {json_path} and {markdown_path}", file=out)
    return 0


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
