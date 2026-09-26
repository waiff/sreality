"""One fit substrate from several engine runs: each labelled pair read where its label was made.

`harness fit` and `harness evaluate` read ONE run directory — the stored rows of one pass over
one cohort export. The W7 refit's labels were made against several exports: the old judge draws
against the W1 trial cohort, the j2 verdicts and the operator's g13 rulings against the g13
trial export, and the Browse merges against a dozen other cohorts. This module writes the one
directory those commands read, and changes nothing about how they read it.

Each labelled pair's row comes from the HOME of the source whose label the store keeps — the
export that label was made against (DECISIONS.md 8: a ruling keeps the evidence the engine saw
at that moment) — and from the next run in that source's own fallback order when the home did
not block the pair. The kept label is decided exactly as `labels.label_pairs` decides it: the
highest tier by precedence, and inside a tier the LATER source, because a later file is how a
re-judge corrects an earlier one. A pair no run stores is counted against its source and left
out; a run scored under different settings than the others is refused, because two settings
rows are two feature definitions.

    python3 -m autodedup.refit_substrate <spec.json> --out <dir>

The spec names the runs and the sources in precedence-relevant order:
`{"runs": {name: run_dir}, "precedence": [...]?, "sources": [{"name", "judgements" |
"operator_labels", "homes": [run names], operator flags as `harness` spells them}]}`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from autodedup.labels import (
    OPERATOR_TIER,
    TIER_PRECEDENCE,
    WEIGHT_OPERATOR,
    Label,
    PairKey,
    effective_precedence,
    labels_by_tier,
    load_judgements,
    load_operator_labels,
    operator_label_pairs,
    pair_key,
)

PAIRS_FILE: str = "pairs.jsonl.gz"
RUN_FILE: str = "run.json"
REPORT_FILE: str = "substrate.json"
SUBSTRATE_KEY: str = "substrate"


@dataclass(slots=True)
class LabelSource:
    """One label file as the store reads it, with the runs its rows are looked up in."""

    name: str
    tiers: dict[str, dict[PairKey, Label]]
    homes: tuple[str, ...]
    path: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def labelled(self) -> set[PairKey]:
        return {key for tier in self.tiers.values() for key in tier}


def judgement_source(name: str, path: str | Path, homes: Sequence[str]) -> LabelSource:
    return LabelSource(name, labels_by_tier(load_judgements(path)), tuple(homes), str(path))


def operator_source(
    name: str,
    path: str | Path,
    homes: Sequence[str],
    *,
    include_implied: bool = True,
    implied_weight: float = WEIGHT_OPERATOR,
    include_browse_merge: bool = True,
    browse_merge_weight: float = WEIGHT_OPERATOR,
) -> LabelSource:
    pairs = operator_label_pairs(
        load_operator_labels(path),
        include_implied=include_implied, implied_weight=implied_weight,
        include_browse_merge=include_browse_merge, browse_merge_weight=browse_merge_weight,
    )
    notes = {"include_implied": include_implied, "implied_weight": implied_weight,
             "include_browse_merge": include_browse_merge,
             "browse_merge_weight": browse_merge_weight}
    return LabelSource(name, {OPERATOR_TIER: pairs}, tuple(homes), str(path), notes)


def kept_labels(
    sources: Sequence[LabelSource], precedence: Sequence[str] = TIER_PRECEDENCE
) -> dict[PairKey, tuple[Label, LabelSource]]:
    """The label the store keeps for every pair, and the source it came from.

    Identical to `labels.label_pairs` over the sources' files concatenated in this order (the
    test pins it): the first tier of the effective precedence that labels the pair wins, and
    inside it the last source to label it. An abstention wins like any label — the harness
    then drops the pair, which is the harness's decision, not this module's."""
    ranked = effective_precedence(
        [tier for source in sources for tier in source.tiers], precedence
    )
    out: dict[PairKey, tuple[Label, LabelSource]] = {}
    for tier in reversed(ranked):
        for source in sources:
            for key, label in source.tiers.get(tier, {}).items():
                out[key] = (label, source)
    return out


def _settings_of(run_dir: Path) -> dict[str, Any]:
    payload = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError(f"{run_dir / RUN_FILE} records no settings")
    return settings


def _digest(settings: Mapping[str, Any]) -> str:
    text = json.dumps(settings, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stored_rows(run_dir: Path, wanted: set[PairKey]) -> dict[PairKey, dict[str, Any]]:
    rows: dict[PairKey, dict[str, Any]] = {}
    with gzip.open(run_dir / PAIRS_FILE, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = pair_key(row["lo"], row["hi"])
            if key in wanted:
                rows[key] = row
    return rows


def assemble(
    runs: Mapping[str, Path],
    sources: Sequence[LabelSource],
    out_dir: Path,
    precedence: Sequence[str] = TIER_PRECEDENCE,
) -> dict[str, Any]:
    """Write `out_dir/pairs.jsonl.gz` + `run.json` + `substrate.json`; return the report."""
    if sum(1 for source in sources if OPERATOR_TIER in source.tiers) > 1:
        # Inside the operator tier explicit > browse_merge > implied is decided over the POOLED
        # rows (`operator_label_pairs`), never by file order, so the tier comes as one source.
        raise ValueError("pass the operator tier as ONE source (pool its files first)")
    unknown = sorted({home for source in sources for home in source.homes} - set(runs))
    if unknown:
        raise ValueError(f"homes name runs the spec does not: {unknown}")
    digests = {name: _digest(_settings_of(Path(path))) for name, path in runs.items()}
    if len(set(digests.values())) > 1:
        raise ValueError(f"the runs were scored under different settings: {digests}")
    kept = kept_labels(sources, precedence)
    classed = {key: pair for key, pair in kept.items() if pair[0].y is not None}
    wanted_by_run: dict[str, set[PairKey]] = {name: set() for name in runs}
    for key, (_, source) in classed.items():
        for home in source.homes:
            wanted_by_run[home].add(key)
    found: dict[str, dict[PairKey, dict[str, Any]]] = {
        name: _stored_rows(Path(runs[name]), wanted) if wanted else {}
        for name, wanted in wanted_by_run.items()
    }

    per_source: dict[str, dict[str, Any]] = {
        source.name: {
            "path": source.path, "homes": list(source.homes), "labelled": len(source.labelled()),
            "kept": 0, "kept_abstaining": 0, "at_home": 0, "fallback": {}, "missing": 0,
            **({"flags": source.notes} if source.notes else {}),
        }
        for source in sources
    }
    for _, source in kept.values():
        per_source[source.name]["kept"] += 1
    for label, source in kept.values():
        if label.y is None:
            per_source[source.name]["kept_abstaining"] += 1
    taken: dict[str, int] = {name: 0 for name in runs}
    rows: list[dict[str, Any]] = []
    missing: list[list[Any]] = []
    for key in sorted(classed):
        _, source = classed[key]
        cell = per_source[source.name]
        home = next((name for name in source.homes if key in found[name]), None)
        if home is None:
            cell["missing"] += 1
            missing.append([key[0], key[1], source.name])
            continue
        if home == source.homes[0]:
            cell["at_home"] += 1
        else:
            cell["fallback"][home] = cell["fallback"].get(home, 0) + 1
        taken[home] += 1
        rows.append({**found[home][key], SUBSTRATE_KEY: home})

    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_dir / PAIRS_FILE, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "runs": {name: {"path": str(runs[name]), "rows_taken": taken[name]} for name in runs},
        "settings_sha256": next(iter(digests.values()), None),
        "precedence": effective_precedence(
            [tier for source in sources for tier in source.tiers], precedence
        ),
        "sources": per_source,
        "rows": len(rows),
        "kept_with_class": len(classed),
        "kept_abstaining": len(kept) - len(classed),
        "missing": len(missing),
        "missing_pairs": missing,
    }
    first = Path(next(iter(runs.values())))
    (out_dir / RUN_FILE).write_text(json.dumps({
        "settings": _settings_of(first),
        "substrate": {key: value for key, value in report.items() if key != "missing_pairs"},
    }, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (out_dir / REPORT_FILE).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return report


def sources_from_spec(spec: Mapping[str, Any]) -> list[LabelSource]:
    out: list[LabelSource] = []
    for item in spec.get("sources") or ():
        homes = tuple(item.get("homes") or ())
        if not homes:
            raise ValueError(f"source {item.get('name')!r} names no home run")
        if item.get("judgements"):
            out.append(judgement_source(item["name"], item["judgements"], homes))
        elif item.get("operator_labels"):
            implied = item.get("implied_weight")
            merge = item.get("browse_merge_weight")
            out.append(operator_source(
                item["name"], item["operator_labels"], homes,
                include_implied=not item.get("exclude_implied", False),
                implied_weight=WEIGHT_OPERATOR if implied is None else float(implied),
                include_browse_merge=not item.get("exclude_browse_merge", False),
                browse_merge_weight=WEIGHT_OPERATOR if merge is None else float(merge),
            ))
        else:
            raise ValueError(f"source {item.get('name')!r} names no label file")
    return out


def main(argv: Iterable[str] | None = None, out: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.refit_substrate", description=__doc__)
    parser.add_argument("spec", help="the substrate spec JSON (runs + ordered label sources)")
    parser.add_argument("--out", required=True, help="the run directory to write")
    args = parser.parse_args(list(argv) if argv is not None else None)
    sink = out if out is not None else sys.stdout
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    runs = {str(name): Path(path) for name, path in (spec.get("runs") or {}).items()}
    try:
        report = assemble(runs, sources_from_spec(spec), Path(args.out),
                          tuple(spec.get("precedence") or TIER_PRECEDENCE))
    except (ValueError, FileNotFoundError) as exc:
        print(f"substrate failed: {exc}", file=sys.stderr)
        return 1
    print(f"substrate {args.out}: {report['rows']} rows from {len(runs)} runs, "
          f"{report['missing']} labelled pairs no run stores", file=sink)
    for name, cell in report["sources"].items():
        print(f"  {name}: kept {cell['kept']} (abstain {cell['kept_abstaining']}) at home "
              f"{cell['at_home']} fallback {sum(cell['fallback'].values())} missing "
              f"{cell['missing']}", file=sink)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
