"""Region census: pick the one region the autonomous dedup engine is first trialled in.

Reads ALL-TIME listings (active + delisted) at two block grains — town
(`listing_location.obec_kod`) and quarter (`listing_location.cast_obce_kod`) — and reports,
per block, how much of the signal the future engine needs is actually present there: portal
breadth, category spread, disposition/area/floor/broker coverage, location precision, and
the raw in-block pair count under a COARSE block x category_main x category_type key — an
upper bound on any finer blocking the engine adopts, not a figure comparable to a live lane
(the shipped path-C generator blocks on town plus disposition or area, and splits Praha).

Three query layers, because cost differs by an order of magnitude. The COVERAGE layer is one
statement of corpus-wide totals, so the block layer's INNER JOIN has a denominator. The BLOCK
layer is one grouped pass over `listings JOIN listing_location` and nothing else. The DETAIL
layer joins images, pHash, CLIP embeddings and the shared-pin aggregate, and runs only for the
handful of blocks the block layer shortlists.

`census.json` is written incrementally — after coverage, after each grain's block pass, and
again after the detail loop — and a detail statement that fails is recorded on its block
(`detail_error`) rather than voiding the whole run.

Cohort note: the census counts every listing that has a `listing_location` row carrying the
grain's code. That is deliberately WIDER than the consumer serving rule
(`location_data.claims_common.served_location_predicate`) — blocking wants every row that has
a block code, not only the rows Browse shows — so the served count is reported alongside
(`n_served`) rather than used as a filter. Evaluated on the already-joined row (`listing_id`
is that table's primary key) the EXISTS in that predicate reduces to exactly the
geom/country_status test spelled out below — pinned against the one definition by
`tests/autodedup/test_census.py::test_served_clause_matches_the_one_pinned_definition`, so the
census's `n_served` can never come to mean something other than what Browse means.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

SOURCES: tuple[str, ...] = (
    "sreality", "bazos", "bezrealitky", "idnes", "mmreality", "remax", "ceskereality",
    "realitymix", "maxima",
)
CATEGORY_MAIN: tuple[str, ...] = ("byt", "dum", "pozemek", "komercni", "ostatni")
CATEGORY_TYPE: tuple[str, ...] = ("prodej", "pronajem", "drazba", "podil")

GRAINS: tuple[str, ...] = ("town", "quarter")

# The four categories a trial region must carry for the engine to be exercised on more than
# one shape of listing; `ostatni` is a residual bucket and is deliberately not required.
REQUIRED_CATEGORY_MAIN: tuple[str, ...] = ("byt", "dum", "pozemek", "komercni")

_PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _json_default(value: Any) -> str:
    return str(value)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Dump a report, stringifying what json cannot (datetimes, Decimals)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )



# --- SQL ------------------------------------------------------------------------------
# Two constants, one per grain, because the grain is a COLUMN and a column name is never
# string-built into SQL. Everything else is identical between them.

CENSUS_TOWNS_SQL = """
WITH base AS (
    SELECT
        ll.obec_kod                                   AS block_code,
        ll.obec_name                                  AS block_name,
        l.source                                      AS source,
        l.category_main                               AS category_main,
        l.category_type                               AS category_type,
        l.is_active                                   AS is_active,
        l.disposition                                 AS disposition,
        l.area_m2                                     AS area_m2,
        l.floor                                       AS floor,
        l.broker_identity_id                          AS broker_identity_id,
        (ll.geom IS NOT NULL OR ll.country_status = 'foreign') AS served,
        gr.is_address_grain                           AS is_address_grain,
        gr.rank >= (SELECT r.rank FROM location_granularity_rank r
                     WHERE r.granularity = 'street')  AS at_street_grain
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    LEFT JOIN location_granularity_rank gr ON gr.granularity = ll.granularity
    WHERE ll.obec_kod IS NOT NULL
),
cells AS (
    SELECT block_code, category_main, category_type, count(*) AS n
    FROM base
    WHERE category_main IS NOT NULL AND category_type IS NOT NULL
    GROUP BY 1, 2, 3
),
pairs AS (
    SELECT block_code, sum(n * (n - 1) / 2)::bigint AS raw_inblock_pairs
    FROM cells
    GROUP BY 1
),
agg AS (
    SELECT
        block_code,
        min(block_name)                                               AS block_name,
        count(*)                                                      AS n_total,
        count(*) FILTER (WHERE is_active)                             AS n_active,
        count(*) FILTER (WHERE NOT is_active)                         AS n_inactive,
        count(*) FILTER (WHERE served)                                AS n_served,
        count(*) FILTER (WHERE category_main IS NULL
                            OR category_type IS NULL)                 AS n_uncategorised,
        count(*) FILTER (WHERE source = 'sreality')                   AS src_sreality,
        count(*) FILTER (WHERE source = 'bazos')                      AS src_bazos,
        count(*) FILTER (WHERE source = 'bezrealitky')                AS src_bezrealitky,
        count(*) FILTER (WHERE source = 'idnes')                      AS src_idnes,
        count(*) FILTER (WHERE source = 'mmreality')                  AS src_mmreality,
        count(*) FILTER (WHERE source = 'remax')                      AS src_remax,
        count(*) FILTER (WHERE source = 'ceskereality')               AS src_ceskereality,
        count(*) FILTER (WHERE source = 'realitymix')                 AS src_realitymix,
        count(*) FILTER (WHERE source = 'maxima')                     AS src_maxima,
        count(*) FILTER (WHERE category_main = 'byt')                 AS cat_byt,
        count(*) FILTER (WHERE category_main = 'dum')                 AS cat_dum,
        count(*) FILTER (WHERE category_main = 'pozemek')             AS cat_pozemek,
        count(*) FILTER (WHERE category_main = 'komercni')            AS cat_komercni,
        count(*) FILTER (WHERE category_main = 'ostatni')             AS cat_ostatni,
        count(*) FILTER (WHERE category_type = 'prodej')              AS type_prodej,
        count(*) FILTER (WHERE category_type = 'pronajem')            AS type_pronajem,
        count(*) FILTER (WHERE category_type = 'drazba')              AS type_drazba,
        count(*) FILTER (WHERE category_type = 'podil')               AS type_podil,
        count(*) FILTER (WHERE disposition IS NOT NULL)               AS with_disposition,
        count(*) FILTER (WHERE area_m2 > 0)                           AS with_area,
        count(*) FILTER (WHERE floor IS NOT NULL)                     AS with_floor,
        count(*) FILTER (WHERE broker_identity_id IS NOT NULL)        AS with_broker,
        count(*) FILTER (WHERE at_street_grain)                       AS with_street,
        count(*) FILTER (WHERE is_address_grain)                      AS with_point
    FROM base
    GROUP BY 1
)
-- Portal breadth is summed from the nine counters rather than aggregated with
-- a de-duplicating aggregate: that is an ordered agg, which bars HashAgg for the whole rollup
-- and forces a sort of every base row. Same number, hashable plan.
-- LEFT JOIN because `cells` drops uncategorised rows, so an all-uncategorised block still
-- gets a census line (with zero pairs) instead of disappearing.
SELECT
    a.*,
    coalesce(p.raw_inblock_pairs, 0)                                  AS raw_inblock_pairs,
    (  (a.src_sreality > 0)::int     + (a.src_bazos > 0)::int
     + (a.src_bezrealitky > 0)::int  + (a.src_idnes > 0)::int
     + (a.src_mmreality > 0)::int    + (a.src_remax > 0)::int
     + (a.src_ceskereality > 0)::int + (a.src_realitymix > 0)::int
     + (a.src_maxima > 0)::int)                                       AS n_sources
