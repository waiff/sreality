"""`mf_reference()` -- THE MF reference rent (migration 563) -- proven against the replayed
schema, plus the one rail that keeps its six codes and five notes in one place.

The matrix EXECUTES the function over a seeded rent map (a stale revision, an obec-priced
town, a KÚ-priced town with a partial, a uniform and a non-uniform VK, a retired KÚ, an
unpriced town) and reads each answer's numbers AND its detail shape, because the shape is
the contract every reader renders by (value | range + note | note | none). Nothing else in
CI can see any of it: PREPARE type-checks the function without computing a value, and the
fake connections cannot evaluate SQL.

The plan test proves the planner INLINES the function into properties_public and
browse_projection (no `Function Scan`): a `SET` clause, `STRICT` or plpgsql body would turn
the lateral into a per-row call over every Browse row.

DB tests run in CI's migrations lane (`TEST_DATABASE_URL`, `DB_RAILS_REQUIRED=1`) inside a
transaction that is always rolled back; locally they skip. The rail tests are offline and
run everywhere -- and stay RED until the readers that render by shape (PR-C) have deleted
the two retired client literals. That ordering is the point of the rail.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"
_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = next(_ROOT.glob("migrations/*_mf_reference.sql"))

needs_db = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set -- the mf_reference matrix runs in CI's migrations lane",
)


def _notes() -> dict[str, str]:
    """{status: note} read OUT of the migration, so this file never spells a note."""
    text = _MIGRATION.read_text(encoding="utf-8")
    block = text[text.index("'note', case s.status"):]
    block = block[:block.index("end,")]
    return dict(re.findall(r"when '(\w+)'\s+then '([^']+)'", block))


NOTES = _notes()


# --- the rail (offline) -----------------------------------------------------------------

def _git_grep(literal: str, *paths: str) -> list[str]:
    out = subprocess.run(
        ["git", "grep", "-l", "-F", "-e", literal, "--", *paths],
        cwd=_ROOT, capture_output=True, text=True, check=False,
    )
    assert out.returncode in (0, 1), out.stderr
    return sorted(out.stdout.split())


def test_the_measure_defines_five_notes_for_its_six_codes():
    assert set(NOTES) == {
        "territory_coarse", "no_rent_cell", "not_in_cz", "location_unknown", "inputs_missing",
    }
    assert all(NOTES.values())
    assert "then 'ok'" in _MIGRATION.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", sorted(NOTES))
def test_each_note_exists_only_in_the_migration_that_defines_it(status):
    """RED by: a client, doc or test spelling a note instead of rendering `detail.note`."""
    assert _git_grep(NOTES[status]) == [str(_MIGRATION.relative_to(_ROOT))]


@pytest.mark.parametrize("literal", ["chybí území nebo cena", "No MF reference for this listing"])
def test_the_retired_client_reasons_are_gone(literal):
    """RED until the readers render the measure's own result by shape (PR-C): the
    extension's hard-coded reason and the SPA's English empty text are the two client-side
    rules this measure replaces."""
    assert _git_grep(literal, "frontend/src", "chrome-extension/src") == []


# --- the matrix (executed against the replayed schema) ----------------------------------

O_OBEC, KO1 = 990_001, 990_011                             # priced per obec
O_KU, K1, K2, K3, K4 = 990_002, 990_021, 990_022, 990_023, 990_024   # priced per KÚ
O_NONE, KN1 = 990_003, 990_031                             # priced nowhere

_CALL_SQL = """
SELECT mf_reference_rent_czk, mf_gross_yield_pct, mf_reference_rent
  FROM mf_reference(%(category_main)s, %(category_type)s, %(disposition)s,
                    %(area_m2)s::numeric, %(price_czk)s::bigint, %(condition)s,
                    %(has_balcony)s::boolean, %(terrace)s::boolean, %(furnished)s,
                    %(garage)s::boolean, %(has_lift)s::boolean, %(building_type)s,
                    %(obec_kod)s::bigint, %(katastr_kod)s::bigint,
                    %(country_status)s::country_status)
