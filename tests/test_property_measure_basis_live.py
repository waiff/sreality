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
    pid: int | None,
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


def _skew_property_ids_past(cur: Any, listing_id: int) -> None:
    """Burn properties.id values until the next one is above `listing_id`.

    listings.id and properties.id are independent sequences, so in a fresh CI
    database they collide — and then `l.id` and `l.property_id` are the SAME
    number and a stamp assertion cannot tell the two apart. That is exactly the
    confusion migration 424's column comment warns about, so the tests below make
    the two ranges disjoint before asserting on them."""
    while True:
        cur.execute("SELECT nextval(pg_get_serial_sequence('properties', 'id'))")
        if int(cur.fetchone()[0]) > listing_id:
            return


def _stamp_of_child(cur: Any, listing_id: int) -> Any:
    cur.execute(
        "SELECT p.id, p.price_per_m2_source_listing_id FROM properties p "
        "JOIN listings l ON l.property_id = p.id WHERE l.id = %s",
        (listing_id,),
    )
    return cur.fetchone()


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


def test_usable_area_keeps_its_independent_trust_order_pick(cur):
    """W3 changes the per-m2 DENOMINATOR. usable_area is not it.

    usable_area is a live Browse + Watchdog filter column (properties ->
    browse_list.usable_area -> browse_stats_properties' usable_area_min/max_filter,
    and the matcher's min/max_usable_area over properties_public). Binding it to
    the representative child would silently narrow every saved filter that uses
    it, so it keeps the golden-record pick it has always had: the best non-NULL
    value in source-trust order — here the delisted sreality sibling's, because
    trust beats liveness for a field's best-known value.
    """
    pid, stale, _live = _divergent_pair(cur)
    _recompute(cur, pid)
    _price, area, usable, _basis, _repr, _active = _rollup(cur, pid)
    assert area == 78.0, "the denominator still comes from the representative child"
    assert usable == 1100.0, (
        f"usable_area must still be the trust-order pick (listing {stale}); "
        "rebinding it to the representative child is a different measure's wave"
    )


def test_a_siblings_usable_area_survives_a_repr_child_that_has_none(cur):
    """The shape that a coherence rule on usable_area would silently NULL.

    The representative child carries an area but no usable_area; a sibling
    carries one. Taking usable_area from the representative child returns NULL
    here — and the property drops out of a saved "usable area >= 60" Browse view
    and stops firing its watchdog, for a field this wave does not define.
    """
    pid = _new_property(cur)
    repr_child = _add_child(
        cur, pid, source="sreality", price=5_000_000, area=78.0, usable=None,
    )
    _add_child(cur, pid, source="idnes", price=4_900_000, area=None, usable=72.0)
    _recompute(cur, pid)
    _price, area, usable, basis, repr_ref, _active = _rollup(cur, pid)

    assert repr_ref == repr_child
    assert (area, usable) == (78.0, 72.0), "no property may lose a usable_area it had"
    assert basis == repr_child, "the measure itself is unaffected: one row backs it"


def test_the_area_fallback_skips_a_higher_trust_child_that_carries_no_area(cur):
    """The fallback must reproduce the pre-W3 area pick: the best-ranked child
    THAT HAS AN AREA. A more-trusted sibling carrying only a usable_area is not a
    denominator — selecting it would leave properties.area_m2 NULL and drop the
    property out of every area and per-m2 filter."""
    pid = _new_property(cur)
    priced = _add_child(cur, pid, source="sreality", price=5_000_000, area=None)
    _add_child(cur, pid, source="idnes", price=4_900_000, area=None, usable=90.0)
    dimensional = _add_child(cur, pid, source="mmreality", price=4_800_000, area=70.0)
    _recompute(cur, pid)
    _price, area, usable, basis, repr_ref, _active = _rollup(cur, pid)

    assert repr_ref == priced
    assert area == 70.0, f"the area must come from listing {dimensional}, which has one"
    assert usable == 90.0
    assert basis is None, "price and area came from two rows — nothing to stamp"


