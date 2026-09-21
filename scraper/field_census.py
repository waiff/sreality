"""The field-capture instrument: what each portal PUBLISHES, and what we STORED.

Two measurements, one module, so the re-bless and the live health check can never
measure different things:

  * the **key census** — the keys a portal's stored payload actually carries, with the
    share of sampled rows carrying each and (for low-cardinality keys) its values. This
    is the evidence a parser is read against: `remax`'s parser reads `balkon` / `lodzie`
    and the portal has never emitted either, which is why `has_balcony` is 0/0 on every
    active remax row while the parser's own hand-authored fixtures stay green.
  * the **fill + validity matrix** — per (source, field), how many sampled ACTIVE rows
    carry a value, and for the fields whose canon `toolkit.filter_registry` defines, how
    many carry a value OUTSIDE it. Fill alone cannot see a wrong value (the lesson the
    per-m2 checks were built on); the value histogram is what makes validity measurable.

Both are SAMPLED over the newest active rows per source: the full-table form of either
does not return inside the verification lane's per-check budget, and the newest slice is
the cohort that reflects what the parsers write TODAY — a regression is ~100% of what
arrived since it shipped, but only churn-fraction of the stock.

Neither measurement reads `listing_description_enrichments`: that ledger still records
cells a later detail re-fetch wiped as filled.

Re-bless a reviewed change:  python -m scraper.field_census --bless
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scraper.db import LISTING_COLUMNS, connect
from toolkit import filter_registry

_ROOT = Path(__file__).resolve().parent.parent
CENSUS_DIR = _ROOT / "data" / "field_capture" / "census"
BASELINE_PATH = _ROOT / "data" / "field_capture" / "fill_baseline.json"

BLESS_COMMAND = "python -m scraper.field_census --bless"

# How many of the newest ACTIVE rows per source each measurement reads. 1,000 keeps the
# worst-case binomial standard error of a share at 1.6 pp, which every threshold below is
# sized against, and keeps the census query at ~1 s on the widest payload (sreality).
SAMPLE_ROWS = 1000
# Values are recorded only for a key/field with at most this many distinct values in the
# sample, and then ALL of them are — a partial list of a vocabulary is not evidence about
# the vocabulary. Above the cap the key is a number, an id or prose: no values, and the
# recorded `distinct` count says so. The cap doubles as the bound on any one cell's rows.
MAX_DISTINCT_FOR_VALUES = 12
# A key carried by fewer than this share of sampled rows folds into a counted `rare`
# bucket: a census is evidence for a reviewer, not a dump of every one-off key.
RARE_KEY_PCT = 1.0

SAMPLE_PREDICATE = (
    f"the newest {SAMPLE_ROWS} rows per source with is_active, by first_seen_at desc"
)

# jsonb has a null of its own, and it is the defect shape idnes shows: the key is present
# and its value is null, so a key-presence census and a column-fill matrix disagree. Give
# it a name rather than dropping it.
JSON_NULL = "(json null)"

# `description` is the substrate of the post-publication text lane, not a portal-stated
# attribute; `published_at` and `source_url` are identity, kept out of every content hash.
_NON_ATTRIBUTE = frozenset({"description", "published_at", "source_url"})
ATTRIBUTE_FIELDS: tuple[str, ...] = tuple(
    c for c in LISTING_COLUMNS if c not in _NON_ATTRIBUTE
)

# The nine listing portals, from the per-portal config row (CLAUDE.md rule 21) rather than
# a tenth list that can drift. `kind = 'scraper'` excludes the on-demand URL parsers.
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


def _matrix_sql() -> str:
    """The matrix SQL, with the field list interpolated from `ATTRIBUTE_FIELDS`.

    The field list is the scraper's own column contract, never a second hard-coded list —
    a second list is what let `data_quality_by_source` drift out of step with what the
    parsers write. `tests.sql_corpus` resolves a composed `*_SQL` constant by import, so
    this stays inside the placeholder guard and the PREPARE sweep.

    The off-canon count is computed HERE, over every distinct value, not from the value
    rows: those are capped, and an off-canon spelling sitting outside the cap would read
    as perfect validity.
    """
    cols = ", ".join(f"l.{c}" for c in ATTRIBUTE_FIELDS)
    spine = ", ".join(f"'{c}'" for c in ATTRIBUTE_FIELDS)
    return f"""