"""

_FLAT: dict[str, Any] = {
    "category_main": "byt", "category_type": "prodej", "disposition": "2+1",
    "area_m2": 50, "price_czk": 3_000_000, "condition": "dobry",
    "has_balcony": True, "terrace": False, "furnished": "ne", "garage": False,
    "has_lift": True, "building_type": "cihla", "obec_kod": O_OBEC,
    "katastr_kod": None, "country_status": "cz",
}


_SEEDED: dict[str, int] = {}


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set -- the migrations lane is "
            "misconfigured and this matrix would otherwise have skipped green."
        )
    import psycopg

    # Autocommit off: the seed, the REFRESH and every read are one transaction that is
    # rolled back, so nothing here reaches the schema the rest of the lane asserts on.
    c = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=60000",
    )
    try:
        with c.cursor() as cur:
            _SEEDED.update(_seed_rent_map(cur))
        yield c
    finally:
        c.rollback()
        c.close()


def _unit(cur: Any, version: int, level: str, code: int, name: str,
          parent: int | None = None, valid_to: str | None = None) -> int:
    cur.execute(
        "INSERT INTO ruian_admin_units (level, code, name, name_norm, parent_id, path, "
        "  display_path, valid_from, valid_to, first_version_id, last_version_id) "
        "VALUES (%s, %s, %s, lower(%s), %s, %s::ltree, %s, '2020-01-01', %s, %s, %s) "
        "RETURNING id",
        (level, code, name, name, parent, f"{level}_{code}", name, valid_to, version, version),
    )
    return int(cur.fetchone()[0])


def _revision(cur: Any, sha: str) -> int:
    cur.execute(
        "INSERT INTO rent_map_revisions (source_date, source_filename, file_sha256, row_count) "
        "VALUES ('2026-08-15', 'mf-test.xlsx', %s, 0) RETURNING source_revision",
        (sha,),
    )
    return int(cur.fetchone()[0])


def _cells(cur: Any, rev: int, level: str, rows: list[tuple[int, int, int | None]],
           name: str) -> None:
    for code, vk, std in rows:
        cur.execute(
            "INSERT INTO rent_map_values (source_revision, ruian_code, level, kraj, ku_name, "
            "  obec_name, vk, ref_rent_per_m2, ref_rent_novostavba_per_m2) "
            "VALUES (%s, %s, %s, 'Kraj Test', %s, %s, %s, %s, %s)",
            (rev, code, level, f"KU {code}" if level == "ku" else None, name, vk, std,
             None if std is None else std + 40),
        )


def _seed_rent_map(cur: Any) -> dict[str, int]:
    cur.execute(
        "INSERT INTO registry_versions (label, kind, source_date, artifact_urls, "
        "  proj_version, proj_pipeline) "
        "VALUES ('mf-test', 'baseline', '2026-09-01', '{}', 'test', 'test') RETURNING id"
    )
    version = int(cur.fetchone()[0])
    obec = _unit(cur, version, "obec", O_OBEC, "Obecov")
    _unit(cur, version, "katastralni_uzemi", KO1, "Obecov KU", obec)
    ku_town = _unit(cur, version, "obec", O_KU, "Katastrov")
    for k in (K1, K2, K3):
        _unit(cur, version, "katastralni_uzemi", k, f"KU {k}", ku_town)
    _unit(cur, version, "katastralni_uzemi", K4, f"KU {K4}", ku_town, valid_to="2024-01-01")
    none_town = _unit(cur, version, "obec", O_NONE, "Nikde")
    _unit(cur, version, "katastralni_uzemi", KN1, "Nikde KU", none_town)

    # A stale revision whose every number would be visibly wrong if it leaked through.
    stale = _revision(cur, "mf-test-stale")
    _cells(cur, stale, "obec", [(O_OBEC, 2, 999)], "Obecov")
    cur.execute(
        "INSERT INTO rent_map_adjustments VALUES (%s, 2, false, 'balcony', 500)", (stale,))

    latest = _revision(cur, "mf-test-latest")
    _cells(cur, latest, "obec",
           [(O_OBEC, 1, 300), (O_OBEC, 2, 250), (O_OBEC, 3, 220), (O_OBEC, 4, 180)], "Obecov")
    _cells(cur, latest, "ku", [
        (K1, 1, 100), (K2, 1, 120), (K3, 1, 110),          # all priced, not uniform
        (K1, 2, 250), (K2, 2, 250), (K3, 2, 250),          # all priced, uniform
        (K1, 3, 195), (K2, 3, 238), (K3, 3, None),         # partial (Jihlava's VK3)
        (K4, 2, 999), (K4, 3, 999),                        # retired KÚ: never counted
    ], "Katastrov")
    for vk, nov, attribute, czk in [
        (2, False, "balcony", 5), (2, False, "elevator", 44), (2, False, "garage", 37),
        (2, False, "terrace", 10), (2, False, "furnished", 15), (2, False, "other_material", 20),
        (3, False, "balcony", 5), (3, False, "garage", 37), (3, False, "elevator", 44),
        (3, True, "balcony", 6), (3, True, "other_material", 20),
    ]:
        cur.execute("INSERT INTO rent_map_adjustments VALUES (%s, %s, %s, %s, %s)",
                    (latest, vk, nov, attribute, czk))
    cur.execute("REFRESH MATERIALIZED VIEW rent_map_cells")
    return {"latest": latest, "stale": stale}


def mf(conn: Any, **overrides: Any) -> tuple[Any, Any, dict[str, Any]] | None:
    with conn.cursor() as cur:
        cur.execute(_CALL_SQL, {**_FLAT, **overrides})
        rows = cur.fetchall()
    assert len(rows) <= 1
    return rows[0] if rows else None


def _is_value(d: dict[str, Any]) -> bool:
    return d.get("monthly_rent_czk") is not None and "range" not in d


@needs_db
def test_an_obec_priced_flat_reads_the_latest_revisions_obec_cell(conn):
    rent, yld, d = mf(conn)
    assert (rent, float(yld)) == (14_950, 5.98)            # (250 + 5 + 44) x 50
    assert d == {
        "status": "ok",
        "territory": {"ruian_code": O_OBEC, "level": "obec", "name": "Obecov",
                      "kraj": "Kraj Test"},
        "vk": 2, "is_novostavba": False,
        "source_revision": _SEEDED["latest"], "source_date": "2026-08-15",
        "base_per_m2": 250,
        "adjustments": [{"attribute": "balcony", "czk_per_m2": 5},
                        {"attribute": "elevator", "czk_per_m2": 44}],
        "adjustments_sum_per_m2": 49, "total_per_m2": 299,
        "area_m2": 50, "monthly_rent_czk": 14_950,
    }


@needs_db
def test_every_amenity_adjustment_applies(conn):
    rent, _, d = mf(conn, terrace=True, furnished="ano", garage=True)
    assert d["adjustments_sum_per_m2"] == 5 + 44 + 37 + 10 + 15
    assert rent == (250 + 111) * 50


@needs_db
def test_a_ten_room_flat_is_vk4(conn):
    rent, _, d = mf(conn, disposition="10+1", area_m2=100, has_balcony=False, has_lift=False)
    assert (d["vk"], rent) == (4, 18_000)


@needs_db
def test_a_null_condition_keeps_its_adjustments(conn):
    rent, _, d = mf(conn, condition=None)
    assert (rent, d["is_novostavba"], d["adjustments_sum_per_m2"]) == (14_950, False, 49)


@pytest.mark.parametrize(("building_type", "rent"), [
    ("skelet", (260 + 6 + 20) * 60), ("cihla", (260 + 6) * 60), ("panel", (260 + 6) * 60),
])
@needs_db
def test_a_new_build_reads_the_novostavba_column_and_the_material_rule(conn, building_type, rent):
    got, _, d = mf(conn, disposition="3+kk", area_m2=60, condition="novostavba",
                   has_lift=False, building_type=building_type)
    assert (got, d["is_novostavba"], d["base_per_m2"]) == (rent, True, 260)


@needs_db
def test_a_town_whose_every_ku_has_one_price_reads_that_price(conn):
    rent, _, d = mf(conn, obec_kod=O_KU, has_balcony=False, has_lift=False)
    assert rent == 250 * 50
    assert d["territory"] == {"ruian_code": O_KU, "level": "obec", "name": "Katastrov",
                              "kraj": "Kraj Test", "basis": "town_uniform"}


@needs_db
def test_a_ku_priced_town_without_a_bound_ku_reads_the_range_and_the_note(conn):
    """Jihlava's exact shape: VK3 priced 195 and 238 in two of three current KÚ."""
    rent, yld, d = mf(conn, obec_kod=O_KU, disposition="3+1", area_m2=75,
                      price_czk=5_100_000, has_lift=False, garage=True)
    assert (rent, yld) == (None, None)
    assert d["status"] == "territory_coarse" and d["note"] == NOTES["territory_coarse"]
    assert d["range"] == {"per_m2_min": 237, "per_m2_max": 280,
                          "rent_min_czk": 17_775, "rent_max_czk": 21_000,
                          "yield_min_pct": 4.18, "yield_max_pct": 4.94}
    assert "monthly_rent_czk" not in d and "base_per_m2" not in d and "total_per_m2" not in d
    assert d["adjustments_sum_per_m2"] == 42 and d["territory"]["level"] == "obec"


