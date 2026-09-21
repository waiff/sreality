"""M198 — is one property ever recorded under two different TOWNS? Asked corpus-wide.

The trial cohort is SELECTED by `listing_location.obec_kod`, so a pair whose two adverts carry
different towns can only appear in it when BOTH towns happen to be in the cohort. The case the
question is about — a village advertised under its district town, a Praha quarter against a
neighbouring obec, a plain resolver error — puts the second advert outside the scope
altogether, and the trial can neither see it nor count it. Inside g7 the answer is therefore a
foregone 0 of 5,158 merges, which proves nothing about the corpus.

This probe asks it where it can be answered: over EVERY listing, restricted to pairs a
broker's own statement of identity certifies — E60's rare agency order code, the one evidence
in this programme that is not an inference from resemblance. Two adverts printing the same
code are two views of one order, and one order is one unit, wherever the two rows say they
are. So `obec_differ` over that population IS the false-veto rate a different-town veto would
pay, measured without a label, without a judge and without a model.

Read-only, one keyset-paged scan, nothing written anywhere. The regex prefilter is a
CANDIDATE filter, never the rule: the authority is `text_facts.reference_codes`, unchanged, so
the codes this probe groups on are the codes the engine certifies on.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from autodedup.census import _run, write_json
from autodedup.guards import pair_veto
from autodedup.text_facts import MAX_CODE_POPULATION, reference_codes

TIMEOUT_S_MAX: int = 900

# A code the body prints is preceded by a keyword (`text_facts._REFERENCE`) or wrapped in
# `[ID …]`. These stems are the SQL-side candidate filter for those keywords, accented and
# unaccented, deliberately wider than the regex: a row this misses is a row the probe never
# sees, so it errs long. `~*` is a seq scan either way — there is no index on `description`
# and ruling D8 forbids adding one.
CODE_HINT_RE: str = (
    r"(ev\.?\s*[cčČC]|[eE]viden[cč]|zak[aá]zk|[rR]eferen[cč]n|[cč][ií]slo\s+nab[ií]dky"
    r"|k[oó]d\s+nab[ií]dky|nab[ií]dka\s*[cč]\.|\[\s*[iI][dD]\s*[0-9])"
)

SCAN_SQL: str = """
SELECT
    l.id                AS id,
    l.source            AS source,
    l.category_main     AS category_main,
    l.category_type     AS category_type,
    l.area_m2           AS area_m2,
    l.disposition       AS disposition,
    l.floor             AS floor,
    l.description       AS description,
    ll.obec_kod         AS obec_kod,
    ll.obec_name        AS obec_name,
    ll.cast_obce_kod    AS cast_obce_kod,
    ll.okres_kod        AS okres_kod,
    ll.okres_name       AS okres_name,
    ll.kraj_kod         AS kraj_kod,
    ll.granularity      AS granularity
FROM listings l
LEFT JOIN listing_location ll ON ll.listing_id = l.id
WHERE l.id > %(after)s
  AND l.description IS NOT NULL
  AND l.description ~* %(hint)s
ORDER BY l.id
LIMIT %(batch)s
"""

# The denominator every applicability claim needs: how much of each portal carries a town at
# all (E12 — a missing town is never a mismatch, so a veto simply does not apply there).
COVERAGE_SQL: str = """
SELECT
    l.source                                                       AS source,
    count(*)                                                       AS n_listings,
    count(ll.listing_id)                                           AS n_location_rows,
    count(*) FILTER (WHERE ll.obec_kod IS NOT NULL)                AS n_with_obec,
    count(*) FILTER (WHERE ll.cast_obce_kod IS NOT NULL)           AS n_with_cast_obce,
    count(*) FILTER (WHERE ll.okres_kod IS NOT NULL)               AS n_with_okres,
    count(*) FILTER (WHERE l.description IS NOT NULL)              AS n_with_description
