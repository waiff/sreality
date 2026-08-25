"""One measure, one definition, one label — proven against the replayed schema.

Migration 425 replaced eight hand-typed `price / area` expressions with a single
named measure. The property that matters is not that each view compiles: it is
that ALL of them return the SAME number for the SAME row, down to the last digit.
That is unassertable offline — the fake DB connections cannot evaluate SQL, and
`PREPARE` (tests/test_sql_schema_prepare.py) type-checks a statement without ever
computing a value, so a view left on the UNROUNDED form would pass every existing
gate while silently skipping rows at the SPA's keyset page seam (migration 200).

So: seed one property + its one listing, then read the per-m2 figure back through
every live relation that publishes it and require byte identity.

Runs in CI's migrations job (`TEST_DATABASE_URL`); skipped locally.
"""

from __future__ import annotations

import itertools
import os
import uuid
from decimal import Decimal
from typing import Any

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)


@pytest.fixture()
def cur():
    """Each test runs in a transaction that is ALWAYS rolled back — these tests
    insert real properties/listings/browse_list rows into the schema the rest of
    the CI job asserts against."""
    import psycopg

    # Bounded from the start: this fixture writes to `properties` / `listings` /
    # `browse_list`, which other steps of the same job also touch, so a lock wait
    # here would hang the runner until the job timeout rather than reporting
    # anything. A blocked statement now fails in seconds with the blocker named.
    conn = psycopg.connect(  # autocommit off: everything below is one transaction
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


# sreality rows must carry a positive sreality_id (the sign CHECK, migration
# 311); every other portal carries NULL or a negative synthetic one.
_SREALITY_IDS = itertools.count(9_200_000_001)

# Never walked with nextval anywhere in this module: listings_id_seq STARTs AT
# 10,000,000 while properties.id starts at 1, and a one-at-a-time loop over that
# gap hung a CI runner for 15 minutes without tripping statement_timeout (every
# individual statement was fast). Ids come back from RETURNING instead.


def _seed(
    cur: Any,
    *,
    price: int,
    area: float,
    category_main: str = "byt",
    category_type: str = "prodej",
) -> tuple[int, int, str]:
    """One property and its single representative child, with a coherent
    numerator and denominator (that coherence is W3 / migration 424's job; this
    wave assumes it and measures the ratio)."""
    district = f"w4-{uuid.uuid4()}"
    sid = next(_SREALITY_IDS)

    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    pid = int(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, disposition, district, "
        "is_active, published_at, property_id) "
        "VALUES (%s, 'sreality', %s, '{}'::jsonb, %s, %s, %s, %s, '3+kk', %s, "
        "true, now(), %s) RETURNING id",
        (sid, f"w4-{uuid.uuid4()}", category_main, category_type, price, area,
         district, pid),
    )
    lid = int(cur.fetchone()[0])

    cur.execute(
        "UPDATE properties SET category_main = %s, category_type = %s, "
        "       current_price_czk = %s, area_m2 = %s, disposition = '3+kk', "
        "       district = %s, status = 'active', is_active = true, "
        "       published_at = now(), repr_listing_id = %s, "
        "       repr_listing_ref_id = %s "
        " WHERE id = %s",
        (category_main, category_type, price, area, district, sid, lid, pid),
    )

    # browse_list is a materialised copy of browse_projection (`select *`), so
    # this is how the row reaches the two Browse RPCs without paying for a full
    # rebuild_browse_list() inside the test transaction.
    cur.execute(
        "INSERT INTO browse_list SELECT * FROM browse_projection WHERE property_id = %s",
        (pid,),
    )
    assert cur.rowcount == 1, "browse_projection did not publish the seeded property"

    return pid, lid, district


def _put_on_the_board(cur: Any, pid: int) -> None:
    """pipeline_board_public is invoker-mode over property_pipeline_public, which
    is account-scoped, so the card needs an account and a stage of its own."""
    cur.execute("INSERT INTO accounts (name) VALUES ('w4-measure') RETURNING id")
    account_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pipeline_stages (key, label, position, is_entry, account_id) "
        "VALUES (%s, 'W4', 1, true, %s) RETURNING id",
        (f"w4-{uuid.uuid4()}", account_id),
    )
    stage_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO property_pipeline (property_id, stage_id, board_position, "
        "entered_stage_at, added_at, updated_at, account_id) "
        "VALUES (%s, %s, 1, now(), now(), now(), %s)",
        (pid, stage_id, account_id),
    )


def _one(cur: Any, sql: str, args: tuple[Any, ...]) -> Any:
    cur.execute(sql, args)
    row = cur.fetchone()
    return None if row is None else row[0]