@needs_db
def test_a_fully_priced_but_uneven_town_is_a_range_and_a_rental_range_has_no_yield(conn):
    rent, yld, d = mf(conn, obec_kod=O_KU, disposition="1+kk", area_m2=30,
                      category_type="pronajem", has_balcony=False, has_lift=False)
    assert (rent, yld) == (None, None)
    assert d["range"] == {"per_m2_min": 100, "per_m2_max": 120,
                          "rent_min_czk": 3_000, "rent_max_czk": 3_600}


@needs_db
def test_a_bound_ku_with_a_cell_reads_its_own_cell(conn):
    rent, _, d = mf(conn, obec_kod=O_KU, katastr_kod=K1, disposition="3+1", area_m2=75,
                    has_lift=False, garage=True)
    assert rent == (195 + 42) * 75
    assert (d["territory"]["level"], d["territory"]["ruian_code"]) == ("ku", K1)


@needs_db
def test_a_bound_ku_without_a_cell_in_a_ku_priced_town_is_no_rent_cell(conn):
    assert mf(conn, obec_kod=O_KU, katastr_kod=K3, disposition="3+1")[2] == {
        "status": "no_rent_cell", "note": NOTES["no_rent_cell"]}


@needs_db
def test_a_bound_ku_without_a_cell_in_an_obec_priced_town_reads_the_obec(conn):
    rent, _, d = mf(conn, katastr_kod=KO1)
    assert (rent, d["territory"]["level"], d["territory"]["ruian_code"]) == (14_950, "obec", O_OBEC)


