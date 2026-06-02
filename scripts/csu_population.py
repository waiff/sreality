"""Parse ČSÚ DataStat JSON-stat population exports and match to curated cities.

Download the source JSON from the Czech Statistical Office DataStat portal:

    https://data.csu.gov.cz/datastat/data/VYBER/OBY02AT02

(dataset "Počet obyvatel v obcích k 1. 1." — population in municipalities as
of 1 January). Save it as ``data/csu_population.json`` and the seed
(``scripts/seed_curated_cities.py``) ingests it into ``city_population``,
matching municipalities to ``curated_cities`` by ``(name, kraj)`` with a
diacritics-insensitive fallback.

This replaces the Wikidata SPARQL fetcher (``fetch_population_wikidata.py``)
as the population source: the operator now downloads the official ČSÚ file
directly instead of relying on a third-party mirror.

File shape (JSON-stat 2.0)
--------------------------
A flat ``value`` array indexed by the cross product of the dimensions named
in ``id`` — here ``IndicatorType × UZ25 × CasR``:

* ``IndicatorType`` — a single population-count indicator (``2406P``).
* ``UZ25`` ("Kraje a obce") — mixes 14 NUTS3 *kraj* codes (``CZ0xx``) with
  ~6 000 municipality codes. ``dimension.UZ25.category.child`` maps each kraj
  code to its municipality codes, which is how we recover each
  municipality's kraj name (and how we tell municipalities apart from the
  kraj-level aggregates, which we drop).
* ``CasR`` — the year axis; we take the most recent year present.

JSON-stat is row-major over ``size``, so a cell's flat index is
``sum(coord_i * stride_i)`` where ``stride_i`` is the product of the sizes of
the dimensions after ``i``. ``value`` may be a dense list or a sparse
``{index: value}`` object; both are handled.
"""

from __future__ import annotations

import argparse
import json
import logging
import unicodedata
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("csu_population")

# ČSÚ DataStat download URL for the population-in-municipalities dataset.
# Surfaced in argparse help and the seed/workflow docs so the operator knows
# where the file comes from.
DATASTAT_URL = "https://data.csu.gov.cz/datastat/data/VYBER/OBY02AT02"


def slugify(s: str) -> str:
    """Diacritics-stripped, lowercased, whitespace-normalised join key.

    NFKD (not NFD) so the non-breaking space the curated CSV uses in
    multi-word names ("Kralupy nad\\xa0Vltavou") decomposes to a plain
    space and joins the ČSÚ "Kralupy nad Vltavou". Combining marks are
    dropped and all whitespace runs collapse to single spaces.
    """
    norm = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    return " ".join(stripped.lower().split())


def _strides(sizes: list[int]) -> list[int]:
    """Row-major strides for a JSON-stat ``size`` vector."""
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    return strides


def _value_at(values: Any, flat: int) -> Any:
    """Read one cell from a dense list or sparse dict ``value`` container."""
    if isinstance(values, dict):
        return values.get(str(flat))
    if 0 <= flat < len(values):
        return values[flat]
    return None


