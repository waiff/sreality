"""The path C SQL, offline: the three forms of each rung statement share ONE select body,
every placeholder a statement names is one the lane supplies, the area band never separates
two areas the tolerance accepts, the location read stays on the projection (rule 24), and the
column order matches migration 492. CI's schema-replay job PREPAREs the statements
themselves; the lane's `verify` mode holds them to the oracle on real towns."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import pytest

from toolkit import dedup_candidates_sql as sql
from toolkit import dedup_candidates as dc
from toolkit import dedup_sim_settings as dss

_ROOT = Path(__file__).resolve().parent.parent.parent
_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_MARK = " SELECT %(inputs_id)s::bigint"  # where every rung's pair select begins

INPUTS = dc.path_inputs("C", dss.effective_settings(None))


def _placeholders(statement: str) -> set[str]:
    return set(_PLACEHOLDER.findall(statement))


def _lane_args() -> dict[str, object]:
    return {**sql.rung_params(INPUTS), "block_key": 554782, "id_from": 0, "id_to": 1 << 62,
            "inputs_id": 1, "generation_id": 1}


def test_each_rung_has_insert_count_and_rows_over_one_select_body() -> None:
    for rung, stmts in sql.RUNG_SQL.items():
        body = _MARK + stmts["rows"].split(_MARK, 1)[1]  # the pair select: list + joins + where
        assert body in stmts["insert"], rung
        assert body in stmts["count"], rung
        assert stmts["insert"].startswith("WITH base AS (")
        assert " INSERT INTO dedup_sim.candidate_pairs (" in stmts["insert"]
        assert stmts["insert"].rstrip().endswith("last_seen_at = now()")
        assert stmts["count"].startswith("WITH base AS (") and " SELECT count(*) FROM (" in stmts["count"]
        assert f"'{rung}'" in body


def test_every_placeholder_a_rung_statement_names_is_one_the_lane_supplies() -> None:
    supplied = set(_lane_args())
    for rung, stmts in sql.RUNG_SQL.items():
        for form, statement in stmts.items():
            missing = _placeholders(statement) - supplied
            assert not missing, f"{rung}/{form} names {missing}"


def test_placeholders_are_all_named_and_cast_where_their_type_is_not_implied() -> None:
    corpus = [s for stmts in sql.RUNG_SQL.values() for s in stmts.values()] + [
        sql.BLOCKS_SQL, sql.BLOCK_IDS_SQL, sql.STALE_SWEEP_SQL, sql.FUNNEL_SQL, sql.TOP_BUCKETS_SQL,
        sql.PAIR_MATRIX_SQL, sql.LISTINGS_WITH_CANDIDATES_SQL, sql.PAIRS_PER_BLOCK_SQL,
        sql.BLOCK_NAMES_SQL, sql.BLOCK_ATTRS_SQL,
    ]
    for statement in corpus:
        assert "%s" not in statement.replace("%(", ""), "positional placeholder"
        # a bare parameter in a SELECT list has no type for PREPARE; every one is cast
        for m in re.finditer(r"%\((\w+)\)s(?!::)", statement):
            name = m.group(1)
            before = statement[max(0, m.start() - 40):m.start()]
            assert re.search(r"(=|>=|<=|<>|<|>|IN \(|ANY\()\s*$", before), (
                f"{name} is neither cast nor compared: …{before!r}")


def test_every_statement_the_lane_runs_is_covered_by_the_full_parameter_set() -> None:
    """The regression this pins: BLOCK_IDS_SQL is built from the same base CTE as the rungs,
    so when the CTE grew a district parameter the statement silently needed one too. The lane
    now passes the WHOLE parameter set to every per-town statement; this asserts that is
    enough for all of them, so the next parameter the CTE grows cannot break a call site."""
    supplied = set(_lane_args())
    per_town = {
        "BLOCK_IDS_SQL": sql.BLOCK_IDS_SQL,
        "BLOCK_ATTRS_SQL": sql.BLOCK_ATTRS_SQL,
        **{f"{r}/{f}": st for r, forms in sql.RUNG_SQL.items() for f, st in forms.items()},
    }
    for name, statement in per_town.items():
        missing = _placeholders(statement) - supplied
        assert not missing, f"{name} names {missing}, which no call site supplies"


def test_the_location_read_stays_on_the_answer_table_never_the_legacy_columns() -> None:
    corpus = "\n".join(
        [s for stmts in sql.RUNG_SQL.values() for s in stmts.values()]
        + [sql.BLOCKS_SQL, sql.BLOCK_IDS_SQL, sql.FUNNEL_SQL, sql.TOP_BUCKETS_SQL, sql.BLOCK_ATTRS_SQL,
           sql.BLOCK_NAMES_SQL]
    ).lower()
    for legacy in ("x.geom", "x.obec_id", "x.okres_id", "x.region_id", "x.ku_id", "x.obec ", "x.street",
                   "street_name_key", "geocode_cache", "browse_list", "locality_district_id"):
        assert legacy not in corpus, legacy
    # W2-b: the one answer table, and the frozen W1 projection is gone from the tree.
    assert "listing_location" in corpus
    assert "listing_location_current" not in corpus
    # the granularity floor is applied by RANK, never by enum order
    assert "location_granularity_rank" in corpus
    assert "granularity >=" not in corpus and "granularity > " not in corpus


def test_c1_needs_both_dispositions_and_c3_needs_one_missing() -> None:
    c1 = sql.RUNG_SQL["C1"]["rows"]
    assert "JOIN base b ON b.disposition = a.disposition" in c1
    assert "WHERE a.disposition IS NOT NULL" in c1
    c3 = sql.RUNG_SQL["C3"]["rows"]
    assert "WHERE (a.disposition IS NULL OR b.disposition IS NULL)" in c3
    assert "FROM band a" in c3 and "JOIN (VALUES (-1), (0), (1)) d(off) ON TRUE" in c3
    assert "b.band = a.band + d.off" in c3


def test_category_guard_and_floor_rule_are_the_same_predicates_on_both_rungs() -> None:
    for rung in ("C1", "C3"):
        s = sql.RUNG_SQL[rung]["rows"]
        assert "a.category_type IS NULL OR b.category_type IS NULL OR a.category_type = b.category_type" in s
        assert "(a.category_main = 'dum' AND b.category_main = 'komercni')" in s
        assert "(a.category_main = 'komercni' AND b.category_main = 'dum')" in s
        assert "ABS(a.floor - b.floor) <= %(floor_tolerance)s::int" in s
        assert "b.listing_id > a.listing_id" in s


def test_both_rungs_carry_the_district_guard_and_it_never_vetoes_on_an_unknown() -> None:
    for rung in ("C1", "C3"):
        body = sql.RUNG_SQL[rung]["rows"]
        assert "(a.district IS NULL OR b.district IS NULL OR a.district = b.district)" in body
    # the district is read only inside a split town, and only from the projection
    base = sql.RUNG_SQL["C1"]["rows"]
    assert "CASE WHEN l.obec_kod = ANY(%(district_split_towns)s::bigint[])" in base
    assert "THEN l.cast_obce_kod END AS district" in base


def test_c1_checks_the_area_but_only_when_both_are_known() -> None:
    body = sql.RUNG_SQL["C1"]["rows"]
    assert "AND (a.area IS NULL OR b.area IS NULL OR " in body
    assert "<= %(c1_area_pct)s::numeric)" in body
    # and it records them, so a stored row says whether the check was made
    assert " a.area, b.area, ABS(a.area - b.area) / GREATEST(a.area, b.area) * 100," in body
    assert "NULL::numeric, NULL::numeric, NULL::numeric" not in body


def test_the_area_is_the_one_headline_area_and_zero_means_missing() -> None:
    """W17: ONE area for every category. `listings.area_m2` is already the plot on land
    (scraper/area.derive_headline_area) — a second choice here was a second definition."""
    body = sql.RUNG_SQL["C3"]["rows"]
    assert "CASE WHEN x.area_m2 > 0 THEN x.area_m2 END AS area" in body
    for gone in ("x.estate_area", "x.usable_area", "CASE WHEN x.category_main = 'pozemek'"):
        assert gone not in body, gone
    assert "x.estate_area" not in sql.FUNNEL_SQL and "l.estate_area" not in sql.FUNNEL_SQL
    assert "(l.area_m2 > 0) AS has_area" in sql.FUNNEL_SQL
    # the land TOLERANCE is still the land one — the category still steers that
    assert "'pozemek' IN (a.category_main, b.category_main)" in body


def test_the_identity_attributes_are_rendered_from_the_one_vocabulary() -> None:
    """The per-category identity attributes are spelled once, in `dedup_candidates`, and
    rendered into both faces of the rule: a `pozemek` reaches the pairing SQL with a NULL
    disposition (so C1's join can never take it) and counts as having none in the funnel."""
    rendered = sql.identity_disposition_sql("x.")
    assert "'pozemek'" in rendered and dc.categories_not_comparing("disposition") == ("pozemek",)
    # COALESCE, never a bare NOT IN: an unknown category must KEEP its disposition
    assert "COALESCE(x.category_main, '') NOT IN ('pozemek')" in rendered
    assert "NULLIF(BTRIM(x.disposition), '')" in rendered
    for rung in ("C1", "C3"):
        assert rendered in sql.RUNG_SQL[rung]["rows"]
    assert sql.identity_disposition_sql("l.") in sql.FUNNEL_SQL


@pytest.mark.parametrize("general,pozemek", [(5, 2), (2, 5), (10, 10), (0.5, 0.1), (50, 2)])
def test_band_never_separates_two_areas_within_the_widest_tolerance(general: float, pozemek: float) -> None:
    w = sql.band_width(general, pozemek)
    t = max(general, pozemek) / 100.0
    for a in [7.0, 10.0, 33.3, 55.5, 60.0, 99.99, 100.0, 100.01, 148.5, 1000.0, 12345.6, 99999.0]:
        for b in (a * (1 - t), a / (1 - t), a * (1 - t / 2), a, a * (1 - t) + 1e-9):
            # b within t of a as a percent of the larger side
            assert abs(a - b) / max(a, b) <= t + 1e-12
            band_a, band_b = math.floor(math.log(a) / w), math.floor(math.log(b) / w)
            assert abs(band_a - band_b) <= 1, (a, b, band_a, band_b)


def test_floor_guard_is_two_valued_when_a_category_is_unknown() -> None:
    # category_main is nullable; `NULL = 'byt'` would make the whole guard NULL and either
    # drop the pair or write NULL into a NOT NULL column. The oracle treats None as "not byt".
    for rung in ("C1", "C3"):
        s = sql.RUNG_SQL[rung]["rows"]
        assert "COALESCE(a.category_main, '') = 'byt' AND COALESCE(b.category_main, '') = 'byt'" in s
        assert "(a.category_main = 'byt' AND b.category_main = 'byt'" not in s


def test_band_is_a_bigint_and_a_zero_tolerance_stays_in_range() -> None:
    # tolerance 0 is a registry-legal value ("areas must match exactly"); the sentinel
    # width makes ln(area)/w ~1e10 for a 100 m2 flat — far outside int4, inside bigint.
    assert "::bigint AS band" in sql.RUNG_SQL["C3"]["rows"]
    w = sql.band_width(0, 0)
    for area in (1.0, 8.6, 100.0, 12345.0, 1e9):
        band = math.floor(math.log(area) / w)
        assert -(2**63) < band < 2**63 - 1
    assert math.floor(math.log(100.0) / w) == math.floor(math.log(100.0) / w)
    assert math.floor(math.log(100.0) / w) != math.floor(math.log(100.01) / w)


def test_band_width_edges() -> None:
    assert sql.band_width(5, 2) == pytest.approx(-math.log(0.95))
    assert sql.band_width(100, 2) == 1e9
    assert sql.band_width(0, 0) == 1e-9


def test_rung_params_come_from_the_inputs() -> None:
    p = sql.rung_params(INPUTS)
    assert p == {
        "floor_tolerance": 2, "c1_area_pct": 20.0,
        "area_pct_general": 5.0, "area_pct_pozemek": 2.0,
        "district_split_towns": [554782, 582786, 554821],
        "band_width": pytest.approx(-math.log(0.95)), "active_only": False,
    }
    assert sql.rung_params({**INPUTS, "l0_candidate_scope": "active"})["active_only"] is True


def test_pair_column_order_matches_migration_492() -> None:
    migration = (_ROOT / "migrations" / "492_new_dedup_candidate_store.sql").read_text(encoding="utf-8")
    body = migration.lower().split("create table if not exists dedup_sim.candidate_pairs (")[1].split(");")[0]
    columns = [line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith(("--", "primary", "check"))]
    for name in sql.PAIR_COLUMN_NAMES:
        assert name in columns, name
    assert len(sql.PAIR_COLUMN_NAMES) == 16
    # the select lists have exactly as many expressions as the insert names (count the
    # top-level commas of the select list of each rung)
    for rung, stmts in sql.RUNG_SQL.items():
        select_list = stmts["rows"].split(_MARK, 1)[1].split(" FROM ", 1)[0]
        depth, commas = 0, 0
        for ch in select_list:
            depth += ch == "("
            depth -= ch == ")"
            commas += ch == "," and depth == 0
        assert commas + 1 == len(sql.PAIR_COLUMN_NAMES), rung


# THE LEDGER of the pairing statements. A byte of any of these changes what the generator
# PAIRS, which means `dedup_candidates.GENERATOR_VERSION` has to bump in the same commit —
# minting a new `inputs_id` and orphaning every `candidate_pairs` row written under the old
# meaning. Not a style rule: a deliberate change updates BOTH.
#
# W16 pinned them because that wave was a refactor and had to prove it moved nothing.
# W17 MOVED them deliberately (c3 -> c4): the rule reads `listings.area_m2` — the one
# headline area — instead of its own `estate_area`-for-pozemek CASE, and land no longer
# carries a disposition into the rule. `BLOCKS_SQL` and `TOP_BUCKETS_SQL` did not move:
# neither reads an attribute.
_PAIRING_SHA256 = {
    "_BASE_CTE": "0e48bdb6fc8a86259b30bb142e74ead869e6fdd966925186efd05f444091a218",
    "BLOCKS_SQL": "0bf089244aaa9b4edf59ae2d22ad2c0f2b9884c202b6469ee8eb093ada8349f7",
    "BLOCK_ATTRS_SQL": "adcf02cad6472fa68c9e834502070fecffe8f5ee60e2c654ee47084e2b7a1401",
    "BLOCK_IDS_SQL": "68c458998a802410c4c831268c5f9c849b2f554579696b1b216463285846652d",
    "TOP_BUCKETS_SQL": "3693dbb3e62f59a4033260c7758775337c3d7085b925a4f0c95cb6238ea8b499",
}


def test_the_pairing_statements_are_byte_for_byte_what_they_were() -> None:
    import hashlib

    for name, digest in _PAIRING_SHA256.items():
        statement = getattr(sql, name)
        got = hashlib.sha256(statement.encode("utf-8")).hexdigest()
        assert got == digest, (
            f"{name} changed; if that is deliberate, bump dedup_candidates.GENERATOR_VERSION"
        )


def test_every_rung_statement_is_byte_for_byte_what_it_was() -> None:
    import hashlib
    import json

    canon = json.dumps({k: v for k, v in sorted(sql.RUNG_SQL.items())}, sort_keys=True)
    assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == \
        "7e85adefe9aa4464e11e44448ccac22ce54d31b352e1ff2dab86b83c68795add", (
            "a rung statement changed; bump GENERATOR_VERSION in the same commit"
        )
    assert dc.GENERATOR_VERSION == {"C": "c4"}


def test_the_rank_floor_is_rendered_from_the_one_module() -> None:
    """The refactor's point: four statements repeated the same floor, and the audit page
    spelled a fifth, looser version of "has a town". One renderer now, and it must produce
    the text those statements already carried."""
    from location_data import location_steps as ls

    floor = ls.obec_rank_floor_sql("gr")
    for name in ("_BASE_CTE", "BLOCKS_SQL", "BLOCK_ATTRS_SQL", "TOP_BUCKETS_SQL"):
        assert floor in getattr(sql, name), name


def test_the_funnel_reports_the_shared_step_columns() -> None:
    """W16: the readout's columns ARE the shared step keys. `with_projection` (the same set
    the audit page calls `with_verdict`, under a name that read as "answered") and
    `with_town` (a town test with no consumer rule) are gone."""
    from location_data import location_steps as ls

    for key in ls.SHARED_STEP_KEYS:
        column = "listings" if key == "all_listings" else key
        assert f" as {column}" in sql.FUNNEL_SQL or f" AS {column}" in sql.FUNNEL_SQL, key
    assert "with_projection" not in sql.FUNNEL_SQL
    assert "AS with_town" not in sql.FUNNEL_SQL
    assert set(sql.FUNNEL_COLUMNS) >= {"with_verdict", "located", "located_town",
                                       "located_foreign", "located_no_town"}


def test_stale_sweep_is_scoped_to_one_parameter_set() -> None:
    assert "WHERE inputs_id = %(inputs_id)s::bigint AND generation_id <> %(generation_id)s::bigint" in sql.STALE_SWEEP_SQL


def test_listings_with_candidates_groups_rather_than_distincts() -> None:
    assert "DISTINCT" not in sql.LISTINGS_WITH_CANDIDATES_SQL.upper()
    assert "GROUP BY id, category_main" in sql.LISTINGS_WITH_CANDIDATES_SQL


# ------------------------------------------------------------------ the audit drill-down


def test_every_clickable_figure_has_a_bucket() -> None:
    """The page makes a figure clickable by naming its bucket; a figure whose bucket is not
    here would be a dead link. The chain's own keys are the floor."""
    from location_data import location_steps as ls

    for key in ls.SHARED_STEP_KEYS:
        assert key in sql.AUDIT_BUCKETS, key
    assert "town_no_attribute" in sql.AUDIT_BUCKETS


def test_the_drilldown_reads_the_shared_town_predicate_not_a_copy() -> None:
    """The whole point of W16 is one vocabulary. Every towned bucket must be the string
    `located_town_sql` produces — a hand-written 'obec_kod is not null' here would be the
    second definition that wave removed."""
    from location_data import location_steps as ls

    shared = ls.located_town_sql("b")
    for bucket in ("eligible", "c1_eligible", "c3_eligible", "town_no_attribute"):
        assert shared in sql.AUDIT_BUCKETS[bucket], bucket


def test_the_drilldown_is_keyset_and_never_offset() -> None:
    stmt = sql.audit_listings_statement("town_no_attribute")
    assert "OFFSET" not in stmt.upper()
    assert "ORDER BY b.id DESC" in stmt
    assert "%(after_id)s" in stmt and "%(limit)s" in stmt


def test_the_drilldown_takes_no_operator_text_into_the_statement() -> None:
    """`bucket` is a KEY. An unknown one must raise rather than interpolate."""
    with pytest.raises(KeyError):
        sql.audit_listings_statement("'; drop table listings; --")


def test_every_bucket_builds_a_statement_with_the_same_placeholders() -> None:
    expected = {"active_only", "source", "category_main", "category_type", "after_id", "limit"}
    for bucket in sql.AUDIT_BUCKETS:
        stmt = sql.audit_listings_statement(bucket)
        found = set(re.findall(r"%\((\w+)\)s", stmt))
        assert found == expected, bucket


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — the PREPARE sweep runs only in the CI DB job",
)
def test_every_bucket_prepares_against_the_real_schema() -> None:
    """The drill-down's statements are built by a FUNCTION, so `tests/sql_corpus.discover`
    (which reads module-level `*_SQL` constants) cannot see them. This is their equivalent
    of the schema sweep: every bucket must parse, name-resolve and type-check against the
    live catalog — the layer a fake connection structurally cannot be."""
    import psycopg

    from tests.sql_corpus import to_prepare_form

    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn, conn.cursor() as cur:
        for i, bucket in enumerate(sorted(sql.AUDIT_BUCKETS)):
            cur.execute(
                f"PREPARE dd_{i} AS " + to_prepare_form(sql.audit_listings_statement(bucket))
            )
