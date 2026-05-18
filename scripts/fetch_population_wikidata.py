"""Pull latest Czech-municipality populations from Wikidata.

Wikidata's `population (P1082)` property carries one or more readings
per Czech municipality, each timestamped via the `point in time
(P585)` qualifier. The underlying source is ČSÚ (Czech statistical
office) — Wikidata mirrors it and exposes a public, auth-free SPARQL
endpoint at https://query.wikidata.org/sparql, which is the path of
least resistance for an automated fetcher.

Inputs
------
`data/obce_v_datech_2025.csv` — the curated city list (columns
`Město`, `Kraj`). Same file the seed reads, so the population set
stays in lockstep with the indexes.

Outputs
-------
`data/csu_population_<year>.csv` — CSV with columns
`name,kraj_name,population,as_of_year`. The seed script
(`seed_curated_cities.py`) already picks this file up on present and
upserts into `city_population`.

Matching
--------
The fetcher pulls every Czech municipality from Wikidata (one HTTPS
round-trip, ~6000 rows), then matches each curated city by
`(label_cs, kraj_label)`. A Czech name + kraj context is enough to
disambiguate every entry on the 206-city curated list — the ambiguous
cases ("Lipová" appears in 4 krajs) all resolve once the kraj is
known. Misses are logged and the row is skipped; the seed treats
missing population as `NULL` so a partial cover is still valid.

Run modes
---------
- `python scripts/fetch_population_wikidata.py`               — emit CSV.
- `python scripts/fetch_population_wikidata.py --year 2024`   — pick the
   snapshot closest to (but not after) that year's January 1.

Workflow trigger: `.github/workflows/refresh_population.yml`. The
operator clicks Run, the CSV is regenerated and committed back to the
branch, the next seed run picks it up.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
import unicodedata
from pathlib import Path

import requests

LOG = logging.getLogger("fetch-population")
ROOT = Path(__file__).resolve().parent.parent
CURATED_CSV_PATH = ROOT / "data" / "obce_v_datech_2025.csv"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Wikidata Q-ids referenced in the SPARQL. Listed here for code
# readers; the query itself is self-contained.
#   Q213       — Czech Republic (country)
#   Q5153359   — municipality of the Czech Republic
#   Q15284     — municipality (superclass, fallback)

SPARQL_QUERY = """\
SELECT ?city ?cityLabel ?krajLabel ?population ?date WHERE {
  ?city wdt:P17 wd:Q213 .
  { ?city wdt:P31/wdt:P279* wd:Q5153359 . }
  UNION
  { ?city wdt:P31/wdt:P279* wd:Q15284 . FILTER EXISTS { ?city wdt:P17 wd:Q213 . } }
  ?city p:P1082 ?popStmt .
  ?popStmt ps:P1082 ?population .
  OPTIONAL { ?popStmt pq:P585 ?date . }
  OPTIONAL {
    ?city wdt:P131* ?ancestor .
    ?ancestor wdt:P31 wd:Q5153437 .
    BIND(?ancestor AS ?kraj)
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "cs". }
}
"""


def _slug(s: str) -> str:
    """Diacritics-stripped lowercase form for fuzzy joins."""
    norm = unicodedata.normalize("NFD", s)
    return "".join(c for c in norm if unicodedata.category(c) != "Mn").lower().strip()


def fetch_wikidata_rows() -> list[dict]:
    """Run the SPARQL query and return the raw bindings list."""
    LOG.info("Querying Wikidata SPARQL endpoint")
    resp = requests.get(
        WIKIDATA_SPARQL,
        params={"query": SPARQL_QUERY, "format": "json"},
        headers={
            "User-Agent": (
                "sreality-population-seed/1.0 "
                "(https://github.com/waiff/sreality)"
            ),
            "Accept": "application/sparql-results+json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    bindings = data.get("results", {}).get("bindings", [])
    LOG.info("Got %d raw bindings from Wikidata", len(bindings))
    return bindings


def reduce_to_latest(
    bindings: list[dict],
    cutoff_year: int,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Group by (city_slug, kraj_slug), keep the latest population
    reading at or before `cutoff_year`-01-01. Returns the same
    `{(name, kraj_name): (population, as_of_year)}` shape the seed's
    `read_population_csv` consumes.

    Pre-cutoff records win over later ones so a 2030 hypothetical
    can't outpace the 2024 official. When two readings share the
    latest year, the one with the higher precision date wins (date >
    year-only).
    """
    best: dict[tuple[str, str], dict] = {}
    cutoff = dt.date(cutoff_year, 12, 31)
    for row in bindings:
        try:
            name = row["cityLabel"]["value"].strip()
            kraj = row.get("krajLabel", {}).get("value", "").strip()
            pop = int(float(row["population"]["value"]))
        except (KeyError, ValueError):
            continue
        if not name or not kraj or pop <= 0:
            continue
        date_str = row.get("date", {}).get("value", "")
        # Wikidata serialises dates as `2024-01-01T00:00:00Z`. Year-only
        # readings come back as `2024-00-00T00:00:00Z` which datetime
        # refuses; fall back to parsing the year string by hand.
        as_of = _parse_year(date_str)
        if as_of is None:
            continue
        if as_of > cutoff_year:
            continue
        key = (_slug(name), _slug(kraj))
        prev = best.get(key)
        if prev is None or as_of > prev["year"]:
            best[key] = {
                "name": name,
                "kraj": kraj,
                "population": pop,
                "year": as_of,
            }
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for entry in best.values():
        out[(entry["name"], entry["kraj"])] = (entry["population"], entry["year"])
    LOG.info("Reduced to %d distinct (city, kraj) pairs", len(out))
    return out