def parse_population_jsonstat(
    doc: dict[str, Any],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Return ``{(municipality_name, kraj_name): (population, as_of_year)}``.

    Picks the most recent year present. Kraj-level aggregates (the 14
    ``CZ0xx`` rows) are dropped — only true municipalities are returned. When
    the same ``(name, kraj)`` appears more than once (Czech municipality names
    repeat within a kraj), the larger population wins, which resolves to the
    notable town a curated-city row refers to.
    """
    dims: list[str] = doc["id"]
    sizes: list[int] = doc["size"]
    values = doc["value"]

    role = doc.get("role", {})
    geo_dim = (role.get("geo") or ["UZ25"])[0]
    time_dim = (role.get("time") or ["CasR"])[0]

    dim_pos = {name: i for i, name in enumerate(dims)}
    if geo_dim not in dim_pos or time_dim not in dim_pos:
        raise ValueError(
            f"JSON-stat is missing geo/time dimension "
            f"(geo={geo_dim!r}, time={time_dim!r}, id={dims})"
        )

    strides = _strides(sizes)

    geo_cat = doc["dimension"][geo_dim]["category"]
    geo_index: dict[str, int] = geo_cat["index"]
    geo_label: dict[str, str] = geo_cat.get("label", {})
    child: dict[str, list[str]] = geo_cat.get("child", {})
    if not child:
        raise ValueError(
            "JSON-stat geo dimension has no `child` map; cannot derive each "
            "municipality's kraj. Re-download the full OBY02AT02 export."
        )

    time_cat = doc["dimension"][time_dim]["category"]
    time_index: dict[str, int] = time_cat["index"]
    latest_year_code = max(time_index, key=lambda y: int(y))
    latest_year = int(latest_year_code)
    time_pos = time_index[latest_year_code]

    # municipality code -> kraj name, via the kraj->children map.
    kraj_of: dict[str, str] = {}
    for kraj_code, muni_codes in child.items():
        kraj_name = (geo_label.get(kraj_code) or "").strip()
        for code in muni_codes:
            kraj_of[code] = kraj_name

    base_coord = [0] * len(dims)
    base_coord[dim_pos[time_dim]] = time_pos

    out: dict[tuple[str, str], tuple[int, int]] = {}
    for code, gpos in geo_index.items():
        kraj_name = kraj_of.get(code)
        if kraj_name is None:
            continue  # kraj-level aggregate, not a municipality
        name = (geo_label.get(code) or "").strip()
        if not name:
            continue
        coord = list(base_coord)
        coord[dim_pos[geo_dim]] = gpos
        flat = sum(c * s for c, s in zip(coord, strides))
        raw = _value_at(values, flat)
        if raw is None:
            continue
        try:
            pop = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        if pop <= 0:
            continue
        key = (name, kraj_name)
        prev = out.get(key)
        if prev is None or pop > prev[0]:
            out[key] = (pop, latest_year)
    LOG.info(
        "Parsed %d municipalities from JSON-stat (latest year %d)",
        len(out), latest_year,
    )
    return out


def load_population_jsonstat(path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_population_jsonstat(doc)


def match_to_curated(
    parsed: dict[tuple[str, str], tuple[int, int]],
    curated: Iterable[tuple[str, str]],
) -> tuple[dict[tuple[str, str], tuple[int, int]], list[tuple[str, str]]]:
    """Re-key parsed ČSÚ rows onto the curated ``(name, kraj)`` pairs.

    Matching is diacritics-insensitive so ČSÚ vs curated spelling drift
    ("Plzeň" vs "Plzen") still joins. Returns the curated-keyed population
    dict (the shape ``seed_curated_cities.upsert_population`` consumes) plus
    the list of curated cities with no population match, for logging.
    """
    by_slug: dict[tuple[str, str], tuple[int, int]] = {}
    for (name, kraj), payload in parsed.items():
        by_slug[(slugify(name), slugify(kraj))] = payload

    matched: dict[tuple[str, str], tuple[int, int]] = {}
    misses: list[tuple[str, str]] = []
    for name, kraj in curated:
        payload = by_slug.get((slugify(name), slugify(kraj)))
        if payload is None:
            misses.append((name, kraj))
            continue
        matched[(name, kraj)] = payload
    LOG.info(
        "Matched %d curated cities to ČSÚ population (%d misses)",
        len(matched), len(misses),
    )
    if misses:
        LOG.warning(
            "No population for: %s%s",
            ", ".join(f"{n} ({k})" for n, k in misses[:20]),
            " ..." if len(misses) > 20 else "",
        )
    return matched, misses


def _read_curated(path: Path) -> list[tuple[str, str]]:
    import csv

    out: list[tuple[str, str]] = []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("Město") or "").strip()
            kraj = (row.get("Kraj") or "").strip()
            if name and kraj:
                out.append((name, kraj))
    return out


def main(argv: list[str] | None = None) -> int:
    """Standalone diagnostic: parse a JSON-stat file and report coverage.

    Does not write to the DB — that is the seed's job. Use this to sanity
    check a freshly downloaded ČSÚ file before committing it.
    """
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        prog="csu_population",
        description=f"Parse a ČSÚ DataStat JSON-stat population export. "
                    f"Download from {DATASTAT_URL}",
    )
    p.add_argument("json_path", help="Path to the downloaded OBY02AT02 JSON")
    p.add_argument(
        "--curated-csv", default=str(root / "data" / "obce_v_datech_2025.csv"),
        help="Curated city list to report match coverage against",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
    )

    parsed = load_population_jsonstat(Path(args.json_path))
    curated_path = Path(args.curated_csv)
    if curated_path.exists():
        matched, misses = match_to_curated(parsed, _read_curated(curated_path))
        LOG.info("Coverage: %d matched, %d missed", len(matched), len(misses))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