with src as (
    select source from portals where kind = 'scraper' and is_enabled
),
sampled as (
    select src.source, s.*
      from src,
           lateral (select {cols}
                      from listings l
                     where l.source = src.source and l.is_active
                     order by l.first_seen_at desc
                     limit %(sample)s) s
),
totals as (
    select source, count(*)::bigint as n_sampled from sampled group by 1
),
cells as (
    select sampled.source, kv.key as field, left(kv.value, 60) as value
      from sampled, lateral jsonb_each_text(to_jsonb(sampled)) kv
     where kv.key <> 'source' and kv.value is not null
),
counted as (
    select source, field, value, count(*)::bigint as n from cells group by 1, 2, 3
),
per_value as (
    select c.source, c.field, c.value, c.n,
           coalesce(jsonb_typeof(%(canon)s::jsonb -> c.field) = 'array'
                    and not (%(canon)s::jsonb -> c.field) ? c.value, false) as off_canon
      from counted c
),
filled as (
    select source, field, sum(n)::bigint as n_filled, count(*)::bigint as n_distinct,
           coalesce(sum(n) filter (where off_canon), 0)::bigint as n_off_canon
      from per_value group by 1, 2
),
ranked as (
    select source, field, value, n, off_canon,
           row_number() over (partition by source, field order by n desc, value) as rn,
           row_number() over (partition by source, field, off_canon
                              order by n desc, value) as rn_off
      from per_value
),
spine as (
    select t.source, f.field from totals t cross join unnest(array[{spine}]) f(field)
)
select t.n_sampled, sp.source, sp.field,
       coalesce(fl.n_filled, 0) as n_filled, coalesce(fl.n_distinct, 0) as n_distinct,
       coalesce(fl.n_off_canon, 0) as n_off_canon,
       r.value, r.n, r.off_canon
  from spine sp
  join totals t on t.source = sp.source
  left join filled fl on fl.source = sp.source and fl.field = sp.field
  left join ranked r
    on r.source = sp.source and r.field = sp.field
   and ((fl.n_distinct <= %(max_distinct)s and r.rn <= %(max_distinct)s)
        or (r.off_canon and r.rn_off <= %(max_distinct)s))
 order by sp.source, sp.field, r.n desc nulls last, r.value