def _every_surface(cur: Any, pid: int, lid: int) -> dict[str, Any]:
    """The per-m2 figure as each live relation publishes it."""
    return {
        "listings_public": _one(
            cur, "SELECT price_per_m2 FROM listings_public WHERE id = %s", (lid,)
        ),
        "properties_public": _one(
            cur, "SELECT price_per_m2 FROM properties_public WHERE property_id = %s",
            (pid,),
        ),
        "browse_projection": _one(
            cur, "SELECT price_per_m2 FROM browse_projection WHERE property_id = %s",
            (pid,),
        ),
        "listing_feed_public": _one(
            cur, "SELECT price_per_m2 FROM listing_feed_public WHERE id = %s", (lid,)
        ),
        "pipeline_board_public": _one(
            cur, "SELECT price_per_m2 FROM pipeline_board_public WHERE property_id = %s",
            (pid,),
        ),
    }


def _bases(cur: Any, pid: int, lid: int) -> dict[str, Any]:
    return {
        "listings_public": _one(
            cur, "SELECT price_per_m2_basis FROM listings_public WHERE id = %s", (lid,)
        ),
        "properties_public": _one(
            cur,
            "SELECT price_per_m2_basis FROM properties_public WHERE property_id = %s",
            (pid,),
        ),
        "browse_projection": _one(
            cur,
            "SELECT price_per_m2_basis FROM browse_projection WHERE property_id = %s",
            (pid,),
        ),
        "listing_feed_public": _one(
            cur, "SELECT price_per_m2_basis FROM listing_feed_public WHERE id = %s",
            (lid,),
        ),
        "pipeline_board_public": _one(
            cur,
            "SELECT price_per_m2_basis FROM pipeline_board_public WHERE property_id = %s",
            (pid,),
        ),
    }


def _browse_stats(cur: Any, pids: int | list[int]) -> dict[str, Any]:
    ids = [pids] if isinstance(pids, int) else list(pids)
    return _one(
        cur, "SELECT browse_stats_properties(property_ids_filter => %s::bigint[])",
        (ids,),
    )


def _region_stats(cur: Any, district: str) -> dict[str, Any]:
    return _one(
        cur, "SELECT region_stats(districts_filter => %s::text[])", ([district],)
    )


# --------------------------------------------------------------------------
# The keystone assertion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price, area, expected",
    [
        # An exact quotient: every surface must agree, and nothing may drift.
        (5_000_000, 50.0, Decimal("100000.00")),
        # A REPEATING quotient (1e6 / 33 = 30303.0303...). This is the case that
        # separates the rounded views from the unrounded ones: before 425
        # listings_public and listing_feed_public returned ~18 significant
        # digits here while properties_public and browse_projection returned two.
        (1_000_000, 33.0, Decimal("30303.03")),
    ],
)
def test_every_relation_publishes_the_identical_per_m2(cur, price, area, expected):
    pid, lid, district = _seed(cur, price=price, area=area)
    _put_on_the_board(cur, pid)

    got = _every_surface(cur, pid, lid)
    assert set(got.values()) == {expected}, (
        f"the six surfaces disagree about one row's Kc/m2: {got} — a consumer "
        f"re-derived the formula instead of reading measure_price_per_m2"
    )
    for name, value in got.items():
        assert value.as_tuple().exponent == -2, (
            f"{name} returned {value} — the measure must be round(x, 2). An "
            f"unrounded numeric does not round-trip through a JS Number and the "
            f"SPA keyset cursor silently skips rows at the page seam "
            f"(migration 200)"
        )

    # browse_stats_properties publishes the same measure through percentile_cont
    # ::int; with a single row every percentile IS that row.
    stats = _browse_stats(cur, pid)
    assert stats["total"] == 1, stats
    assert stats["ppm2"] == {
        "p25": int(expected),
        "p50": int(expected),
        "p75": int(expected),
    }, f"browse_stats_properties disagrees with the views: {stats['ppm2']}"


def test_every_relation_publishes_the_identical_basis_label(cur):
    pid, lid, district = _seed(cur, price=5_000_000, area=50.0)
    _put_on_the_board(cur, pid)

    bases = _bases(cur, pid, lid)
    assert set(bases.values()) == {"sale_capital_czk_m2"}, bases
    assert _browse_stats(cur, pid)["ppm2_basis"] == "sale_capital_czk_m2"
    assert _region_stats(cur, district)["ppm2_basis"] == "sale_capital_czk_m2"


