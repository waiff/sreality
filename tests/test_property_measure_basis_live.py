"""The property-grain per-m2 measure must have ONE row behind it (migration 424).

`properties.current_price_czk` is the representative child's price. Before W3
`properties.area_m2` was picked independently, in source-trust order, across ALL
children — so a merged property's per-m2 could divide one portal's price by
another portal's area. Only executed SQL can show that the rollup now keeps the
pair together: the fake connections in tests/test_recompute_property_stats.py
assert control flow, and PREPARE only proves the statement compiles.

Runs in CI's migrations job (`TEST_DATABASE_URL`); skipped locally.
"""

from __future__ import annotations

import itertools
import os
import uuid
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
    insert real listings/properties rows into the schema the rest of the CI job
    asserts against."""
    import psycopg

    conn = psycopg.connect(_DB_URL)  # autocommit off: everything below is one transaction
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


# sreality rows must carry a positive sreality_id (the sign CHECK, migration
# 311); every other portal carries NULL or a negative synthetic one.
_SREALITY_IDS = itertools.count(9_100_000_001)


def _new_property(cur: Any) -> int:
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    return int(cur.fetchone()[0])


def _add_child(
    cur: Any,
    pid: int,
    *,
    source: str,
    price: int | None,
    area: float | None,
    usable: float | None = None,
    active: bool = True,
) -> int:
    """One child listing, obeying the sign CHECK (migration 311)."""
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, usable_area, "
        "is_active, property_id) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            next(_SREALITY_IDS) if source == "sreality" else None,
            source,
            f"w3-{uuid.uuid4()}",
            price,
            area,
            usable,
            active,
            pid,
        ),
    )
    return int(cur.fetchone()[0])


def _rollup(cur: Any, pid: int) -> tuple[Any, ...]:
    cur.execute(
        "SELECT current_price_czk, area_m2, usable_area, "
        "       price_per_m2_source_listing_id, repr_listing_ref_id, is_active "
        "FROM properties WHERE id = %s",
        (pid,),
    )
    return cur.fetchone()


def _recompute(cur: Any, pid: int) -> None:
    from scripts.recompute_property_stats import _RECOMPUTE_ONE_SQL

    cur.execute(_RECOMPUTE_ONE_SQL, {"pid": pid})


def _divergent_pair(cur: Any) -> tuple[int, int, int]:
    """The shape that produced the defect: the representative child is NOT the
    most-trusted one, because `repr` orders active-first while the old area pick
    ordered trust-first. A delisted sreality sibling (rank 1) carrying a 1200 m2
    plot area therefore supplied the denominator for an active mmreality
    listing's price.
    """
    pid = _new_property(cur)
    stale = _add_child(
        cur, pid, source="sreality", price=6_000_000, area=1200.0, usable=1100.0,
        active=False,
    )
    live = _add_child(
        cur, pid, source="mmreality", price=5_000_000, area=78.0, usable=72.0,
        active=True,
    )
    return pid, stale, live


def test_denominator_comes_from_the_child_that_supplied_the_price(cur):
    pid, _stale, live = _divergent_pair(cur)
    _recompute(cur, pid)
    price, area, _usable, basis, repr_ref, _active = _rollup(cur, pid)

    assert (price, area) == (5_000_000, 78.0), (
        "price and area must be the active child's; the 1200 m2 belongs to the "
        "delisted sibling and divides into a per-m2 that describes neither listing"
    )
    assert basis == live == repr_ref, "the measure must name the one row it came from"


def test_usable_area_rides_the_same_child_as_the_area(cur):
    pid, _stale, live = _divergent_pair(cur)
    _recompute(cur, pid)
    _price, _area, usable, _basis, _repr, _active = _rollup(cur, pid)
    assert usable == 72.0, (
        "usable_area picked independently is how one portal's floor area ends up "
        f"quoted beside another's usable area (listing {live})"
    )


def test_stamp_never_names_a_row_other_than_the_priced_child(cur):
    """The invariant migration 424 documents, over every shape in this file."""
    pids = [_divergent_pair(cur)[0]]

    plain = _new_property(cur)
    _add_child(cur, plain, source="sreality", price=4_000_000, area=60.0)
    pids.append(plain)

    for pid in pids:
        _recompute(cur, pid)
    cur.execute(
        "SELECT count(*) FROM properties WHERE id = ANY(%s) "
        "AND price_per_m2_source_listing_id IS NOT NULL "
        "AND price_per_m2_source_listing_id IS DISTINCT FROM repr_listing_ref_id",
        (pids,),
    )
    assert cur.fetchone()[0] == 0


def test_priced_child_without_an_area_leaves_the_measure_unlabelled(cur):
    """The spec's fixture: mmreality supplies the area, sreality the price.

    area_m2 still falls back to the sibling — it is a display/filter column and
    NULLing it would drop the property out of every area filter — but the basis
    stamp stays NULL, which is the whole point: no surface may present that
    ratio as the sreality listing's figure.
    """
    pid = _new_property(cur)
    priced = _add_child(cur, pid, source="sreality", price=5_000_000, area=None)
    _add_child(cur, pid, source="mmreality", price=4_900_000, area=1200.0)
    _recompute(cur, pid)
    price, area, _usable, basis, repr_ref, _active = _rollup(cur, pid)

    assert (price, area) == (5_000_000, 1200.0)
    assert repr_ref == priced
    assert basis is None, "a cross-row ratio must not be stamped as one row's measure"


def test_zero_area_is_not_a_valid_basis(cur):
    """The measure's validity bound lives in price_per_m2_basis, not in callers."""
    pid = _new_property(cur)
    _add_child(cur, pid, source="sreality", price=5_000_000, area=0.0)
    _recompute(cur, pid)
    assert _rollup(cur, pid)[3] is None


