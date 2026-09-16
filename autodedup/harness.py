"""Local CLI over the W1 cohort artifact — read it, summarise it, eyeball a few listings.

    python3 -m autodedup.harness stats out/cohort.jsonl.gz [--json out/summary.json]
    python3 -m autodedup.harness stats out/cohort.jsonl.gz --catalog-df 5
    python3 -m autodedup.harness sample out/cohort.jsonl.gz --block turnov --n 5

No database, no network, no secrets: the artifact is the whole input. The artifact carries no
PII by contract (PROGRAM.md E28), so everything here is safe to print and to paste into a PR.

`stats` is also the artifact's only pre-W2 validator: it reconciles the per-block listing
counts against the cohort total and prints an integrity block, so a malformed payload or a
dropped record surfaces here rather than deep inside a feature pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from autodedup.dataset import CATALOG_POP_MIN, Dataset, Image, Listing, load

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
    return parser


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    stream = out if out is not None else sys.stdout
    path = Path(args.artifact)
    if not path.is_file():
        print(f"no such artifact: {path}", file=sys.stderr)
        return 1
    return int(args.func(args, stream))


if __name__ == "__main__":
    raise SystemExit(main())
