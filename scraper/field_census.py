"""The field-capture instrument: what each portal PUBLISHES, and what we STORED.

Two measurements, one module, so the re-bless and the live health check can never
measure different things:

  * the **key census** — the keys a portal's stored payload actually carries, with the
    share of sampled rows carrying each and (for low-cardinality keys) its values. This
    is the evidence a parser is read against: `remax`'s parser reads `balkon` / `lodzie`
    and the portal has never emitted either, which is why `has_balcony` is 0/0 on every
    active remax row while the parser's own hand-authored fixtures stay green.
  * the **fill + validity matrix** — per (source, field), how many ACTIVE rows carry a
    value, and for the fields whose canon `toolkit.filter_registry` defines, how many
    carry a value OUTSIDE it. Fill alone cannot see a wrong value (the lesson the per-m2
    checks were built on); the value histogram is what makes validity measurable.

The two read different cohorts, and the difference is the point. The census is EVIDENCE
about a payload's key space, so it samples the newest rows — what the portal ships today.
The matrix is an ALARM compared against a blessed baseline, so it reads EVERY active row:
a sampled cohort cannot be compared to itself a week later. The newest-N slice rotates
with whatever a portal's walk happened to cover (mmreality's newest 1,000 went from a
balanced mix to 73% commercial rentals inside two days, moving `cellar` fill 40% -> 8%
with no parser touched), and no threshold can separate that from a regression.

Both halves are aggregates: the matrix never expands a row into (field, value) pairs over
the whole stock, which costs ~32 s against ~12 s for the aggregate form.

Neither measurement reads `listing_description_enrichments`: that ledger still records
cells a later detail re-fetch wiped as filled.

Re-bless a reviewed change:  python -m scraper.field_census --bless
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping, NamedTuple, Sequence

from scraper.db import LISTING_COLUMNS, connect
from toolkit import filter_registry

_ROOT = Path(__file__).resolve().parent.parent
CENSUS_DIR = _ROOT / "data" / "field_capture" / "census"
BASELINE_PATH = _ROOT / "data" / "field_capture" / "fill_baseline.json"

BLESS_COMMAND = "python -m scraper.field_census --bless"

# How many of the newest ACTIVE rows per source the CENSUS reads (the matrix reads all of
# them). 1,000 keeps the census query at ~1 s on the widest payload (sreality) and is deep
# enough for a key on 1% of pages to appear ten times.
SAMPLE_ROWS = 1000
# Values are recorded only for a key/field with at most this many distinct values in the
# sample, and then ALL of them are — a partial list of a vocabulary is not evidence about
# the vocabulary. Above the cap the key is a number, an id or prose: no values, and the
# recorded `distinct` count says so. The cap doubles as the bound on any one cell's rows.
MAX_DISTINCT_FOR_VALUES = 12
# A key carried by fewer than this share of sampled rows folds into a counted `rare`
# bucket: a census is evidence for a reviewer, not a dump of every one-off key.
RARE_KEY_PCT = 1.0
# A recorded value is cut to this many characters. Long enough for every enum label any
# portal ships, short enough that a prose cell under the distinct cap cannot bloat the
# file. A value AT this length may be a prefix, so a consumer replaying values back
# through a parser must skip it (tests/scraper/attribute_probes.py does).
VALUE_TRUNCATE_CHARS = 60

SAMPLE_PREDICATE = (
    f"the newest {SAMPLE_ROWS} rows per source with is_active, by first_seen_at desc"
)
MATRIX_COHORT = "every is_active row of every enabled scraper portal"

# jsonb has a null of its own, and it is the defect shape idnes shows: the key is present
# and its value is null, so a key-presence census and a column-fill matrix disagree. Give
# it a name rather than dropping it.
JSON_NULL = "(json null)"

# The canon is interpolated into the matrix SQL as literals, so a registry label that is
# not a bare one is refused at import rather than becoming broken SQL at 03:30. A space
# is bare (`price_unit` is `za nemovitost`); a quote or a backslash is what would break
# the literal.
_SQL_SAFE_VALUE = re.compile(r"[A-Za-z0-9_+ ]+")

# `description` is the substrate of the post-publication text lane, not a portal-stated
# attribute; `published_at` and `source_url` are identity, kept out of every content hash.
_NON_ATTRIBUTE = frozenset({"description", "published_at", "source_url"})
ATTRIBUTE_FIELDS: tuple[str, ...] = tuple(
    c for c in LISTING_COLUMNS if c not in _NON_ATTRIBUTE
)

# The nine listing portals, from the per-portal config row (CLAUDE.md rule 21) rather than
# a tenth list that can drift. `kind = 'scraper'` stays the posture filter: portals.kind still
# admits 'parser', though migration 650 deleted the last such row.
PORTAL_SOURCES_SQL = """
select source from portals where kind = 'scraper' and is_enabled order by source
"""

# ONE substrate rule, no per-portal branch: the params table when the portal stores an HTML
# table (idnes, ceskereality, realitymix, remax, maxima), the native payload otherwise
# (sreality's flattened estate, bezrealitky's GraphQL fields, mmreality's object, bazos's
# title block), plus mmreality's accessory NAMES — which live two levels down and are the
# only place that portal states a balcony — and the presence of the description column.
FIELD_CENSUS_SQL = """
with sampled as (
    select l.raw_json as raw, l.description as descr
      from listings l
     where l.source = %(source)s
       and l.is_active
       and l.raw_json is not null
     order by l.first_seen_at desc
     limit %(sample)s
),
substrate as (
    select coalesce(s.raw -> 'params', s.raw)
           || coalesce((select jsonb_object_agg(a ->> 'name', to_jsonb('present'::text))
                          from jsonb_array_elements(
                                 case when jsonb_typeof(s.raw -> 'accessoryGroups') = 'array'
                                      then s.raw -> 'accessoryGroups' else '[]'::jsonb end) g,
                               jsonb_array_elements(
                                 case when jsonb_typeof(g -> 'accessories') = 'array'
                                      then g -> 'accessories' else '[]'::jsonb end) a
                         where a ? 'name'), '{}'::jsonb)
           || jsonb_strip_nulls(jsonb_build_object('listings.description',
                case when length(btrim(coalesce(s.descr, ''))) > 0
                     then to_jsonb('present'::text) end)) as obj
      from sampled s
),
pairs as (
    select kv.key as key, left(kv.value, 60) as value
      from substrate,
           lateral jsonb_each_text(case when jsonb_typeof(substrate.obj) = 'object'
                                        then substrate.obj else '{}'::jsonb end) kv
),
per_value as (
    select key, value, count(*)::bigint as n from pairs group by 1, 2
),
keys as (
    select key, sum(n)::bigint as key_rows, count(*)::bigint as key_distinct
      from per_value group by 1
)
select (select count(*) from sampled) as n_sampled,
       k.key, k.key_rows, k.key_distinct, v.value, v.n
  from keys k
  left join per_value v
    on v.key = k.key and k.key_distinct <= %(max_distinct)s
 order by k.key_rows desc, k.key, v.n desc nulls last, v.value