FROM listings l
LEFT JOIN listing_location ll ON ll.listing_id = l.id
GROUP BY 1
ORDER BY 2 DESC
"""

ARG_DEFAULTS: dict[str, Any] = {
    "batch": 5000,
    "max_rows": 0,          # 0 = no cap; otherwise stop after this many scanned rows
    "timeout_s": 300,
    "sample": 80,
    "coverage": 1,
}


@dataclass(frozen=True, slots=True)
class Side:
    """One advert, reduced to what this probe compares. Satisfies `guards.GuardSide`."""

    id: int
    source: str | None
    category_main: str | None
    category_type: str | None
    area_m2: float | None
    disposition: str | None
    floor: int | None
    obec_kod: int | None
    obec_name: str | None
    cast_obce_kod: int | None
    okres_kod: int | None
    okres_name: str | None
    kraj_kod: int | None
    granularity: str | None


def side_of(row: dict[str, Any]) -> Side:
    def _int(value: Any) -> int | None:
        return int(value) if value is not None else None

    def _num(value: Any) -> float | None:
        return float(value) if value is not None else None

    return Side(
        id=int(row["id"]),
        source=row.get("source"),
        category_main=row.get("category_main"),
        category_type=row.get("category_type"),
        area_m2=_num(row.get("area_m2")),
        disposition=row.get("disposition"),
        floor=_int(row.get("floor")),
        obec_kod=_int(row.get("obec_kod")),
        obec_name=row.get("obec_name"),
        cast_obce_kod=_int(row.get("cast_obce_kod")),
        okres_kod=_int(row.get("okres_kod")),
        okres_name=row.get("okres_name"),
        kraj_kod=_int(row.get("kraj_kod")),
        granularity=row.get("granularity"),
    )


def code_pairs(
    by_code: dict[str, list[Side]], *, max_population: int = MAX_CODE_POPULATION
) -> Iterator[tuple[str, Side, Side]]:
    """Every pair two adverts printing ONE rare code make — E60's population rail applied.

    A code carried by more than `max_population` listings is a per-broker sequence number or a
    template, not an order key, and certifies nothing; it is dropped WHOLE rather than sampled,
    exactly as `structural_truth` drops it."""
    for code in sorted(by_code):
        sides = by_code[code]
        if not 2 <= len(sides) <= max_population:
            continue
        ordered = sorted(sides, key=lambda s: s.id)
        for i, lo in enumerate(ordered):
            for hi in ordered[i + 1:]:
                yield code, lo, hi


def classify(lo: Side, hi: Side) -> dict[str, Any]:
    """What the two rows say about where they are — E12 throughout: unknown is never differ."""

    def rung(a: Any, b: Any) -> bool | None:
        return None if (a is None or b is None) else a != b

    return {
        "obec": rung(lo.obec_kod, hi.obec_kod),
        "cast_obce": rung(lo.cast_obce_kod, hi.cast_obce_kod),
        "okres": rung(lo.okres_kod, hi.okres_kod),
        "kraj": rung(lo.kraj_kod, hi.kraj_kod),
        "same_source": lo.source == hi.source,
        "source_pair": "|".join(sorted([str(lo.source), str(hi.source)])),
        "granularity_pair": "|".join(sorted([str(lo.granularity), str(hi.granularity)])),
        "guard_veto": pair_veto(lo, hi),
    }


def parse_args(args: dict[str, str]) -> dict[str, int]:
    unknown = sorted(set(args) - set(ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown town arg(s) {', '.join(unknown)}; known: {', '.join(sorted(ARG_DEFAULTS))}"
        )
    out = dict(ARG_DEFAULTS)
    for key, raw in args.items():
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"town arg {key} must be an integer, got {raw!r}") from None
    if not 1 <= int(out["batch"]) <= 20000:
        raise ValueError("town arg batch must be between 1 and 20000")
    if int(out["max_rows"]) < 0:
        raise ValueError("town arg max_rows must not be negative")
    if int(out["sample"]) < 0:
        raise ValueError("town arg sample must not be negative")
    if int(out["coverage"]) not in (0, 1):
        raise ValueError("town arg coverage must be 0 or 1")
    # Reaches SQL as text inside SET LOCAL, so it is clamped as well as coerced.
    if not 1 <= int(out["timeout_s"]) <= TIMEOUT_S_MAX:
        raise ValueError(f"town arg timeout_s must be between 1 and {TIMEOUT_S_MAX}")
    return out


def run_town(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Any
) -> dict[str, Any]:
    params = parse_args(args)
    timeout_ms = int(params["timeout_s"]) * 1000
    batch = int(params["batch"])
    max_rows = int(params["max_rows"])

    out_path = Path(out_dir) / "town.json"
    payload: dict[str, Any] = {"parameters": params}
    by_code: dict[str, list[Side]] = defaultdict(list)
    scanned = 0
    with_code = 0
    started = time.monotonic()

    conn = conn_factory()
    try:
        if params["coverage"]:
            try:
                payload["coverage"] = _run(conn, COVERAGE_SQL, {}, timeout_ms)
            except Exception as exc:  # noqa: BLE001 — a denominator is nice-to-have
                payload["coverage_error"] = f"{type(exc).__name__}: {exc}"
            write_json(out_path, payload)

        after = 0
        while True:
            rows = _run(
                conn,
                SCAN_SQL,
                {"after": after, "batch": batch, "hint": CODE_HINT_RE},
                timeout_ms,
            )
            if not rows:
                break
            scanned += len(rows)
            after = max(int(r["id"]) for r in rows)
            for row in rows:
                codes = reference_codes(row.get("description"))
                if not codes:
                    continue
                with_code += 1
                side = side_of(row)
                for code in codes:
                    by_code[code].append(side)
            print(f"[town] scanned={scanned} with_code={with_code} codes={len(by_code)}",
                  flush=True)
            if max_rows and scanned >= max_rows:
                break
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    tally: Counter = Counter()
    by_source_pair: dict[str, Counter] = defaultdict(Counter)
    by_granularity: dict[str, Counter] = defaultdict(Counter)
    by_town_pair: Counter = Counter()
    sample: list[dict[str, Any]] = []
    code_population = Counter({code: len(sides) for code, sides in by_code.items()})

    for code, lo, hi in code_pairs(by_code):
        verdict = classify(lo, hi)
        tally["pairs"] += 1
        if verdict["guard_veto"]:
            # The rule floor refuses this pair for a reason that has nothing to do with the
            # town (two categories, two areas, two floors). Counting it would let a broker's
            # code for one ORDER covering two units pose as a cross-town duplicate.
            tally[f"guard_veto:{verdict['guard_veto']}"] += 1
            continue
        tally["pairs_guard_clean"] += 1
        tally["same_source" if verdict["same_source"] else "cross_source"] += 1
        for rung in ("obec", "cast_obce", "okres", "kraj"):
            value = verdict[rung]
            if value is None:
                tally[f"{rung}:unknown"] += 1
            elif value:
                tally[f"{rung}:differ"] += 1
            else:
                tally[f"{rung}:same"] += 1
        if verdict["obec"]:
            by_source_pair[verdict["source_pair"]]["obec_differ"] += 1
            by_granularity[verdict["granularity_pair"]]["obec_differ"] += 1
            by_town_pair["|".join(sorted([str(lo.obec_name), str(hi.obec_name)]))] += 1
            if len(sample) < int(params["sample"]):
                sample.append({
                    "code": code, "lo": lo.id, "hi": hi.id,
                    "lo_source": lo.source, "hi_source": hi.source,
                    "lo_obec": [lo.obec_kod, lo.obec_name],
                    "hi_obec": [hi.obec_kod, hi.obec_name],
                    "lo_okres": [lo.okres_kod, lo.okres_name],
                    "hi_okres": [hi.okres_kod, hi.okres_name],
                    "lo_granularity": lo.granularity, "hi_granularity": hi.granularity,
                    "category": [lo.category_main, lo.category_type],
                    "area": [lo.area_m2, hi.area_m2],
                    "code_population": code_population[code],
                })
        elif verdict["obec"] is False:
            by_source_pair[verdict["source_pair"]]["obec_same"] += 1
            by_granularity[verdict["granularity_pair"]]["obec_same"] += 1

    payload.update({
        "scanned_rows": scanned,
        "rows_with_code": with_code,
        "distinct_codes": len(by_code),
        "codes_over_population_rail": sum(
            1 for n in code_population.values() if n > MAX_CODE_POPULATION
        ),
        "max_code_population": MAX_CODE_POPULATION,
        "tally": dict(sorted(tally.items())),
        "by_source_pair": {k: dict(v) for k, v in sorted(by_source_pair.items())},
        "by_granularity_pair": {k: dict(v) for k, v in sorted(by_granularity.items())},
        "cross_town_pairs_by_town_pair": dict(by_town_pair.most_common(80)),
        "cross_town_sample": sample,
        "elapsed_s": round(time.monotonic() - started, 1),
    })
    write_json(out_path, payload)
    return {k: v for k, v in payload.items() if k not in ("cross_town_sample", "coverage")}
