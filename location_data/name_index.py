"""Rebuild `ruian_name_index` — the in-house typo-tolerant gazetteer (01 §3.6).

Normalization happens HERE, in the loader, not in a generated column or a trigger:
`unaccent()` is STABLE, not IMMUTABLE (01 §0.4 trap 2), so `name_norm` is written by a
named Python function and the DB only stores the result.

The table is REBUILDABLE — a pure function of the registry tables plus a version label —
so a rebuild deletes this version's rows and re-inserts them.

CLI:  python -m location_data.name_index [--version-id N]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from location_data import loader_db

LOG = logging.getLogger("location_data.name_index")

_PUNCT = re.compile(r"[^0-9a-z]+")
# 'Krásný Les u Frýdlantu' -> base 'Krásný Les', qualifier 'u Frýdlantu' (01 §3.6).
_QUALIFIER = re.compile(
    r"^(?P<base>.+?)\s+(?P<qualifier>(?:u|nad|pod|na|při|za|ve|v)\s+\S+)$",
    re.IGNORECASE,
)
_STREET_PREFIX = re.compile(r"^(?:ulice|ul\.?)\s+", re.IGNORECASE)

# Levels worth searching for. `adresni_misto` is not an admin unit and streets come from
# their own table; volební okrsek has no name.
_UNIT_LEVELS = (
    "stat", "region_soudrznosti", "kraj", "okres", "orp", "pou", "obec",
    "spravni_obvod", "momc", "cast_obce", "katastralni_uzemi", "zsj",
)


def deaccent(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def normalize_name(value: str) -> str:
    """lower(unaccent(name)) with punctuation folded to single spaces."""
    return _PUNCT.sub(" ", deaccent(value).lower()).strip()


def normalize_street_name(value: str) -> str:
    """Street normalization: the generic leading street word is dropped, the rest is not
    (a Czech street name carries its own type word — 'náměstí Míru' is not 'Míru')."""
    return normalize_name(_STREET_PREFIX.sub("", value.strip()))


def split_qualifier(value: str) -> tuple[str, str | None]:
    match = _QUALIFIER.match(value.strip())
    if not match:
        return value.strip(), None
    return match.group("base").strip(), match.group("qualifier").strip()


@dataclass(frozen=True, slots=True)
class NameRow:
    entity_kind: str
    entity_id: int
    name: str
    name_norm: str
    name_kind: str
    qualifier: str | None
    parent_obec_unit_id: int | None
    parent_okres_unit_id: int | None
    psc_set: list[str]


def build_rows(
    entity_kind: str,
    entity_id: int,
    name: str,
    *,
    is_street: bool,
    parent_obec_unit_id: int | None,
    parent_okres_unit_id: int | None,
    psc_set: Iterable[str] = (),
) -> list[NameRow]:
    """The alias rows one entity contributes: official, deaccented, qualifier-stripped.

    `deaccented` is first-class because ceskereality stores streets ~98 % de-accented and
    realitymix 27 %, so an exact-string join across sources silently fails without it.
    """
    normalize = normalize_street_name if is_street else normalize_name
    psc = sorted({p for p in psc_set if p})
    rows: list[NameRow] = []
    seen: set[tuple[str, str]] = set()

    def add(text: str, kind: str, qualifier: str | None) -> None:
        norm = normalize(text)
        if not norm or (norm, kind) in seen:
            return
        seen.add((norm, kind))
        rows.append(NameRow(
            entity_kind=entity_kind,
            entity_id=entity_id,
            name=text,
            name_norm=norm,
            name_kind=kind,
            qualifier=qualifier,
            parent_obec_unit_id=parent_obec_unit_id,
            parent_okres_unit_id=parent_okres_unit_id,
            psc_set=psc,
        ))

    base, qualifier = split_qualifier(name)
    add(name, "official", qualifier)
    if deaccent(name) != name:
        add(deaccent(name), "deaccented", qualifier)
    if qualifier:
        add(base, "qualifier_stripped", qualifier)
    return rows


def count_homonyms(rows: list[NameRow]) -> dict[tuple[str, str], int]:
    """How many distinct entities of one kind share a normalized name — >1 means a
    disambiguator is mandatory downstream."""
    buckets: dict[tuple[str, str], set[int]] = {}
    for row in rows:
        buckets.setdefault((row.entity_kind, row.name_norm), set()).add(row.entity_id)
    return {key: len(ids) for key, ids in buckets.items()}


def _current_version_id(conn: psycopg.Connection) -> int:
    version_id = loader_db.scalar(
        conn, "SELECT id FROM registry_versions WHERE is_current LIMIT 1"
    )
    if version_id is None:
        raise RuntimeError("no current registry_version — run the baseline load first")
    return int(version_id)


def _psc_sets(conn: psycopg.Connection, column: str) -> dict[int, list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {column}, array_agg(DISTINCT psc)
            FROM ruian_address_points
            WHERE valid_to IS NULL AND {column} IS NOT NULL
            GROUP BY 1
            """
        )
        return {int(unit_id): list(psc) for unit_id, psc in cur.fetchall()}