def test_property_with_no_area_at_all_is_still_recomputed(cur):
    """best_dims is LEFT-JOINed: an inner join would drop the whole property out
    of the UPDATE, silently freezing is_active and every other rolled-up column."""
    pid = _new_property(cur)
    _add_child(cur, pid, source="bazos", price=None, area=None, active=False)
    cur.execute("UPDATE properties SET is_active = true WHERE id = %s", (pid,))
    _recompute(cur, pid)
    _price, area, usable, basis, _repr, active = _rollup(cur, pid)
    assert (area, usable, basis) == (None, None, None)
    assert active is False, "the row must still have been updated"


def test_singleton_insert_path_stamps_the_basis(cur):
    """A brand-new listing gets its property from scraper.db, not the sweep; a
    5-minute NULL basis on every new listing is a hole in the measure."""
    from scraper import db

    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2) "
        "VALUES (NULL, 'bezrealitky', %s, '{}'::jsonb, 'byt', 'prodej', 3000000, 55.0) "
        "RETURNING id",
        (f"w3-single-{uuid.uuid4()}",),
    )
    lid = int(cur.fetchone()[0])
    db._create_singleton_property(cur.connection, lid, "bezrealitky")

    cur.execute(
        "SELECT p.price_per_m2_source_listing_id FROM properties p "
        "JOIN listings l ON l.property_id = p.id WHERE l.id = %s",
        (lid,),
    )
    assert cur.fetchone()[0] == lid


def test_source_trust_rank_is_not_reordered_around_a_parser_bug(cur):
    """mmreality outranking five portals is what lets a listing-grain area defect
    reach a merged property. Re-ranking it would also silently change survivorship
    for the ~30 other fields that share this order (rule 21) — W3 fixes the grain,
    W1 fixes the parser, and the ranks stay put."""
    cur.execute(
        "SELECT source_trust_rank(s) FROM unnest(ARRAY['sreality','bezrealitky',"
        "'idnes','mmreality','remax','maxima','ceskereality','realitymix','bazos']) s"
    )
    assert [r[0] for r in cur.fetchall()] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