@pytest.mark.parametrize(
    "category_main, category_type, expected_basis",
    [
        ("byt", "prodej", "sale_capital_czk_m2"),
        ("byt", "pronajem", "rent_monthly_czk_m2"),
        ("pozemek", "prodej", "land_capital_czk_m2"),
        # Rent wins over land: a rented plot is a MONTHLY figure and must never
        # be labelled capital.
        ("pozemek", "pronajem", "rent_monthly_czk_m2"),
        # Capital category_type is an enumerated allowlist, so drazba/podil keep
        # their figure ...
        ("dum", "drazba", "sale_capital_czk_m2"),
        ("dum", "podil", "sale_capital_czk_m2"),
    ],
)
def test_basis_resolves_from_the_category_pair(
    cur, category_main, category_type, expected_basis
):
    pid, lid, _district = _seed(
        cur, price=5_000_000, area=50.0,
        category_main=category_main, category_type=category_type,
    )
    # pipeline_board_public is a LEFT JOIN off property_pipeline_public: with no
    # card the property has no row there at all, so _one() returns None and the
    # set picks up a phantom disagreement that is really an empty result.
    _put_on_the_board(cur, pid)
    assert set(_bases(cur, pid, lid).values()) == {expected_basis}


def test_an_unknown_category_type_yields_no_measure_and_no_label(cur):
    """... and anything OUTSIDE the vocabulary yields a visible gap, never a
    guess. A silent basis switch is strictly worse than a missing number."""
    pid, lid, _district = _seed(
        cur, price=5_000_000, area=50.0, category_type="zcela-novy-typ"
    )
    _put_on_the_board(cur, pid)
    assert set(_every_surface(cur, pid, lid).values()) == {None}
    assert set(_bases(cur, pid, lid).values()) == {None}


@pytest.mark.parametrize(
    "category_main, category_type, price, survives",
    [
        # Rent floor: 1 000 CZK. "136 Kc" commercial rentals are a Kc/m2/month
        # advert price mis-parsed as a total; 136 / 250 m2 = 0.54 is not a number
        # any surface should render.
        ("komercni", "pronajem", 999, False),
        ("komercni", "pronajem", 1_000, True),
        # Sale floor: 100 000 CZK.
        ("byt", "prodej", 99_999, False),
        ("byt", "prodej", 100_000, True),
        # Land has NO floor: cheap plots are real, and a plot denominator makes
        # a genuinely small Kc/m2.
        ("pozemek", "prodej", 50, True),
    ],
)
def test_per_basis_validity_floors(cur, category_main, category_type, price, survives):
    pid, lid, _district = _seed(
        cur, price=price, area=50.0,
        category_main=category_main, category_type=category_type,
    )
    _put_on_the_board(cur, pid)  # see the note in the basis test above
    values = _every_surface(cur, pid, lid)
    if survives:
        assert None not in values.values(), values
    else:
        assert set(values.values()) == {None}, values

    # ... and the price itself is untouched. The floor withholds a DERIVED
    # figure; destroying a scraped price to protect it would be backwards.
    assert _one(cur, "SELECT price_czk FROM listings WHERE id = %s", (lid,)) == price
    assert (
        _one(cur, "SELECT current_price_czk FROM properties WHERE id = %s", (pid,))
        == price
    )
    # The label survives the floor: the basis is a property of the category
    # pair, not of the value.
    assert set(_bases(cur, pid, lid).values()) == {
        "rent_monthly_czk_m2" if category_type == "pronajem"
        else "land_capital_czk_m2" if category_main == "pozemek"
        else "sale_capital_czk_m2"
    }


def test_a_mixed_cohort_is_labelled_mixed_not_guessed(cur):
    """`category_type_filter` is nullable by architectural rule 22 ("Vse"), so a
    cohort pooling a capital sale and a monthly rent is ONE click away. It must
    say so rather than pick one of the two."""
    sale, _l1, district = _seed(cur, price=5_000_000, area=50.0)
    cur.execute(
        "INSERT INTO properties (category_main, category_type, current_price_czk, "
        "area_m2, district, status, is_active, published_at) "
        "VALUES ('byt', 'pronajem', 20000, 50, %s, 'active', true, now()) RETURNING id",
        (district,),
    )
    rent = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO browse_list SELECT * FROM browse_projection WHERE property_id = %s",
        (rent,),
    )

    stats = _browse_stats(cur, [sale, rent])
    assert stats["total"] == 2, stats
    assert stats["ppm2_basis"] == "mixed", (
        "a cohort pooling capital sale prices and monthly rents into one Kc/m2 "
        "distribution must be labelled 'mixed'"
    )
    assert _region_stats(cur, district)["ppm2_basis"] == "mixed"