FROM agg a
LEFT JOIN pairs p ON p.block_code = a.block_code
WHERE a.n_total BETWEEN %(min_n)s AND %(max_n)s
ORDER BY n_sources DESC, a.n_total DESC
LIMIT %(top)s
"""

CENSUS_QUARTERS_SQL = """
WITH base AS (
    SELECT
        ll.cast_obce_kod                              AS block_code,
        ll.cast_obce_name                             AS block_name,
        l.source                                      AS source,
        l.category_main                               AS category_main,
        l.category_type                               AS category_type,
        l.is_active                                   AS is_active,
        l.disposition                                 AS disposition,
        l.area_m2                                     AS area_m2,
        l.floor                                       AS floor,
        l.broker_identity_id                          AS broker_identity_id,
        (ll.geom IS NOT NULL OR ll.country_status = 'foreign') AS served,
        gr.is_address_grain                           AS is_address_grain,
        gr.rank >= (SELECT r.rank FROM location_granularity_rank r
                     WHERE r.granularity = 'street')  AS at_street_grain
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    LEFT JOIN location_granularity_rank gr ON gr.granularity = ll.granularity
    WHERE ll.cast_obce_kod IS NOT NULL
),
cells AS (
    SELECT block_code, category_main, category_type, count(*) AS n
    FROM base
    WHERE category_main IS NOT NULL AND category_type IS NOT NULL
    GROUP BY 1, 2, 3
),
pairs AS (
    SELECT block_code, sum(n * (n - 1) / 2)::bigint AS raw_inblock_pairs
    FROM cells
    GROUP BY 1
),
agg AS (
    SELECT
        block_code,
        min(block_name)                                               AS block_name,
        count(*)                                                      AS n_total,
        count(*) FILTER (WHERE is_active)                             AS n_active,
        count(*) FILTER (WHERE NOT is_active)                         AS n_inactive,
        count(*) FILTER (WHERE served)                                AS n_served,
        count(*) FILTER (WHERE category_main IS NULL
                            OR category_type IS NULL)                 AS n_uncategorised,
        count(*) FILTER (WHERE source = 'sreality')                   AS src_sreality,
        count(*) FILTER (WHERE source = 'bazos')                      AS src_bazos,
        count(*) FILTER (WHERE source = 'bezrealitky')                AS src_bezrealitky,
        count(*) FILTER (WHERE source = 'idnes')                      AS src_idnes,
        count(*) FILTER (WHERE source = 'mmreality')                  AS src_mmreality,
        count(*) FILTER (WHERE source = 'remax')                      AS src_remax,
        count(*) FILTER (WHERE source = 'ceskereality')               AS src_ceskereality,
        count(*) FILTER (WHERE source = 'realitymix')                 AS src_realitymix,
        count(*) FILTER (WHERE source = 'maxima')                     AS src_maxima,
        count(*) FILTER (WHERE category_main = 'byt')                 AS cat_byt,
        count(*) FILTER (WHERE category_main = 'dum')                 AS cat_dum,
        count(*) FILTER (WHERE category_main = 'pozemek')             AS cat_pozemek,
        count(*) FILTER (WHERE category_main = 'komercni')            AS cat_komercni,
        count(*) FILTER (WHERE category_main = 'ostatni')             AS cat_ostatni,
        count(*) FILTER (WHERE category_type = 'prodej')              AS type_prodej,
        count(*) FILTER (WHERE category_type = 'pronajem')            AS type_pronajem,
        count(*) FILTER (WHERE category_type = 'drazba')              AS type_drazba,
        count(*) FILTER (WHERE category_type = 'podil')               AS type_podil,
        count(*) FILTER (WHERE disposition IS NOT NULL)               AS with_disposition,
        count(*) FILTER (WHERE area_m2 > 0)                           AS with_area,
        count(*) FILTER (WHERE floor IS NOT NULL)                     AS with_floor,
        count(*) FILTER (WHERE broker_identity_id IS NOT NULL)        AS with_broker,
        count(*) FILTER (WHERE at_street_grain)                       AS with_street,
        count(*) FILTER (WHERE is_address_grain)                      AS with_point
    FROM base
    GROUP BY 1
)
-- Portal breadth is summed from the nine counters rather than aggregated with
-- a de-duplicating aggregate: that is an ordered agg, which bars HashAgg for the whole rollup
-- and forces a sort of every base row. Same number, hashable plan.
-- LEFT JOIN because `cells` drops uncategorised rows, so an all-uncategorised block still
-- gets a census line (with zero pairs) instead of disappearing.
SELECT
    a.*,
    coalesce(p.raw_inblock_pairs, 0)                                  AS raw_inblock_pairs,
    (  (a.src_sreality > 0)::int     + (a.src_bazos > 0)::int
     + (a.src_bezrealitky > 0)::int  + (a.src_idnes > 0)::int
     + (a.src_mmreality > 0)::int    + (a.src_remax > 0)::int
     + (a.src_ceskereality > 0)::int + (a.src_realitymix > 0)::int
     + (a.src_maxima > 0)::int)                                       AS n_sources