"""
assert f"left(kv.value, {VALUE_TRUNCATE_CHARS})" in FIELD_CENSUS_SQL, (
    "VALUE_TRUNCATE_CHARS drifted from the census SQL"
)


def _matrix_sql() -> str:
    """The matrix SQL, one aggregate pass over the active stock, generated from
    `ATTRIBUTE_FIELDS` and the canon.

    The field list is the scraper's own column contract, never a second hard-coded list —
    a second list is what let `data_quality_by_source` drift out of step with what the
    parsers write. `tests.sql_corpus` resolves a composed `*_SQL` constant by import, so
    this stays inside the placeholder guard and the PREPARE sweep.

    Every number is an aggregate over the whole stock, so nothing here is an estimate:
    the off-canon count is over every row, and a canon-bearing field also gets a count
    per canonical value (which is where the statutory `energy_rating` 'G' share and the
    live `condition` spellings come from) plus the NAMES of whatever sits outside the
    canon. `x::text = 'true'` is how a boolean is recognised without a second type table:
    it is false for every other column type, so a field is boolean exactly where the two
    counts add up to the filled count.
    """
    aggs: list[str] = []
    cells: list[str] = []
    for i, field in enumerate(ATTRIBUTE_FIELDS):
        aggs.append(f"count(l.{field}) as n{i}")
        aggs.append(f"count(*) filter (where l.{field}::text = 'true') as t{i}")
        aggs.append(f"count(*) filter (where l.{field}::text = 'false') as x{i}")
        values = canonical_values(field)
        if values is None:
            cells.append(
                f"('{field}', a.n{i}, null::bigint, null::bigint, a.t{i}, a.x{i}, "
                "null::jsonb, null::text[])"
            )
            continue
        members = sorted(values)
        if any(not _SQL_SAFE_VALUE.fullmatch(v) for v in members):
            raise ValueError(f"canonical value for {field} is not a bare label: {members}")
        canon = json.dumps(members)
        off = f"l.{field} is not null and not '{canon}'::jsonb ? l.{field}::text"
        pairs = ", ".join(
            f"'{v}', count(*) filter (where l.{field}::text = '{v}')" for v in members
        )
        aggs.append(f"count(distinct l.{field}::text) as d{i}")
        aggs.append(f"count(*) filter (where {off}) as o{i}")
        aggs.append(f"array_agg(distinct l.{field}::text) filter (where {off}) as ov{i}")
        aggs.append(f"jsonb_build_object({pairs}) as vv{i}")
        cells.append(
            f"('{field}', a.n{i}, a.d{i}, a.o{i}, a.t{i}, a.x{i}, a.vv{i}, a.ov{i})"
        )
    agg_list = ",\n           ".join(aggs)
    cell_list = ",\n           ".join(cells)
    return f"""
