"""The psycopg side of the resolver: load the claims and the registry inputs, call the pure
core, write the one `listing_location` row.

Connection mode is the drain's `scraper.db.connect_session()` — the SESSION-mode pooler, a
dedicated backend, psycopg3's default `prepare_threshold` — so the statements below are
server-side PREPARED once and reused for every listing in the run. It stays autocommit
("callers manage transactions explicitly"), so every atomic unit is an explicit
`with conn.transaction():` block, and there is still not a session advisory lock anywhere
near this file — the lease is a row-CAS, which is correct on either pooler mode.

**Round trips are the drain's cost model**, not CPU, and the constant is measured: ~75 ms of
network round trip from a GitHub-hosted runner to the Frankfurt pooler, against 0.02-0.5 ms
of server-side work per registry question. So the wall clock is ~99 % latency and ~1 % query,
and the lever is the NUMBER of round trips.

W2-a halves that number by deleting questions rather than by tuning them. Fifteen registry
query kinds become eight:

* parcels went with the rung that could never reach them;
* `nearest_obec_within`, `distance_to_admin_boundary_m`, `cast_obce_for_point` and
  `cast_obce_extent_m` went with the sliver fallback, the rule-2 boundary comparison and the
  ČástObce point lookup — FILL takes the quarter off the bound entity's own chain instead;
* `pin_clusters` went with the collision epoch;
* `admin_unit_by_code` and `admin_unit` FOLDED into `admin_chain`, which now returns the
  unit itself ahead of its ancestors, so "this unit and its chain" is one trip and not three.

The five policy/constant loaders went with the tables they read. What is left is four
point-free questions the run cache shares corpus-wide (name, code, street, address point)
and two point-keyed ones (`containing_obec`, `in_czechia_polygon`) that `warm_points` asks
once per SLICE. The ~62 % cache plateau was those point-keyed questions; with the other
three gone, the surviving misses are two per distinct pin.

The registry view here answers exactly the questions `types.RegistryView` declares, so the
pure core cannot reach past it into SQL. `purpose IN ('pip','authoritative')` is deliberate:
containment wants the `ST_Subdivide`d `pip` geometries, and preferring `pip` when rows exist
degrades to the authoritative polygon when the boundary loader has not populated them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from location_data.resolver.types import (
    AddressPoint,
    AdminUnit,
    Claim,
    Street,
)

_CURRENT_REGISTRY_SQL = "SELECT id, label FROM registry_versions WHERE is_current LIMIT 1"

# ONE projection, ONE admissibility predicate — the row unpacking in `_claim` is positional,
# so a second hand-written column list is a silent mis-mapping waiting to happen
# (`test_resolver_jobs.test_the_claims_select_maps_onto_claim_positionally` pins it).
#
# THE LICENCE RAIL LIVES HERE (W2-a). Migration 384 enforced it with three CHECK constraints
# on the stores of record — a `position_licence_class` column on each projection and on the
# resolution, each constrained `<> 'ephemeral_display_only'`. `listing_location` has no such
# column, because the guard moved one step upstream and became stronger: a Mapy-class
# coordinate is not refused at write time, it is never READ.
_ADMISSIBLE_LICENCE_CLASSES = "('portal', 'operator')"

# AND SO DOES THE CONTRACT-VERSION RAIL, for the same reason and in the same predicate.
# `location_claims` is append-only and its fingerprint hashes `extractor_version`, so a
# contract BUMP does not supersede the old version's rows — it inserts new ones beside them.
# W1-c bumped all nine contracts at once, which means every listing whose body has not
# changed since carries two claims for the same fact, and the SUPERSEDED one has the LOWER
# id: it would win every "first admissible claim of this type" tie in BIND and FILL, and the
# resolver would serve the retired contract's answer indefinitely.
#
# Filtering at READ is what makes that a cleanup rather than a correctness step: the rows
# stay on disk (nothing in this program deletes evidence in place) and W2-b's migration
# deletes the inactive-entry claims at its leisure. `is_active` lives on the contract HEADER,
# one per source, so the EXISTS is two primary-key hops per claim row.
#
# Operator claims carry NO contract entry by construction
# (`location_data/operator_corrections.py` writes `contract_entry_id NULL` +
# `licence_class 'operator'`), so they are named explicitly rather than let through by a
# NULL-tolerant join — a portal claim that somehow lost its entry id must NOT be admitted.
_ACTIVE_CONTRACT_ENTRY = """
       (c.contract_entry_id IS NULL AND c.licence_class = 'operator'
        OR EXISTS (SELECT 1 FROM portal_contract_entries pce
                     JOIN portal_contracts pc ON pc.id = pce.contract_id
                    WHERE pce.id = c.contract_entry_id AND pc.is_active))"""

_CLAIMS_SELECT = f"""
SELECT id, listing_id, source, claim_type::text, surface::text, extraction_method::text,
       licence_class::text, first_observed_at, value_text, value_num,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_Y(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_X(value_geom) END,
       value_jsonb, declared_precision_label, declared_radius_m,
       blur_evidence::text, claim_confidence::text, subject_scoped
  FROM location_claims c
 WHERE c.licence_class IN {_ADMISSIBLE_LICENCE_CLASSES}
   AND{_ACTIVE_CONTRACT_ENTRY}
"""

_CLAIMS_SQL = _CLAIMS_SELECT + "   AND c.listing_id = %s\n ORDER BY c.id"

# The whole SLICE in one query instead of one per listing; the per-listing order is still
# `id`, which is what `core.resolve` re-sorts on anyway.
_CLAIMS_BULK_SQL = (
    _CLAIMS_SELECT + "   AND c.listing_id = ANY(%s::bigint[])\n ORDER BY c.listing_id, c.id"
)

_SOURCES_BULK_SQL = "SELECT id, source FROM listings WHERE id = ANY(%s::bigint[])"

# ------------------------------------------------------------------ the registry reads

_ADDRESS_POINT_COLUMNS = """
       ap.kod_adm, ap.obec_unit_id, ap.obec_kod, ap.psc,
       ST_Y(ap.geom), ST_X(ap.geom), ap.ulice_kod, s.name_norm, s.name,
       ap.cislo_domovni, ap.cislo_orientacni, ap.znak_orientacniho,
       ap.cast_obce_unit_id, ap.cast_obce_kod"""

_ADDRESS_POINT_SQL = f"""
SELECT {_ADDRESS_POINT_COLUMNS}
  FROM ruian_address_points ap
  LEFT JOIN ruian_streets s ON s.id = ap.street_id
 WHERE ap.kod_adm = %s AND ap.valid_to IS NULL
"""

# The obec is addressed by UNIT ID, not by `obec_kod`. Both columns exist on every row and
# are 1:1, but only `obec_unit_id` is indexed: `ruian_ap_obec_hn (obec_unit_id,
# cislo_domovni)` is exactly this lookup's index and `obec_kod` has none at all. Measured on
# the live 3,020,222-row mirror, house number only, no street: `obec_kod` planned a parallel
# scan of the whole PK — 21,494 ms, 2,954,453 buffers. The form below touches 424 buffers.
# `u.level = %s::ruian_level` casts the PARAMETER (not the column), so
# `ruian_admin_units_code (level, code)` keeps both of its columns.
#
# `IN (...)` over EVERY unit row carrying that code, not `= (SELECT ... valid_to IS NULL
# LIMIT 1)`: `ruian_admin_units` is SCD-2, so a code can have a closed row and an open one,
# and an address point still points at the row that was current when it was loaded.
_OBEC_UNIT_ID_SUBQUERY = """
    (SELECT u.id FROM ruian_admin_units u
      WHERE u.level = 'obec'::ruian_level AND u.code = %s)
"""

_ADDRESS_POINTS_BY_NUMBER_SQL = f"""
SELECT {_ADDRESS_POINT_COLUMNS}
  FROM ruian_address_points ap
  LEFT JOIN ruian_streets s ON s.id = ap.street_id
 WHERE ap.obec_unit_id IN {_OBEC_UNIT_ID_SUBQUERY.strip()}
   AND ap.valid_to IS NULL
   AND (%s::text IS NULL OR s.name_norm = %s::text)
   AND (%s::integer IS NULL OR ap.cislo_domovni = %s::integer)
   AND (%s::integer IS NULL OR ap.cislo_orientacni = %s::integer)
 ORDER BY ap.kod_adm
 LIMIT 50
"""

_STREETS_IN_OBEC_SQL = """
SELECT s.code, s.name, s.name_norm, u.code
  FROM ruian_streets s
  JOIN ruian_admin_units u ON u.id = s.obec_unit_id
 WHERE u.code = %s AND u.level = 'obec' AND s.valid_to IS NULL
 ORDER BY s.code
"""

# The unit column list `_admin_unit` unpacks positionally.
_ADMIN_COLUMNS = """
       u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END"""

_ADMIN_BY_NAME_SQL = f"""
SELECT {_ADMIN_COLUMNS}, n.qualifier, n.homonym_count, n.psc_set
  FROM ruian_name_index n
  JOIN ruian_admin_units u ON u.id = n.entity_id AND u.level = n.entity_kind
 WHERE n.registry_version_id = %s
   AND n.name_norm = %s
   AND (cardinality(%s::text[]) = 0 OR u.level::text = ANY(%s::text[]))
   AND u.valid_to IS NULL
 ORDER BY u.level, u.code
"""

# The chain INCLUDING the unit itself, coarsest last. `depth` is what orders it, and it is
# what lets `admin_unit(id)` and `admin_chain(id)` be the same round trip: FILL reads the
# bound entity's own level off row 0 and its ancestors off the rest.
_ADMIN_CHAIN_TAIL = f"""
SELECT {_ADMIN_COLUMNS}, NULL::text, 1, NULL::char(5)[]
  FROM chain c
  JOIN ruian_admin_units u ON u.id = c.id
 ORDER BY c.depth
"""

_ADMIN_CHAIN_SQL = f"""
WITH RECURSIVE chain AS (
  SELECT u.id, u.parent_id, 0 AS depth FROM ruian_admin_units u WHERE u.id = %s
  UNION ALL
  SELECT p.id, p.parent_id, c.depth + 1
    FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
{_ADMIN_CHAIN_TAIL}
"""

_ADMIN_CHAIN_BY_CODE_SQL = f"""
WITH RECURSIVE chain AS (
  SELECT a.id, a.parent_id, 0 AS depth
    FROM (SELECT u.id, u.parent_id FROM ruian_admin_units u
           WHERE u.level = %s::ruian_level AND u.code = %s AND u.valid_to IS NULL
           ORDER BY u.valid_from DESC
           LIMIT 1) a
  UNION ALL
  SELECT p.id, p.parent_id, c.depth + 1
    FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
{_ADMIN_CHAIN_TAIL}
"""

_PSC_OBEC_SQL = """
SELECT DISTINCT obec_kod FROM ruian_address_points
 WHERE psc = %s AND valid_to IS NULL ORDER BY obec_kod
"""

# ------------------------------------------------------ the two point-keyed questions
#
# Both are keyed on the LISTING'S OWN coordinate, so the run cache can never share an answer
# between two listings. Each therefore has exactly ONE shape, taking parallel
# `(idx[], lat[], lon[])` arrays: `warm_points` calls it with the whole slice's points (one
# trip for 250 listings) and `SqlRegistryView` calls the SAME text with a one-element array
# when a point was not warmed. One shape, so the lazy path can never drift from the warmed
# one — which is the property replay depends on.
_PT = "ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)"
_POINTS = "unnest(%s::int[], %s::double precision[], %s::double precision[]) AS p(idx, lat, lon)"

# `purpose IN ('pip','authoritative')` cannot use `ruian_aug_pip_gist (geom) WHERE purpose =
# 'pip'` — an IN-list does not imply the partial index's predicate — so the planner fell back
# to the unpartitioned index and ran ST_Covers against RAW obec polygons: 194 ms and 1,225
# buffers, nearly all of it detoasting geometry the pip pieces exist to avoid. Split into two
# branches under one LIMIT instead: `Limit -> Append` stops at the first row, so the
# authoritative branch is `never executed` whenever a pip piece covers the point. Measured:
# 0.24 ms/point. The authoritative branch stays because boundaries load per unit.
_CONTAINING_OBEC_BRANCH = f"""
    SELECT {_ADMIN_COLUMNS}, NULL::text, 1, NULL::char(5)[]
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s
       AND g.purpose = '{{purpose}}'
       AND u.level = 'obec'
       AND ST_Covers(g.geom, {_PT})
     LIMIT 1
"""

_CONTAINING_OBEC_SQL = f"""
SELECT p.idx, c.*
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    ({_CONTAINING_OBEC_BRANCH.format(purpose="pip")})
    UNION ALL
    ({_CONTAINING_OBEC_BRANCH.format(purpose="authoritative")})
    LIMIT 1
  ) c
"""

_IN_CZ_SQL = f"""
SELECT p.idx, ST_Covers(cz.geom, {_PT})
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    SELECT g.geom FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s AND g.purpose = 'authoritative' AND u.level = 'stat'
     LIMIT 1) cz
"""

# ------------------------------------------------------------------------ the one write

_UPSERT_LISTING_LOCATION_SQL = """
INSERT INTO listing_location AS ll (
    listing_id, geom,
    country_code, kraj_name, okres_name, obec_name, cast_obce_name,
    street_name, house_number_cp, house_number_co, psc,
    kraj_kod, okres_kod, obec_kod, cast_obce_kod, ulice_kod, ruian_adm_kod,
    match_confidence, granularity, uncertainty_radius_m,
    country_status, disputed, pin_shared_by_n,
    resolver_version, resolved_at, claim_set_hash, registry_version)
VALUES (
    %(listing_id)s,
    CASE WHEN %(lat)s::double precision IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
    %(country_code)s, %(kraj_name)s, %(okres_name)s, %(obec_name)s, %(cast_obce_name)s,
    %(street_name)s, %(house_number_cp)s, %(house_number_co)s, %(psc)s,
    %(kraj_kod)s, %(okres_kod)s, %(obec_kod)s, %(cast_obce_kod)s, %(ulice_kod)s,
    %(ruian_adm_kod)s,
    %(match_confidence)s::match_confidence, %(granularity)s::location_granularity,
    %(uncertainty_radius_m)s,
    %(country_status)s::country_status, %(disputed)s, %(pin_shared_by_n)s,
    %(resolver_version)s, now(), decode(%(claim_set_hash)s, 'hex'), %(registry_version)s)
ON CONFLICT (listing_id) DO UPDATE SET
    geom = EXCLUDED.geom, country_code = EXCLUDED.country_code,
    kraj_name = EXCLUDED.kraj_name, okres_name = EXCLUDED.okres_name,
    obec_name = EXCLUDED.obec_name, cast_obce_name = EXCLUDED.cast_obce_name,
    street_name = EXCLUDED.street_name, house_number_cp = EXCLUDED.house_number_cp,
    house_number_co = EXCLUDED.house_number_co, psc = EXCLUDED.psc,
    kraj_kod = EXCLUDED.kraj_kod, okres_kod = EXCLUDED.okres_kod,
    obec_kod = EXCLUDED.obec_kod, cast_obce_kod = EXCLUDED.cast_obce_kod,
    ulice_kod = EXCLUDED.ulice_kod, ruian_adm_kod = EXCLUDED.ruian_adm_kod,
    match_confidence = EXCLUDED.match_confidence, granularity = EXCLUDED.granularity,
    uncertainty_radius_m = EXCLUDED.uncertainty_radius_m,
    country_status = EXCLUDED.country_status, disputed = EXCLUDED.disputed,
    pin_shared_by_n = EXCLUDED.pin_shared_by_n,
    resolver_version = EXCLUDED.resolver_version, resolved_at = now(),
    claim_set_hash = EXCLUDED.claim_set_hash, registry_version = EXCLUDED.registry_version
WHERE ll.listing_id = EXCLUDED.listing_id
"""


def current_registry_version(conn: psycopg.Connection) -> tuple[int, str]:
    with conn.cursor() as cur:
        cur.execute(_CURRENT_REGISTRY_SQL)
        row = cur.fetchone()
    if row is None:
        raise LookupError("no registry_versions row is is_current — load the mirror first")
    return int(row[0]), str(row[1])


def _claim(row: Sequence[Any]) -> Claim:
    return Claim(
        id=row[0], listing_id=row[1], source=row[2], claim_type=row[3], surface=row[4],
        extraction_method=row[5], licence_class=row[6],
        observed_at=row[7], value_text=row[8],
        value_num=None if row[9] is None else float(row[9]),
        lat=None if row[10] is None else float(row[10]),
        lon=None if row[11] is None else float(row[11]),
        value_jsonb=row[12] or {}, declared_precision_label=row[13],
        declared_radius_m=None if row[14] is None else float(row[14]),
        blur_evidence=row[15], claim_confidence=row[16], subject_scoped=row[17],
    )


def load_claims(conn: psycopg.Connection, listing_id: int) -> list[Claim]:
    with conn.cursor() as cur:
        cur.execute(_CLAIMS_SQL, (listing_id,))
        return [_claim(row) for row in cur.fetchall()]


def load_claims_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> dict[int, list[Claim]]:
    """Every claim for a whole slice, grouped by listing. Listings with no claims are absent
    from the mapping, exactly as `load_claims` returns an empty list for them."""
    out: dict[int, list[Claim]] = {}
    if not listing_ids:
        return out
    with conn.cursor() as cur:
        cur.execute(_CLAIMS_BULK_SQL, (list(listing_ids),))
        for row in cur.fetchall():
            out.setdefault(int(row[1]), []).append(_claim(row))
    return out


def sources_bulk(conn: psycopg.Connection, listing_ids: Sequence[int]) -> dict[int, str]:
    """`listings.source` for a whole slice — the one fact a claimless listing cannot read
    off a claim, because it has none."""
    if not listing_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_SOURCES_BULK_SQL, (list(listing_ids),))
        return {int(r[0]): str(r[1]) for r in cur.fetchall()}


def _admin_unit(row: Sequence[Any]) -> AdminUnit:
    path = str(row[5] or "")
    return AdminUnit(
        unit_id=int(row[0]), level=str(row[1]), code=int(row[2]), name=str(row[3]),
        name_norm=str(row[4]), path=path, parent_id=row[6],
        lat=None if row[7] is None else float(row[7]),
        lon=None if row[8] is None else float(row[8]),
        qualifier=row[9], homonym_count=int(row[10] or 1),
        psc_set=tuple(str(p).strip() for p in (row[11] or ())),
        obec_kod=_path_code(path, "b"), okres_kod=_path_code(path, "o"),
        kraj_kod=_path_code(path, "k"),
    )


def _path_code(path: str, prefix: str) -> int | None:
    """The ltree path is `k{kraj}.o{okres}.b{obec}.c{cast_obce}` — level-prefixed NUMERIC
    codes, which is what makes this parse safe (a name label would not even insert)."""
    for label in path.split("."):
        if label.startswith(prefix) and label[1:].isdigit():
            return int(label[1:])
    return None


def _address_point(row: Sequence[Any]) -> AddressPoint:
    return AddressPoint(
        kod_adm=int(row[0]), obec_unit_id=int(row[1]), obec_kod=int(row[2]), psc=str(row[3]),
        lat=None if row[4] is None else float(row[4]),
        lon=None if row[5] is None else float(row[5]),
        ulice_kod=row[6], street_name_norm=row[7], street_name=row[8],
        cislo_domovni=row[9], cislo_orientacni=row[10], znak_orientacniho=row[11],
        cast_obce_unit_id=row[12], cast_obce_kod=row[13],
    )


class QueryStats:
    """Per-query-KIND round trips and wall clock for one run — the run's own log names which
    question spent the budget, which an aggregate-only line could not."""

    __slots__ = ("queries", "seconds", "rows")

    def __init__(self) -> None:
        self.queries: dict[str, int] = {}
        self.seconds: dict[str, float] = {}
        self.rows: dict[str, int] = {}

    def record(self, kind: str, elapsed: float, rows: int) -> None:
        self.queries[kind] = self.queries.get(kind, 0) + 1
        self.seconds[kind] = self.seconds.get(kind, 0.0) + elapsed
        self.rows[kind] = self.rows.get(kind, 0) + rows

    def report(self, limit: int = 8) -> str:
        """The slowest kinds first — total seconds is what a budget is spent in, not the
        per-call average (a 3 ms question asked 20,000 times outranks a 700 ms one asked
        twice)."""
        ranked = sorted(self.seconds.items(), key=lambda kv: -kv[1])[:limit]
        return " ".join(
            f"{kind}(q={self.queries[kind]},{total:.1f}s,"
            f"{1000.0 * total / max(self.queries[kind], 1):.0f}ms)"
            for kind, total in ranked
        )


class SqlRegistryView:
    """`types.RegistryView` over the mirror, pinned to one `registry_version_id`."""

    def __init__(
        self,
        conn: psycopg.Connection,
        registry_version_id: int,
        stats: QueryStats | None = None,
    ) -> None:
        self._conn = conn
        self._version = registry_version_id
        self.stats = stats if stats is not None else QueryStats()

    def _rows(self, kind: str, sql: str, params: tuple) -> list[tuple]:
        started = time.perf_counter()
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.stats.record(kind, time.perf_counter() - started, len(rows))
        return rows

    def address_point(self, kod_adm: int) -> AddressPoint | None:
        rows = self._rows("address_point", _ADDRESS_POINT_SQL, (kod_adm,))
        return _address_point(rows[0]) if rows else None

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> list[AddressPoint]:
        rows = self._rows(
            "address_points_by_number",
            _ADDRESS_POINTS_BY_NUMBER_SQL,
            (obec_kod, street_name_norm, street_name_norm, cislo_domovni, cislo_domovni,
             cislo_orientacni, cislo_orientacni),
        )
        return [_address_point(r) for r in rows]

    def streets_in_obec(self, obec_kod: int) -> list[Street]:
        return [
            Street(code=int(r[0]), name=str(r[1]), name_norm=str(r[2]), obec_kod=int(r[3]))
            for r in self._rows("streets_in_obec", _STREETS_IN_OBEC_SQL, (obec_kod,))
        ]

    def admin_units_by_name(self, name_norm: str, *, levels: Sequence[str] = ()) -> list[AdminUnit]:
        wanted = list(levels)
        return [
            _admin_unit(r)
            for r in self._rows(
                "admin_units_by_name", _ADMIN_BY_NAME_SQL,
                (self._version, name_norm, wanted, wanted),
            )
        ]

    def admin_chain(self, unit_id: int) -> list[AdminUnit]:
        return [
            _admin_unit(r) for r in self._rows("admin_chain", _ADMIN_CHAIN_SQL, (unit_id,))
        ]

    def admin_chain_by_code(self, level: str, code: int) -> list[AdminUnit]:
        return [
            _admin_unit(r)
            for r in self._rows("admin_chain_by_code", _ADMIN_CHAIN_BY_CODE_SQL, (level, code))
        ]

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return [int(r[0]) for r in self._rows("obec_codes_for_psc", _PSC_OBEC_SQL, (psc,))]

    # ---- the two point-keyed questions: ONE array-shaped statement, called here with one
    # point and by `warm_points` with the whole slice. `_bulk` returns {idx: answer}.

    def containing_obec_bulk(
        self, points: Sequence[tuple[float, float]]
    ) -> dict[int, AdminUnit]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows(
            "containing_obec", _CONTAINING_OBEC_SQL,
            (idx, lats, lons, self._version, self._version),
        )
        return {int(r[0]): _admin_unit(r[1:]) for r in rows}

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        return self.containing_obec_bulk([(lat, lon)]).get(0)

    def in_czechia_polygon_bulk(self, points: Sequence[tuple[float, float]]) -> dict[int, bool]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows("in_czechia_polygon", _IN_CZ_SQL, (idx, lats, lons, self._version))
        return {int(r[0]): bool(r[1]) for r in rows if r[1] is not None}

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self.in_czechia_polygon_bulk([(lat, lon)]).get(0)


def _point_arrays(
    points: Sequence[tuple[float, float]]
) -> tuple[list[int], list[float], list[float]]:
    return (
        list(range(len(points))),
        [float(p[0]) for p in points],
        [float(p[1]) for p in points],
    )


class RunCache:
    """Memo for the corpus-constant questions of ONE run, plus its own instrumentation.

    Safe because the mirror it fronts is immutable for the run's lifetime: it is pinned to a
    `registry_version_id`, and a load mints a NEW version rather than editing one. Same
    question, same answer — so the pure core sees exactly what an uncached run would.

    `max_entries` is a memory rail, not a hit-rate policy: at the cap the whole memo is
    dropped and refills. Correctness cannot depend on what is resident.
    """

    __slots__ = ("_values", "_max", "hits", "misses", "seconds", "asked_by_kind",
                 "missed_by_kind")

    def __init__(self, max_entries: int = 250_000) -> None:
        self._values: dict[Any, Any] = {}
        self._max = max_entries
        self.hits = 0
        self.misses = 0
        self.seconds = 0.0
        # Every key is a tuple whose head is the question's name, so the per-KIND hit rate
        # comes for free — and it is the number that says whether a key is too narrow.
        self.asked_by_kind: dict[str, int] = {}
        self.missed_by_kind: dict[str, int] = {}

    def _count(self, key: Any, missed: bool) -> None:
        kind = key[0] if isinstance(key, tuple) and key else str(key)
        self.asked_by_kind[kind] = self.asked_by_kind.get(kind, 0) + 1
        if missed:
            self.missed_by_kind[kind] = self.missed_by_kind.get(kind, 0) + 1

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        try:
            value = self._values[key]
        except KeyError:
            pass
        else:
            self.hits += 1
            self._count(key, missed=False)
            return value
        self.misses += 1
        self._count(key, missed=True)
        started = time.perf_counter()
        value = compute()
        self.seconds += time.perf_counter() - started
        self.put(key, value)
        return value

    def put(self, key: Any, value: Any) -> None:
        """Pre-seed an answer (`warm_points`). Identical to what `get` would have stored, so
        a warmed run and a cold one hand the core the same bytes."""
        if len(self._values) >= self._max:
            self._values.clear()
        self._values[key] = value

    @property
    def hit_rate(self) -> float:
        asked = self.hits + self.misses
        return 0.0 if asked == 0 else self.hits / asked

    def report(self, limit: int = 8) -> str:
        """The kinds that MISS most — a kind with a low hit rate is either keyed on something
        per-listing (warm it per slice) or keyed too narrowly (widen it)."""
        ranked = sorted(self.missed_by_kind.items(), key=lambda kv: -kv[1])[:limit]
        return " ".join(
            f"{kind}(asked={self.asked_by_kind.get(kind, 0)},miss={missed})"
            for kind, missed in ranked
        )


class CachedRegistryView:
    """`types.RegistryView` over a `SqlRegistryView`, memoized for one run (see `RunCache`).

    Every list-returning method hands back a TUPLE, not the cached list: the protocol asks
    for a `Sequence`, and an immutable one cannot be mutated by a caller into poisoning the
    next listing's answer. It forwards by EXPLICIT method, not by `__getattr__`, so a
    question added to `RegistryView` and to `SqlRegistryView` but not here raises
    AttributeError mid-drain rather than quietly falling through.
    """

    __slots__ = ("_inner", "_cache")

    def __init__(self, inner: Any, cache: RunCache) -> None:
        self._inner = inner
        self._cache = cache

    def address_point(self, kod_adm: int) -> AddressPoint | None:
        return self._cache.get(
            ("address_point", kod_adm), lambda: self._inner.address_point(kod_adm)
        )

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> Sequence[AddressPoint]:
        key = ("address_points_by_number", obec_kod, street_name_norm, cislo_domovni,
               cislo_orientacni)
        return self._cache.get(
            key,
            lambda: tuple(
                self._inner.address_points_by_number(
                    obec_kod=obec_kod, street_name_norm=street_name_norm,
                    cislo_domovni=cislo_domovni, cislo_orientacni=cislo_orientacni,
                )
            ),
        )

    def streets_in_obec(self, obec_kod: int) -> Sequence[Street]:
        return self._cache.get(
            ("streets_in_obec", obec_kod), lambda: tuple(self._inner.streets_in_obec(obec_kod))
        )

    def admin_units_by_name(
        self, name_norm: str, *, levels: Sequence[str] = ()
    ) -> Sequence[AdminUnit]:
        """Keyed on the NAME only, with the level narrowing applied in Python.

        The level tuple was part of the key, so one name asked four different ways was four
        cache entries and four round trips for one immutable answer. The unnarrowed query is
        the SUPERSET of every narrowing and its `ORDER BY u.level, u.code` survives a stable
        filter, so the narrowed list is element-for-element what the narrowed query returned.
        """
        wanted = frozenset(levels)
        every = self._cache.get(
            ("admin_units_by_name", name_norm),
            lambda: tuple(self._inner.admin_units_by_name(name_norm)),
        )
        if not wanted:
            return every
        return tuple(unit for unit in every if unit.level in wanted)

    def admin_chain(self, unit_id: int) -> Sequence[AdminUnit]:
        return self._cache.get(
            ("admin_chain", unit_id), lambda: tuple(self._inner.admin_chain(unit_id))
        )

    def admin_chain_by_code(self, level: str, code: int) -> Sequence[AdminUnit]:
        return self._cache.get(
            ("admin_chain_by_code", level, code),
            lambda: tuple(self._inner.admin_chain_by_code(level, code)),
        )

    def obec_codes_for_psc(self, psc: str) -> Sequence[int]:
        return self._cache.get(
            ("obec_codes_for_psc", psc), lambda: tuple(self._inner.obec_codes_for_psc(psc))
        )

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        return self._cache.get(
            ("containing_obec", lat, lon), lambda: self._inner.containing_obec(lat, lon)
        )

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self._cache.get(
            ("in_czechia_polygon", lat, lon), lambda: self._inner.in_czechia_polygon(lat, lon)
        )


def warm_points(
    registry: SqlRegistryView, cache: RunCache, points: Sequence[tuple[float, float]]
) -> None:
    """Answer the TWO point-keyed questions for a WHOLE SLICE in two round trips.

    These are the questions the run cache can never share between listings — the key IS the
    listing's coordinate. Asking them ahead of the slice does not change a single answer:
    `warm_points` writes exactly the key/value `CachedRegistryView` would have computed
    lazily, from the SAME statement text (the `*_bulk` methods are the single-point methods
    with a longer array), so the pure core is handed identical bytes.

    A NULL answer is an answer: it is cached too, or every rural point would re-ask per call.
    """
    if not points:
        return
    coords = sorted(set(points))
    covering = registry.containing_obec_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("containing_obec", lat, lon), covering.get(i))
    in_cz = registry.in_czechia_polygon_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("in_czechia_polygon", lat, lon), in_cz.get(i))


def upsert_listing_locations_bulk(
    conn: psycopg.Connection, rows: Sequence[dict[str, Any]]
) -> None:
    """The slice's answer rows in ONE `executemany`, which psycopg pipelines into one round
    trip. It is the drain's ONLY projection write."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_LISTING_LOCATION_SQL, list(rows))