FROM agg a
LEFT JOIN pairs p ON p.block_code = a.block_code
WHERE a.n_total BETWEEN %(min_n)s AND %(max_n)s
ORDER BY n_sources DESC, a.n_total DESC
LIMIT %(top)s
"""

# One block at a time. The grain travels as a pair of nullable codes rather than a column
# name: the caller binds the grain's code and NULL for the other, so each branch stays an
# indexable equality instead of a CASE over a column name. Both codes carry an explicit
# `::bigint` because the unused one is ALWAYS NULL — psycopg sends no type OID for a None, so
# without the cast Postgres cannot infer a type for the `IS NULL` branch and Parse fails with
# 42P18 (the same shape tests/test_sql_schema_prepare.py records as an unchecked artifact).
CENSUS_DETAIL_SQL = """
WITH block AS (
    SELECT
        l.id                 AS id,
        l.broker_identity_id AS broker_identity_id,
        l.first_seen_at      AS first_seen_at,
        l.inactive_at        AS inactive_at,
        ll.geom              AS geom
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
),
stored_images AS (
    SELECT
        i.listing_id           AS listing_id,
        (i.phash IS NOT NULL)  AS has_phash,
        EXISTS (SELECT 1 FROM image_clip_embeddings e WHERE e.image_id = i.id) AS has_clip
    FROM images i
    JOIN block b ON b.id = i.listing_id
    WHERE i.storage_path IS NOT NULL
),
img AS (
    SELECT
        listing_id                             AS listing_id,
        count(*)                               AS n_images,
        count(*) FILTER (WHERE has_phash)      AS n_phash,
        count(*) FILTER (WHERE has_clip)       AS n_clip
    FROM stored_images
    GROUP BY 1
),
pins AS (
    SELECT ST_AsBinary(geom) AS pin, count(*) AS n
    FROM block
    WHERE geom IS NOT NULL
    GROUP BY 1
),
years AS (
    SELECT jsonb_object_agg(y, c) AS by_year
    FROM (
        SELECT extract(year FROM inactive_at)::int::text AS y, count(*) AS c
        FROM block
        WHERE inactive_at IS NOT NULL
        GROUP BY 1
    ) t
)
SELECT
    (SELECT count(*) FROM block)                                      AS n_block_listings,
    coalesce((SELECT sum(n_images) FROM img), 0)::bigint              AS n_images,
    (SELECT count(*) FROM img)                                        AS n_listings_with_images,
    (SELECT count(*) FROM img WHERE n_phash > 0)                      AS n_listings_with_phash,
    (SELECT count(*) FROM img WHERE n_clip > 0)                       AS n_listings_with_clip,
    coalesce((SELECT sum(n) FROM pins WHERE n >= %(pin_share_min)s), 0)::bigint
                                                                      AS shared_pin_listings,
    (SELECT count(DISTINCT broker_identity_id) FROM block
      WHERE broker_identity_id IS NOT NULL)                           AS n_distinct_brokers,
    (SELECT min(first_seen_at) FROM block)                            AS first_seen_min,
    (SELECT max(first_seen_at) FROM block)                            AS first_seen_max,
    coalesce((SELECT by_year FROM years), '{}'::jsonb)                AS n_inactive_by_year