with src as (
    select source from portals where kind = 'scraper' and is_enabled
),
agg as (
    select l.source, count(*)::bigint as n_active,
           {agg_list}
      from listings l
      join src on src.source = l.source
     where l.is_active
     group by 1
)
select a.source, a.n_active, c.field, c.n_filled, c.n_distinct, c.n_off_canon,
       c.n_true, c.n_false, c.value_counts, c.off_canon_values
  from agg a
  cross join lateral (values
           {cell_list}
       ) c(field, n_filled, n_distinct, n_off_canon, n_true, n_false,
           value_counts, off_canon_values)
 order by a.source, c.field
"""


def census_params(**extra: Any) -> dict[str, Any]:
    return {"sample": SAMPLE_ROWS, "max_distinct": MAX_DISTINCT_FOR_VALUES, **extra}


@lru_cache(maxsize=None)
def canonical_values(field: str) -> frozenset[str] | None:
    """The canon for a listings column, read from `toolkit.filter_registry` — the one
    place that already defines "what counts as a known value" and generates the SPA and
    API schema from it. None where the column carries no closed vocabulary at all."""
    values = filter_registry.COLUMN_CANONICAL_VALUES.get(field)
    return frozenset(values) if values else None


# Built once at import, after the canon is readable: the statement is a pure function of
# the column contract and the filter registry, so it cannot drift from what is measured.
FIELD_MATRIX_SQL = _matrix_sql()


# --- pure reductions (unit-tested with no DB) ------------------------------


def _iso(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()


def reduce_census(
    rows: Sequence[Sequence[Any]], *, portal: str, generated_at: _dt.datetime
) -> dict[str, Any]:
    """(n_sampled, key, key_rows, key_distinct, value, n) rows -> one portal's census."""
    n_sampled = int(rows[0][0]) if rows else 0
    keys: dict[str, dict[str, Any]] = {}
    for _, key, key_rows, key_distinct, value, n in rows:
        entry = keys.setdefault(
            key, {"n": int(key_rows), "pct": 0.0, "distinct": int(key_distinct)}
        )
        if n is None:
            continue
        entry.setdefault("values", {})[JSON_NULL if value is None else value] = int(n)
    rare: list[str] = []
    kept: dict[str, dict[str, Any]] = {}
    for key, entry in sorted(keys.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        entry["pct"] = round(100.0 * entry["n"] / n_sampled, 1) if n_sampled else 0.0
        if entry["pct"] < RARE_KEY_PCT:
            rare.append(key)
        else:
            kept[key] = entry
    return {
        "portal": portal,
        "generated_at": _iso(generated_at),
        "sample": {"n_rows": n_sampled, "predicate": SAMPLE_PREDICATE,
                   "rare_key_pct": RARE_KEY_PCT, "max_distinct_for_values": MAX_DISTINCT_FOR_VALUES},
        "keys": kept,
        "rare": {"n_keys": len(rare), "keys": sorted(rare)},
    }


def cell_key(source: str, field: str) -> str:
    return f"{source}/{field}"


def reduce_matrix(
    rows: Sequence[Sequence[Any]], *, generated_at: _dt.datetime
) -> dict[str, Any]:
    """(source, n_active, field, n_filled, n_distinct, n_off_canon, n_true, n_false,
    value_counts, off_canon_values) rows -> the (source, field) fill + validity matrix."""
    cells: dict[str, dict[str, Any]] = {}
    fields: dict[str, str] = {}
    booleans: dict[str, tuple[int, int]] = {}
    boolean_fields: set[str] = set()
    for (source, n_active, field, n_filled, n_distinct, n_off,
         n_true, n_false, value_counts, off_values) in rows:
        key = cell_key(source, field)
        fields[key] = field
        n_active, n_filled = int(n_active), int(n_filled)
        cell: dict[str, Any] = {
            "n": n_active, "filled": n_filled,
            "fill": round(n_filled / n_active, 4) if n_active else 0.0,
        }
        if n_distinct is not None:
            cell["distinct"] = int(n_distinct)
        if n_off is not None:
            cell["off_canon"] = round(int(n_off) / n_filled, 4) if n_filled else 0.0
        if off_values:
            cell["off_canon_values"] = sorted(off_values)
        kept = {v: int(n) for v, n in (value_counts or {}).items() if int(n)}
        if kept:
            cell["values"] = dict(sorted(kept.items(), key=lambda kv: (-kv[1], kv[0])))
        booleans[key] = (int(n_true), int(n_false))
        # A field is boolean where SOME source's rows are nothing but true/false. Derived
        # from the data, not from a type table: a cell with zero fill has no values to
        # read, and that cell is exactly the one the check must report as a boolean zero.
        if n_filled and int(n_true) + int(n_false) == n_filled:
            boolean_fields.add(field)
        cells[key] = cell
    for key, cell in cells.items():
        if fields[key] in boolean_fields:
            cell["true"], cell["false"] = booleans[key]
    return {
        "generated_at": _iso(generated_at),
        "cohort": MATRIX_COHORT,
        "cells": {key: cells[key] for key in sorted(cells)},
    }


# --- the regression arms (pure; scripts/checks/quality.py turns them into cells) ---

# The numbers are the harness's (THRESHOLDS `parsing_fill_*`, `parsing_vocabulary_rise_*`,
# `parsing_census_stale_days`, each beside its basis), passed in by the parsing check. A cell that
# drifts past a line honestly is a re-bless, which is why the check names BLESS_COMMAND.
FILL, VOCABULARY = "fill", "vocabulary"


class Finding(NamedTuple):
    """One (source, field) cell that moved against the blessed matrix, on one arm."""

    key: str
    arm: str
    status: str
    reason: str


def compare_to_baseline(
    live: Mapping[str, Any], baseline: Mapping[str, Any], *,
    fill_drop_warn_pp: float, fill_drop_fail_pp: float, fill_drop_min_baseline: float,
    fill_collapse_ratio: float, fill_collapse_min_rows: int,
    off_canon_rise_warn_pp: float, off_canon_rise_fail_pp: float,
    inert: Collection[str] = (),
) -> list[Finding]:
    """Every live cell that moved against the blessed matrix: one finding per cell and arm."""
    findings: list[Finding] = []
    base_cells: Mapping[str, Any] = baseline.get("cells") or {}
    live_cells: Mapping[str, Any] = live.get("cells") or {}
    live_sources = {key.split("/", 1)[0] for key in live_cells}
    # A blessed cell the live matrix no longer produces is the strongest form of a portal that
    # stopped writing: the spine comes from portals.is_enabled and from having active ads, so one
    # flag flip takes all of a portal's cells out of the measurement.
    missing: dict[str, list[str]] = {}
    for key in sorted(set(base_cells) - set(live_cells)):
        missing.setdefault(key.split("/", 1)[0], []).append(key)
    for source, gone in sorted(missing.items()):
        if source in live_sources:
            status, reason = "warn", (
                f"blessed, but absent from the live matrix ({len(gone)} of {source}'s blessed "
                "fields are; its other fields are measured)")
        else:
            status, reason = "fail", (
                f"blessed, but absent from the live matrix with all {len(gone)} of {source}'s "
                "blessed fields: the portal's ads left the measurement")
        findings.extend(Finding(key, FILL, status, reason) for key in gone)
    for key, cell in sorted(live_cells.items()):
        was = base_cells.get(key)
        if not isinstance(was, Mapping):
            continue  # a cell the baseline lacks arrives green: the next re-bless adds it
        # The lines are strict, at the matrix's own four decimals: 10.0 pp does not cross 10 pp.
        base_fill, fill = float(was.get("fill", 0.0)), float(cell.get("fill", 0.0))
        drop = round(base_fill - fill, 4)
        base_filled, filled = int(was.get("filled", 0)), int(cell.get("filled", 0))
        if key in inert:
            # Nothing can write it today (a `none` producer, or a closed gate), so its fill only
            # falls; a wrong spelling is still wrong whoever wrote it.
            pass
        elif base_fill >= fill_drop_min_baseline and drop > fill_drop_warn_pp:
            failed = drop > fill_drop_fail_pp
            limit = fill_drop_fail_pp if failed else fill_drop_warn_pp
            findings.append(Finding(key, FILL, "fail" if failed else "warn", (
                f"fill {base_fill:.1%} -> {fill:.1%}: -{drop * 100:.1f} pp of "
                f"{int(cell.get('n', 0)):,} active ads, past {limit * 100:g} pp")))
        elif base_filled >= fill_collapse_min_rows and filled < fill_collapse_ratio * base_filled:
            findings.append(Finding(key, FILL, "fail", (
                f"filled {base_filled:,} -> {filled:,} of {int(cell.get('n', 0)):,} active ads: "
                f"under {fill_collapse_ratio:.0%} of the blessed count")))
        base_off, off = float(was.get("off_canon", 0.0)), float(cell.get("off_canon", 0.0))
        rise = round(off - base_off, 4)
        if rise > off_canon_rise_warn_pp:
            failed = rise > off_canon_rise_fail_pp
            limit = off_canon_rise_fail_pp if failed else off_canon_rise_warn_pp
            spellings = ", ".join(cell.get("off_canon_values", [])[:4])
            findings.append(Finding(key, VOCABULARY, "fail" if failed else "warn", (
                f"off the canon {base_off:.1%} -> {off:.1%} (+{rise * 100:.1f} pp"
                f"{': ' + spellings if spellings else ''}), past {limit * 100:g} pp")))
    return findings


def zero_fill_cells(matrix: Mapping[str, Any]) -> list[str]:
    """Cells with no value at all. They cannot fall further, so they are green by
    construction — and they are the whole reason this wave exists, so the check names
    them on every run until W2's contract declares a producer for each.

    Read from the LIVE matrix, never from the baseline: a cell a later wave repairs must
    stop being named the run it is repaired, not the day someone re-blesses."""
    return sorted(
        key for key, cell in (matrix.get("cells") or {}).items()
        if isinstance(cell, Mapping) and int(cell.get("filled", 0)) == 0
    )


def stale_censuses(
    censuses: Iterable[Mapping[str, Any]], *, now: _dt.datetime, max_age_days: int
) -> list[str]:
    """A line per census older than `max_age_days` whole days, or with no readable stamp."""
    out: list[str] = []
    for census in censuses:
        stamped = str(census.get("generated_at") or "")
        try:
            when = _dt.datetime.fromisoformat(stamped)
        except ValueError:
            out.append(f"{census.get('portal')} has no readable generated_at")
            continue
        age = (now - when).days
        if age > max_age_days:
            out.append(f"{census.get('portal')} census is {age}d old")
    return out


# --- checked-in goldens ----------------------------------------------------


def dumps(doc: Mapping[str, Any], *, line_maps: tuple[str, ...]) -> str:
    """Stable text: one LINE per census key / matrix cell, so a re-bless reads as a diff
    rather than as a reflowed blob."""
    parts: list[str] = []
    for key in sorted(doc):
        value = doc[key]
        if key in line_maps and isinstance(value, dict):
            inner = ",\n".join(
                f"    {json.dumps(k, ensure_ascii=False)}: "
                f"{json.dumps(v, ensure_ascii=False, sort_keys=True)}"
                for k, v in value.items()
            )
            body = f"{{\n{inner}\n  }}" if inner else "{}"
        else:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True)
        parts.append(f"  {json.dumps(key, ensure_ascii=False)}: {body}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def load_censuses() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CENSUS_DIR.glob("*.json"))
    ]