def test_region_stats_can_finally_be_scoped_to_one_basis(cur):
    """Before 425 region_stats had NO category parameter at all: sale flats,
    monthly rentals, houses and land pooled into one Kc/m2 distribution
    unconditionally. This is the fix, and the two new parameters are appended
    with defaults so every existing 5-argument call keeps working."""
    _sale, _l1, district = _seed(cur, price=5_000_000, area=50.0)
    cur.execute(
        "INSERT INTO properties (category_main, category_type, current_price_czk, "
        "area_m2, district, status, is_active, published_at) "
        "VALUES ('byt', 'pronajem', 20000, 50, %s, 'active', true, now())",
        (district,),
    )

    pooled = _region_stats(cur, district)
    assert pooled["ppm2_basis"] == "mixed"

    scoped = _one(
        cur,
        "SELECT region_stats(districts_filter => %s::text[], "
        "                    category_type_filter => 'prodej')",
        ([district],),
    )
    assert scoped["ppm2_basis"] == "sale_capital_czk_m2"
    assert scoped["ppm2"]["p50"] == 100_000
    assert scoped["total_active"] == 1


# --------------------------------------------------------------------------
# The plans
# --------------------------------------------------------------------------


def _plan(cur: Any, sql: str, args: tuple[Any, ...] = ()) -> str:
    cur.execute(sql, args)
    return "\n".join(r[0] for r in cur.fetchall())


def test_the_measure_inlines_into_the_calling_query(cur):
    """The single most important non-obvious property of migration 425.

    A SQL function carrying a `SET` clause (e.g. `SET search_path`) cannot be
    inlined: the planner stops folding the body into the query, every predicate
    on the measure becomes a per-row function call, and index conditions that
    used to push down stop pushing down. Adding a SET clause is a one-line,
    plausible-looking "hardening" change — this is what catches it.
    """
    plan = _plan(
        cur,
        "EXPLAIN (VERBOSE, COSTS OFF) SELECT measure_price_per_m2("
        "price_czk::numeric, area_m2::numeric, category_main, category_type) "
        "FROM listings",
    )
    assert "measure_price_per_m2(" not in plan, (
        "measure_price_per_m2 did NOT inline — the plan still shows a function "
        "call. Almost certainly a SET clause (or a non-IMMUTABLE / multi-"
        "statement body) was added to it; see migration 425's header"
    )
    assert "CASE" in plan, plan

    basis_plan = _plan(
        cur,
        "EXPLAIN (VERBOSE, COSTS OFF) SELECT measure_price_per_m2_basis("
        "category_main, category_type) FROM listings",
    )
    assert "measure_price_per_m2_basis(" not in basis_plan, basis_plan


def test_browse_list_keeps_its_covering_indexes(cur):
    """Migration 371 retyped a body from a file and silently regressed migration
    283's anon grant plus three covering indexes; 376 exists only to repair that.
    425 re-emits browse_projection, and rebuild_browse_list() rebuilds the table
    (and its indexes) from it — so the indexes are exactly what a careless
    re-emission would take out again."""
    cur.execute(
        "SELECT indexname FROM pg_indexes "
        " WHERE schemaname = 'public' AND tablename = 'browse_list'"
    )
    have = {r[0] for r in cur.fetchall()}
    assert {
        "browse_list_pk",
        "browse_list_cat_first_seen_idx",
        "browse_list_obec_price_idx",
        "browse_list_okres_price_idx",
        "browse_list_region_price_idx",
    } <= have, have

    # ... and the planner can still reach them. seqscan is disabled because the
    # replayed table is tiny, where a sequential scan would win on cost and hide
    # a missing index.
    cur.execute("SET LOCAL enable_seqscan = off")
    plan = _plan(
        cur,
        "EXPLAIN (COSTS OFF) SELECT property_id, price_per_m2 FROM browse_list "
        " WHERE obec_id = 554782 AND category_type = 'prodej' "
        "   AND price_czk BETWEEN 1000000 AND 9000000 AND price_per_m2 >= 50000",
    )
    assert "browse_list_obec_price_idx" in plan, plan


def test_properties_public_keyset_paging_still_uses_an_index(cur):
    """The SPA pages Browse with (price_per_m2, property_id) keyset seeks. The
    measure sits in the ORDER BY and in the seek predicate, so a non-inlinable
    or non-IMMUTABLE measure turns this into a full scan + sort of `properties`."""
    cur.execute("SET LOCAL enable_seqscan = off")
    plan = _plan(
        cur,
        "EXPLAIN (COSTS OFF) SELECT property_id, price_per_m2 FROM properties_public "
        " WHERE category_main = 'byt' AND category_type = 'prodej' "
        "   AND (price_per_m2 > 95000.00 "
        "        OR (price_per_m2 = 95000.00 AND property_id > 0)) "
        " ORDER BY price_per_m2 ASC, property_id ASC LIMIT 30",
    )
    assert "Seq Scan on properties" not in plan, plan
    assert "Index" in plan, plan
    # The measure must appear EXPANDED in the sort key, not as a call.
    assert "measure_price_per_m2(" not in plan, plan