"""

# The denominator for everything the block layer's INNER JOIN drops: a block's n_total is only
# interpretable next to how much of the corpus carries a grain code at all. One pass, no
# per-block work, so it is cheap enough to run every time.
CENSUS_COVERAGE_SQL = """
SELECT
    (SELECT count(*) FROM listings)                                   AS n_listings,
    (SELECT count(*) FROM listings l JOIN listing_location ll ON ll.listing_id = l.id)
                                                                      AS n_with_location_row,
    (SELECT count(*) FROM listing_location WHERE obec_kod IS NOT NULL)
                                                                      AS n_with_town_code,
    (SELECT count(*) FROM listing_location WHERE cast_obce_kod IS NOT NULL)
                                                                      AS n_with_quarter_code,
    (SELECT count(*) FROM listing_location ll
      WHERE (ll.geom IS NOT NULL OR ll.country_status = 'foreign'))   AS n_served
"""

BLOCK_SQL_BY_GRAIN: dict[str, str] = {
    "town": CENSUS_TOWNS_SQL,
    "quarter": CENSUS_QUARTERS_SQL,
}


def sql_placeholders(sql: str) -> set[str]:
    """The `%(name)s` parameter names a statement binds."""
    return set(_PLACEHOLDER.findall(sql))


def block_params(*, min_n: int, max_n: int, top: int) -> dict[str, int]:
    return {"min_n": min_n, "max_n": max_n, "top": top}


def detail_params(*, grain: str, block_code: int, pin_share_min: int) -> dict[str, Any]:
    if grain not in GRAINS:
        raise ValueError(f"unknown grain {grain!r}; use one of {', '.join(GRAINS)}")
    return {
        "obec_kod": block_code if grain == "town" else None,
        "cast_obce_kod": block_code if grain == "quarter" else None,
        "pin_share_min": pin_share_min,
    }


# --- selection score -------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreTargets:
    min_sources: int = 6
    min_per_category: int = 30
    min_image_share: float = 0.7
    size_min: int = 1500
    size_max: int = 5000


@dataclass(frozen=True)
class BlockScore:
    total: float
    sources: float
    categories: float
    types: float
    images: float
    size: float
    pin_penalty: float

    def as_dict(self) -> dict[str, float]:
        return {
            "total": self.total,
            "sources": self.sources,
            "categories": self.categories,
            "types": self.types,
            "images": self.images,
            "size": self.size,
            "pin_penalty": self.pin_penalty,
        }


WEIGHTS: dict[str, float] = {
    "sources": 0.25,
    "categories": 0.25,
    "types": 0.15,
    "images": 0.15,
    "size": 0.20,
    "pin_penalty": -0.20,
}


def _num(block: dict[str, Any], key: str) -> float:
    value = block.get(key)
    return 0.0 if value is None else float(value)


def score_block(block: dict[str, Any], targets: ScoreTargets = ScoreTargets()) -> BlockScore:
    """Rank a censused block as a dedup trial region. Pure: no DB, no clock."""
    n_total = _num(block, "n_total")

    sources = min(_num(block, "n_sources") / targets.min_sources, 1.0)

    present = sum(
        1 for cat in REQUIRED_CATEGORY_MAIN if _num(block, f"cat_{cat}") >= targets.min_per_category
    )
    categories = present / len(REQUIRED_CATEGORY_MAIN)

    types = 1.0 if _num(block, "type_prodej") > 0 and _num(block, "type_pronajem") > 0 else 0.0

    with_images = block.get("n_listings_with_images")
    if with_images is None or n_total <= 0:
        images = 0.0
    else:
        images = min((float(with_images) / n_total) / targets.min_image_share, 1.0)

    if n_total <= 0:
        size = 0.0
    elif n_total < targets.size_min:
        size = n_total / targets.size_min
    elif n_total > targets.size_max:
        size = targets.size_max / n_total
    else:
        size = 1.0

    pin_penalty = min(_num(block, "shared_pin_listings") / n_total, 1.0) if n_total > 0 else 0.0

    total = (
        WEIGHTS["sources"] * sources
        + WEIGHTS["categories"] * categories
        + WEIGHTS["types"] * types
        + WEIGHTS["images"] * images
        + WEIGHTS["size"] * size
        + WEIGHTS["pin_penalty"] * pin_penalty
    )
    return BlockScore(
        total=max(0.0, min(1.0, total)),
        sources=sources,
        categories=categories,
        types=types,
        images=images,
        size=size,
        pin_penalty=pin_penalty,
    )


# --- rendering -------------------------------------------------------------------------

TABLE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("grain", "grain", 7),
    ("block_code", "code", 9),
    ("block_name", "name", 22),
    ("n_total", "total", 7),
    ("n_active", "active", 7),
    ("n_sources", "src", 4),
    ("cat_byt", "byt", 7),
    ("cat_dum", "dum", 7),
    ("cat_pozemek", "pozem", 7),
    ("cat_komercni", "komer", 7),
    ("type_prodej", "prodej", 7),
    ("type_pronajem", "najem", 7),
    ("with_area", "area", 7),
    ("with_street", "street", 7),
    ("n_listings_with_images", "imgs", 7),
    ("n_listings_with_clip", "clip", 7),
    ("shared_pin_listings", "pinsh", 7),
    ("raw_inblock_pairs", "pairs", 13),
    ("score", "score", 6),
)


def _cell(value: Any, width: int) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.3f}"
    else:
        text = str(value)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def render_table(rows: Sequence[dict[str, Any]]) -> str:
    """Fixed-width ranked table, one line per block."""
    header = " ".join(label.ljust(width) for _, label, width in TABLE_COLUMNS).rstrip()
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            " ".join(_cell(row.get(key), width) for key, _, width in TABLE_COLUMNS).rstrip()
        )
    return "\n".join(lines)


# --- DB layer --------------------------------------------------------------------------


def _rows_as_dicts(cur: Any) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _run(conn: Any, sql: str, params: dict[str, Any], timeout_ms: int) -> list[dict[str, Any]]:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params)
            return _rows_as_dicts(cur)


def fetch_coverage(conn: Any, *, timeout_ms: int) -> dict[str, Any]:
    rows = _run(conn, CENSUS_COVERAGE_SQL, {}, timeout_ms)
    return rows[0] if rows else {}


def fetch_blocks(
    conn: Any, *, grain: str, min_n: int, max_n: int, top: int, timeout_ms: int
) -> list[dict[str, Any]]:
    sql = BLOCK_SQL_BY_GRAIN[grain]
    rows = _run(conn, sql, block_params(min_n=min_n, max_n=max_n, top=top), timeout_ms)
    for row in rows:
        row["grain"] = grain
    return rows


def fetch_detail(
    conn: Any, *, grain: str, block_code: int, pin_share_min: int, timeout_ms: int
) -> dict[str, Any]:
    params = detail_params(grain=grain, block_code=block_code, pin_share_min=pin_share_min)
    rows = _run(conn, CENSUS_DETAIL_SQL, params, timeout_ms)
    return rows[0] if rows else {}


# --- mode entry ------------------------------------------------------------------------

TIMEOUT_S_MAX = 900

ARG_DEFAULTS: dict[str, int] = {
    "min_n": 800,
    "max_n": 8000,
    "top": 60,
    "detail_top": 12,
    "timeout_s": 540,
    "pin_share_min": 10,
}


def parse_census_args(args: dict[str, str]) -> dict[str, int]:
    """Coerce the lane's k=v strings into the census parameters. Unknown keys are fatal."""
    unknown = sorted(set(args) - set(ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown census arg(s) {', '.join(unknown)}; known: {', '.join(sorted(ARG_DEFAULTS))}"
        )
    out = dict(ARG_DEFAULTS)
    for key, raw in args.items():
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"census arg {key} must be an integer, got {raw!r}") from None
    if out["min_n"] > out["max_n"]:
        raise ValueError("min_n must not exceed max_n")
    for key in ("top", "detail_top", "timeout_s"):
        if out[key] <= 0:
            raise ValueError(f"census arg {key} must be positive")
    # timeout_s is the one workflow-supplied value that reaches SQL as text (SET LOCAL takes no
    # parameter), so it is clamped here as well as coerced.
    if not 1 <= out["timeout_s"] <= TIMEOUT_S_MAX:
        raise ValueError(f"census arg timeout_s must be between 1 and {TIMEOUT_S_MAX}")
    return out


