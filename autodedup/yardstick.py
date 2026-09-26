"""The yardstick: one engine generation measured against the operator's own Browse merges (E299).

The operator merged 362 groups by hand in Browse before the engine existed, and every one of
them is a statement the engine can be measured against at no cost: these adverts are one
property. Migration 564 copied them into `autodedup.operator_merges`; the `labels` lane exports
them as `operator_merges.jsonl`; this module reads that file (or a JSON dump of the table)
beside ONE generation's artifacts — the cohort it was scored on, its `pairs.jsonl.gz` and its
`clusters.json` — and says, for every operator-ruled pair whose two adverts are both in the
cohort, what the engine did with it:

  * `together`            — the two adverts ended in one engine group (whatever the pair's own
                            zone: a band pair joined through a third advert counts);
  * `split_by_invariant`  — the pair itself scored `merge` and a cluster invariant kept the two
                            groups apart (`refused_by` names the invariant, `blocker` the member
                            pair a stated fact separates and the facts that do it);
  * `band` / `reject`     — the decision layer's zone (`stored: false` = scored below the store
                            floor, re-decided here to name the reason);
  * `veto`                — a decision-time veto (E61's unit designators);
  * `vetoed_at_blocking`  — a hard guard (E2-E5, `guards.pair_veto`) refused the pair before
                            any probe could spend a slot on it;
  * `never_paired`        — no probe produced the pair (`never_paired_cause`: no shared key, a
                            shared key the blocker exploded, or the fan-out cap).

Every miss carries both sides' stated price / area / disposition / floor and the fact names
`indistinguishable.distinguishing_facts` reads at promotion's bar (`facts`) and at the
cluster's (`cluster_facts`), plus whether `d43.ClusterRelation` lets the two share a group. A
never-paired or blocking-vetoed pair is also DECIDED here (`would`), so a blocking loss is told
apart from a pair the decision layer would have lost anyway.

Offline, $0, no database: the artifacts are the whole input, exactly as for `harness run`. The
operator's word is the target, not evidence — a pair the operator later separated by hand
(an explicit verdict other than `same`, or a permanent negative) is not measured, it is counted
apart as `contradicted`.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from autodedup.blocking import build_index, generate_pairs
from autodedup.d43 import ClusterRelation
from autodedup.dataset import Dataset, Listing
from autodedup.decide import decide_pair
from autodedup.features import FeatureContext, pair_features
from autodedup.fingerprint import Fingerprint, build_all
from autodedup.guards import pair_veto
from autodedup.hazard_context import ContextIndex
from autodedup.indistinguishable import CLUSTER, FEATURE_SLOTS, PROMOTE, distinguishing_facts
from autodedup.labels import (
    OPERATOR_POSITIVE_VERDICT,
    SOURCE_BROWSE_MERGE,
    PairKey,
    pair_key,
    parse_operator_merge,
)
from autodedup.model import LogisticModel
from autodedup.settings import Settings

YARDSTICK_VERSION: int = 1
YARDSTICK_STEM: str = "yardstick"

TOGETHER: str = "together"
SPLIT: str = "split_by_invariant"
BAND: str = "band"
REJECT: str = "reject"
VETO: str = "veto"
BLOCKING_VETO: str = "vetoed_at_blocking"
NEVER_PAIRED: str = "never_paired"
# The miss list's order: the earliest place the pipeline lost the pair first.
OUTCOMES: tuple[str, ...] = (TOGETHER, NEVER_PAIRED, BLOCKING_VETO, REJECT, VETO, BAND, SPLIT)
MISSES: tuple[str, ...] = tuple(outcome for outcome in OUTCOMES if outcome != TOGETHER)

CAUSE_NO_SHARED_KEY: str = "no_shared_key"
CAUSE_EXPLODED_KEY: str = "exploded_key"
CAUSE_FANOUT_CAP: str = "fanout_cap"
PRICE_CONFLICT: str = "price_conflict"

DEFAULT_TOP: int = 30

Feats = dict[str, tuple[float, bool]]


# --- the operator's pairs ------------------------------------------------------------------


@dataclass(slots=True)
class RuledPair:
    lo: int
    hi: int
    merge_group_id: str
    provenance: str = SOURCE_BROWSE_MERGE
    groups: list[str] = field(default_factory=list)

    @property
    def key(self) -> PairKey:
        return (self.lo, self.hi)


@dataclass(slots=True)
class OperatorPairs:
    """The pairs to measure, the group each came from, and what was set aside and why."""

    pairs: dict[PairKey, RuledPair] = field(default_factory=dict)
    groups: dict[str, list[PairKey]] = field(default_factory=dict)
    n_groups_read: int = 0
    n_groups_not_live: int = 0
    n_contradicted: int = 0
    n_label_rows_skipped: int = 0
    sources: list[str] = field(default_factory=list)

    def add(self, key: PairKey, group: str, provenance: str) -> None:
        self.groups.setdefault(group, [])
        if key not in self.groups[group]:
            self.groups[group].append(key)
        known = self.pairs.get(key)
        if known is None:
            self.pairs[key] = RuledPair(key[0], key[1], group, provenance, [group])
        elif group not in known.groups:
            known.groups.append(group)


def _payloads(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, Mapping):
        for name in ("groups", "operator_merges", "pairs"):
            if name in data:
                inner = data[name]
                if name == "pairs":
                    return [{"pairs": inner, "merge_group_id": data.get("merge_group_id")
                             or path.stem}]
                return list(inner or ())
        return [data]
    return list(data or ())


def load_operator_pairs(
    paths: Sequence[str | Path],
    *,
    label_sources: Iterable[str] = (SOURCE_BROWSE_MERGE,),
    include_not_live: bool = False,
) -> OperatorPairs:
    """Every pair the operator's merges still assert, from any mix of the accepted shapes.

    `operator_merges.jsonl` rows and table-dump rows are GROUPS (`labels.parse_operator_merge`,
    which applies 560's side rule and reads each pair's standing). `operator_labels.jsonl` rows
    are PAIRS: the `browse_merge` ones by default, grouped by their `merge_group_id` — any other
    `source` only when `label_sources` names it, because an implied or explicit label is not a
    Browse merge and the yardstick's population is the operator's merges."""
    wanted = set(label_sources)
    out = OperatorPairs(sources=[str(path) for path in paths])
    for raw in paths:
        path = Path(raw)
        if path.suffix != ".jsonl" and path.suffix != ".json":
            raise ValueError(f"{path}: expected a .jsonl or .json file")
        for payload in _payloads(path):
            if not isinstance(payload, Mapping):
                raise ValueError(f"{path}: every row must be a JSON object")
            if "listing_lo" in payload and "listing_hi" in payload:
                source = str(payload.get("source") or "")
                verdict = str(payload.get("verdict") or "")
                if source not in wanted or verdict != OPERATOR_POSITIVE_VERDICT:
                    out.n_label_rows_skipped += 1
                    continue
                key = pair_key(payload["listing_lo"], payload["listing_hi"])
                # The group a pair label speaks for: its merge, else the confirmed engine group
                # that implied it, else the pair alone.
                if payload.get("merge_group_id"):
                    group = str(payload["merge_group_id"])
                elif payload.get("cluster_key") is not None:
                    group = f"cluster:{int(payload['cluster_key'])}"
                else:
                    group = f"{source}:{key[0]}-{key[1]}"
                out.add(key, group, source)
                continue
            merge = parse_operator_merge(payload)
            out.n_groups_read += 1
            if merge.status != "live" and not include_not_live:
                out.n_groups_not_live += 1
                continue
            group = merge.merge_group_id or f"{path.stem}#{out.n_groups_read}"
            out.groups.setdefault(group, [])
            for pair in merge.pairs:
                if not pair.same:
                    out.n_contradicted += 1
                    continue
                out.add(pair.key, group, SOURCE_BROWSE_MERGE)
    return out


# --- the generation's artifacts ------------------------------------------------------------


def cluster_index(payload: Mapping[str, Any]) -> tuple[dict[int, int], dict[int, list[int]]]:
    clusters = {
        int(key): sorted(int(member) for member in members)
        for key, members in (payload.get("clusters") or {}).items()
    }
    member_of = {member: key for key, members in clusters.items() for member in members}
    return member_of, clusters


def _feats(row: Mapping[str, Any]) -> Feats:
    out: Feats = {}
    for name, entry in (row.get("feats") or {}).items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out[str(name)] = (float(entry[0] or 0.0), bool(entry[1]))
    return out


def read_stored(
    path: Path, wanted: set[PairKey], listings: set[int]
) -> tuple[dict[PairKey, dict[str, Any]], dict[PairKey, Feats]]:
    """One pass over `pairs.jsonl.gz`: the rows of the ruled pairs, and the D43 slots of every
    stored pair between two listings the cluster relation may be asked about."""
    rows: dict[PairKey, dict[str, Any]] = {}
    slots: dict[PairKey, Feats] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = pair_key(row["lo"], row["hi"])
            if key[0] not in listings or key[1] not in listings:
                continue
            feats = _feats(row)
            slots[key] = {name: feats[name] for name in FEATURE_SLOTS if name in feats}
            if key in wanted:
                row["_feats"] = feats
                rows[key] = row
    return rows, slots


# --- the engine, rebuilt only as far as a missing pair needs -------------------------------


class Engine:
    """Fingerprints, the blocking pass and the feature context, built once and lazily."""

    def __init__(self, dataset: Dataset, settings: Settings, model: LogisticModel) -> None:
        self.dataset = dataset
        self.settings = settings
        self.model = model
        self._fps: dict[int, Fingerprint] | None = None
        self._pairs: dict[PairKey, set[str]] | None = None
        self._index: Any = None
        self._ctx: FeatureContext | None = None
        self._hazard: ContextIndex | None = None

    @property
    def fps(self) -> dict[int, Fingerprint]:
        if self._fps is None:
            self._fps = build_all(self.dataset, self.settings)
        return self._fps

    @property
    def retrieved(self) -> dict[PairKey, set[str]]:
        if self._pairs is None:
            self._pairs, _ = generate_pairs(self.fps, self.settings)
        return self._pairs

    @property
    def index(self) -> Any:
        if self._index is None:
            self._index = build_index(self.fps.values(), self.settings)
        return self._index

    def _context(self) -> tuple[FeatureContext, ContextIndex]:
        if self._ctx is None:
            self._ctx = FeatureContext.build(self.fps, self.settings, self.dataset)
            self._ctx.index_attrs(self.fps, self.dataset.listings)
            self._hazard = ContextIndex.build(
                self.dataset.listings, self.dataset.images_by_listing)
        assert self._hazard is not None
        return self._ctx, self._hazard

    def blocking_veto(self, lo: int, hi: int) -> str | None:
        return pair_veto(self.fps[lo], self.fps[hi], self.settings)

    def never_paired_cause(self, lo: int, hi: int) -> tuple[str, list[str]]:
        """Why no probe produced the pair: the probes it shares a key on, and which failed."""
        index = self.index
        fa, fb = self.fps[lo], self.fps[hi]
        shared: set[tuple[str, Any]] = set()
        for first, second in ((fa, fb), (fb, fa)):
            posted = set(index.index_keys(second))
            shared |= {entry for entry in index.probe_keys(first) if entry in posted}
        probes = sorted({probe for probe, _ in shared})
        if not shared:
            return CAUSE_NO_SHARED_KEY, probes
        if all(key in index.exploded[probe] for probe, key in shared):
            return CAUSE_EXPLODED_KEY, probes
        return CAUSE_FANOUT_CAP, probes

    def decide(self, lo: int, hi: int, probes: Iterable[str]) -> tuple[Any, Feats]:
        ctx, hazard = self._context()
        fa, fb = self.fps[lo], self.fps[hi]
        la, lb = self.dataset.listings[lo], self.dataset.listings[hi]
        feats = pair_features(
            fa, fb, la, lb, self.dataset.images(lo), self.dataset.images(hi), ctx, self.settings)
        decision = decide_pair(fa, fb, la, lb, feats, sorted(probes), self.model, self.settings,
                               hazard)
        return decision, feats


# --- one pair ------------------------------------------------------------------------------


def side_facts(listing: Listing) -> dict[str, Any]:
    return {
        "id": listing.id,
        "source": listing.source,
        "source_id_native": listing.source_id_native,
        "block": listing.block,
        "category": f"{listing.category_main}/{listing.category_type}",
        "price": listing.price,
        "area_m2": listing.area_m2,
        "disposition": listing.disposition,
        "floor": listing.floor,
        "total_floors": listing.total_floors,
        "is_active": listing.is_active,
    }


def category_of(la: Listing, lb: Listing) -> str:
    if la.category_type == lb.category_type:
        return str(la.category_type or "(none)")
    return "|".join(sorted((str(la.category_type), str(lb.category_type))))


def source_pair_of(la: Listing, lb: Listing) -> str:
    return "|".join(sorted((la.source or "?", lb.source or "?")))


def _fact_names(la: Listing, lb: Listing, feats: Feats | None, settings: Settings,
                mode: str) -> list[str]:
    return sorted({fact.name for fact in distinguishing_facts(la, lb, feats, settings, mode)})


def refused_by(conflicts: Sequence[Mapping[str, Any]], lo: int, hi: int) -> list[str]:
    """The invariants that refused a union holding both adverts: the pair's own edge, or any
    refused edge whose would-be group contained the two."""
    out: set[str] = set()
    for row in conflicts:
        edge = pair_key(row.get("lo", 0), row.get("hi", 0))
        members = set(int(member) for member in (row.get("members") or ()))
        if edge == (lo, hi) or (lo in members and hi in members):
            out.add(str(row.get("invariant") or "?"))
    return sorted(out)


def measure_pair(
    ruled: RuledPair,
    *,
    engine: Engine,
    stored: Mapping[PairKey, dict[str, Any]],
    member_of: Mapping[int, int],
    clusters: Mapping[int, list[int]],
    conflicts: Sequence[Mapping[str, Any]],
    relation: ClusterRelation,
    slots: Mapping[PairKey, Feats],
) -> dict[str, Any]:
    lo, hi = ruled.key
    listings = engine.dataset.listings
    la, lb = listings[lo], listings[hi]
    settings = engine.settings
    cluster_lo, cluster_hi = member_of.get(lo), member_of.get(hi)
    together = cluster_lo is not None and cluster_lo == cluster_hi
    entry: dict[str, Any] = {
        "lo": lo,
        "hi": hi,
        "merge_group_id": ruled.merge_group_id,
        "groups": list(ruled.groups),
        "provenance": ruled.provenance,
        "category_type": category_of(la, lb),
        "source_pair": source_pair_of(la, lb),
        "cluster_lo": cluster_lo,
        "cluster_hi": cluster_hi,
        "together": together,
        "stored": False,
        "zone": None,
        "score": None,
        "certificate": None,
        "reason": None,
        "veto": None,
        "probes": [],
        "blocking_veto": None,
        "never_paired_cause": None,
        "would": None,
    }
    row = stored.get((lo, hi))
    feats: Feats | None
    if row is not None:
        feats = row.get("_feats") or {}
        entry.update({
            "stored": True, "zone": row.get("zone"), "score": row.get("score"),
            "certificate": row.get("certificate"), "reason": row.get("reason"),
            "veto": row.get("veto"), "probes": sorted(row.get("probes") or ()),
        })
        fate = {"merge": SPLIT, "band": BAND, "reject": REJECT, "veto": VETO}.get(
            str(row.get("zone")), REJECT)
    else:
        veto = engine.blocking_veto(lo, hi)
        probes = engine.retrieved.get((lo, hi))
        decision, feats = engine.decide(lo, hi, probes or ())
        verdict = {"zone": decision.zone, "score": decision.score,
                   "certificate": decision.certificate, "reason": decision.reason,
                   "veto": decision.veto}
        if veto is not None:
            entry["blocking_veto"] = veto
            entry["would"] = verdict
            fate = BLOCKING_VETO
        elif probes is None:
            cause, shared = engine.never_paired_cause(lo, hi)
            entry["never_paired_cause"] = cause
            entry["probes"] = shared
            entry["would"] = verdict
            fate = NEVER_PAIRED
        else:
            # Retrieved and scored, but below the store floor: the run kept no row, so the
            # decision is re-taken here to name its reason.
            entry.update({"zone": decision.zone, "score": decision.score,
                          "certificate": decision.certificate, "reason": decision.reason,
                          "veto": decision.veto, "probes": sorted(probes)})
            fate = {"merge": SPLIT, "band": BAND, "veto": VETO}.get(decision.zone, REJECT)
    entry["outcome"] = TOGETHER if together else fate
    entry["facts"] = _fact_names(la, lb, feats, settings, PROMOTE)
    entry["cluster_facts"] = _fact_names(la, lb, feats, settings, CLUSTER)
    entry["cluster_relation_ok"] = relation.ok(lo, hi)
    entry["refused_by"] = [] if together else refused_by(conflicts, lo, hi)
    entry["blocker"] = None
    if not together:
        union = set(clusters.get(cluster_lo, [lo]) if cluster_lo is not None else [lo])
        union |= set(clusters.get(cluster_hi, [hi]) if cluster_hi is not None else [hi])
        blocker = relation.violating_pair(sorted(union))
        if blocker is not None:
            a, b = listings.get(blocker[0]), listings.get(blocker[1])
            facts = (_fact_names(a, b, slots.get(blocker), settings, CLUSTER)
                     if a is not None and b is not None else [])
            # The relation's one refusal that is not a stated fact: E157's price limb.
            entry["blocker"] = {"lo": blocker[0], "hi": blocker[1],
                                "facts": facts or [PRICE_CONFLICT]}
    entry["sides"] = [side_facts(la), side_facts(lb)]
    return entry


# --- the whole yardstick -------------------------------------------------------------------


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _table(entries: Sequence[Mapping[str, Any]], field_name: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = out.setdefault(str(entry[field_name]),
                                {"n": 0, **{outcome: 0 for outcome in OUTCOMES}})
        bucket["n"] += 1
        bucket[entry["outcome"]] += 1
    for bucket in out.values():
        bucket["together_share"] = _share(bucket[TOGETHER], bucket["n"])
    return dict(sorted(out.items(), key=lambda item: (-item[1]["n"], item[0])))


def measure(
    dataset: Dataset,
    settings: Settings,
    model: LogisticModel,
    *,
    pairs_path: Path,
    clusters_payload: Mapping[str, Any],
    operator: OperatorPairs,
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    listings = dataset.listings
    member_of, clusters = cluster_index(clusters_payload)
    conflicts = list(clusters_payload.get("conflicts") or ())
    in_cohort = {key: pair for key, pair in operator.pairs.items()
                 if key[0] in listings and key[1] in listings}
    one_side = sum(1 for key in operator.pairs
                   if (key[0] in listings) != (key[1] in listings))
    touched: set[int] = set()
    for lo, hi in in_cohort:
        touched |= {lo, hi}
        for member in (lo, hi):
            if member in member_of:
                touched |= set(clusters[member_of[member]])
    stored, slots = read_stored(pairs_path, set(in_cohort), touched)
    engine = Engine(dataset, settings, model)
    relation = ClusterRelation(listings, slots, settings)
    entries = [
        measure_pair(pair, engine=engine, stored=stored, member_of=member_of,
                     clusters=clusters, conflicts=conflicts, relation=relation,
                     slots=slots)
        for _, pair in sorted(in_cohort.items())
    ]

    by_outcome = {outcome: sum(1 for e in entries if e["outcome"] == outcome)
                  for outcome in OUTCOMES}
    groups_in = {}
    for entry in entries:
        for group in entry["groups"]:
            state = groups_in.setdefault(group, [0, 0])
            state[0] += 1
            state[1] += 1 if entry["together"] else 0
    whole = sum(1 for n, ok in groups_in.values() if ok == n)
    none = sum(1 for n, ok in groups_in.values() if ok == 0)
    misses = sorted(
        (entry for entry in entries if entry["outcome"] != TOGETHER),
        key=lambda e: (OUTCOMES.index(e["outcome"]), e["merge_group_id"], e["lo"], e["hi"]),
    )
    n = len(entries)
    return {
        "yardstick_version": YARDSTICK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": dict(inputs or {}),
        "operator": {
            "sources": operator.sources,
            "groups_read": operator.n_groups_read,
            "groups_not_live_skipped": operator.n_groups_not_live,
            "groups_with_pairs": sum(1 for keys in operator.groups.values() if keys),
            "pairs_ruled": len(operator.pairs),
            "pairs_contradicted_skipped": operator.n_contradicted,
            "label_rows_skipped": operator.n_label_rows_skipped,
        },
        "coverage": {
            "pairs_in_cohort": n,
            "pairs_one_side_absent": one_side,
            "pairs_both_absent": len(operator.pairs) - n - one_side,
            "groups_in_cohort": len(groups_in),
        },
        "headline": {
            "pairs": n,
            "together": by_outcome[TOGETHER],
            "together_share": _share(by_outcome[TOGETHER], n),
            "missed": n - by_outcome[TOGETHER],
            "groups": len(groups_in),
            "groups_whole": whole,
            "groups_partial": len(groups_in) - whole - none,
            "groups_none": none,
            "groups_whole_share": _share(whole, len(groups_in)),
        },
        "by_outcome": {outcome: {"n": count, "share": _share(count, n)}
                       for outcome, count in by_outcome.items()},
        # Over the MISSES: a pair no probe produced can still end up together through a third
        # advert, and that is not a loss.
        "never_paired_causes": _histogram(
            e["never_paired_cause"] for e in misses if e["outcome"] == NEVER_PAIRED),
        "blocking_vetoes": _histogram(
            e["blocking_veto"] for e in misses if e["outcome"] == BLOCKING_VETO),
        "refused_by": _histogram(name for e in misses for name in e["refused_by"]),
        "by_category_type": _table(entries, "category_type"),
        "by_source_pair": _table(entries, "source_pair"),
        "misses": misses,
        "pairs": entries,
    }


def _histogram(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


# --- rendering -----------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100.0 * value:.1f}%"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _side_line(side: Mapping[str, Any]) -> str:
    return (f"{side['source']} {side['id']} {_num(side['price'])} Kc {_num(side['area_m2'])} m2 "
            f"{side['disposition'] or '-'} fl {_num(side['floor'])}/{_num(side['total_floors'])}")


def _group_label(group: Any) -> str:
    text = str(group)
    return text[:8] if len(text) == 36 and text.count("-") == 4 else text


def miss_reason(entry: Mapping[str, Any]) -> str:
    outcome = entry["outcome"]
    if outcome == NEVER_PAIRED:
        shared = ",".join(entry["probes"]) or "-"
        return f"{entry['never_paired_cause']} [{shared}]"
    if outcome == BLOCKING_VETO:
        return f"pair_veto:{entry['blocking_veto']}"
    if outcome == SPLIT:
        return "refused:" + (",".join(entry["refused_by"]) or "?")
    return str(entry.get("reason") or "-")


def render_lines(report: Mapping[str, Any], top: int = DEFAULT_TOP) -> list[str]:
    head = report["headline"]
    cov = report["coverage"]
    op = report["operator"]
    lines = [
        f"group rows read {op['groups_read']}  groups with pairs {op['groups_with_pairs']}"
        f"  ruled pairs {op['pairs_ruled']}  (contradicted, not measured: "
        f"{op['pairs_contradicted_skipped']})",
        f"in cohort: pairs {cov['pairs_in_cohort']}  one side absent "
        f"{cov['pairs_one_side_absent']}  both absent {cov['pairs_both_absent']}"
        f"  groups {cov['groups_in_cohort']}",
        f"together {head['together']} of {head['pairs']} ({_pct(head['together_share'])})"
        f"  groups whole {head['groups_whole']} / partial {head['groups_partial']}"
        f" / none {head['groups_none']}",
        "",
        f"{'outcome':<22}{'n':>6}{'share':>9}",
    ]
    for outcome, row in report["by_outcome"].items():
        lines.append(f"{outcome:<22}{row['n']:>6}{_pct(row['share']):>9}")
    for title, name in (("category_type", "by_category_type"), ("source pair", "by_source_pair")):
        lines += ["", f"{title:<30}{'n':>5}{'together':>10}"
                      "  misses never/blocking_veto/reject/veto/band/split"]
        for key, row in report[name].items():
            misses = "/".join(str(row[outcome]) for outcome in MISSES)
            lines.append(f"{key:<30}{row['n']:>5}{_pct(row['together_share']):>10}  {misses}")
    misses = report["misses"]
    lines += ["", f"MISS LIST (top {min(top, len(misses))} of {len(misses)})"]
    for entry in misses[:top]:
        a, b = entry["sides"]
        facts = ",".join(entry["facts"]) or "-"
        cluster_facts = ",".join(entry["cluster_facts"]) or "-"
        blocker = entry.get("blocker")
        blocked = (f" blocker {blocker['lo']}x{blocker['hi']}:{','.join(blocker['facts']) or '-'}"
                   if blocker else "")
        lines.append(
            f"- {entry['outcome']:<18} {entry['lo']} x {entry['hi']}  group "
            f"{_group_label(entry['merge_group_id'])}  {entry['category_type']}  "
            f"{miss_reason(entry)}")
        lines.append(f"    {_side_line(a)}  |  {_side_line(b)}")
        lines.append(f"    facts {facts}  cluster_facts {cluster_facts}"
                     f"  relation_ok {entry['cluster_relation_ok']}{blocked}")
    return lines


def write_report(report: Mapping[str, Any], out_dir: Path, top: int = DEFAULT_TOP
                 ) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{YARDSTICK_STEM}.json"
    text_path = out_dir / f"{YARDSTICK_STEM}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
                         encoding="utf-8")
    body = "\n".join(render_lines(report, top))
    text_path.write_text(f"# Yardstick\n\n```\n{body}\n```\n", encoding="utf-8")
    return json_path, text_path
