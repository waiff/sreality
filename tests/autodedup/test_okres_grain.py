"""E961 — an OKRES grain for the real-time lane's scope, so a kraj is seeded as its districts.

Středočeský kraj (27) is 130,207 located adverts in 1,133 towns and 12 okresy: as town blocks it
is 1,133 walks against the rolling-day cap of 60 (19 days before the build has listed it) and a
scope row of 1,133 entries; as okres blocks it is 12. The grain is spelled `okres:` in `rt_scope`
(RÚIAN, like `obec:` / `cast_obce:`) and `district:` in the apply scope (English, like `town:` /
`quarter:`). `okres_kod` has no index, so an okres is walked through its own obce on the town
index and filtered on its code; and nothing is named beside a block that already holds it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup import apply as AP
from autodedup import incremental_sql as S
from autodedup import legacy_retire as L
from autodedup import reconcile, rt_equivalence
from autodedup.incremental_lane import (
    CURSOR_NEW,
    ENTER_INTERVAL_HOURS,
    LANE_NAME,
    MAX_ENTER_SCANS_PER_DAY,
    SqlWork,
    resolve_scope_districts,
    resolve_scope_parents,
    run_incremental,
    scope_setting_key,
    walk_blocks,
)
from autodedup.incremental_scope import (
    DEFAULT_SCOPE,
    GRAIN_OF,
    Scope,
    ScopeBlock,
    ScopeError,
    parse_scope,
)
from autodedup import incremental_lane
from scraper import realtime_worker as rw
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import EMPTY_PAYLOAD, FakePg, _in_scope
from tests.autodedup.test_legacy_retire import LiveStreamDb, _factory, _intact_pair
from tests.autodedup.test_one_lane import RT, LaneDb
from tests.autodedup.test_apply import _pair_group
from tests.autodedup.test_rt_equivalence import LIVE, _db as _equivalence_db
from tests.autodedup.test_rt_equivalence import _run as _equivalence_run

GEN = "rt"
PRAHA_VYCHOD, PRAHA_ZAPAD, BEROUN_OKRES = 3209, 3210, 3202
# Two obce of Praha-východ (Brandýs nad Labem-Stará Boleslav, Čelákovice) and Beroun's town.
BRANDYS, CELAKOVICE, BEROUN = 538043, 538132, 531057
PRAHA, VYSOCANY = 554782, 490245
DISTRICT = Scope((ScopeBlock("okres", PRAHA_VYCHOD),))


def _register(db: FakePg) -> FakePg:
    """`public.ruian_admin_units` as the two lookups read it: okres -> its obce (each okres
    placed under a kraj), quarter -> its obec."""
    db.admin_districts.update({PRAHA_VYCHOD: [BRANDYS, CELAKOVICE],
                               BEROUN_OKRES: [BEROUN, 531065]})
    db.admin_parents[VYSOCANY] = PRAHA
    return db


def _place(db: FakePg, listing_id: int, *, obec: int | None, okres: int | None,
           granularity: str = "address") -> None:
    db.locations[listing_id] = {"obec_kod": obec, "cast_obce_kod": None, "okres_kod": okres,
                                "granularity": granularity}


def _arrived(db: FakePg, listing_id: int, **place: Any) -> None:
    db.listings[listing_id] = {"first_seen_at": db.now - timedelta(days=9),
                               "inactive_at": None, "is_active": True}
    _place(db, listing_id, **place)


def _fp_row() -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d", "cell_key": "o1",
            "cell_group": "byt", "is_active": True, "ev_images": 0, "ev_phash": 0,
            "ev_clip": 0, "ev_tags": 0, "ev_complete": True,
            "first_decided_at": lane_world.EXPORTED_AT}


# ------------------------------------------------------------------ the grammar


def test_an_okres_is_a_scope_grain_of_its_own_spelled_as_ruian_spells_it() -> None:
    scope = parse_scope(f"okres:{PRAHA_VYCHOD} obec:563510")
    assert scope.okres_codes == [PRAHA_VYCHOD] and scope.obec_codes == [563510]
    assert parse_scope([{"grain": "okres", "code": str(PRAHA_VYCHOD)}]).blocks == DISTRICT.blocks
    assert parse_scope(scope.as_json()).key() == scope.key()
    assert scope.label() == f"okres:{PRAHA_VYCHOD} obec:563510"
    assert scope.params() == {"all_scope": False, "obec": [563510], "cast_obce": [],
                              "okres": [PRAHA_VYCHOD]}
    # The apply scope's English spellings stay its own, and there is no kraj grain.
    for refused in (f"district:{PRAHA_VYCHOD}", "town:563510", "kraj:27"):
        with pytest.raises(ScopeError, match="unknown scope grain"):
            parse_scope(refused)


def test_the_apply_scopes_district_is_the_lanes_okres_and_the_default_has_none() -> None:
    assert GRAIN_OF == {"town": "obec", "quarter": "cast_obce", "district": "okres"}
    assert DEFAULT_SCOPE.okres_codes == [], "no cohort block is a district"


# ------------------------------------------------------------------ the register


def test_an_okres_block_needs_the_register_to_place_it_under_a_kraj_with_its_obce() -> None:
    db = _register(FakePg())
    assert resolve_scope_districts(db, DISTRICT, {}) == {PRAHA_VYCHOD: (BRANDYS, CELAKOVICE)}
    read = len(db.statements)
    assert resolve_scope_districts(db, parse_scope("obec:563510"), {}) == {}
    assert len(db.statements) == read, "no okres, no register read"
    with pytest.raises(ScopeError, match=r"okres \[9999\] .* cannot place under a kraj"):
        resolve_scope_districts(db, parse_scope("okres:9999"), {})


def test_a_town_or_a_quarter_beside_its_own_okres_is_refused() -> None:
    """E941's rule one grain up: the okres block already holds every advert of Beroun, so
    beside it the town would sit in the snapshot twice and wait on the whole district's walk."""
    db = _register(FakePg())
    town = parse_scope(f"okres:{BEROUN_OKRES} obec:{BEROUN}")
    with pytest.raises(ScopeError,
                       match=rf"obec:{BEROUN} beside its own okres okres:{BEROUN_OKRES}"):
        resolve_scope_districts(db, town, resolve_scope_parents(db, town))
    db.admin_districts[9999] = [PRAHA]                  # a register that put Praha in an okres
    quarter = parse_scope(f"okres:9999 cast_obce:{VYSOCANY}")
    with pytest.raises(ScopeError, match=rf"cast_obce:{VYSOCANY} beside its own okres okres:9999"):
        resolve_scope_districts(db, quarter, resolve_scope_parents(db, quarter))
    apart = parse_scope(f"okres:{PRAHA_VYCHOD} obec:{BEROUN} cast_obce:{VYSOCANY}")
    assert resolve_scope_districts(db, apart, resolve_scope_parents(db, apart)) == {
        PRAHA_VYCHOD: (BRANDYS, CELAKOVICE)}