def load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def write_census(census: Mapping[str, Any]) -> Path:
    CENSUS_DIR.mkdir(parents=True, exist_ok=True)
    path = CENSUS_DIR / f"{census['portal']}.json"
    path.write_text(dumps(census, line_maps=("keys",)), encoding="utf-8")
    return path


def write_baseline(matrix: Mapping[str, Any]) -> Path:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(dumps(matrix, line_maps=("cells",)), encoding="utf-8")
    return BASELINE_PATH


# --- the re-bless ----------------------------------------------------------


def fetch_matrix(conn: Any, *, generated_at: _dt.datetime) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(FIELD_MATRIX_SQL)
        rows = cur.fetchall()
    return reduce_matrix(rows, generated_at=generated_at)


def fetch_census(conn: Any, source: str, *, generated_at: _dt.datetime) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(FIELD_CENSUS_SQL, census_params(source=source))
        rows = cur.fetchall()
    return reduce_census(rows, portal=source, generated_at=generated_at)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bless", action="store_true",
                        help="rewrite the checked-in census + fill baseline")
    args = parser.parse_args()
    now = _dt.datetime.now(_dt.timezone.utc)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(PORTAL_SOURCES_SQL)
            sources = [r[0] for r in cur.fetchall()]
        censuses = [fetch_census(conn, s, generated_at=now) for s in sources]
        matrix = fetch_matrix(conn, generated_at=now)
    if not args.bless:
        print(json.dumps({"portals": len(censuses), "cells": len(matrix["cells"])}))
        return 0
    for census in censuses:
        print(write_census(census))
    print(write_baseline(matrix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
