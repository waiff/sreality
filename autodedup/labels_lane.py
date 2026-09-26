"""`mode=labels` — the operator's own rulings as one reproducible artifact (W6).

The operator validated g4 by hand: groups confirmed or split in the admin UI, residual pairs
ruled one at a time or a card at a time. That testimony is the best label source this
programme has — it outranks gold, which outranks vision, which outranks text — and until now
it lived only in the database, where a fit could read it only through an ad-hoc query nobody
could re-run the same way twice. This lane writes it to a file instead, so `harness fit` and
`harness evaluate` consume LABELS FROM AN ARTIFACT exactly as they already consume judgements.

Three files, all JSONL, the pair files sorted by `(listing_lo, listing_hi)` so two runs over an unchanged
store produce byte-identical output:

  * `operator_labels.jsonl` — one row per operator pair label. `source` says how the operator
    said it: `explicit` is a verdict typed against that pair, `implied` is a member pair of a
    group whose latest cluster-grain verdict is `same`. Explicit always wins: a pair the
    operator separated by hand inside a group they otherwise confirmed is a separation, not a
    confirmation.
  * `must_not_link.jsonl` — the permanent operator negatives (`autodedup.must_not_link`,
    `source='operator'`), which are an INPUT to the next generation's clustering (E27/E33) and
    not merely a report on this one.
  * `operator_merges.jsonl` (E299, migration 564) — the operator's own Browse merges at GROUP
    grain, one row per merge group, sorted by `(merged_at, merge_group_id)`, provenance
    `browse_merge`: every member with its origin side, where it sits today (portal, category,
    property, and the export block it lives in, so a cohort covering the group can be
    dispatched), and every pair the group asserts with that pair's STANDING — the merge's own
    ruling, a verdict typed against the pair, a permanent negative, or none. Read from
    `autodedup.operator_merges`, never from `property_merge_events`. Empty, and flagged in the
    summary, while the store has no migration 564.

A Browse merge's pair rulings used to reach `operator_labels.jsonl` as `explicit` — 560 wrote
them as ordinary pair verdicts. Since 564 links each to its merge, they leave as `source =
"browse_merge"` with the `merge_group_id` appended to the row; nothing else about the row
changes, and a verdict typed by hand after the merge still leaves as `explicit`.

The cap on implied labels is the point of the `max_members` argument. A confirmed group of n
adverts asserts n*(n-1)/2 pairs, and the assertion gets weaker as n grows: an operator
scanning eight cards has not compared every one of the 28 pairs with the same care they gave a
single residual pair. The cap (12 by default, which is above every group this UI can build)
bounds the quadratic and, when a group somehow exceeds it, records the skip instead of
silently flooding the label set.

NOTHING IS WRITTEN. Every statement in `labels_sql` is a SELECT; the only row this mode
appends anywhere is its own `autodedup.iterations` ledger row, and `autodedup.lane` writes
that, not this module.

The operator's `note` and `reasons` are carried VERBATIM. They are prose the operator wrote
about two listings — not advert text, so E28's scrub does not apply — and a label whose reason
has been normalised away cannot be audited against the pair it was written for. They are also
DATA, never instructions: nothing downstream may act on their content.

`decided_by` is the ONE field that is not verbatim. The store holds a login — an e-mail
address — and this artifact is uploaded by a workflow in a PUBLIC repository, where anyone who
can see the run can download it. What a label needs is only whether two rows came from the
same person, so the address leaves as a salted digest (E28's own idiom, the one `export`
applies to brokers). The salt is a constant, not a secret: it must be stable across runs or
the same operator would get a different id in every artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from autodedup.cohort import PRAHA_OBEC_KOD
from autodedup.labels import (
    OPERATOR_MERGES_FILE,
    OPERATOR_MERGES_FORMAT,
    OPERATOR_POSITIVE_VERDICT,
    SOURCE_BROWSE_MERGE,
    MergePair,
    STANDING_BROWSE_MERGE,
    STANDING_EXPLICIT,
    STANDING_MUST_NOT_LINK,
    STANDING_UNRULED,
    merge_pairs,
)
from autodedup.labels_sql import (
    CLUSTER_MEMBERS_SQL,
    CLUSTER_VERDICTS_SQL,
    ENGINE_PAIRS_SQL,
    GENERATION_CLUSTERS_SQL,
    MUST_NOT_LINK_SQL,
    OPERATOR_MERGE_LINKS_SQL,
    OPERATOR_MERGE_MEMBERS_SQL,
    OPERATOR_MERGES_PRESENT_SQL,
    OPERATOR_MERGES_SQL,
    PAIR_VERDICTS_SQL,
    SAMPLE_RANK_SQL,
    STORE_PRESENT_SQL,
)

LABELS_FILE: str = "operator_labels.jsonl"
MUST_NOT_LINK_FILE: str = "must_not_link.jsonl"
# E910: the pairs whose NEWEST pair ruling is `same` — the must-links the real-time lane reads
# (`incremental_sql.RT_MUST_LINK_SQL`), for `harness run --must-link` and the replay.
MUST_LINK_FILE: str = "must_link.jsonl"
# The labels file's own shape version. Version 2 appended `merge_group_id` and the third
# `source` value `browse_merge` (E299); every version-1 field is unchanged.
LABELS_FORMAT: int = 2
# How many blocks the summary lists, ranked by the operator groups they would bring into a
# cohort — the answer to "which export makes the yardstick cover the operator's merges".
TOP_BLOCKS: int = 40

DEFAULT_GENERATION: str = "g4"
# Above the largest group the validation UI can build (eight members), so the cap bites only
# on a pathological cluster rather than on the operator's real work.
DEFAULT_MAX_MEMBERS: int = 12
MAX_MAX_MEMBERS: int = 64
DEFAULT_TIMEOUT_MS: int = 120_000
# The sample's NAME, not a fresh draw: `v1` is the seed the operator's validation session ran
# under, and the ranks in this artifact mean nothing except under the seed that produced them
# (api/routes/autodedup.py `_seed`, same default, same charset).
DEFAULT_SAMPLE_SEED: str = "v1"
CHUNK: int = 1_000

SOURCE_EXPLICIT: str = "explicit"
SOURCE_IMPLIED: str = "implied"
PROVENANCE_BROWSE_MERGE: str = SOURCE_BROWSE_MERGE

# `autodedup.verdicts.verdict` (migrations 528 + 532) -> the JUDGE's vocabulary, so an operator
# row and a judge row about the same pair can be read side by side without a lookup table in
# between. Four of the five values coincide with `judge.py`'s four-way; the operator's fifth,
# `same_project_different_unit`, has no judge equivalent and is carried through unchanged
# rather than flattened into `different_property`, which would throw away the project.
RELATION_OF: dict[str, str] = {
    "same": "same_property",
    "different": "different_property",
    "same_building_different_unit": "same_building_different_unit",
    "same_project_different_unit": "same_project_different_unit",
    "unsure": "insufficient_evidence",
}

CONFIRMING_CLUSTER_VERDICT: str = "same"

# Pseudonymisation salt for `decided_by` (see the module docstring). A constant, stable
# across runs, so "same operator" survives the digest.
DECIDED_BY_SALT: str = "autodedup-operator-v1"
DECIDED_BY_PREFIX: str = "op:"

ARG_KEYS: tuple[str, ...] = ("generation", "max_members", "timeout_ms", "sample_seed")

# A generation is a short slug the score lane stamps (`g1`, `g4`). Validated here because it
# reaches a query as a parameter and an operator string is not a vocabulary.
_GENERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
# The route's own closed charset for a seed (1-16 of a-z0-9), repeated here because the seed
# reaches `md5(key || seed)` as a parameter and an operator string is not a vocabulary.
_SEED_RE = re.compile(r"^[a-z0-9]{1,16}$")


@dataclass(slots=True)
class LabelArgs:
    generation: str
    max_members: int
    timeout_ms: int
    sample_seed: str


def parse_args(args: dict[str, str]) -> LabelArgs:
    unknown = sorted(set(args) - set(ARG_KEYS))
    if unknown:
        raise SystemExit(
            f"unknown labels arg(s) {', '.join(unknown)}; known: {', '.join(ARG_KEYS)}"
        )
    generation = (args.get("generation") or "").strip() or DEFAULT_GENERATION
    if not _GENERATION_RE.match(generation):
        raise SystemExit(f"generation must be a short slug like g4, got {generation!r}")
    max_members = _positive_int(args.get("max_members"), DEFAULT_MAX_MEMBERS, "max_members")
    if max_members < 2 or max_members > MAX_MAX_MEMBERS:
        raise SystemExit(f"max_members must be between 2 and {MAX_MAX_MEMBERS}")
    timeout_ms = _positive_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, "timeout_ms")
    sample_seed = (args.get("sample_seed") or "").strip() or DEFAULT_SAMPLE_SEED
    if not _SEED_RE.match(sample_seed):
        raise SystemExit(f"sample_seed must be 1-16 characters of a-z0-9, got {sample_seed!r}")
    return LabelArgs(generation=generation, max_members=max_members, timeout_ms=timeout_ms,
                     sample_seed=sample_seed)


def _positive_int(raw: str | None, default: int, name: str) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) <= 0:
        raise SystemExit(f"{name} must be a positive integer, got {raw!r}")
    return int(text)


# --- reading -------------------------------------------------------------------------------


def _rows_as_dicts(cur: Any) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _run(
    conn: Any, sql: str, params: dict[str, Any] | None, timeout_ms: int
) -> list[dict[str, Any]]:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params)
            return _rows_as_dicts(cur)


def store_ready(conn: Any) -> bool:
    if conn is None:
        return False
    try:
        rows = _run(conn, STORE_PRESENT_SQL, None, 10_000)
    except Exception:  # noqa: BLE001 — an unreadable probe reads as "not ready"
        return False
    return bool(rows and rows[0].get("present"))


def merges_ready(conn: Any) -> bool:
    """Migration 564's table AND its verdict column. Either missing reads as "not yet": the
    pair labels still export, the group file is written empty and the summary says why."""
    try:
        rows = _run(conn, OPERATOR_MERGES_PRESENT_SQL, None, 10_000)
    except Exception:  # noqa: BLE001 — an unreadable probe reads as "not ready"
        return False
    return bool(rows and rows[0].get("present"))


def batched(items: Sequence[Any], size: int = CHUNK) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


# --- shaping -------------------------------------------------------------------------------


def pair_key(lo: Any, hi: Any) -> tuple[int, int]:
    left, right = int(lo), int(hi)
    return (left, right) if left <= right else (right, left)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def decider(value: Any) -> str | None:
    """A login -> a stable opaque id. Only the ANSWER "same person?" survives, by design."""
    text = str(value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256((DECIDED_BY_SALT + text).encode("utf-8")).hexdigest()
    return DECIDED_BY_PREFIX + digest[:16]


def _reasons(value: Any) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value]


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def engine_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the stored pass thought of this pair, or None when it never stored the pair at all.

    An absent row is a FACT about the engine (the pair fell below `store_floor`, or blocking
    never produced it), so it stays null rather than being filled with a zero score that would
    read as "the engine scored it and said no"."""
    if row is None:
        return None
    return {
        "score": _float_or_none(row.get("score")),
        "zone": (str(row["zone"]) if row.get("zone") else None),
        "decision": (str(row["decision"]) if row.get("decision") else None),
        "guard_veto": (str(row["guard_veto"]) if row.get("guard_veto") else None),
        "cluster_key": _int_or_none(row.get("cluster_key")),
        "model_version": (str(row["model_version"]) if row.get("model_version") else None),
        "feature_version": _int_or_none(row.get("feature_version")),
        "families": _int_or_none(row.get("families")),
        "scored_at": _iso(row.get("decided_at")),
    }


