"""Local CLI over the W1 cohort artifact — read it, summarise it, run the whole engine on it.

    python3 -m autodedup.harness stats out/cohort.jsonl.gz [--json out/summary.json]
    python3 -m autodedup.harness stats out/cohort.jsonl.gz --catalog-df 5
    python3 -m autodedup.harness sample out/cohort.jsonl.gz --block turnov --n 5
    python3 -m autodedup.harness run out/cohort.jsonl.gz --out runs/r1 [--settings s.json]
    python3 -m autodedup.harness pair out/cohort.jsonl.gz 101 102
    python3 -m autodedup.harness judge-sample runs/r1 --zone merge --n 400

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
from typing import Any, Callable, Iterable, Sequence

from autodedup.blocking import build_index, generate_pairs
from autodedup.cluster import cluster_pairs, cluster_rows
from autodedup.dataset import (
    CATALOG_POP_MIN,
    UNASSIGNED_BLOCK,
    Dataset,
    Image,
    Listing,
    load,
)
from autodedup.decide import CERTIFICATES, ZONES, Decision, decide_pair
from autodedup.features import (
    MIN_RARE_BLOCK_DOCS,
    RARE_TOKEN_CAP,
    FEATURE_ORDER,
    FeatureContext,
    pair_features,
)
from autodedup.fingerprint import Fingerprint, build_all
from autodedup.model import LogisticModel, hand_initialised
from autodedup.settings import Settings

PAIRS_FILE: str = "pairs.jsonl.gz"
RUN_FILE: str = "run.json"
CLUSTERS_FILE: str = "clusters.json"
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
    """E27's permanent negatives as a JSON list of `[lo, hi]`, normalised to lo<hi."""
    if not path:
        return frozenset()
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return frozenset(
        (int(min(lo, hi)), int(max(lo, hi))) for lo, hi in (tuple(row) for row in rows)
    )


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
    stored = 0

    clock = time.perf_counter()
    with gzip.open(out_dir / PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for (lo, hi) in sorted(pairs):
            probes = pairs[(lo, hi)]
            fa, fb = fps[lo], fps[hi]
            la, lb = dataset.listings[lo], dataset.listings[hi]
            feats = pair_features(
                fa, fb, la, lb, dataset.images(lo), dataset.images(hi), ctx, settings
            )
            decision = decide_pair(fa, fb, la, lb, feats, probes, model, settings)
            decisions.append(decision)
            zones[decision.zone] += 1
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
            if decision.certificate:
                certificates[decision.certificate] += 1
            for family in decision.families:
                family_counts[family] = family_counts.get(family, 0) + 1
            scores.append(decision.score)
            block = pair_block(la, lb)
            _bump(per_block, block, decision)
            _bump(per_source_pair, source_pair(fa, fb), decision)
            if decision.score >= settings.store_floor or decision.zone in ("merge", "band"):
                stored += 1
                row = decision.to_json()
                row.update({
                    "block": block,
                    "block_key": pair_block_key(fa, fb),
                    "source_pair": source_pair(fa, fb),
                    "cross_source": fa.source != fb.source,
                    "probes": sorted(probes),
                    "exploded": sorted(set(exploded[lo]) | set(exploded[hi])),
                    "feats": {
                        name: [value, present]
                        for name, (value, present) in feats.items()
                    },
                })
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    timings["features_decide_s"] = time.perf_counter() - clock

    clock = time.perf_counter()
    clusters = cluster_pairs(decisions, dataset.listings, fps, settings, must_not_link)
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
        ("window", f"{fp.first_seen_at} .. {fp.inactive_at or fp.last_seen_at}"),
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
    decision = decide_pair(fa, fb, la, lb, feats, probes, model, settings)

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
    return parser


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    stream = out if out is not None else sys.stdout
    artifact = getattr(args, "artifact", None)
    if artifact is not None and not Path(artifact).is_file():
        print(f"no such artifact: {artifact}", file=sys.stderr)
        return 1
    return int(args.func(args, stream))


if __name__ == "__main__":
    raise SystemExit(main())