@pytest.mark.parametrize("overrides", [
    {"obec_kod": O_NONE},                                    # no cell anywhere in the town
    {"obec_kod": O_KU, "disposition": "4+1"},                # a KÚ town with no VK4 price
])
@needs_db
def test_no_published_cell_is_no_rent_cell(conn, overrides):
    assert mf(conn, **overrides) == (None, None, {"status": "no_rent_cell",
                                                  "note": NOTES["no_rent_cell"]})


@pytest.mark.parametrize("overrides", [
    {"category_type": "pronajem"}, {"price_czk": 99_999}, {"price_czk": None},
])
@needs_db
def test_a_flat_without_a_usable_sale_price_keeps_its_rent_without_a_yield(conn, overrides):
    rent, yld, d = mf(conn, **overrides)
    assert (rent, yld) == (14_950, None) and _is_value(d)


@pytest.mark.parametrize(("overrides", "status"), [
    ({"country_status": "foreign", "obec_kod": None}, "not_in_cz"),
    ({"obec_kod": None}, "location_unknown"),
    ({"obec_kod": None, "country_status": None}, "location_unknown"),
    ({"disposition": None}, "inputs_missing"),
    ({"disposition": "atypický"}, "inputs_missing"),
    ({"area_m2": None}, "inputs_missing"),
    ({"area_m2": 11.9}, "inputs_missing"),
])
@needs_db
def test_every_reason_is_a_note_and_nothing_else(conn, overrides, status):
    assert mf(conn, **overrides) == (None, None, {"status": status, "note": NOTES[status]})


@pytest.mark.parametrize("category_main", ["dum", "pozemek", None])
@needs_db
def test_a_non_flat_has_no_row(conn, category_main):
    assert mf(conn, category_main=category_main) is None


# --- the views and the estimation hand-off ----------------------------------------------

def _property(cur: Any, *, obec: int, disposition: str, area: float, price: int) -> tuple[int, int]:
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    pid = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, category_main, "
        "  category_type, price_czk, area_m2, disposition, is_active, published_at, property_id) "
        "VALUES (NULL, 'idnes', %s, '{}'::jsonb, 'byt', 'prodej', %s, %s, %s, true, now(), %s) "
        "RETURNING id",
        (f"mf-{pid}", price, area, disposition, pid),
    )
    lid = int(cur.fetchone()[0])
    cur.execute(
        "UPDATE properties SET category_main = 'byt', category_type = 'prodej', "
        "  current_price_czk = %s, area_m2 = %s, disposition = %s, condition = 'dobry', "
        "  has_balcony = true, garage = true, has_lift = false, building_type = 'cihla', "
        "  status = 'active', is_active = true, published_at = now(), repr_listing_ref_id = %s "
        " WHERE id = %s",
        (price, area, disposition, lid, pid),
    )
    cur.execute(
        "INSERT INTO listing_location (listing_id, geom, match_confidence, granularity, "
        "  uncertainty_radius_m, country_status, resolver_version, claim_set_hash, "
        "  registry_version, obec_kod) "
        "VALUES (%s, ST_SetSRID(ST_MakePoint(15.59, 49.40), 4326), 'exact', 'building', 5, "
        "  'cz', 'test', '\\x00'::bytea, 'test', %s)",
        (lid, obec),
    )
    return pid, lid