def run_census(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Any
) -> dict[str, Any]:
    params = parse_census_args(args)
    timeout_ms = params["timeout_s"] * 1000
    timings: dict[str, float] = {}
    grains: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, Any] = {}
    errors: list[str] = []

    out_path = Path(out_dir) / "census.json"
    payload: dict[str, Any] = {
        "parameters": params,
        "timings": timings,
        "coverage": coverage,
        "grains": grains,
        "errors": errors,
        "ranking": [],
        "table": "",
    }

    def flush() -> None:
        write_json(out_path, payload)

    conn = conn_factory()
    try:
        started = time.monotonic()
        try:
            coverage.update(fetch_coverage(conn, timeout_ms=timeout_ms))
        except Exception as exc:  # noqa: BLE001 — a denominator is nice-to-have, not the census
            errors.append(f"coverage: {type(exc).__name__}: {exc}")
        timings["coverage_s"] = round(time.monotonic() - started, 3)
        flush()

        for grain in GRAINS:
            started = time.monotonic()
            blocks = fetch_blocks(
                conn,
                grain=grain,
                min_n=params["min_n"],
                max_n=params["max_n"],
                top=params["top"],
                timeout_ms=timeout_ms,
            )
            timings[f"blocks_{grain}_s"] = round(time.monotonic() - started, 3)
            grains[grain] = blocks
            # Each grain's pass costs minutes of production DB time; land it before spending
            # more, so a later timeout cannot throw away what already succeeded.
            flush()

        detail_started = time.monotonic()
        for grain, blocks in grains.items():
            for block in blocks[: params["detail_top"]]:
                block["has_detail"] = False
                try:
                    detail = fetch_detail(
                        conn,
                        grain=grain,
                        block_code=block["block_code"],
                        pin_share_min=params["pin_share_min"],
                        timeout_ms=timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001 — one slow block must not void the rest
                    block["detail_error"] = f"{type(exc).__name__}: {exc}"
                    errors.append(f"detail {grain}/{block['block_code']}: {block['detail_error']}")
                    continue
                block.update(detail)
                block["has_detail"] = bool(detail)
        timings["detail_s"] = round(time.monotonic() - detail_started, 3)
        flush()
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    ranking: list[dict[str, Any]] = []
    for blocks in grains.values():
        for block in blocks:
            if not block.get("has_detail"):
                continue
            score = score_block(block)
            block["score"] = score.total
            block["score_parts"] = score.as_dict()
            ranking.append(block)
    ranking.sort(key=lambda b: (-float(b["score"]), -float(b["n_total"] or 0)))

    table = render_table(ranking)
    print(table)

    payload["ranking"] = [
        {
            "grain": b["grain"],
            "block_code": b["block_code"],
            "block_name": b["block_name"],
            "score": b["score"],
            "score_parts": b["score_parts"],
        }
        for b in ranking
    ]
    payload["table"] = table
    flush()
    return {
        "blocks_town": len(grains.get("town", [])),
        "blocks_quarter": len(grains.get("quarter", [])),
        "detailed": len(ranking),
        "errors": errors,
        "best": ranking[0]["block_name"] if ranking else None,
        "best_code": ranking[0]["block_code"] if ranking else None,
        "timings": timings,
        "census_json": str(out_path),
    }
