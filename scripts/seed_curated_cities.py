"""Seed Phase QUAL data: curated cities + index definitions + values + population.

Run from GitHub Actions (workflow .github/workflows/seed-curated-cities.yml)
or from a local checkout that has both `MAPY_CZ_API_KEY` and
`SUPABASE_DB_URL` in the env.

Steps:
  1. Read data/obce_v_datech_2025.csv (the operator-supplied input).
  2. Geocode each (Mesto, Kraj) pair via Mapy.cz; cache results in
     data/curated_cities_geocoded.json so re-runs are deterministic and
     auditable.
  3. Optionally fetch latest CSU municipality population from
     data/csu_population_2024.csv (committed alongside if available).
  4. Connect to Supabase via SUPABASE_DB_URL and upsert.

Idempotent: a second run with the same CSV bumps source_revision and
adds a fresh batch of city_index_values; cities + definitions + population
upsert in place.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "obce_v_datech_2025.csv"
GEOCODE_CACHE_PATH = ROOT / "data" / "curated_cities_geocoded.json"
POPULATION_CSV_PATH = ROOT / "data" / "csu_population_2024.csv"

LOG = logging.getLogger("seed_curated_cities")


# --- index column → slug / label / category mapping ----------------------
# Order matters: it drives sort_order in city_index_definitions, which the
# UI uses to render the dropdown + the /cities table column order.

INDEX_METADATA: list[dict[str, str]] = [
    # Overall + category aggregates.
    {"header": "Celkové hodnocení - calculated", "slug": "celkove_hodnoceni",
     "label_en": "Overall rating", "category": "overall"},
    {"header": "Zdraví a životní prostředí", "slug": "zdravi_zivotni_prostredi",
     "label_en": "Health and environment", "category": "health_env"},
    {"header": "Materiální zabezpečení a vzdělání", "slug": "material_vzdelani",
     "label_en": "Material conditions and education", "category": "material_edu"},
    {"header": "Vztahy a služby", "slug": "vztahy_sluzby",
     "label_en": "Relations and services", "category": "services_relations"},

    # Health and environment sub-indexes.
    {"header": "Index praktických lékařů", "slug": "prakticti_lekari",
     "label_en": "GPs availability", "category": "sub_index"},
    {"header": "Index dětských lékařů", "slug": "detsti_lekari",
     "label_en": "Pediatricians availability", "category": "sub_index"},
    {"header": "Index dojezdu do nemocnice", "slug": "dojezd_nemocnice",
     "label_en": "Hospital access time", "category": "sub_index"},
    {"header": "Index lékáren", "slug": "lekarny",
     "label_en": "Pharmacies availability", "category": "sub_index"},
    {"header": "Index průměrné délky života", "slug": "prumerna_delka_zivota",
     "label_en": "Life expectancy", "category": "sub_index"},
    {"header": "Index znečištění ovzduší", "slug": "znecisteni_ovzdusi",
     "label_en": "Air quality", "category": "sub_index"},
    {"header": "Index znečišťovatelů", "slug": "znecistovatele",
     "label_en": "Polluters proximity", "category": "sub_index"},
    {"header": "Index chráněných území", "slug": "chranena_uzemi",
     "label_en": "Protected areas", "category": "sub_index"},

    # Material conditions sub-indexes.
    {"header": "Index nezaměstnanosti", "slug": "nezamestnanost",
     "label_en": "Employment rate", "category": "sub_index"},
    {"header": "Index nabídky pracovních míst", "slug": "pracovni_mista",
     "label_en": "Job offers", "category": "sub_index"},
    {"header": "Index finanční dostupnosti bydlení", "slug": "dostupnost_bydleni",
     "label_en": "Housing affordability", "category": "sub_index"},
    {"header": "Index hmotné nouze", "slug": "hmotna_nouze",
     "label_en": "Material need recipients", "category": "sub_index"},
    {"header": "Index exekucí", "slug": "exekuce",
     "label_en": "Foreclosure rate", "category": "sub_index"},
    {"header": "Index kapacity mateřských škol", "slug": "kapacita_ms",
     "label_en": "Kindergarten capacity", "category": "sub_index"},
    {"header": "Index kapacity základních škol", "slug": "kapacita_zs",
     "label_en": "Primary school capacity", "category": "sub_index"},
    {"header": "Index kvalitních středních škol", "slug": "kvalitni_ss",
     "label_en": "Quality secondary schools", "category": "sub_index"},

    # Services and relations sub-indexes.
    {"header": "Index marketů", "slug": "markety",
     "label_en": "Grocery stores", "category": "sub_index"},
    {"header": "Index bankomatů", "slug": "bankomaty",
     "label_en": "ATM availability", "category": "sub_index"},
    {"header": "Index restaurací", "slug": "restaurace",
     "label_en": "Restaurants", "category": "sub_index"},
    {"header": "Index kin", "slug": "kina",
     "label_en": "Cinemas", "category": "sub_index"},
    {"header": "Index digitalizace", "slug": "digitalizace",
     "label_en": "Digital services", "category": "sub_index"},
    {"header": "Index silniční sítě", "slug": "silnicni_sit",
     "label_en": "Road network", "category": "sub_index"},
    {"header": "Index železniční dopravy", "slug": "zeleznicni_doprava",
     "label_en": "Rail transport", "category": "sub_index"},
    {"header": "Index sounáležitosti", "slug": "sounalezitost",
     "label_en": "Community belonging", "category": "sub_index"},
    {"header": "Index zájmu o obecní a krajské volby", "slug": "zajem_volby",
     "label_en": "Voter turnout", "category": "sub_index"},
    {"header": "Index bezpečnosti", "slug": "bezpecnost",
     "label_en": "Safety", "category": "sub_index"},
    {"header": "Index hazardu", "slug": "hazard",
     "label_en": "Gambling absence", "category": "sub_index"},
    {"header": "Index stěhování mladých", "slug": "stehovani_mladych",
     "label_en": "Young residents retention", "category": "sub_index"},
    {"header": "Index přírůstku obyvatelstva", "slug": "prirustek_obyvatel",
     "label_en": "Population growth", "category": "sub_index"},
]

INDEX_BY_HEADER: dict[str, dict[str, str]] = {
    m["header"]: m for m in INDEX_METADATA
}


@dataclass
class CsvRow:
    name: str
    kraj_name: str
    values: dict[str, float]  # slug → value


def read_csv(path: Path) -> list[CsvRow]:
    rows: list[CsvRow] = []
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            name = (row.get("Město") or "").strip()
            kraj = (row.get("Kraj") or "").strip()
            if not name or not kraj:
                LOG.warning("CSV row %d has empty name or kraj, skipping", row_idx)
                continue
            values: dict[str, float] = {}
            for header, cell in row.items():
                if header in ("Město", "Kraj"):
                    continue
                meta = INDEX_BY_HEADER.get(header)
                if meta is None:
                    LOG.warning("Unknown column header %r in row %d", header, row_idx)
                    continue
                if cell is None or cell.strip() == "":
                    continue
                try:
                    values[meta["slug"]] = float(cell.replace(",", "."))
                except ValueError:
                    LOG.warning(
                        "Bad numeric value %r for %s in row %d",
                        cell, header, row_idx,
                    )
            rows.append(CsvRow(name=name, kraj_name=kraj, values=values))
    LOG.info("Read %d city rows from %s", len(rows), path)
    return rows


# --- geocoding -----------------------------------------------------------


def load_geocode_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_geocode_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def cache_key(name: str, kraj: str) -> str:
    return f"{name}||{kraj}"


def geocode_one(name: str, kraj: str) -> dict[str, Any]:
    """Geocode one (city, kraj) pair via Mapy.cz.

    Returns the first item with type == 'regional.municipality' if any,
    else the first item ranked by the source's relevance. Raises if no
    items returned.
    """
    from scraper.geocoding import geocode, GeocodingError, _TYPE_SPECIFICITY  # noqa: E501

    query = f"{name}, {kraj}, Česká republika"
    try:
        result = geocode(query, lang="cs", limit=5)
    except GeocodingError as exc:
        raise RuntimeError(f"Geocoding failed for {query!r}: {exc}") from exc

    # Prefer a municipality-level hit when available; the helper picks
    # the highest-specificity item, which for a city query is usually
    # `regional.municipality`. Confidence "low" is acceptable here
    # because municipality is the maximum specificity for a city query.
    is_muni = result.matched_type == "regional.municipality"
    radius_m = _radius_from_bbox(result.bbox) if result.bbox else 5000
    return {
        "name": name,
        "kraj_name": kraj,
        "lat": result.lat,
        "lng": result.lng,
        "matched_type": result.matched_type,
        "matched_address": result.matched_address,
        "default_radius_m": radius_m,
        "is_municipality": is_muni,
        "raw_query": query,
    }


def _radius_from_bbox(bbox: tuple[float, float, float, float]) -> int:
    """Approximate the city's footprint radius from its Mapy.cz bbox.

    `bbox` is (west, south, east, north) in degrees. Convert the
    diagonal half-length to metres assuming ~111km per degree of
    latitude and cos(lat) for longitude scaling. Clamp to 2-25km.
    """
    import math
    west, south, east, north = bbox
    lat_mid = (north + south) / 2
    dlat_m = (north - south) * 111000.0
    dlng_m = (east - west) * 111000.0 * math.cos(math.radians(lat_mid))
    half_diag = 0.5 * math.hypot(dlat_m, dlng_m)
    return max(2000, min(25000, int(half_diag)))


def geocode_all(rows: list[CsvRow], cache_path: Path) -> dict[str, dict[str, Any]]:
    cache = load_geocode_cache(cache_path)
    new_hits = 0
    for row in rows:
        key = cache_key(row.name, row.kraj_name)
        if key in cache:
            continue
        LOG.info("Geocoding %s, %s ...", row.name, row.kraj_name)
        cache[key] = geocode_one(row.name, row.kraj_name)
        new_hits += 1
        # Save after each hit so a network blip doesn't lose progress.
        save_geocode_cache(cache_path, cache)
    LOG.info("Geocoded %d new entries; cache now has %d", new_hits, len(cache))
    return cache


# --- population ----------------------------------------------------------


def read_population_csv(path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    """Returns {(name, kraj_name): (population, as_of_year)}.

    Optional: if the file is missing the seed completes without
    populating city_population — operator can refresh later.
    """
    out: dict[tuple[str, str], tuple[int, int]] = {}
    if not path.exists():
        LOG.info("No population CSV at %s — skipping population seed", path)
        return out
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or row.get("Město") or "").strip()
            kraj = (row.get("kraj_name") or row.get("Kraj") or "").strip()
            pop_raw = (row.get("population") or "").strip()
            year_raw = (row.get("as_of_year") or "2024").strip()
            if not name or not kraj or not pop_raw:
                continue
            try:
                pop = int(pop_raw.replace(" ", "").replace(",", ""))
                year = int(year_raw)
            except ValueError:
                continue
            out[(name, kraj)] = (pop, year)
    LOG.info("Read %d population rows", len(out))
    return out


# --- DB writes -----------------------------------------------------------


def upsert_definitions(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for order, meta in enumerate(INDEX_METADATA):
            cur.execute(
                """
                insert into city_index_definitions
                  (index_name, label_cs, label_en, category, sort_order)
                values (%s, %s, %s, %s, %s)
                on conflict (index_name) do update set
                  label_cs   = excluded.label_cs,
                  label_en   = excluded.label_en,
                  category   = excluded.category,
                  sort_order = excluded.sort_order
                """,
                (meta["slug"], meta["header"], meta["label_en"],
                 meta["category"], order),
            )
    LOG.info("Upserted %d index definitions", len(INDEX_METADATA))


def upsert_cities(
    conn: psycopg.Connection,
    rows: list[CsvRow],
    geocodes: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], int]:
    """Returns {(name, kraj): city_id}."""
    ids: dict[tuple[str, str], int] = {}
    with conn.cursor() as cur:
        for row in rows:
            geo = geocodes.get(cache_key(row.name, row.kraj_name))
            if geo is None:
                LOG.error("No geocode for %s, %s — skipping", row.name, row.kraj_name)
                continue
            cur.execute(
                """
                insert into curated_cities
                  (name, kraj_name, centroid, default_radius_m,
                   source, source_confidence)
                values (
                  %s, %s,
                  st_setsrid(st_makepoint(%s, %s), 4326)::geography,
                  %s, %s, %s
                )
                on conflict (name, kraj_name) do update set
                  centroid          = excluded.centroid,
                  default_radius_m  = excluded.default_radius_m,
                  source            = excluded.source,
                  source_confidence = excluded.source_confidence
                returning id
                """,
                (
                    row.name, row.kraj_name,
                    geo["lng"], geo["lat"],
                    geo.get("default_radius_m", 5000),
                    geo.get("source", "mapy_cz"),
                    geo.get("matched_type"),
                ),
            )
            (city_id,) = cur.fetchone()
            ids[(row.name, row.kraj_name)] = city_id
    LOG.info("Upserted %d curated cities", len(ids))
    return ids


def insert_revision_and_values(
    conn: psycopg.Connection,
    rows: list[CsvRow],
    city_ids: dict[tuple[str, str], int],
    source_filename: str,
    uploaded_by: str | None,
) -> int:
    raw_rows = [{"name": r.name, "kraj_name": r.kraj_name, "values": r.values}
                for r in rows]
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into city_index_revisions
              (uploaded_by, source_filename, row_count, raw_rows)
            values (%s, %s, %s, %s::jsonb)
            returning source_revision
            """,
            (uploaded_by, source_filename, len(rows), json.dumps(raw_rows)),
        )
        (revision,) = cur.fetchone()

        values_payload: list[tuple[int, int, str, float]] = []
        for row in rows:
            cid = city_ids.get((row.name, row.kraj_name))
            if cid is None:
                continue
            for slug, value in row.values.items():
                values_payload.append((cid, revision, slug, value))

        with cur.copy(
            "copy city_index_values (city_id, source_revision, index_name, value) "
            "from stdin"
        ) as copy:
            for record in values_payload:
                copy.write_row(record)

    LOG.info("Inserted revision %d with %d values", revision, len(values_payload))
    return revision


