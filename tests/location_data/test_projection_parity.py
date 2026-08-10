"""The parity test 01 §7.1.1 / §A.2 check 5 require: `builder_output(row) ==
location_geo_cell_key/…(row)` over a golden set.

Neither projection has a generated column, so the BUILDER writes every derived value and
migration 384's IMMUTABLE SQL functions are the single definition it must agree with. There
is no database in the pytest job, so parity is asserted in two layers:

1. **The SQL body is pinned here** and, once migration 384 is on the branch, compared
   against the migration text — so a change to either side fails.
2. **A reference implementation transcribed from that SQL** (`_sql_geo_cell_key`,
   `_sql_street_block_key`, …) is run against the builder over a battery of rows,
   including the roundings and the diacritics that are the whole reason the SQL spelling is
   `round(numeric)` and not `to_char()`.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from location_data.resolver import derived

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "384_location_w1_serving.sql"
)

# Pinned from 01 §7.1.1 / migration 384. Whitespace-normalized on comparison.
PINNED_BODIES = {
    "location_geo_cell_key": (
        "select case when g is null then null else 'c:' || "
        "round(ST_Y(g)::numeric, 4)::text || ':' || round(ST_X(g)::numeric, 4)::text end"
    ),
    "location_street_block_key": (
        "select case when obec_kod is null or street is null then null else "
        "obec_kod::text || ':' || lower(unaccent(street)) || ':' || coalesce(hn, '') end"
    ),
    "location_addr_block_key": (
        "select case when ruian_adm_kod is null then null else "
        "'a:' || ruian_adm_kod::text end"
    ),
    "location_building_block_key": (
        "select case when stavebni_objekt_kod is null then null else "
        "'b:' || stavebni_objekt_kod::text end"
    ),
}


def _squash(text: str) -> str:
    return " ".join(text.split())


def _bodies_from_migration() -> dict[str, str]:
    sql = _MIGRATION.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for match in re.finditer(
        r"create function (\w+)\s*\([^)]*\)\s*returns\s+\w+\s*language sql \w+ as \$fn\$(.*?)\$fn\$",
        sql,
        re.IGNORECASE | re.DOTALL,
    ):
        out[match.group(1)] = _squash(match.group(2))
    return out


@pytest.mark.skipif(not _MIGRATION.exists(), reason="migration 384 lands with PR-A")
def test_the_pinned_bodies_still_match_the_migration():
    bodies = _bodies_from_migration()
    for name, pinned in PINNED_BODIES.items():
        assert name in bodies, f"migration 384 no longer declares {name}"
        assert bodies[name] == _squash(pinned), (
            f"{name} changed in migration 384 — update the Python builder and this pin together"
        )


# --------------------------------------------------------------------------- reference


def _pg_round_4(value: float) -> str:
    """`round(x::numeric, 4)::text`: float8→numeric is the shortest round-trip form,
    `round` is half away from zero, and the result carries the scale (so `50.1` prints
    `50.1000`)."""
    rounded = Decimal(repr(float(value))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return str(Decimal("0.0000") if rounded == 0 else rounded)


def _sql_geo_cell_key(lat, lon):
    if lat is None or lon is None:
        return None
    return "c:" + _pg_round_4(lat) + ":" + _pg_round_4(lon)


def _sql_unaccent(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )


def _sql_street_block_key(obec_kod, street, hn):
    if obec_kod is None or street is None:
        return None
    return f"{obec_kod}" + ":" + _sql_unaccent(street).lower() + ":" + (hn or "")


def _sql_addr_block_key(kod):
    return None if kod is None else "a:" + str(kod)


def _sql_building_block_key(kod):
    return None if kod is None else "b:" + str(kod)


GEO_BATTERY = [
    (50.089480, 14.398606), (50.1, 14.0), (49.0, 12.00005), (50.10102, 14.34804),
    (48.5551234, 18.9999999), (51.05555, 12.00004999), (49.593577, 17.29866),
    (50.0, 14.0), (50.12413, 14.12853), (49.7573, 18.0158),
]

STREET_BATTERY = [
    (554782, "Nad Bořislavkou", "487"), (554782, "28. října", None),
    (599212, "Slunečná", "12/3a"), (554782, "náměstí Míru", ""),
    (None, "Slunečná", "1"), (554782, None, "1"), (563943, "Žižkova třída", "9"),
]


@pytest.mark.parametrize("lat, lon", GEO_BATTERY)
def test_geo_cell_key_matches_the_sql_definition(lat, lon):
    assert derived.geo_cell_key(lat, lon) == _sql_geo_cell_key(lat, lon)


def test_geo_cell_key_is_null_for_a_null_geometry():
    assert derived.geo_cell_key(None, None) is None
    assert derived.geo_cell_key(50.0, None) is None


def test_geo_cell_key_pads_to_four_decimals_like_the_numeric_cast():
    assert derived.geo_cell_key(50.1, 14.0) == "c:50.1000:14.0000"


@pytest.mark.parametrize("obec_kod, street, hn", STREET_BATTERY)
def test_street_block_key_matches_the_sql_definition(obec_kod, street, hn):
    assert derived.street_block_key(obec_kod, street, hn) == _sql_street_block_key(
        obec_kod, street, hn
    )


def test_street_block_key_folds_diacritics_but_not_punctuation():
    """`lower(unaccent(street))` is NOT the gazetteer's `name_norm` — the block key keeps
    spaces and punctuation, and the two keys must not be quietly unified."""
    assert derived.street_block_key(554782, "Nad Bořislavkou", "487") == (
        "554782:nad borislavkou:487"
    )
    assert derived.street_block_key(554782, "28. října", None) == "554782:28. rijna:"


@pytest.mark.parametrize("kod", [None, 0, 21690278, 999999999999])
def test_addr_and_building_block_keys_match_the_sql_definition(kod):
    assert derived.addr_block_key(kod) == _sql_addr_block_key(kod)
    assert derived.building_block_key(kod) == _sql_building_block_key(kod)


def test_the_four_keys_are_one_visibly_prefixed_family_that_cannot_collide():
    keys = {
        derived.geo_cell_key(50.0, 14.0),
        derived.addr_block_key(500014),
        derived.building_block_key(500014),
        derived.street_block_key(500014, "A", None),
    }
    assert len(keys) == 4