def _parse_year(s: str) -> int | None:
    if not s or len(s) < 4:
        return None
    try:
        return int(s[:4])
    except ValueError:
        return None


def read_curated_cities(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Město") or "").strip()
            kraj = (row.get("Kraj") or "").strip()
            if name and kraj:
                out.append((name, kraj))
    LOG.info("Read %d curated cities from %s", len(out), path)
    return out


def match_curated_to_populations(
    curated: list[tuple[str, str]],
    populations: dict[tuple[str, str], tuple[int, int]],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Match each curated (name, kraj) to a Wikidata population row.

    Matching is diacritics-insensitive. Returns the matched rows
    (suitable for the CSV writer) and the list of misses for logging.
    """
    by_slug = {
        (_slug(name), _slug(kraj)): (name, kraj, pop, year)
        for (name, kraj), (pop, year) in [
            ((k[0], k[1]), v) for k, v in populations.items()
        ]
    }
    matched: list[dict] = []
    misses: list[tuple[str, str]] = []
    for name, kraj in curated:
        key = (_slug(name), _slug(kraj))
        hit = by_slug.get(key)
        if hit is None:
            misses.append((name, kraj))
            continue
        _, _, pop, year = hit
        matched.append({
            "name": name,
            "kraj_name": kraj,
            "population": pop,
            "as_of_year": year,
        })
    LOG.info("Matched %d / %d curated cities (%d misses)",
             len(matched), len(curated), len(misses))
    if misses:
        LOG.warning("Misses: %s", ", ".join(f"{n} ({k})" for n, k in misses[:20]))
    return matched, misses


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: (r["kraj_name"], r["name"]))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["name", "kraj_name", "population", "as_of_year"],
        )
        writer.writeheader()
        writer.writerows(rows_sorted)
    LOG.info("Wrote %d rows to %s", len(rows_sorted), path)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--year", type=int, default=dt.date.today().year,
        help="Latest year of population to accept (default: current year).",
    )
    p.add_argument(
        "--curated-csv", default=str(CURATED_CSV_PATH),
        help="Curated city list CSV (Město / Kraj columns).",
    )
    p.add_argument(
        "--out", default=None,
        help="Output CSV path. Defaults to data/csu_population_<year>.csv.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    curated_path = Path(args.curated_csv)
    out_path = Path(args.out) if args.out else (
        ROOT / "data" / f"csu_population_{args.year}.csv"
    )

    curated = read_curated_cities(curated_path)
    bindings = fetch_wikidata_rows()
    populations = reduce_to_latest(bindings, args.year)
    matched, _misses = match_curated_to_populations(curated, populations)
    write_csv(matched, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