def upsert_population(
    conn: psycopg.Connection,
    population: dict[tuple[str, str], tuple[int, int]],
    city_ids: dict[tuple[str, str], int],
) -> int:
    written = 0
    with conn.cursor() as cur:
        for key, (pop, year) in population.items():
            cid = city_ids.get(key)
            if cid is None:
                continue
            cur.execute(
                """
                insert into city_population (city_id, as_of_year, population, source)
                values (%s, %s, %s, 'csu')
                on conflict (city_id, as_of_year) do update set
                  population = excluded.population,
                  loaded_at  = now()
                """,
                (cid, year, pop),
            )
            written += 1
    LOG.info("Upserted population for %d cities", written)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="seed_curated_cities")
    p.add_argument("--csv", default=str(CSV_PATH),
                   help="Path to obce_v_datech_*.csv")
    p.add_argument("--population-csv", default=str(POPULATION_CSV_PATH),
                   help="Path to csu_population_*.csv (optional)")
    p.add_argument("--geocode-cache", default=str(GEOCODE_CACHE_PATH))
    p.add_argument("--dry-run", action="store_true",
                   help="Geocode + plan, but don't write to the DB")
    p.add_argument("--source-filename", default=None,
                   help="Override the source_filename written to "
                        "city_index_revisions (defaults to the CSV's basename)")
    p.add_argument("--uploaded-by", default=os.environ.get("GITHUB_ACTOR"),
                   help="Optional uploader name (defaults to $GITHUB_ACTOR)")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    csv_path = Path(args.csv)
    if not csv_path.exists():
        LOG.error("CSV not found at %s", csv_path)
        return 2

    sys.path.insert(0, str(ROOT))  # so `from scraper.geocoding import ...` works
    rows = read_csv(csv_path)
    if not rows:
        LOG.error("CSV produced zero rows")
        return 2

    # Sanity: every column we found should be in INDEX_METADATA, else
    # the operator added a new column without updating this script.
    seen_slugs: set[str] = set()
    for r in rows:
        seen_slugs.update(r.values.keys())
    known_slugs = {m["slug"] for m in INDEX_METADATA}
    unknown = seen_slugs - known_slugs
    if unknown:
        LOG.error("CSV has unknown index slugs %s — update INDEX_METADATA "
                  "in this script first", sorted(unknown))
        return 2

    cache_path = Path(args.geocode_cache)
    try:
        geocodes = geocode_all(rows, cache_path)
    except RuntimeError as exc:
        LOG.error("Geocoding failed: %s", exc)
        return 2

    missing = [r for r in rows
               if cache_key(r.name, r.kraj_name) not in geocodes]
    if missing:
        LOG.error("Missing geocodes for %d rows: %s",
                  len(missing), [(r.name, r.kraj_name) for r in missing[:5]])
        return 2

    population = read_population_csv(Path(args.population_csv))

    if args.dry_run:
        LOG.info("Dry-run: would seed %d cities, %d index values, %d "
                 "population rows", len(rows), sum(len(r.values) for r in rows),
                 len(population))
        return 0

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        LOG.error("SUPABASE_DB_URL is not set")
        return 2

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            upsert_definitions(conn)
            city_ids = upsert_cities(conn, rows, geocodes)
            insert_revision_and_values(
                conn, rows, city_ids,
                source_filename=args.source_filename or csv_path.name,
                uploaded_by=args.uploaded_by,
            )
            upsert_population(conn, population, city_ids)

    LOG.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