def test_stamp_never_names_a_row_other_than_the_priced_child(cur):
    """The invariant migration 424 documents, over the shapes below — a stamped
    measure always names the same row repr_listing_ref_id does."""
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
    stamp stays NULL, which is the whole point: the ratio describes neither
    listing, and the label says so.
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
    """best_area is LEFT-JOINed: an inner join would drop the whole property out
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

    lid = _add_child(cur, None, source="bezrealitky", price=3_000_000, area=55.0)
    _skew_property_ids_past(cur, lid)
    db._create_singleton_property(cur.connection, lid, "bezrealitky")

    pid, stamp = _stamp_of_child(cur, lid)
    assert stamp == lid != pid, "the stamp is a listings.id, not the property's own id"


def test_cheap_rollup_restamps_an_already_linked_singleton(cur):
    """_cheap_property_rollup runs on EVERY re-scrape of an already-linked
    listing (db._ensure_property dispatches to it), so it — not the sweep — is
    what keeps a singleton's basis true as its price and area move."""
    from scraper import db

    lid = _add_child(cur, None, source="bazos", price=2_500_000, area=48.0)
    _skew_property_ids_past(cur, lid)
    db._ensure_property(cur.connection, lid, "bazos")
    pid, _first = _stamp_of_child(cur, lid)

    cur.execute(
        "UPDATE properties SET price_per_m2_source_listing_id = NULL WHERE id = %s",
        (pid,),
    )
    db._ensure_property(cur.connection, lid, "bazos")  # already linked -> cheap rollup

    assert _stamp_of_child(cur, lid) == (pid, lid), (
        "the rollup must stamp the CHILD's listings.id; stamping l.property_id "
        "instead type-checks, passes a substring assertion, and is wrong"
    )


def test_straggler_attach_stamps_the_basis(cur):
    """The sweep's attach path adopts every property_id-NULL listing. It inserts
    the property directly, so it stamps the basis itself or the row waits a full
    sweep unlabelled."""
    from scripts.recompute_property_stats import _attach_stragglers

    lid = _add_child(cur, None, source="remax", price=7_100_000, area=91.0)
    _skew_property_ids_past(cur, lid)
    _attach_stragglers(cur.connection, skip_native_backfill=True)

    pid, stamp = _stamp_of_child(cur, lid)
    assert stamp == lid != pid


def test_unmerge_split_stamps_each_detached_child(cur):
    """split_property_to_singletons builds a fresh property per detached child
    and only recompute_mf_one (not the golden recompute) runs on the new ids, so
    an unstamped insert there leaves the measure unlabelled indefinitely."""
    from toolkit.property_identity import split_property_to_singletons

    pid = _new_property(cur)
    anchor = _add_child(cur, pid, source="sreality", price=5_000_000, area=80.0)
    detached = _add_child(cur, pid, source="idnes", price=4_800_000, area=64.0)
    _skew_property_ids_past(cur, detached)

    result = split_property_to_singletons(cur.connection, property_id=pid)
    assert result["data"]["detached_listing_ids"] == [detached]

    new_pid, stamp = _stamp_of_child(cur, detached)
    assert new_pid != pid
    assert stamp == detached != new_pid
    assert _rollup(cur, pid)[3] == anchor, "the survivor keeps its own basis"


def test_source_trust_rank_is_not_reordered_around_a_parser_bug(cur):
    """mmreality outranking five portals is what lets a listing-grain area defect
    reach a merged property. Re-ranking it would also silently change survivorship
    for the ~30 other fields that share this order (rule 21) — W3 fixes the grain,
    W1 fixes the parser, and the ranks stay put."""
    # WITH ORDINALITY + ORDER BY: unnest() row order is not guaranteed, so a bare
    # SELECT would be asserting on an ordering Postgres never promised.
    cur.execute(
        "SELECT source_trust_rank(s) FROM unnest(ARRAY['sreality','bezrealitky',"
        "'idnes','mmreality','remax','maxima','ceskereality','realitymix','bazos']) "
        "WITH ORDINALITY AS t(s, n) ORDER BY t.n"
    )
    assert [r[0] for r in cur.fetchall()] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