"""


FIELD_MATRIX_SQL = _matrix_sql()


def canon_param() -> str:
    """The canon as one jsonb argument: {field: [canonical values]} for every attribute
    field a filter constrains. Fields absent from it are counted, never judged."""
    return json.dumps(
        {
            field: sorted(values)
            for field in ATTRIBUTE_FIELDS
            if (values := canonical_values(field)) is not None
        },
        sort_keys=True,
    )


def census_params(**extra: Any) -> dict[str, Any]:
    return {"sample": SAMPLE_ROWS, "max_distinct": MAX_DISTINCT_FOR_VALUES, **extra}


@lru_cache(maxsize=None)
def canonical_values(field: str) -> frozenset[str] | None:
    """The canon for a listings column, read from `toolkit.filter_registry` — the one
    place that already defines "what counts as a known value" and generates the SPA and
    API schema from it. None where no filter constrains the column: `price_unit` has no
    canonical option list anywhere today, so its vocabulary can be COUNTED here but not
    judged (W5 is where its four spellings collapse to two)."""
    values = {
        str(option.value)
        for f in filter_registry.all_filters()
        if f.pg_column == field and f.enum_values
        for option in f.enum_values
        if option.value != filter_registry.UNKNOWN_FILTER_VALUE
    }
    return frozenset(values) or None


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
    """(n_sampled, source, field, n_filled, n_distinct, n_off_canon, value, n, off_canon)
    rows -> the (source, field) fill + validity matrix."""
    cells: dict[str, dict[str, Any]] = {}
    fields: dict[str, str] = {}
    values: dict[str, dict[str, int]] = {}
    off_values: dict[str, dict[str, int]] = {}
    for n_sampled, source, field, n_filled, n_distinct, n_off, value, n, off in rows:
        key = cell_key(source, field)
        fields[key] = field
        cells.setdefault(
            key,
            {"n": int(n_sampled), "filled": int(n_filled),
             "fill": round(int(n_filled) / int(n_sampled), 4) if n_sampled else 0.0,
             "distinct": int(n_distinct),
             "off_canon": round(int(n_off) / int(n_filled), 4) if n_filled else 0.0},
        )
        if n is None:
            continue
        label = JSON_NULL if value is None else value
        (off_values if off else values).setdefault(key, {})[label] = int(n)
    # A field is boolean when SOME source's sample shows nothing but true/false. Derived
    # from the data, not from a type table: a cell with zero fill has no values to read,
    # and that cell is exactly the one the check must still report as a boolean zero.
    boolean_fields = {
        fields[key] for key, seen in values.items() if seen and set(seen) <= {"true", "false"}
    }
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(cells):
        cell = cells[key]
        field = fields[key]
        seen = values.get(key, {})
        if canonical_values(field) is None:
            # No canon to judge against, so the vocabulary itself is the evidence — this
            # is how `price_unit`'s four live spellings and `area_basis` stay visible.
            cell.pop("off_canon")
            if seen and cell["distinct"] <= MAX_DISTINCT_FOR_VALUES:
                cell["values"] = seen
        elif off_values.get(key):
            cell["off_canon_values"] = sorted(off_values[key])
        if field in boolean_fields:
            cell["true"] = seen.get("true", 0)
            cell["false"] = seen.get("false", 0)
        out[key] = cell
    return {
        "generated_at": _iso(generated_at),
        "sample": {"n_rows_per_source": SAMPLE_ROWS, "predicate": SAMPLE_PREDICATE},
        "cells": out,
    }


# --- the regression arms (pure; verify_pipeline turns these into a status) ---

# Sized against the sample, not against taste. The worst-case standard error of a share
# at n=1,000 is 0.5/sqrt(1000) = 1.6 pp, so:
#   * a 10 pp fall is 6.3 sigma and a 20 pp fall 12.6 sigma — neither is sampling noise;
#   * the relative arm catches a partial break the absolute one cannot (ceskereality's
#     `furnished` key mismatch would sit at ~2.5%, not 0%): a cell that had 50+ filled
#     rows and keeps under a quarter of them is >5 sigma at any baseline share.
# The `>= 0.10` floor on the absolute arm keeps a cell that was always near-zero from
# ringing on a one-row wobble; the relative arm covers those instead.
FILL_DROP_WARN_PP = 0.10
FILL_DROP_FAIL_PP = 0.20
FILL_DROP_MIN_BASELINE = 0.10
FILL_COLLAPSE_RATIO = 0.25
FILL_COLLAPSE_MIN_ROWS = 50
# An off-canon share that RISES is a portal or parser that started emitting a spelling
# nothing maps. 5 pp is 3.2 sigma, 15 pp is 9.5 sigma.
OFF_CANON_RISE_WARN_PP = 0.05
OFF_CANON_RISE_FAIL_PP = 0.15
# The census is a sample of a live key space; a portal that renames a key leaves a stale
# census reading green. 30 days is the reviewed-diff cadence, not a CI calendar.
CENSUS_STALE_DAYS = 30


def compare_to_baseline(
    live: Mapping[str, Any], baseline: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """(fail offenders, warn offenders) for the live matrix against the blessed one.

    A cell the baseline does not carry is never an offender — a new portal or a new
    column arrives green and is blessed into the baseline by the next re-bless."""
    fails: list[str] = []
    warns: list[str] = []
    base_cells: Mapping[str, Any] = baseline.get("cells") or {}
    for key, cell in sorted((live.get("cells") or {}).items()):
        was = base_cells.get(key)
        if not isinstance(was, Mapping):
            continue
        drop = float(was.get("fill", 0.0)) - float(cell.get("fill", 0.0))
        base_filled = int(was.get("filled", 0))
        if float(was.get("fill", 0.0)) >= FILL_DROP_MIN_BASELINE and drop >= FILL_DROP_WARN_PP:
            line = (
                f"{key} fill {was['fill']:.1%} -> {cell['fill']:.1%} "
                f"(-{drop * 100:.1f} pp of {cell['n']} sampled)"
            )
            (fails if drop >= FILL_DROP_FAIL_PP else warns).append(line)
        elif (
            base_filled >= FILL_COLLAPSE_MIN_ROWS
            and cell.get("filled", 0) < FILL_COLLAPSE_RATIO * base_filled
        ):
            fails.append(
                f"{key} filled {base_filled} -> {cell['filled']} of {cell['n']} sampled "
                f"(under {FILL_COLLAPSE_RATIO:.0%} of the blessed count)"
            )
        rise = float(cell.get("off_canon", 0.0)) - float(was.get("off_canon", 0.0))
        if rise >= OFF_CANON_RISE_WARN_PP:
            line = (
                f"{key} off-canon {was.get('off_canon', 0.0):.1%} -> "
                f"{cell.get('off_canon', 0.0):.1%} ({', '.join(cell.get('off_canon_values', [])[:4])})"
            )
            (fails if rise >= OFF_CANON_RISE_FAIL_PP else warns).append(line)
    return fails, warns


def known_zero_cells(baseline: Mapping[str, Any]) -> list[str]:
    """Cells the blessed baseline recorded as never filled. They cannot fall further, so
    they are green by construction — and they are the whole reason this wave exists, so
    the check names them on every run until W2's contract declares a producer for each."""
    return sorted(
        key for key, cell in (baseline.get("cells") or {}).items()
        if isinstance(cell, Mapping) and int(cell.get("filled", 0)) == 0
    )


def booleans_never_false(matrix: Mapping[str, Any]) -> list[str]:
    """Boolean cells with a true count and no false, ever, in the sample.

    Reported as a NUMBER, never as a verdict: whether silence means `false` or `unknown`
    is the absence semantics the W2 attribute contract declares per (portal, field), and
    until it exists there is nothing here to be right or wrong against."""
    return sorted(
        f"{key} {cell['true']}/{cell['n']} true, 0 false"
        for key, cell in (matrix.get("cells") or {}).items()
        if isinstance(cell, Mapping) and cell.get("false") == 0 and cell.get("true", 0) > 0
    )


def stale_censuses(
    censuses: Iterable[Mapping[str, Any]], *, now: _dt.datetime
) -> list[str]:
    out: list[str] = []
    for census in censuses:
        stamped = str(census.get("generated_at") or "")
        try:
            when = _dt.datetime.fromisoformat(stamped)
        except ValueError:
            out.append(f"{census.get('portal')} has no readable generated_at")
            continue
        age = (now - when).days
        if age > CENSUS_STALE_DAYS:
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
        cur.execute(FIELD_MATRIX_SQL, census_params(canon=canon_param()))
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