@pytest.fixture(scope="module")
def adverts(conn):
    with conn.cursor() as cur:
        coarse = _property(cur, obec=O_KU, disposition="3+1", area=75, price=5_100_000)
        value = _property(cur, obec=O_OBEC, disposition="3+1", area=75, price=5_100_000)
        for pid, _lid in (coarse, value):
            cur.execute(
                "INSERT INTO browse_list SELECT * FROM browse_projection WHERE property_id = %s",
                (pid,))
            assert cur.rowcount == 1
    return {"coarse": coarse, "value": value}


def _one(conn: Any, sql: str, *params: Any) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


@needs_db
def test_properties_public_and_browse_projection_publish_the_measure(conn, adverts):
    pid, _ = adverts["value"]
    rent, yld, detail = _one(conn, "SELECT mf_reference_rent_czk, mf_gross_yield_pct, "
                                   "mf_reference_rent FROM properties_public WHERE property_id = %s",
                             pid)
    assert rent == (220 + 5 + 37) * 75 and float(yld) == round(rent * 12 / 5_100_000 * 100, 2)
    assert _is_value(detail) and detail["status"] == "ok"
    assert _one(conn, "SELECT mf_reference_rent_czk, mf_gross_yield_pct FROM browse_projection "
                      "WHERE property_id = %s", pid) == (rent, yld)


@needs_db
def test_a_town_level_flat_has_a_range_on_detail_and_no_yield_in_browse(conn, adverts):
    pid, _ = adverts["coarse"]
    rent, yld, detail = _one(conn, "SELECT mf_reference_rent_czk, mf_gross_yield_pct, "
                                   "mf_reference_rent FROM properties_public WHERE property_id = %s",
                             pid)
    assert (rent, yld, detail["status"]) == (None, None, "territory_coarse")
    assert detail["note"] == NOTES["territory_coarse"] and "range" in detail
    assert _one(conn, "SELECT mf_reference_rent_czk, mf_gross_yield_pct FROM browse_projection "
                      "WHERE property_id = %s", pid) == (None, None)


@needs_db
def test_the_portal_lane_serves_the_propertys_yield_from_the_read_model(conn, adverts):
    """Q8 (b): one number per property on every advert, read from browse_list -- never a
    second per-row computation in listing_feed_public."""
    pid, lid = adverts["value"]
    (browse_yield,) = _one(conn, "SELECT mf_gross_yield_pct FROM browse_list "
                                 "WHERE property_id = %s", pid)
    assert browse_yield is not None
    assert _one(conn, "SELECT mf_gross_yield_pct FROM listing_feed_public WHERE id = %s",
                lid) == (browse_yield,)


@needs_db
def test_an_estimation_of_our_advert_hands_the_listing_pages_facts_to_the_measure(conn, adverts):
    """The run's reference equals the listing page's: the same golden facts and the same
    stored codes reach the same function."""
    from api.estimation_runs import _MF_FACT_KEYS, _SUBJECT_MF_FACTS_SQL
    from toolkit.rent_map import MF_ENGINE, compute_reference_rent

    pid, lid = adverts["value"]
    with conn.cursor() as cur:
        cur.execute(_SUBJECT_MF_FACTS_SQL, {"listing_id": lid})
        facts = dict(zip(_MF_FACT_KEYS, cur.fetchone()))
    ref = compute_reference_rent(conn, disposition="3+1", area_m2=75, **facts)
    (page,) = _one(conn, "SELECT mf_reference_rent FROM properties_public WHERE property_id = %s",
                   pid)
    assert ref == {**page, "engine": MF_ENGINE}


def _plan_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


@pytest.mark.parametrize(("relation", "key", "function", "reads"), [
    ("properties_public", "property_id", "mf_reference", "rent_map_cells"),
    ("browse_projection", "property_id", "mf_reference", "rent_map_cells"),
    ("listing_feed_public", "id", "browse_list_mf", "browse_list"),
])
@needs_db
def test_the_measure_is_inlined_into_every_view_that_calls_it(conn, adverts, relation, key,
                                                              function, reads):
    """RED by: a SET search_path, STRICT, SECURITY DEFINER or a plpgsql body on the function
    -- each turns the lateral into a Function Scan, one call per Browse row."""
    pid, lid = adverts["value"]
    (plan,) = _one(conn, f"EXPLAIN (FORMAT JSON) SELECT * FROM {relation} "
                         f"WHERE {key} = {int(lid if key == 'id' else pid)}")
    nodes = list(_plan_nodes((plan if isinstance(plan, list) else json.loads(plan))[0]["Plan"]))
    assert not [n for n in nodes if n.get("Function Name") == function
                or n["Node Type"] == "Function Scan"]
    assert reads in {n.get("Relation Name") for n in nodes}
