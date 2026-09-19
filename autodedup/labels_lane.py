"""`mode=labels` — the operator's own rulings as one reproducible artifact (W6).

The operator validated g4 by hand: groups confirmed or split in the admin UI, residual pairs
ruled one at a time or a card at a time. That testimony is the best label source this
programme has — it outranks gold, which outranks vision, which outranks text — and until now
it lived only in the database, where a fit could read it only through an ad-hoc query nobody
could re-run the same way twice. This lane writes it to a file instead, so `harness fit` and
`harness evaluate` consume LABELS FROM AN ARTIFACT exactly as they already consume judgements.

Two files, both JSONL, both sorted by `(listing_lo, listing_hi)` so two runs over an unchanged
store produce byte-identical output:

  * `operator_labels.jsonl` — one row per operator pair label. `source` says how the operator
    said it: `explicit` is a verdict typed against that pair, `implied` is a member pair of a
    group whose latest cluster-grain verdict is `same`. Explicit always wins: a pair the
    operator separated by hand inside a group they otherwise confirmed is a separation, not a
    confirmation.
  * `must_not_link.jsonl` — the permanent operator negatives (`autodedup.must_not_link`,
    `source='operator'`), which are an INPUT to the next generation's clustering (E27/E33) and
    not merely a report on this one.

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

from autodedup.labels_sql import (
    CLUSTER_MEMBERS_SQL,
    CLUSTER_VERDICTS_SQL,
    ENGINE_PAIRS_SQL,
    GENERATION_CLUSTERS_SQL,
    MUST_NOT_LINK_SQL,
    PAIR_VERDICTS_SQL,
    STORE_PRESENT_SQL,
)

LABELS_FILE: str = "operator_labels.jsonl"
MUST_NOT_LINK_FILE: str = "must_not_link.jsonl"

DEFAULT_GENERATION: str = "g4"
# Above the largest group the validation UI can build (eight members), so the cap bites only
# on a pathological cluster rather than on the operator's real work.
DEFAULT_MAX_MEMBERS: int = 12
MAX_MAX_MEMBERS: int = 64
DEFAULT_TIMEOUT_MS: int = 120_000
CHUNK: int = 1_000

SOURCE_EXPLICIT: str = "explicit"
SOURCE_IMPLIED: str = "implied"

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

ARG_KEYS: tuple[str, ...] = ("generation", "max_members", "timeout_ms")

# A generation is a short slug the score lane stamps (`g1`, `g4`). Validated here because it
# reaches a query as a parameter and an operator string is not a vocabulary.
_GENERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")


@dataclass(slots=True)
class LabelArgs:
    generation: str
    max_members: int
    timeout_ms: int


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
    return LabelArgs(generation=generation, max_members=max_members, timeout_ms=timeout_ms)


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
    must_not_link: bool,
    engine: dict[str, Any] | None,
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
        "must_not_link": bool(must_not_link),
        "engine": engine,
    }


def member_pairs(members: Sequence[int]) -> Iterator[tuple[int, int]]:
    ordered = sorted({int(member) for member in members})
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            yield (left, right)


def implied_from_clusters(
    cluster_rows: Sequence[dict[str, Any]],
    members_by_cluster: dict[int, list[int]],
    *,
    max_members: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Member pairs of every group whose latest cluster-grain verdict is `same`.

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
        members = members_by_cluster.get(key) or []
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
        members_by_cluster: dict[int, list[int]] = {}
        for chunk in batched(keys):
            for row in _run(conn, CLUSTER_MEMBERS_SQL, {"keys": chunk}, timeout):
                members_by_cluster.setdefault(int(row["cluster_key"]), []).append(
                    int(row["listing_id"])
                )
        mnl_rows = _run(conn, MUST_NOT_LINK_SQL, None, timeout)
        timings["read_s"] = round(time.monotonic() - started, 3)

        implied_rows, cluster_counts = implied_from_clusters(
            cluster_rows, members_by_cluster, max_members=parsed.max_members
        )

        # Explicit wins, and it wins BEFORE the engine view is fetched so the artifact and the
        # pairs query cover exactly the same set.
        by_key: dict[tuple[int, int], dict[str, Any]] = {}
        for row in implied_rows:
            by_key[pair_key(row["lo"], row["hi"])] = {**row, "source": SOURCE_IMPLIED}
        n_implied_shadowed = 0
        for row in explicit_rows:
            key = pair_key(row["listing_lo"], row["listing_hi"])
            if key in by_key:
                n_implied_shadowed += 1
            by_key[key] = {
                "lo": key[0],
                "hi": key[1],
                "verdict": str(row.get("verdict") or ""),
                "reasons": _reasons(row.get("reasons")),
                "note": (row.get("note") or None),
                "decided_by": (row.get("decided_by") or None),
                "decided_at": row.get("decided_at"),
                "cluster_key": None,
                "source": SOURCE_EXPLICIT,
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
            must_not_link=key in mnl_keys,
            engine=engine_view(engine_rows.get(key)),
        )
        for key in keys_sorted
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

    labels_path = out_dir / LABELS_FILE
    mnl_path = out_dir / MUST_NOT_LINK_FILE
    _write_jsonl(labels_path, records)
    _write_jsonl(mnl_path, mnl_records)

    counts = {
        "labels": len(records),
        "explicit": sum(1 for r in records if r["source"] == SOURCE_EXPLICIT),
        "implied": sum(1 for r in records if r["source"] == SOURCE_IMPLIED),
        "implied_shadowed_by_explicit": n_implied_shadowed,
        "engine_view": sum(1 for r in records if r["engine"] is not None),
        "must_not_link": len(mnl_records),
        **{f"cluster_{name}": value for name, value in cluster_counts.items()},
    }
    generation_stats = generation_rows[0] if generation_rows else {}
    summary = {
        "artifacts": {"labels": str(labels_path), "must_not_link": str(mnl_path)},
        "bytes": {
            "labels": labels_path.stat().st_size,
            "must_not_link": mnl_path.stat().st_size,
        },
        "counts": counts,
        "by_verdict": _histogram(records, lambda r: r["verdict"]),
        "by_source": _histogram(records, lambda r: r["source"]),
        "by_zone": _histogram(records, _zone_of),
        "by_zone_verdict": _nested(records, _zone_of, lambda r: r["verdict"]),
        "generation": {
            "generation": parsed.generation,
            "n_clusters": _int_or_none(generation_stats.get("n_clusters")),
            "max_size": _int_or_none(generation_stats.get("max_size")),
        },
        "params": {
            "generation": parsed.generation,
            "max_members": parsed.max_members,
            "timeout_ms": parsed.timeout_ms,
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
