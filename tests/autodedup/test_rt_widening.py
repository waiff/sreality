"""E964 — a seed WIDENS a seeded generation without emptying it.

The operator, 2026-10-17: after Prague and Středočeský kraj, "expand beyond to all other regions
when possible". Until now every wider scope was `rt_seed fresh=true`: the whole generation
deleted, every area's merging paused for the days the rebuild took (A9's global skip). Now a
seed over a seeded generation whose scope keeps every block (or replaces it by one that contains
it) keeps every row, lists only the blocks it has not read whole, re-cuts the calibration over
the whole scope, keeps the forward cursors and opens the build for the new blocks only; and the
reconcile runs during any build, holding back only the groups that touch a block not yet read.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup import apply as AP
from autodedup import incremental_lane, reconcile, rt_lease
from autodedup import incremental_sql as S
from autodedup.export_sql import COHORT_PHASH_POP_SQL
from autodedup.incremental import bootstrap_key
from autodedup.incremental_lane import (
    BUILD_BYTES_PER_LISTING,
    CURSOR_CHANGED,
    CURSOR_ENTER,
    CURSOR_FLIPPED,
    CURSOR_NEW,
    CUT_BYTES_PER_LISTING,
    LANE_NAME,
    MAX_SCHEMA_MB,
    SNAPSHOT_BYTES_PER_ROW,
    SqlStore,
    pass_rate_key,
    run_incremental,
    run_rt_seed,
    scope_setting_key,
)
from autodedup.incremental_scope import (
    Scope,
    ScopeBlock,
    ScopeError,
    contains,
    parse_scope,
    widening,
)
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_apply import _pair_group
from tests.autodedup.test_one_lane import RT, LaneDb

GEN = "rt"
SCORER = {"settings": "default", "model": "prior"}
TOWN, OTHER_TOWN = 563510, 577626
BEROUN, BEROUN_OKRES, KRALUV_DVUR = 531057, 3202, 531065
PRAHA, VYSOCANY = 554782, 490245
OLD_IDS = range(4000, 4009)
NEW_IDS = range(5000, 5006)
ARRIVAL = 6000
_MB = 1_048_576
# Every table and row a pass or a seed writes for the generation, by what a widening must keep.
_KEPT = ("rt_fp", "fp_key", "pairs", "clusters", "cluster_members", "cluster_conflicts",
         "cells", "reject_ledger")


def _add_block(conn: FakePg, ids: range, *, obec: int, okres: int | None = None,
               base: int = 700_000) -> None:
    """`lane_world`'s listing rows, located in `obec` (and `okres`): consecutive ones share their
    photographs, so the block's build decides pairs with image agreement."""
    for index, listing_id in enumerate(ids):
        start = base + 10 * (index // 2)
        lane_world._listing_row(conn, listing_id, lane_world.SOURCES[index % 3],
                                [111_000, start, start + 1, start + 2, start + 3])
        conn.locations[listing_id].update(obec_kod=obec, okres_kod=okres)


def _seed(conn: FakePg, tmp_path: Path, scope: str, **args: Any) -> dict[str, Any]:
    return run_rt_seed(lambda: conn, {**SCORER, "rt_scope": scope, **args}, tmp_path)


def _build(conn: FakePg) -> list[dict[str, Any]]:
    """Passes until the build phase ends by itself (E98)."""
    out = []
    for _ in range(12):
        out.append(run_incremental(lambda: conn))
        assert not out[-1]["aborted"], out[-1]
        if conn.settings.get(bootstrap_key(GEN)) is False:
            return out
    raise AssertionError("the build never ended")


def _kept(conn: FakePg) -> dict[str, Any]:
    state = conn.snapshot()
    return {name: state[name] for name in _KEPT}


def _built_town(tmp_path: Path) -> FakePg:
    """`obec:563510` seeded and built; `obec:577626` holds adverts the seed never listed (ids
    below the forward cursor, as a widening's new blocks hold them in production)."""
    conn = lane_world.world()
    _add_block(conn, NEW_IDS, obec=OTHER_TOWN)
    _seed(conn, tmp_path, f"obec:{TOWN}")
    _build(conn)
    return conn


def _adverts(conn: FakePg, ids: Any) -> list[AP.Member]:
    """The adverts as the reconcile reads them: their LIVE location codes (`apply.Member`)."""
    return [AP.Member(int(i), None, None, None, conn.locations[i].get("obec_kod"),
                      conn.locations[i].get("cast_obce_kod"), conn.locations[i].get("okres_kod"))
            for i in ids]


def _arrival(conn: FakePg, listing_id: int = ARRIVAL, obec: int = TOWN) -> None:
    lane_world._listing_row(conn, listing_id, "sreality", [123_456_789, 987_654_321])
    conn.listings[listing_id]["first_seen_at"] = conn.now - timedelta(hours=1)
    conn.locations[listing_id]["obec_kod"] = obec


# ------------------------------------------------------------------ what "contains" means


def test_a_block_contains_itself_its_okres_its_towns_and_a_town_its_quarters() -> None:
    parents = {VYSOCANY: PRAHA, 490999: BEROUN}
    districts = {BEROUN_OKRES: [BEROUN, KRALUV_DVUR]}
    okres, beroun = ScopeBlock("okres", BEROUN_OKRES), ScopeBlock("obec", BEROUN)
    assert contains(beroun, beroun, parents, districts)
    assert contains(okres, beroun, parents, districts)
    assert contains(okres, ScopeBlock("obec", KRALUV_DVUR), parents, districts)
    assert contains(okres, ScopeBlock("cast_obce", 490999), parents, districts), (
        "a quarter of a town of it")
    assert contains(ScopeBlock("obec", PRAHA), ScopeBlock("cast_obce", VYSOCANY), parents,
                    districts)
    # Nothing else holds anything: another okres's town, a town's sibling, Praha under no okres,
    # and the narrower block never holds the wider one.
    assert not contains(okres, ScopeBlock("obec", TOWN), parents, districts)
    assert not contains(okres, ScopeBlock("cast_obce", VYSOCANY), parents, districts)
    assert not contains(beroun, okres, parents, districts)
    assert not contains(ScopeBlock("cast_obce", VYSOCANY), ScopeBlock("obec", PRAHA), parents,
                        districts)
    assert not contains(ScopeBlock("okres", 3209), beroun, parents, districts), "unregistered"


def test_a_widening_keeps_adds_and_replaces_and_a_narrower_scope_drops() -> None:
    parents, districts = {VYSOCANY: PRAHA}, {BEROUN_OKRES: [BEROUN, KRALUV_DVUR]}
    old = parse_scope(f"obec:{TOWN} obec:{BEROUN} cast_obce:{VYSOCANY}")
    plan = widening(old, parse_scope(f"obec:{TOWN} okres:{BEROUN_OKRES} obec:{PRAHA}"),
                    parents, districts)
    assert [b.key for b in plan.kept] == [f"obec:{TOWN}"]
    assert [b.key for b in plan.added] == [f"okres:{BEROUN_OKRES}", f"obec:{PRAHA}"]
    assert plan.replaced == {f"obec:{BEROUN}": f"okres:{BEROUN_OKRES}",
                             f"cast_obce:{VYSOCANY}": f"obec:{PRAHA}"}
    assert plan.dropped == ()
    # Narrower: a block left out. Reshaped: a town traded for one of its quarters.
    narrower = widening(old, parse_scope(f"obec:{TOWN} obec:{BEROUN}"), parents, districts)
    assert [b.key for b in narrower.dropped] == [f"cast_obce:{VYSOCANY}"]
    reshaped = widening(parse_scope(f"obec:{PRAHA}"), parse_scope(f"cast_obce:{VYSOCANY}"),
                        parents, districts)
    assert [b.key for b in reshaped.dropped] == [f"obec:{PRAHA}"]
    assert widening(old, old, parents, districts).added == (), "the same scope adds nothing"
    with pytest.raises(ScopeError, match="widens nothing"):
        widening(old, Scope((), whole_corpus=True), parents, districts)


# ------------------------------------------------------------------ the widening keeps the store


def test_a_widening_keeps_every_row_pair_cluster_and_ledger_row(tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    # The prior model leaves this world's pairs in the band, so a group and a below-floor
    # reject the store answers from its ledger (E943) are written in, to be seen kept.
    conn.reject_ledger[(GEN, 4000, 4008)] = {
        "dd_lo": "a", "dd_hi": "b", "calibration_digest": conn.calibration[GEN]["digest"],
        "feature_version": 1, "engine_digest": "e", "decided_at": conn.now}
    conn.clusters[(GEN, 4000)] = {"size": 2, "generation": GEN}
    conn.cluster_members.update({(GEN, 4000, 4000), (GEN, 4000, 4001)})
    conn.cluster_conflicts.append({"kind": "invariant", "detail": {"generation": GEN}})
    before, cursors = _kept(conn), {k: dict(v) for k, v in conn.cursors.items()}
    assert conn.pairs and conn.rt_fp and conn.fp_key and conn.cells

    out = _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")

    assert out["mode"] == "widening" and out["fresh"] is False
    assert _kept(conn) == before, "every row the generation held is still there, unchanged"
    for name in (CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED, reconcile.CURSOR):
        assert conn.cursors[name] == cursors[name], f"{name} kept: no arrival is skipped"
    assert conn.cursors[CURSOR_ENTER] == {"last_listing_id": 0, "last_snapshot_id": 0}
    assert conn.settings[bootstrap_key(GEN)] is True, "the build opens for the new block"
    assert conn.settings[scope_setting_key(GEN)] == [{"grain": "obec", "code": TOWN},
                                                     {"grain": "obec", "code": OTHER_TOWN}]
    record = out["widening"]
    assert record["added"] == record["walked"] == [f"obec:{OTHER_TOWN}"]
    assert (record["scope_listings"], record["held"], record["owed"]) == (15, 9, 6)
    assert record["from"] == [{"grain": "obec", "code": TOWN}] and record["replaced"] == {}
    snapshot = {(block, i) for (g, block, i) in conn.scope_ids if g == GEN}
    assert snapshot == ({(f"obec:{TOWN}", i) for i in OLD_IDS}
                        | {(f"obec:{OTHER_TOWN}", i) for i in NEW_IDS})
    assert out["calibration"]["scope_listings"] == 15, "the cut is over the WHOLE scope"
    assert {row["block_key"] for row in conn.scope_scans if row["generation"] == GEN} == {
        f"obec:{TOWN}", f"obec:{OTHER_TOWN}"}, "the old block was read whole; not walked again"
    assert conn.lease[LANE_NAME]["expires_at"] <= conn.now, "the lease went back"


def test_the_new_block_builds_while_an_old_blocks_arrival_is_claimed(tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    _arrival(conn)

    first = run_incremental(lambda: conn)

    assert first["feeds"].get("entered") == len(NEW_IDS), "the build claims the new block"
    assert first["feeds"].get("new") == 1, "and the forward feed the old block's arrival"
    assert {i for (g, i) in conn.rt_fp if g == GEN} >= set(OLD_IDS) | set(NEW_IDS) | {ARRIVAL}
    assert first["bootstrap"]["ended_this_pass"] is True
    assert conn.settings[bootstrap_key(GEN)] is False
    assert any(set(NEW_IDS) & {lo, hi} for (g, lo, hi) in conn.pairs if g == GEN), (
        "the new block's pairs were decided")


def test_the_reconcile_runs_in_a_widening_build_and_waits_only_on_the_unread_block(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A9 amended: the reconcile runs during the build; `_unread` (the per-block rail) names
    only the adverts of the block the build has not read whole — never the old block's."""
    conn = _built_town(tmp_path)
    _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    _arrival(conn)
    conn.settings[pass_rate_key(GEN)] = 3.5 / 787.5          # three adverts a build pass
    seen: list[dict[str, Any]] = []
    everyone = sorted(set(OLD_IDS) | set(NEW_IDS) | {ARRIVAL})

    def spy(conn_: Any, generation: str, touched: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append({"blocks": list(kwargs["blocks"]), "building": kwargs["building"],
                     "unread": reconcile._unread(conn_, generation, _adverts(conn_, everyone),
                                                 kwargs["blocks"],
                                                 building=kwargs["building"])})
        return {"counts": {}}

    monkeypatch.setattr(reconcile, "run", spy)
    building = run_incremental(lambda: conn)

    assert building["bootstrap"]["active"] and not building["bootstrap"]["ended_this_pass"]
    assert building["reconcile"] == {"counts": {}, "seconds": building["reconcile"]["seconds"]}
    (call,) = seen
    assert call["building"] is True, "the reconcile ran inside the build"
    assert set(call["blocks"]) == {f"obec:{TOWN}", f"obec:{OTHER_TOWN}"}
    assert call["unread"] == set(NEW_IDS), "only the new block waits; the old one merges"
    for _ in range(4):
        run_incremental(lambda: conn)
    assert conn.settings[bootstrap_key(GEN)] is False and seen[-1]["unread"] == set()
    assert seen[-1]["building"] is False


def test_the_reconcile_merges_a_read_blocks_group_while_a_widened_block_builds() -> None:
    """The apply path's own fake: one group read whole in the old block merges; one with an
    advert in the new block, whose snapshot still holds an advert the build has not reached,
    waits (`block_not_fully_read`) and merges once the block is read."""
    old_block, new_block = f"obec:{TOWN}", f"obec:{OTHER_TOWN}"
    db = LaneDb()
    db.live_scope()
    db.walked = {old_block, new_block}
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    for lid, block in ((10, old_block), (11, old_block), (20, old_block), (21, new_block),
                       (99, new_block)):
        db.snapshot[lid] = block
        if lid != 99:
            db.read.add(lid)

    def run() -> dict[str, Any]:
        return reconcile.run(db, RT, [], run_id="rt:test", deadline=10 ** 9,
                             blocks=[old_block, new_block], merge=db.merge([]),
                             clock=lambda: 0.0, now=lambda: db.now)

    out = run()
    assert out["counts"]["applied"] == 1 and out["counts"][reconcile.WAITING] == 1
    assert db.listings[11]["property_id"] == 100 and db.listings[21]["property_id"] == 400
    db.read.add(99)
    assert run()["counts"]["applied"] == 1 and db.listings[21]["property_id"] == 300


def test_a_build_pass_re_claims_what_the_reconcile_handed_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E960's re-claim rode only passes a reconcile followed, and a build had none: a widening's
    days of build would have left every stale old-block group waiting. Now a build pass
    re-claims them too."""
    from tests.autodedup.test_endpoint_facts import _open_scope
    from tests.autodedup.test_reject_ledger import _seeded_reject_world

    world = _seeded_reject_world(tmp_path)
    _open_scope(monkeypatch)
    assert not run_incremental(lambda: world)["aborted"]
    target, fresh = 4006, 4007
    world.rt_fp[(GEN, target)]["updated_at"] = world.now - timedelta(
        hours=reconcile.FRESH_DECISION_HOURS + 1)
    world.settings[reconcile.redecide_key(GEN)] = [[target, fresh]]
    engine = world.settings[incremental_lane.engine_key(GEN)]
    engine["since"] = (world.now - timedelta(days=1)).isoformat()
    world.settings[bootstrap_key(GEN)] = True                  # a widening's build is open

    out = run_incremental(lambda: world)

    assert out["bootstrap"]["active"] is True
    assert out["feeds"].get("stale") == 1 and out["evidence"]["stale_claimed"] == 1
    assert world.rt_fp[(GEN, target)]["updated_at"] == world.now, "refreshed in the build"


# ------------------------------------------------------------------ the calibration and the ledger


def test_the_widening_re_cuts_over_the_whole_scope_and_the_ledger_re_keys(tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    before = conn.calibration[GEN]["digest"]
    assert conn.calibration[GEN]["n_listings"] == len(OLD_IDS)
    conn.reject_ledger[(GEN, 4000, 4008)] = {
        "dd_lo": "a", "dd_hi": "b", "calibration_digest": before, "feature_version": 1,
        "engine_digest": "e", "decided_at": conn.now}

    out = _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")

    after = conn.calibration[GEN]["digest"]
    assert out["calibration"]["digest_before"] == before and out["calibration"]["moved"] is True
    assert after != before and conn.calibration[GEN]["n_listings"] == 15
    assert out["calibration"]["scope_listings"] == 15
    assert {phash for phash in conn.phash_pop} >= {
        int(row["phash"]) for row in conn.image_rows if row["listing_id"] in NEW_IDS}, (
        "the new block's photographs are in the population")
    # The ledger keeps its row, keyed on the old digest: inert for a store cut since (E943).
    key = [(4000, 4008, "a", "b")]
    from autodedup.features import FEATURE_VERSION

    conn.reject_ledger[(GEN, 4000, 4008)]["feature_version"] = FEATURE_VERSION
    assert SqlStore(conn, GEN, calibration_digest=before, engine_digest="e").rejected(key)
    assert not SqlStore(conn, GEN, calibration_digest=after, engine_digest="e").rejected(key)
    passed = run_incremental(lambda: conn)
    assert passed["calibration"]["digest"] == after, "the passes run on the widened cut"


def test_the_widening_reads_everything_before_its_transaction_writes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The vacuum horizon and the locks (E964 (c), review F6): the walk reads outside any
    transaction; the cut reads in short transactions of its own, each bounded by its own
    statement guard, so no lock on `public` outlives its step; and the ONE write transaction
    holds no read of `public` at all — it opens on the cut's population upsert and ends on the
    lease's fence."""
    conn = _built_town(tmp_path)
    all_before, tx_before = len(conn.statements), conn.transactions
    in_tx_before = len(conn.statements_in_tx)
    opened: list[int] = []
    original = incremental_lane.write_cut

    def mark(conn_: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(len(conn_.statements))
        return original(conn_, *args, **kwargs)

    monkeypatch.setattr(incremental_lane, "write_cut", mark)
    _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")

    before, writing = conn.statements[all_before:opened[0]], conn.statements[opened[0]:]
    reads = {S.RT_SCOPE_BLOCK_SQL, S.RT_CUT_HASHES_SQL, COHORT_PHASH_POP_SQL,
             S.RT_SCOPE_IDS_OF_BLOCKS_SQL, S.RT_FP_HELD_SQL}
    assert reads <= set(before) and not reads & set(writing)
    assert writing[0] == S.RT_PHASH_POP_WRITE_SQL
    assert writing.count(S.RT_LEASE_HOLD_SQL) == 1, "the fence, once, at the end"
    # The walk is in no transaction; every cut step is in one of its own, guard first.
    assert S.RT_SCOPE_BLOCK_SQL not in conn.statements_in_tx[in_tx_before:]
    steps = conn.transactions - tx_before - 1
    assert steps == before.count(S.RT_STATEMENT_GUARD_SQL) >= 3, "hashes, population, facts"
    for read in (S.RT_CUT_HASHES_SQL, COHORT_PHASH_POP_SQL):
        index = before.index(read)
        assert before[index - 1] == S.RT_STATEMENT_GUARD_SQL, read


def test_a_lease_lost_under_the_widening_rolls_all_of_it_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _built_town(tmp_path)
    before = conn.snapshot()
    original = incremental_lane.write_cut

    def cut_then_lose_the_lease(conn_: Any, *args: Any, **kwargs: Any) -> Any:
        out = original(conn_, *args, **kwargs)
        holder = conn_.lease[LANE_NAME]["holder"]
        conn_.other_session().cursor().execute(S.RT_LEASE_RELEASE_SQL,
                                               {"name": LANE_NAME, "holder": holder})
        return out

    monkeypatch.setattr(incremental_lane, "write_cut", cut_then_lose_the_lease)
    with pytest.raises(rt_lease.LeaseLost):
        _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    after = conn.snapshot()
    for name in ("scope_ids", "scope_scans", "calibration", "phash_pop", "cursors", "rt_fp",
                 "pairs"):
        assert after[name] == before[name], name
    assert {k: v for k, v in after["settings"].items()} == before["settings"]


# ------------------------------------------------------------------ what a widening refuses


def test_a_narrower_scope_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    conn = lane_world.world()
    _add_block(conn, NEW_IDS, obec=OTHER_TOWN)
    _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    _build(conn)
    before = conn.snapshot()
    with pytest.raises(SystemExit, match=f"drops obec:{OTHER_TOWN} .*fresh=true"):
        _seed(conn, tmp_path, f"obec:{TOWN}")
    assert conn.snapshot() == before


def test_the_same_scope_and_another_scorer_are_refused(tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    before = conn.snapshot()
    with pytest.raises(SystemExit, match="widens nothing.*fresh=true"):
        _seed(conn, tmp_path, f"obec:{TOWN}")
    with pytest.raises(SystemExit, match="widens nothing"):
        run_rt_seed(lambda: conn, dict(SCORER), tmp_path)       # no scope: the persisted one
    with pytest.raises(SystemExit, match="another scorer.*fresh=true"):
        run_rt_seed(lambda: conn, {"settings": "default", "model": "w6_gold",
                                   "rt_scope": f"obec:{TOWN} obec:{OTHER_TOWN}"}, tmp_path)
    conn.settings["rt_seed_version:rt"] = "e900"
    with pytest.raises(SystemExit, match="rebuilt, never widened"):
        _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    assert conn.snapshot() == {**before, "settings": {**before["settings"],
                                                      "rt_seed_version:rt": "e900"}}


def test_the_second_check_projects_the_new_adverts_build_cut_and_snapshot(
        tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    conn.schema_bytes = 300 * _MB
    out = _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    build = out["storage_build"]
    assert (build["build_listings"], build["snapshot_rows"]) == (len(NEW_IDS), len(NEW_IDS))
    # The re-cut rewrites only the hashes whose count moved: the new adverts' photographs.
    assert build["projected_mb"] == round(300 + len(NEW_IDS) * (
        BUILD_BYTES_PER_LISTING + CUT_BYTES_PER_LISTING + SNAPSHOT_BYTES_PER_ROW) / _MB, 2)


def test_a_widening_over_budget_refuses_before_any_write(tmp_path: Path) -> None:
    conn = _built_town(tmp_path)
    build_mb = len(NEW_IDS) * BUILD_BYTES_PER_LISTING / _MB
    conn.schema_bytes = int((MAX_SCHEMA_MB - build_mb / 2) * _MB)
    before, transactions = conn.snapshot(), conn.transactions
    with pytest.raises(SystemExit, match="listings its store does not hold yet"):
        _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    assert conn.transactions == transactions, "no transaction opened"
    after = conn.snapshot()
    assert after["lease"][LANE_NAME]["expires_at"] <= conn.now, "the lease went back"
    assert {**after, "lease": before["lease"]} == before


# ------------------------------------------------------------------ a town replaced by its okres


def _beroun(tmp_path: Path) -> FakePg:
    """Beroun seeded as a town and built; the rest of its okres never listed."""
    conn = FakePg(now=lane_world.EXPORTED_AT + timedelta(days=1))
    conn.admin_districts[BEROUN_OKRES] = [BEROUN, KRALUV_DVUR]
    _add_block(conn, OLD_IDS, obec=BEROUN, okres=BEROUN_OKRES, base=900_000)
    _add_block(conn, NEW_IDS, obec=KRALUV_DVUR, okres=BEROUN_OKRES)
    _seed(conn, tmp_path, f"obec:{BEROUN}")
    _build(conn)
    return conn


def test_a_town_replaced_by_its_okres_is_a_widening(tmp_path: Path) -> None:
    """E961 (c)'s live case: `obec:531057` becomes `okres:3202`. The okres contains the town, so
    the town's rows stay and the okres's other adverts are walked and built; the snapshot lists
    the town's adverts under the okres now (one block holds each advert, never two)."""
    conn = _beroun(tmp_path)
    before = _kept(conn)

    out = _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")

    assert _kept(conn) == before
    record = out["widening"]
    assert record["replaced"] == {f"obec:{BEROUN}": f"okres:{BEROUN_OKRES}"}
    assert record["added"] == record["walked"] == [f"okres:{BEROUN_OKRES}"]
    assert (record["scope_listings"], record["held"], record["owed"]) == (15, 9, 6)
    blocks = {block for (g, block, _i) in conn.scope_ids if g == GEN}
    assert blocks == {f"okres:{BEROUN_OKRES}"}, "the town's rows are the okres's now"
    assert {i for (g, _b, i) in conn.scope_ids if g == GEN} == set(OLD_IDS) | set(NEW_IDS)
    # The consequence, named (E961 (c)): the town's adverts sit in the okres block, which is
    # unread until the okres is built, so Beroun's groups wait for it — Beroun's alone.
    unread = reconcile._unread(conn, GEN, _adverts(conn, OLD_IDS), [f"okres:{BEROUN_OKRES}"])
    assert unread == set(OLD_IDS)
    _build(conn)
    assert reconcile._unread(conn, GEN, _adverts(conn, OLD_IDS),
                             [f"okres:{BEROUN_OKRES}"]) == set()
    assert {i for (g, i) in conn.rt_fp if g == GEN} == set(OLD_IDS) | set(NEW_IDS)


def test_a_town_beside_its_okres_is_still_refused(tmp_path: Path) -> None:
    conn = _beroun(tmp_path)
    with pytest.raises(SystemExit, match=f"obec:{BEROUN} beside its own okres"):
        _seed(conn, tmp_path, f"obec:{BEROUN} okres:{BEROUN_OKRES}")


def test_fresh_still_empties_a_widened_generation(tmp_path: Path) -> None:
    conn = _beroun(tmp_path)
    assert _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")["mode"] == "widening"

    out = _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}", fresh="true")

    assert out["fresh"] is True and out.get("mode") != "widening"
    assert out["reset"]["pairs"] > 0 and out["reset"]["rt_fp"] == len(OLD_IDS)
    assert not [key for key in conn.pairs if key[0] == GEN] and not conn.rt_fp
    assert conn.cursors[CURSOR_NEW]["last_listing_id"] == max(conn.listings)


def test_a_widening_mid_build_lists_the_old_blocks_it_has_not_read_whole(tmp_path: Path) -> None:
    """A block the generation has not read to its end — never walked, or (here) walked and its
    snapshot lost — is listed by a widening too, so its cut is over the whole scope (E940 (f)),
    and the build that follows owes its adverts as well. (The live `rt` of 2026-10-17 has all
    120 blocks walked; 60 are unread only by fingerprint, which the build, not the walk, owes.)"""
    conn = lane_world.world()
    _add_block(conn, NEW_IDS, obec=OTHER_TOWN)
    _seed(conn, tmp_path, f"obec:{TOWN}")
    conn.scope_scans = [row for row in conn.scope_scans if row["generation"] != GEN]
    conn.scope_ids = {}                    # listed by nothing yet, as a capped seed left it

    out = _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")

    assert out["widening"]["walked"] == [f"obec:{TOWN}", f"obec:{OTHER_TOWN}"]
    assert out["widening"]["added"] == [f"obec:{OTHER_TOWN}"]
    assert out["calibration"]["scope_listings"] == 15
    assert out["widening"]["owed"] == 15, "nothing was built yet"


def test_a_whole_corpus_build_still_holds_the_reconcile(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-block rail needs blocks: `all` walks none and snapshots nothing, so nothing would
    read as unread and a half-built corpus would merge. Its build keeps A9's skip."""
    conn = lane_world.world()
    _seed(conn, tmp_path, f"obec:{TOWN}")
    conn.settings[scope_setting_key(GEN)] = "all"
    conn.settings[bootstrap_key(GEN)] = True
    monkeypatch.setattr(incremental_lane, "guard_agrees", lambda scope, budget: True)
    called: list[Any] = []
    monkeypatch.setattr(reconcile, "run", lambda *a, **k: called.append(k) or {"counts": {}})

    out = run_incremental(lambda: conn)

    assert out["reconcile"] == {"skipped": "bootstrap"} and not called


# ------------------------------------------------------------------ the review of #1847


def _two_blocks(**scope: Any) -> tuple[LaneDb, str, str]:
    """The apply fake with the lane walking the town it held and the town it widened to."""
    old_block, new_block = f"obec:{TOWN}", f"obec:{OTHER_TOWN}"
    db = LaneDb()
    db.live_scope(**({"blocks": ["town:563510", "town:577626"]} | scope))
    db.walked = {old_block, new_block}
    db.snapshot[99] = new_block                       # the new block is still building
    return db, old_block, new_block


def _reconcile(db: LaneDb, blocks: list[str], *, building: bool = True) -> dict[str, Any]:
    return reconcile.run(db, RT, [], run_id="rt:test", deadline=10 ** 9, blocks=blocks,
                         merge=db.merge([]), clock=lambda: 0.0, now=lambda: db.now,
                         building=building)


def test_a_group_waits_on_an_unread_advert_its_merge_would_carry_along() -> None:
    """F2: a group read whole in the held block whose property carries a vouched advert of the
    unread block (merged there by the engine earlier) would move that advert onto the survivor
    without the build ever comparing it with the group: it waits until the block is read, and
    then the stream's current grouping decides — here the build grouped 12 with 10 and 11, so it
    moves as a member, never as a rider (E965 refuses an ungrouped vouched rider)."""
    from tests.autodedup.test_apply import _engine_merged

    db, old_block, new_block = _two_blocks()
    _engine_merged(db, 11, [11, 12], [200, 250])       # 12 rides along with 11 on 200
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    for lid, block in ((10, old_block), (11, old_block), (12, new_block)):
        db.snapshot[lid] = block
    db.read |= {10, 11}

    out = _reconcile(db, [old_block, new_block])
    assert out["counts"][reconcile.WAITING] == 1 and out["counts"]["applied"] == 0
    assert db.listings[11]["property_id"] == 200 and db.listings[12]["property_id"] == 200
    db.read |= {12, 99}
    db.members.append((RT, 10, 12))                    # the build read 12 and grouped it
    assert _reconcile(db, [old_block, new_block])["counts"]["applied"] == 1
    assert db.listings[12]["property_id"] == 100, "moved as the group's member once read"


def test_an_advert_the_snapshot_has_not_placed_waits_on_the_block_it_lives_in() -> None:
    """F3: two forward-feed arrivals the snapshot has not listed yet are placed by their live
    location: in the unread block they wait, in the read block they merge; and during a build
    a pair no scope block holds waits too (outside one it merges as before)."""
    db, old_block, new_block = _two_blocks(blocks=["town:563510", "town:577626",
                                                   "town:888888"])
    db.read.add(1)
    _pair_group(db, 30, [30, 31], [300, 301], gen=RT, where=(OTHER_TOWN, None))
    _pair_group(db, 40, [40, 41], [400, 401], gen=RT, where=(TOWN, None))
    _pair_group(db, 50, [50, 51], [500, 501], gen=RT, where=(888888, None))

    building = _reconcile(db, [old_block, new_block])
    assert building["counts"][reconcile.WAITING] == 2
    assert db.listings[41]["property_id"] == 400, "an arrival in the held block merges"
    assert db.listings[31]["property_id"] == 301 and db.listings[51]["property_id"] == 501
    settled = _reconcile(db, [old_block, new_block], building=False)
    assert settled["counts"][reconcile.WAITING] == 1, "outside a build, only the unread block"
    assert db.listings[51]["property_id"] == 500 and db.listings[31]["property_id"] == 301


def test_a_replaced_block_reads_as_never_walked_to_w8(tmp_path: Path) -> None:
    """F4: after `obec:531057` → `okres:3202` the town's scan rows go with its snapshot rows, so
    W8's gate (`rt_unread_blocks`) names `obec:531057` unread and refuses to undo Beroun's
    merges the lane could not re-merge until the okres is built."""
    conn = _beroun(tmp_path)
    assert f"obec:{BEROUN}" not in AP.rt_unread_blocks(conn, GEN, [f"obec:{BEROUN}"])
    _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")
    assert f"obec:{BEROUN}" in AP.rt_unread_blocks(conn, GEN, [f"obec:{BEROUN}"])
    assert {row["block_key"] for row in conn.scope_scans} == {f"okres:{BEROUN_OKRES}"}


def test_a_re_cut_rewrites_only_the_population_rows_whose_count_moved(tmp_path: Path) -> None:
    """F6: the population upsert skips a hash whose count did not move — a re-cut over the
    same photographs writes no population row at all, a widening only the new ones."""
    conn = _built_town(tmp_path)
    conn.phash_pop_writes = 0
    _seed(conn, tmp_path, f"obec:{TOWN}", recut="true")
    assert conn.phash_pop_writes == 0
    _seed(conn, tmp_path, f"obec:{TOWN} obec:{OTHER_TOWN}")
    new = {int(row["phash"]) for row in conn.image_rows if row["listing_id"] in NEW_IDS}
    assert 0 < conn.phash_pop_writes <= len(new), "the new block's hashes, and the catalogue's"


def test_the_same_scope_re_cuts_only_when_asked_and_over_the_whole_scope(tmp_path: Path) -> None:
    """F7: above RECUT_MAX_LISTINGS the lane never re-cuts itself, so a same-scope seed says so
    and re-cuts with `recut=true`: every row kept, the cut over the whole scope, no build opened
    when nothing is owed; never beside `fresh=true` or on an unseeded generation."""
    conn = _built_town(tmp_path)
    with pytest.raises(SystemExit, match="RECUT_MAX_LISTINGS .*recut=true"):
        _seed(conn, tmp_path, f"obec:{TOWN}")
    before, digest = _kept(conn), conn.calibration[GEN]["digest"]
    conn.listings[4003]["description"] = "Zcela jiny text " * 30   # the statistics move
    out = _seed(conn, tmp_path, f"obec:{TOWN}", recut="true")
    assert out["mode"] == "recut" and out["widening"]["added"] == []
    assert out["widening"]["walked"] == [] and out["calibration"]["scope_listings"] == 9
    assert _kept(conn) == before and conn.calibration[GEN]["digest"] != digest
    assert conn.settings[bootstrap_key(GEN)] is False and out["bootstrap"] is False
    with pytest.raises(SystemExit, match="recut=true re-cuts a seeded generation"):
        _seed(conn, tmp_path, f"obec:{TOWN}", recut="true", fresh="true")
    with pytest.raises(SystemExit, match="recut=true re-cuts a seeded generation"):
        _seed(lane_world.world(), tmp_path, f"obec:{TOWN}", recut="true")


def test_a_replacement_the_wider_block_does_not_list_is_refused(tmp_path: Path) -> None:
    """F8: the register places Beroun inside okres 3202, but two of its adverts carry another
    okres code, so the okres's walk does not list them while the town's still does — refused
    before anything is written. An advert that left the town since its last walk is not."""
    conn = _beroun(tmp_path)
    conn.locations[4007].update(obec_kod=TOWN, okres_kod=None)  # moved away since: drift
    before = conn.snapshot()
    for listing_id in (4002, 4005):
        conn.locations[listing_id]["okres_kod"] = 3209      # a register error
    with pytest.raises(SystemExit, match=r"okres:3202 does not list 2 adverts obec:531057 still "
                                         r"holds \(4002, 4005\)"):
        _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")
    assert conn.snapshot() == {**before, "lease": conn.snapshot()["lease"]}
    for listing_id in (4002, 4005):
        conn.locations[listing_id]["okres_kod"] = BEROUN_OKRES
    assert _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")["mode"] == "widening"


def test_a_carried_advert_never_fingerprinted_holds_its_group_whatever_its_block() -> None:
    """R2: a vouched advert that moved into a READ block after its last walk (no snapshot row,
    its live location in the block) and that the generation never fingerprinted would ride
    along compared with nothing: it holds the group until the lane has read it; then the
    stream's grouping decides (here it joined the group)."""
    from tests.autodedup.test_apply import _engine_merged

    db, old_block, new_block = _two_blocks()
    _engine_merged(db, 11, [11, 12], [200, 250])
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    db.snapshot.pop(99)                                 # every block read
    for lid in (10, 11):
        db.snapshot[lid] = old_block
    db.read |= {10, 11}

    out = _reconcile(db, [old_block, new_block], building=False)
    assert out["counts"][reconcile.WAITING] == 1 and db.listings[12]["property_id"] == 200
    db.read.add(12)                                    # a feed fingerprinted it
    db.members.append((RT, 10, 12))                    # and the pass grouped it
    assert _reconcile(db, [old_block, new_block], building=False)["counts"]["applied"] == 1


def test_a_carried_advert_outside_rt_scope_is_held_inside_the_apply_scope_only() -> None:
    """R2: an apply-scope block outside `rt_scope` admits no advert the lane never read — the
    rider there holds its group; a rider outside both scopes stays the plan's refusal."""
    from tests.autodedup.test_apply import _engine_merged

    db, old_block, _new = _two_blocks(blocks=["town:563510", "town:888888"])
    db.snapshot.pop(99)
    _engine_merged(db, 11, [11, 12], [200, 250])
    _engine_merged(db, 21, [21, 22], [400, 450])
    db.live_scope(blocks=["town:563510", "town:888888"])  # the merges above reset the row
    _pair_group(db, 10, [10, 11], [100, 200], gen=RT)
    _pair_group(db, 20, [20, 21], [300, 400], gen=RT)
    db.location[12] = (888888, None)                   # apply scope only
    db.location[22] = (999999, None)                   # neither scope
    for lid in (10, 11, 20, 21):
        db.snapshot[lid] = old_block
    db.read |= {10, 11, 20, 21}

    out = _reconcile(db, [old_block], building=False)
    assert out["counts"][reconcile.WAITING] == 1 and out["counts"]["applied"] == 0
    assert out["counts"]["skipped_by_reason"] == {AP.SKIP_CARRIES_OUT_OF_SCOPE: 1}


def test_w8_reads_a_replaced_town_as_its_okres_and_a_quarter_as_its_town(tmp_path: Path) -> None:
    """R1: after the replacement W8 maps `obec:531057` to `okres:3202` (refused while the okres
    builds, accepted once it is read whole) and a Prague quarter to `obec:554782`; a key no scope
    block contains stands for itself."""
    conn = _beroun(tmp_path)
    _seed(conn, tmp_path, f"okres:{BEROUN_OKRES}")
    walked_as = AP.rt_walked_as(conn, [f"obec:{BEROUN}", f"obec:{TOWN}"])
    assert walked_as == {f"obec:{BEROUN}": f"okres:{BEROUN_OKRES}", f"obec:{TOWN}": f"obec:{TOWN}"}
    assert f"okres:{BEROUN_OKRES}" in AP.rt_unread_blocks(conn, GEN, [f"okres:{BEROUN_OKRES}"])
    _build(conn)
    assert AP.rt_unread_blocks(conn, GEN, [f"okres:{BEROUN_OKRES}"]) == set()
    conn.settings[scope_setting_key(GEN)] = [{"grain": "obec", "code": PRAHA}]
    conn.admin_parents[490105] = PRAHA
    assert AP.rt_walked_as(conn, ["cast_obce:490105"]) == {"cast_obce:490105": f"obec:{PRAHA}"}