def build_label_record(
    lo: int,
    hi: int,
    *,
    verdict: str,
    source: str,
    reasons: Sequence[str],
    note: str | None,
    decided_by: str | None,
    decided_at: Any,
    cluster_key: int | None,
    sample_rank: int | None,
    must_not_link: bool,
    engine: dict[str, Any] | None,
    merge_group_id: str | None = None,
) -> dict[str, Any]:
    return {
        "listing_lo": int(lo),
        "listing_hi": int(hi),
        "verdict": verdict,
        "relation": RELATION_OF.get(verdict, verdict),
        "source": source,
        "reasons": list(reasons),
        "note": note,
        "decided_by": decider(decided_by),
        "decided_at": _iso(decided_at),
        "cluster_key": cluster_key,
        # Where this label's group sat in the seeded sample order (implied labels only — an
        # explicit pair verdict was typed against a pair, not drawn from the group queue).
        "sample_rank": sample_rank,
        "must_not_link": bool(must_not_link),
        "engine": engine,
        # Appended in format 2 (E299): the Browse merge a `browse_merge` label was ruled by.
        "merge_group_id": merge_group_id,
    }


def member_pairs(members: Sequence[int]) -> Iterator[tuple[int, int]]:
    ordered = sorted({int(member) for member in members})
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            yield (left, right)