def test_a_pass_over_a_town_beside_its_okres_refuses_before_the_lease() -> None:
    db = _register(FakePg())
    db.calibration[GEN] = {"digest": "d", "n_listings": 3, "payload_zlib": EMPTY_PAYLOAD,
                           "settings": {}, "model_version": "hand_v1"}
    db.settings[scope_setting_key(GEN)] = [{"grain": "okres", "code": BEROUN_OKRES},
                                           {"grain": "obec", "code": BEROUN}]
    with pytest.raises(SystemExit, match="beside its own okres"):
        run_incremental(lambda: db)
    assert not db.lease and not db.cursors and not db.rt_fp


def _kraj_world() -> FakePg:
    """`lane_world`'s nine adverts in Jablonec nad Nisou, placed in its okres (3504)."""
    db = lane_world.world()
    for place in db.locations.values():
        place["okres_kod"] = 3504
    db.admin_districts[3504] = [563510]
    return db


def test_a_seed_naming_a_town_beside_its_okres_writes_nothing(tmp_path: Path) -> None:
    """Refused before the storage guard, the lease and the reset — Beroun's case: the town
    leaves the scope when its okres joins it (E961)."""
    db = _kraj_world()
    with pytest.raises(SystemExit, match="obec:563510 beside its own okres okres:3504"):
        lane_world.seed_lane(db, tmp_path, rt_scope="okres:3504 obec:563510")
    assert LANE_NAME not in db.lease and scope_setting_key(GEN) not in db.settings
    assert not db.scope_ids and not db.calibration