def collect_rows(conn: psycopg.Connection) -> list[NameRow]:
    obec_psc = _psc_sets(conn, "obec_unit_id")
    cast_obce_psc = _psc_sets(conn, "cast_obce_unit_id")
    street_psc = _psc_sets(conn, "street_id")

    rows: list[NameRow] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.level::text, u.code, u.name,
                   (SELECT a.id FROM ruian_admin_units a
                     WHERE a.valid_to IS NULL AND a.level = 'obec' AND a.path @> u.path
                     LIMIT 1),
                   (SELECT a.id FROM ruian_admin_units a
                     WHERE a.valid_to IS NULL AND a.level = 'okres' AND a.path @> u.path
                     LIMIT 1)
            FROM ruian_admin_units u
            WHERE u.valid_to IS NULL AND u.level::text = ANY(%s)
            """,
            (list(_UNIT_LEVELS),),
        )
        for unit_id, level, code, name, parent_obec, parent_okres in cur.fetchall():
            if name == str(code):
                continue  # placeholder: no name source yet (filled by the boundary pack)
            psc = obec_psc if level == "obec" else cast_obce_psc if level == "cast_obce" else {}
            rows.extend(build_rows(
                level, int(unit_id), name,
                is_street=False,
                parent_obec_unit_id=parent_obec,
                parent_okres_unit_id=parent_okres,
                psc_set=psc.get(int(unit_id), ()),
            ))

        cur.execute(
            """
            SELECT s.id, s.name, s.obec_unit_id,
                   (SELECT a.id FROM ruian_admin_units a
                     WHERE a.valid_to IS NULL AND a.level = 'okres' AND a.path @> o.path
                     LIMIT 1)
            FROM ruian_streets s
            JOIN ruian_admin_units o ON o.id = s.obec_unit_id
            WHERE s.valid_to IS NULL
            """
        )
        for street_id, name, obec_unit_id, parent_okres in cur.fetchall():
            rows.extend(build_rows(
                "ulice", int(street_id), name,
                is_street=True,
                parent_obec_unit_id=obec_unit_id,
                parent_okres_unit_id=parent_okres,
                psc_set=street_psc.get(int(street_id), ()),
            ))
    return rows


def rebuild(conn: psycopg.Connection, version_id: int) -> int:
    rows = collect_rows(conn)
    homonyms = count_homonyms(rows)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ruian_name_index WHERE registry_version_id = %s", (version_id,)
            )
            with cur.copy(
                """
                COPY ruian_name_index
                     (entity_kind, entity_id, registry_version_id, name, name_norm,
                      name_kind, qualifier, parent_obec_unit_id, parent_okres_unit_id,
                      psc_set, homonym_count)
                FROM STDIN
                """
            ) as copy:
                for row in rows:
                    copy.write_row((
                        row.entity_kind, row.entity_id, version_id, row.name, row.name_norm,
                        row.name_kind, row.qualifier, row.parent_obec_unit_id,
                        row.parent_okres_unit_id, row.psc_set,
                        homonyms[(row.entity_kind, row.name_norm)],
                    ))
    LOG.info("NAME_INDEX rebuilt version=%s rows=%d", version_id, len(rows))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-id", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    with loader_db.open_loader_connection() as conn:
        version_id = args.version_id or _current_version_id(conn)
        rebuild(conn, version_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