def implied_from_clusters(
    cluster_rows: Sequence[dict[str, Any]],
    *,
    max_members: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Member pairs of every group whose latest cluster-grain verdict is `same`.

    THE MEMBERS COME OFF THE VERDICT (`member_ids`, migration 538 / E58) — the set the operator
    was looking at when they ruled — and never off the current clustering. A re-clustering that
    grew or absorbed a group used to change what an old confirmation asserted, silently; the
    statement resolves a LEGACY row with no set to the members of its own generation, which is
    the most that can honestly be said about it.

    A group ruled anything else is NOT expanded into negatives: "this proposal is wrong" does
    not say which of its member pairs were the wrong ones, and manufacturing n*(n-1)/2
    negatives out of it would teach the model a claim the operator never made. The separations
    the operator DID make are explicit pair verdicts, and those are exported as themselves."""
    counts = {"clusters_seen": len(cluster_rows), "clusters_same": 0,
              "clusters_over_cap": 0, "clusters_without_members": 0, "pairs": 0}
    out: list[dict[str, Any]] = []
    for row in cluster_rows:
        if str(row.get("verdict") or "") != CONFIRMING_CLUSTER_VERDICT:
            continue
        counts["clusters_same"] += 1
        key = int(row["cluster_key"])
        members = [int(listing_id) for listing_id in (row.get("member_ids") or ())]
        if len(members) < 2:
            counts["clusters_without_members"] += 1
            continue
        if len(members) > max_members:
            counts["clusters_over_cap"] += 1
            continue
        for lo, hi in member_pairs(members):
            out.append({
                "lo": lo,
                "hi": hi,
                "verdict": CONFIRMING_CLUSTER_VERDICT,
                "reasons": _reasons(row.get("reasons")),
                "note": (row.get("note") or None),
                "decided_by": (row.get("decided_by") or None),
                "decided_at": row.get("decided_at"),
                "cluster_key": key,
            })
            counts["pairs"] += 1
    return out, counts


def export_block(obec_kod: Any, cast_obce_kod: Any) -> str | None:
    """Where an advert lives, spelled as the export lane's `blocks=` takes it: its Praha quarter
    (Praha is split by quarter everywhere in this programme), else its town; None unlocated."""
    if obec_kod is None:
        return None
    if int(obec_kod) == PRAHA_OBEC_KOD and cast_obce_kod is not None:
        return f"quarter:{int(cast_obce_kod)}"
    return f"town:{int(obec_kod)}"


def pair_standing(
    latest: dict[str, Any] | None,
    linked: dict[int, str],
    must_not_link: bool,
) -> tuple[str, str | None]:
    """How one pair of a merge group stands, off its LATEST pair ruling (any operator login).

    The merge's own `same` ruling is `browse_merge`; any other latest ruling — typed before
    the merge (560 then left the pair alone) or after it — is `explicit` with its verdict; with
    no ruling at all, a permanent negative is `must_not_link`, else the pair is `unruled`."""
    if latest is not None:
        verdict = str(latest.get("verdict") or "") or None
        verdict_id = latest.get("verdict_id")
        if (verdict == OPERATOR_POSITIVE_VERDICT and verdict_id is not None
                and int(verdict_id) in linked):
            return STANDING_BROWSE_MERGE, verdict
        return STANDING_EXPLICIT, verdict
    if must_not_link:
        return STANDING_MUST_NOT_LINK, None
    return STANDING_UNRULED, None


def build_merge_record(
    row: dict[str, Any],
    *,
    latest_by_pair: dict[tuple[int, int], dict[str, Any]],
    linked: dict[int, str],
    mnl_keys: set[tuple[int, int]],
    facts: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """One `operator_merges.jsonl` row: the group, its members as they sit today, its pairs."""
    ids = [int(member) for member in (row.get("member_ids") or ())]
    sides = [_int_or_none(side) for side in (row.get("member_sides") or [None] * len(ids))]
    places = [_int_or_none(prop) for prop in
              (row.get("member_property_ids") or [None] * len(ids))]
    members: list[dict[str, Any]] = []
    for listing_id, side, place in zip(ids, sides, places):
        fact = facts.get(listing_id) or {}
        members.append({
            "listing_id": listing_id,
            "side": side,
            "property_id_at_copy": place,
            "property_id": _int_or_none(fact.get("property_id")),
            "source": (str(fact["source"]) if fact.get("source") else None),
            "category_type": (str(fact["category_type"]) if fact.get("category_type") else None),
            "category_main": (str(fact["category_main"]) if fact.get("category_main") else None),
            "town": _int_or_none(fact.get("obec_kod")),
            "quarter": _int_or_none(fact.get("cast_obce_kod")),
            "block": export_block(fact.get("obec_kod"), fact.get("cast_obce_kod")),
        })
    pairs: list[dict[str, Any]] = []
    n_same = 0
    for lo, hi in merge_pairs(ids, sides, places):
        vetoed = (lo, hi) in mnl_keys
        standing, verdict = pair_standing(latest_by_pair.get((lo, hi)), linked, vetoed)
        if MergePair(lo=lo, hi=hi, standing=standing, verdict=verdict, must_not_link=vetoed).same:
            n_same += 1
        pairs.append({"listing_lo": lo, "listing_hi": hi, "standing": standing,
                      "verdict": verdict, "must_not_link": vetoed})
    now = {member["property_id"] for member in members if member["property_id"] is not None}
    return {
        "format": OPERATOR_MERGES_FORMAT,
        "merge_group_id": str(row["merge_group_id"]),
        "provenance": PROVENANCE_BROWSE_MERGE,
        "source": str(row.get("source") or "browse"),
        "status": str(row.get("status") or "live"),
        "status_note": (row.get("status_note") or None),
        "status_at": _iso(row.get("status_at")),
        "merged_at": _iso(row.get("merged_at")),
        "copied_at": _iso(row.get("copied_at")),
        "decided_by": decider(row.get("decided_by")),
        "survivor_property_id": _int_or_none(row.get("survivor_property_id")),
        "retired_property_ids": sorted(
            int(pid) for pid in (row.get("retired_property_ids") or ())),
        "members": members,
        "pairs": pairs,
        "n_members": len(members),
        "n_sides": len(set(sides)),
        "n_pairs": len(pairs),
        "n_pairs_at_copy": _int_or_none(row.get("n_pairs")),
        "n_pairs_same": n_same,
        # Whether the merge still stands where the members sit TODAY: one property holds every
        # located member. A group the operator (or anyone) took apart since reads False.
        "one_property_now": len(now) == 1 and all(
            member["property_id"] is not None for member in members),
        "blocks": sorted({member["block"] for member in members if member["block"]}),
        "category_types": sorted(
            {member["category_type"] for member in members if member["category_type"]}),
        "sources": sorted({member["source"] for member in members if member["source"]}),
    }


def merges_summary(
    records: Sequence[dict[str, Any]], *, present: bool, linked: int
) -> dict[str, Any]:
    """The group file in numbers — and the blocks an export would have to name to cover it."""
    block_groups: dict[str, int] = {}
    for record in records:
        for block in record["blocks"]:
            block_groups[block] = block_groups.get(block, 0) + 1
    ranked = sorted(block_groups.items(), key=lambda item: (-item[1], item[0]))
    standings: dict[str, int] = {}
    for record in records:
        for pair in record["pairs"]:
            standings[pair["standing"]] = standings.get(pair["standing"], 0) + 1
    return {
        "present": present,
        "groups": len(records),
        "by_status": _histogram(records, lambda r: r["status"]),
        "members": sum(r["n_members"] for r in records),
        "members_located": sum(
            1 for r in records for member in r["members"] if member["block"]),
        "pairs": sum(r["n_pairs"] for r in records),
        "pairs_same": sum(r["n_pairs_same"] for r in records),
        "pairs_by_standing": dict(sorted(standings.items())),
        "groups_one_property_now": sum(1 for r in records if r["one_property_now"]),
        "linked_rulings": linked,
        "blocks_ranked": [{"block": block, "groups": n} for block, n in ranked[:TOP_BLOCKS]],
        "n_blocks": len(ranked),
    }


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    """Written to `.part` and renamed: the workflow uploads `out/` with `if: always()`, so a
    half-written artifact must never appear under the name a fit will read."""
    part = path.with_name(path.name + ".part")
    written = 0
    with part.open("wt", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    part.replace(path)
    return written


# --- the mode ------------------------------------------------------------------------------


def run_labels(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    parsed = parse_args(args)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    conn = conn_factory()
    try:
        if not store_ready(conn):
            raise SystemExit(
                "schema autodedup is not present (migrations 528/532/533); nothing to export"
            )
        timeout = parsed.timeout_ms

        started = time.monotonic()
        generation_rows = _run(
            conn, GENERATION_CLUSTERS_SQL, {"generation": parsed.generation}, timeout
        )
        explicit_rows = _run(conn, PAIR_VERDICTS_SQL, None, timeout)
        cluster_rows = _run(
            conn, CLUSTER_VERDICTS_SQL, {"generation": parsed.generation}, timeout
        )
        keys = [int(row["cluster_key"]) for row in cluster_rows]
        # Read for the RUN SUMMARY only — how much of this generation's clustering the ruled
        # keys still describe. The labels themselves come off `member_ids`.
        members_by_cluster: dict[int, list[int]] = {}
        for chunk in batched(keys):
            for row in _run(conn, CLUSTER_MEMBERS_SQL, {
                "keys": chunk, "generation": parsed.generation,
            }, timeout):
                members_by_cluster.setdefault(int(row["cluster_key"]), []).append(
                    int(row["listing_id"])
                )
        # The sample order is a property of the GENERATION's cluster set, so it is read once
        # for every confirmed group, under the seed the artifact stamps.
        sample_rank_by_cluster: dict[int, int] = {}
        for chunk in batched(keys):
            for row in _run(conn, SAMPLE_RANK_SQL, {
                "keys": chunk, "generation": parsed.generation, "seed": parsed.sample_seed,
            }, timeout):
                sample_rank_by_cluster[int(row["cluster_key"])] = int(row["sample_rank"])
        mnl_rows = _run(conn, MUST_NOT_LINK_SQL, None, timeout)
        timings["read_s"] = round(time.monotonic() - started, 3)

        # E299: the operator's Browse merges, off the engine's own copy (migration 564).
        started = time.monotonic()
        merges_present = merges_ready(conn)
        merge_rows: list[dict[str, Any]] = []
        linked: dict[int, str] = {}
        member_facts: dict[int, dict[str, Any]] = {}
        if merges_present:
            merge_rows = _run(conn, OPERATOR_MERGES_SQL, None, timeout)
            linked = {
                int(row["verdict_id"]): str(row["operator_merge_group_id"])
                for row in _run(conn, OPERATOR_MERGE_LINKS_SQL, None, timeout)
            }
            member_ids = sorted({
                int(member) for row in merge_rows for member in (row.get("member_ids") or ())
            })
            for chunk in batched(member_ids):
                for row in _run(conn, OPERATOR_MERGE_MEMBERS_SQL, {"ids": chunk}, timeout):
                    member_facts[int(row["listing_id"])] = row
        timings["merges_read_s"] = round(time.monotonic() - started, 3)

        implied_rows, cluster_counts = implied_from_clusters(
            cluster_rows, max_members=parsed.max_members
        )
        # How many ruled keys the CURRENT clustering of this generation still holds — the one
        # number that says whether the export describes a pass the store can still show.
        cluster_counts["clusters_in_current_clustering"] = sum(
            1 for key in keys if members_by_cluster.get(key)
        )

        # Explicit wins, and it wins BEFORE the engine view is fetched so the artifact and the
        # pairs query cover exactly the same set.
        by_key: dict[tuple[int, int], dict[str, Any]] = {}
        for row in implied_rows:
            by_key[pair_key(row["lo"], row["hi"])] = {**row, "source": SOURCE_IMPLIED}
        n_implied_shadowed = 0
        latest_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
        for row in explicit_rows:
            key = pair_key(row["listing_lo"], row["listing_hi"])
            latest_by_pair[key] = row
            if key in by_key:
                n_implied_shadowed += 1
            verdict = str(row.get("verdict") or "")
            verdict_id = row.get("verdict_id")
            # A pair ruling a Browse merge wrote is the merge's, not a verdict typed against the
            # pair — only while it is still the pair's LATEST ruling and still says `same`.
            merge_group = (
                linked.get(int(verdict_id)) if verdict_id is not None
                and verdict == OPERATOR_POSITIVE_VERDICT else None
            )
            by_key[key] = {
                "lo": key[0],
                "hi": key[1],
                "verdict": verdict,
                "reasons": _reasons(row.get("reasons")),
                "note": (row.get("note") or None),
                "decided_by": (row.get("decided_by") or None),
                "decided_at": row.get("decided_at"),
                "cluster_key": None,
                "source": (PROVENANCE_BROWSE_MERGE if merge_group else SOURCE_EXPLICIT),
                "merge_group_id": merge_group,
            }

        keys_sorted = sorted(by_key)
        started = time.monotonic()
        engine_rows: dict[tuple[int, int], dict[str, Any]] = {}
        for chunk in batched(keys_sorted):
            params = {
                "los": [key[0] for key in chunk], "his": [key[1] for key in chunk],
                "generation": parsed.generation,
            }
            for row in _run(conn, ENGINE_PAIRS_SQL, params, timeout):
                engine_rows[pair_key(row["listing_lo"], row["listing_hi"])] = row
        timings["engine_view_s"] = round(time.monotonic() - started, 3)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    mnl_keys = {pair_key(row["listing_lo"], row["listing_hi"]) for row in mnl_rows}
    records = [
        build_label_record(
            key[0], key[1],
            verdict=by_key[key]["verdict"],
            source=by_key[key]["source"],
            reasons=by_key[key]["reasons"],
            note=by_key[key]["note"],
            decided_by=by_key[key]["decided_by"],
            decided_at=by_key[key]["decided_at"],
            cluster_key=by_key[key]["cluster_key"],
            sample_rank=sample_rank_by_cluster.get(by_key[key]["cluster_key"]),
            must_not_link=key in mnl_keys,
            engine=engine_view(engine_rows.get(key)),
            merge_group_id=by_key[key].get("merge_group_id"),
        )
        for key in keys_sorted
    ]
    merge_records = [
        build_merge_record(
            row, latest_by_pair=latest_by_pair, linked=linked, mnl_keys=mnl_keys,
            facts=member_facts,
        )
        for row in merge_rows
    ]
    mnl_records = [
        {
            "listing_lo": key[0],
            "listing_hi": key[1],
            "source": str(row.get("source") or "operator"),
            "reason": (row.get("reason") or None),
            "created_at": _iso(row.get("created_at")),
        }
        for row, key in sorted(
            ((row, pair_key(row["listing_lo"], row["listing_hi"])) for row in mnl_rows),
            key=lambda item: item[1],
        )
    ]

    ml_records = [{"listing_lo": key[0], "listing_hi": key[1]}
                  for key, row in sorted(latest_by_pair.items())
                  if str(row.get("verdict") or "") == OPERATOR_POSITIVE_VERDICT]
    labels_path = out_dir / LABELS_FILE
    mnl_path = out_dir / MUST_NOT_LINK_FILE
    ml_path = out_dir / MUST_LINK_FILE
    merges_path = out_dir / OPERATOR_MERGES_FILE
    _write_jsonl(labels_path, records)
    _write_jsonl(mnl_path, mnl_records)
    _write_jsonl(ml_path, ml_records)
    # Written on every path, empty without migration 564, so a consumer's path never moves.
    _write_jsonl(merges_path, merge_records)

    counts = {
        "labels": len(records),
        "explicit": sum(1 for r in records if r["source"] == SOURCE_EXPLICIT),
        "implied": sum(1 for r in records if r["source"] == SOURCE_IMPLIED),
        "browse_merge": sum(1 for r in records if r["source"] == PROVENANCE_BROWSE_MERGE),
        "operator_merge_groups": len(merge_records),
        "implied_shadowed_by_explicit": n_implied_shadowed,
        "engine_view": sum(1 for r in records if r["engine"] is not None),
        "sample_ranked": sum(1 for r in records if r["sample_rank"] is not None),
        "must_not_link": len(mnl_records),
        "must_link": len(ml_records),
        **{f"cluster_{name}": value for name, value in cluster_counts.items()},
    }
    generation_stats = generation_rows[0] if generation_rows else {}
    summary = {
        "artifacts": {"labels": str(labels_path), "must_not_link": str(mnl_path),
                      "must_link": str(ml_path), "operator_merges": str(merges_path)},
        "bytes": {
            "labels": labels_path.stat().st_size,
            "must_not_link": mnl_path.stat().st_size,
            "operator_merges": merges_path.stat().st_size,
        },
        "format": {"operator_labels": LABELS_FORMAT, "operator_merges": OPERATOR_MERGES_FORMAT},
        "counts": counts,
        "operator_merges": merges_summary(
            merge_records, present=merges_present, linked=len(linked)),
        "by_verdict": _histogram(records, lambda r: r["verdict"]),
        "by_source": _histogram(records, lambda r: r["source"]),
        "by_zone": _histogram(records, _zone_of),
        "by_zone_verdict": _nested(records, _zone_of, lambda r: r["verdict"]),
        "generation": {
            "generation": parsed.generation,
            "sample_seed": parsed.sample_seed,
            "n_clusters_sampled": len(sample_rank_by_cluster),
            "n_clusters": _int_or_none(generation_stats.get("n_clusters")),
            "max_size": _int_or_none(generation_stats.get("max_size")),
        },
        "params": {
            "generation": parsed.generation,
            "max_members": parsed.max_members,
            "timeout_ms": parsed.timeout_ms,
            "sample_seed": parsed.sample_seed,
        },
        "timings": timings,
    }
    return summary


def _zone_of(record: dict[str, Any]) -> str:
    """The zone the engine put this pair in, with its own name for "the engine never stored it"
    — which is a real and interesting bucket (the operator found a duplicate blocking missed),
    not a missing value."""
    engine = record.get("engine")
    if not engine:
        return "unstored"
    return str(engine.get("zone") or "unzoned")


def _histogram(
    records: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> dict[str, int]:
    out: dict[str, int] = {}
    for record in records:
        name = key(record)
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items()))


def _nested(
    records: Sequence[dict[str, Any]],
    outer: Callable[[dict[str, Any]], str],
    inner: Callable[[dict[str, Any]], str],
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = out.setdefault(outer(record), {})
        name = inner(record)
        bucket[name] = bucket.get(name, 0) + 1
    return {key: dict(sorted(value.items())) for key, value in sorted(out.items())}