def test_a_seed_over_an_okres_larger_than_a_page_pages_it_to_its_end(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The forward feeds start at today's newest id, so a block the seed listed short of its
    newest adverts would be built without them and judged read; E961 refused such a seed, E940
    pages it: every advert listed, the cut taken over all of them, the block read whole."""
    db = _kraj_world()
    monkeypatch.setattr(incremental_lane, "ENTER_SLICE", 5)
    out = lane_world.seed_lane(db, tmp_path, rt_scope="okres:3504")
    assert [walk["rows"] for walk in out["enter_scan"]["blocks"]] == [5, 4]
    assert sorted(lid for _g, _block, lid in db.scope_ids) == sorted(db.locations)
    assert out["calibration"]["scope_listings"] == 9


def test_a_seed_naming_an_okres_the_register_cannot_place_writes_nothing(
        tmp_path: Path) -> None:
    db = lane_world.world()
    with pytest.raises(SystemExit, match="cannot place under a kraj"):
        lane_world.seed_lane(db, tmp_path, rt_scope="okres:9999")
    assert LANE_NAME not in db.lease and scope_setting_key(GEN) not in db.settings
    assert not db.scope_ids and not db.calibration


# ------------------------------------------------------------------ the walk


def test_the_okres_walk_holds_exactly_what_the_feeds_predicate_holds() -> None:
    """Through its obce on the town index, filtered on its own code: an advert of one of its
    towns, and one located to the district alone; never a neighbour's, even one carrying a
    town code of the district (the filter is what makes the block the predicate).

    This compares two hand-written fakes — `fake_pg`'s walk and its `_in_scope` — so it pins
    the SHAPE only. The evidence for the real SQL is CI's PREPARE sweep and production (the
    review of #1842, 2026-10-17): 783,324 rows carry an `okres_kod` and none an obec outside
    the register's obce for that okres; Praha-východ's walk returns 19,812 = its `okres_kod`
    count; the drift slice with the okres arm reads 81,034 buffers, 89.8 ms against 91.3 ms
    without it."""
    db = _register(FakePg())
    _arrived(db, 1, obec=BRANDYS, okres=PRAHA_VYCHOD)
    _arrived(db, 2, obec=None, okres=PRAHA_VYCHOD, granularity="okres")
    _arrived(db, 3, obec=BEROUN, okres=BEROUN_OKRES)
    _arrived(db, 4, obec=CELAKOVICE, okres=PRAHA_ZAPAD)
    _arrived(db, 5, obec=None, okres=PRAHA_ZAPAD, granularity="okres")
    db.cursors[CURSOR_NEW] = {"last_listing_id": 5_000_000}
    districts = resolve_scope_districts(db, DISTRICT, {})
    work = SqlWork(db, DISTRICT, GEN, straggler_window=0, revive_slice=0, drift_slice=0,
                   districts=districts)
    assert [block.key for block in work.enter_blocks] == [f"okres:{PRAHA_VYCHOD}"]
    entered = [item.listing_id for item in work.claim(50) if item.feed == "entered"]
    assert entered == [1, 2]
    assert {key[1] for key in db.scope_ids} == {f"okres:{PRAHA_VYCHOD}"}
    assert S.RT_SCOPE_OKRES_BLOCK_SQL in db.statements
    assert S.RT_SCOPE_BLOCK_SQL not in db.statements, "the town walk is not the okres walk"
    assert {i for i in db.locations if _in_scope(db, i, DISTRICT.params())} == set(entered)


def test_every_feed_holds_an_advert_by_its_okres_and_retires_one_that_left_it() -> None:
    db = FakePg()
    _arrived(db, 11, obec=BRANDYS, okres=PRAHA_VYCHOD)
    _arrived(db, 12, obec=BEROUN, okres=BEROUN_OKRES)
    db.rt_fp[(GEN, 21)] = _fp_row()
    db.rt_fp[(GEN, 22)] = _fp_row()
    _place(db, 21, obec=CELAKOVICE, okres=PRAHA_VYCHOD)
    _place(db, 22, obec=CELAKOVICE, okres=PRAHA_ZAPAD)  # the geocoder moved it out
    items = SqlWork(db, DISTRICT, GEN, max_retire_fraction=1.0).claim(50)
    assert [i.listing_id for i in items if i.feed == "new"] == [11]
    assert [(i.listing_id, i.retire) for i in items if i.feed == "drifted"] == [(22, True)]


def _asked(towns: int, quarters: int, okresy: int) -> float:
    """Walks a day the cadence asks for."""
    hours = ENTER_INTERVAL_HOURS
    return 24 * (towns / hours["obec"] + quarters / hours["cast_obce"] + okresy / hours["okres"])


def test_an_okres_block_is_walked_on_the_quarter_cadence_under_the_same_cap() -> None:
    """6 h, a quarter's: a district is index-served like a town but a dozen towns' rows (≤ 109
    MB). The cap binds the plan, as it already binds the live scope: the cadence is the tail's
    and 60 walks a day is what it may spend. Only a kraj-only scope fits under it."""
    assert ENTER_INTERVAL_HOURS == {"obec": 1.0, "cast_obce": 6.0, "okres": 6.0}
    assert MAX_ENTER_SCANS_PER_DAY == 60
    assert _asked(8, 112, 0) == 640, "the live rt scope: 8 towns + 112 Prague quarters"
    assert _asked(8, 112, 12) == 688 > MAX_ENTER_SCANS_PER_DAY, "Prague + kraj 27"
    assert _asked(0, 0, 12) == 48 <= MAX_ENTER_SCANS_PER_DAY, "kraj 27 alone"
    db = _register(FakePg())
    _place(db, 1, obec=BRANDYS, okres=PRAHA_VYCHOD)
    districts = resolve_scope_districts(db, DISTRICT, {})

    def walk() -> None:
        SqlWork(db, DISTRICT, GEN, districts=districts).refresh_scope_ids(0)

    walk()
    db.now += timedelta(hours=5)
    walk()
    assert len(db.scope_scans) == 1, "inside its 6 h"
    db.now += timedelta(hours=1)
    walk()
    assert [scan["block_key"] for scan in db.scope_scans] == [f"okres:{PRAHA_VYCHOD}"] * 2


def test_an_okres_over_a_page_is_paged_and_every_pass_names_it_until_its_end(
        tmp_path: Path) -> None:
    """Every grain pages under the one page (E940; E961 marked a walk that filled it TRUNCATED,
    its newest adverts lost): the walk that fills the page names the block mid-cycle and leaves
    it unread, the next walk reads past it, and every pass and its heartbeat name a block inside
    a cycle until its end."""
    db = _register(FakePg())
    for lid in (1, 2, 3):
        _place(db, lid, obec=BRANDYS, okres=PRAHA_VYCHOD)
    districts = resolve_scope_districts(db, DISTRICT, {})
    block = f"okres:{PRAHA_VYCHOD}"
    work = SqlWork(db, DISTRICT, GEN, districts=districts, enter_slice=2)
    work.refresh_scope_ids(0)
    assert (work.enter_scan["pages"], work.enter_scan["page_of"]) == (1, {block: [1, None]})
    assert sorted(lid for _g, _block, lid in db.scope_ids) == [1, 2]
    assert block not in work._walked_ever(), "unread until its pages complete"
    db.now += timedelta(hours=6)
    work = SqlWork(db, DISTRICT, GEN, districts=districts, enter_slice=2)
    work.refresh_scope_ids(0)
    assert sorted(lid for _g, _block, lid in db.scope_ids) == [1, 2, 3], "nothing is lost"
    assert work.enter_scan["page_of"] == {} and block in work._walked_ever()

    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    run_incremental(lambda: world)                       # the build claims all nine
    world.settings[incremental_lane.pages_key(GEN)] = {
        "obec:563510": {"after": 4003, "page": 1, "of": 3}}
    out = run_incremental(lambda: world)                 # nothing due: the 1 h cadence
    assert out["enter_scan"]["page_of"] == {"obec:563510": [1, 3]}
    assert rw._autodedup_outcome(0.0, summary=out)["enter_scan"]["page_of"] == {
        "obec:563510": [1, 3]}


def test_the_okres_walk_rides_the_town_index_and_every_feed_spells_the_okres_arm() -> None:
    walk = " ".join(S.RT_SCOPE_OKRES_BLOCK_SQL.split())
    assert "ll.okres_kod = %(okres)s::bigint" in walk
    assert ("(ll.obec_kod = any(%(obce)s::bigint[]) or (ll.obec_kod is null "
            "and ll.granularity = 'okres'))") in walk
    assert "ll.obec_kod = %(obec)s::bigint" in S.RT_SCOPE_BLOCK_SQL, "the town walk unchanged"
    assert "k.level = 'kraj'" in S.RT_SCOPE_OKRES_OBCE_SQL
    for name in ("RT_NEW_LISTINGS_SQL", "RT_NEW_STRAGGLERS_SQL", "RT_CHANGED_LISTINGS_SQL",
                 "RT_FLIPPED_LISTINGS_SQL", "RT_REVIVED_SQL", "RT_SCOPE_DRIFT_SQL"):
        assert "or ll.okres_kod = any(%(okres)s::bigint[])" in getattr(S, name), name


def test_a_seed_over_an_okres_lists_it_and_the_build_reaches_every_advert(
        tmp_path: Path) -> None:
    db = lane_world.world()
    for place in db.locations.values():
        place["okres_kod"] = 3504                       # Jablonec nad Nisou's okres
    db.admin_districts[3504] = [563510]
    ids = sorted(db.locations)
    out = lane_world.seed_lane(db, tmp_path, rt_scope="okres:3504")
    assert out["scope"] == [{"grain": "okres", "code": 3504}]
    assert db.settings[scope_setting_key(GEN)] == [{"grain": "okres", "code": 3504}]
    assert sorted(lid for _g, block, lid in db.scope_ids if block == "okres:3504") == ids
    run_incremental(lambda: db)
    assert {lid for g, lid in db.rt_fp if g == GEN} == set(ids)


def test_the_equivalence_fallback_refuses_a_town_beside_its_okres(tmp_path: Path) -> None:
    db = _equivalence_db(range(0))                    # no snapshot: the fallback walks
    db.settings[scope_setting_key(LIVE)] = [{"grain": "okres", "code": BEROUN_OKRES},
                                            {"grain": "obec", "code": BEROUN}]
    _register(db)
    with pytest.raises(SystemExit, match=f"obec:{BEROUN} beside its own okres"):
        _equivalence_run(db, tmp_path)


def test_the_equivalence_fallback_walks_an_okres_as_the_sweep_does() -> None:
    db = _register(FakePg())
    _place(db, 1, obec=BRANDYS, okres=PRAHA_VYCHOD)
    _place(db, 2, obec=CELAKOVICE, okres=PRAHA_ZAPAD)
    ids, _resolved, source = rt_equivalence.scope_listings(db, GEN, DISTRICT)
    assert (ids, source) == ({1}, "listing_location")
    assert walk_blocks(DISTRICT, {}, {}) == (), "an okres the register did not place: no walk"


# ------------------------------------------------------------------ the apply scope and W8


def test_a_district_block_holds_an_advert_by_its_live_okres_kod() -> None:
    scope = AP.Scope(blocks=frozenset({f"district:{PRAHA_VYCHOD}"}))
    inside = AP.Member(1, 1, "prodej", "byt", BRANDYS, None, PRAHA_VYCHOD)
    assert scope.outside(inside) is None
    assert scope.outside(AP.Member(2, 2, "prodej", "byt", None, None, PRAHA_VYCHOD)) is None
    assert scope.outside(AP.Member(3, 3, "prodej", "byt", BRANDYS, None, PRAHA_ZAPAD)) \
        == AP.OUT_BLOCKS
    assert scope.outside(AP.Member(4, 4, "prodej", "byt", None, None, None)) \
        == AP.OUT_NO_LOCATION
    assert AP.Member.read((5, 50, "prodej", "byt", BRANDYS, None, PRAHA_VYCHOD)) == AP.Member(
        5, 50, "prodej", "byt", BRANDYS, None, PRAHA_VYCHOD)
    assert AP.normalize_block(f" District:{PRAHA_VYCHOD}") == f"district:{PRAHA_VYCHOD}"
    assert AP.scope_fields({"blocks": f"district:{PRAHA_VYCHOD} town:563510"})["blocks"] \
        == frozenset({f"district:{PRAHA_VYCHOD}", "town:563510"})
    with pytest.raises(ValueError, match="district:<code>"):
        AP.normalize_block(f"okres:{PRAHA_VYCHOD}")          # the lane's spelling, not this one


def _district_lane() -> LaneDb:
    """Two groups the lane has read under `okres:3209`, one located inside the district."""
    db = LaneDb()
    db.live_scope(blocks=[f"district:{PRAHA_VYCHOD}"])
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT, where=(BRANDYS, None, PRAHA_VYCHOD))
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT, where=(CELAKOVICE, None, PRAHA_ZAPAD))
    db.walked = {f"okres:{PRAHA_VYCHOD}"}
    for lid in (10, 11, 20, 21):
        db.snapshot[lid] = f"okres:{PRAHA_VYCHOD}"
        db.read.add(lid)
    return db


def _reconcile(db: LaneDb) -> dict:
    return reconcile.run(db, RT, [], run_id="rt:test", deadline=10 ** 9,
                         blocks=[f"okres:{PRAHA_VYCHOD}"], merge=db.merge([]),
                         clock=lambda: 0.0, now=lambda: db.now)


def test_the_reconcile_merges_a_group_its_district_holds_and_no_other() -> None:
    db = _district_lane()
    out = _reconcile(db)
    assert out["counts"]["applied"] == 1
    assert out["counts"]["out_of_scope_by_reason"] == {AP.OUT_BLOCKS: 1}
    assert db.listings[11]["property_id"] == 100 and db.listings[21]["property_id"] == 400


def test_the_reconcile_waits_on_a_block_whose_pages_have_not_completed() -> None:
    """A block inside its first cycle of pages may hold a member the group is still missing: it
    reads as not walked (E940; E961 read a truncated walk the same way), so the group waits."""
    db = _district_lane()
    db.walked = set()
    out = _reconcile(db)
    assert out["counts"]["applied"] == 0 and out["counts"][reconcile.WAITING] == 2
    assert db.listings[11]["property_id"] == 200


def test_w8_reads_a_district_by_its_okres_and_waits_for_the_lanes_okres_walk(
        tmp_path: Path, monkeypatch: Any) -> None:
    assert L.area_params([f"district:{PRAHA_VYCHOD}", "town:563510"]) == {
        "towns": [563510], "quarters": [], "districts": [PRAHA_VYCHOD]}
    with pytest.raises(ValueError, match="district:<code>"):
        L.area_params([f"okres:{PRAHA_VYCHOD}"])
    db = LiveStreamDb()
    db.live_scope(blocks=[f"district:{PRAHA_VYCHOD}"])
    inside = (BRANDYS, None, PRAHA_VYCHOD)
    _intact_pair(db, 100, 200, 1, inside)
    db.group(10, [1, 30], gen="rt")                    # the lane holds the pair apart
    db.listing(30, 3000, where=inside)
    _intact_pair(db, 300, 400, 3, (CELAKOVICE, None, PRAHA_ZAPAD))
    monkeypatch.setattr(L, "detach_listings", db.detach_recording([]))
    args = {"generation": "rt", "retire_legacy": "1", "blocks": f"district:{PRAHA_VYCHOD}"}
    db.walked = {f"obec:{BRANDYS}"}                    # the lane never walked the okres
    with pytest.raises(SystemExit, match=f"has not fully read district:{PRAHA_VYCHOD}"):
        AP.run_apply(_factory(db), args, tmp_path)
    db.walked = {f"okres:{PRAHA_VYCHOD}"}
    out = AP.run_apply(_factory(db), args, tmp_path)
    assert out["dry_run"] is True and out["counts"]["outcomes"] == {"would_retire": 1}
    assert db.statements.count(L.AREA_SQL) == 1


def test_w8_names_an_area_at_the_grain_the_lane_walks_it(tmp_path: Path) -> None:
    """A town inside an okres the lane walks was never walked AS a town, so W8 refuses it as
    unread — and a district over towns the lane walks one by one, the same way."""
    db = LiveStreamDb()
    db.live_scope(blocks=[f"town:{BRANDYS}"])
    db.walked = {f"okres:{PRAHA_VYCHOD}"}
    args = {"generation": "rt", "retire_legacy": "1", "blocks": f"town:{BRANDYS}"}
    with pytest.raises(SystemExit, match=f"has not fully read town:{BRANDYS} "):
        AP.run_apply(_factory(db), args, tmp_path)

