"""E948: the lease a dead predecessor of this container left is released, and its death halves
the claim cap.

2026-10-10: a bootstrap pass took `autodedup.rt_lease` at 07:39:07Z as `0103ceb2490c:1:1791617947`
and the worker process died before 07:40:53Z, when Railway restarted it IN PLACE (the same
container hostname, no deploy, no SIGTERM, so E941's release never ran; most likely the OOM
killer, on a 500-advert claim whose neighbourhood read grows with the store). The lease then
stranded the lane until its TTL, and the next pass would have claimed 500 again. Pinned here:
the release (that row and only that row), its place right before the take, the cap it halves in
the same commit, the claim the cap bounds, the cap's recovery and the seed's reset. The SQL
itself runs against Postgres in `tests/test_rt_lease_fence_live.py`.
"""

from __future__ import annotations

import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup import incremental_lane, rt_lease
from autodedup.incremental_lane import (
    CLAIM_CAP_FLOOR,
    CLAIM_CAP_RECOVER_PASSES,
    LANE_NAME,
    PASS_LIMITS,
    PASS_RATE_PER_S,
    claim_cap_after_pass,
    claim_cap_key,
    full_claim_cap,
    halve_claim_cap,
    pass_rate_key,
    read_claim_cap,
    run_incremental,
)
from autodedup.incremental_sql import (
    RT_LEASE_RELEASE_PREDECESSOR_SQL,
    RT_LEASE_TAKE_SQL,
    RT_SETTING_WRITE_SQL,
)
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_rt_bootstrap import _scope_rows, _work

HOST = "0103ceb2490c"
DEAD = "0103ceb2490c:1:1791617947"      # took the lease at 2026-10-10 07:39:07Z
BOOTED = 1_791_618_053                   # the restart in place, 07:40:53Z
HALVED = {"cap": 250, "clean": 0, "reason": f"{DEAD} died before 2026-10-10T07:40:53Z"}


def _stranded(world: FakePg, holder: str, minutes: float = 38.0) -> None:
    world.lease[LANE_NAME] = {"holder": holder,
                              "expires_at": world.now + timedelta(minutes=minutes)}


def _seeded(tmp_path: Path) -> FakePg:
    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    return world


class _Killed(BaseException):
    """The OOM killer, as a pass sees it: no `except Exception` runs, nothing after is written."""


# ------------------------------------------------------------------ (a) the release


def test_a_dead_predecessors_live_lease_is_released_once_and_named() -> None:
    world = FakePg()
    _stranded(world, DEAD)

    assert rt_lease.release_predecessor(world, HOST, BOOTED) == DEAD
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (DEAD, False)
    assert rt_lease.release_predecessor(world, HOST, BOOTED) is None, "released once"
    assert rt_lease.take(world, f"{HOST}:1:{BOOTED + 60}", 2_400), "the lane is free at once"


@pytest.mark.parametrize("holder", [
    f"{HOST}:1:{BOOTED}",                  # this process's own: minted at its boot second
    f"{HOST}:1:{BOOTED + 600}",            # a start newer than this boot
    "5d1c0ffee123:1:1791617947",           # another container
    f"rt_seed:{HOST}:1:1791617947",        # a seed
    "dispatch:gh-18234567890",             # a live apply or unapply
    f"{HOST}:1:nineteen",                  # not the worker's shape: no cast is attempted
])
def test_no_other_writers_lease_is_ever_released(holder: str) -> None:
    world = FakePg()
    _stranded(world, holder)

    assert rt_lease.release_predecessor(world, HOST, BOOTED) is None
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (holder, True)


def test_a_lease_the_predecessor_gave_back_is_no_death() -> None:
    """A pass that ended released its lease and left its holder on the row: only a LIVE row
    is a process that died holding it, so a clean restart halves nothing."""
    world = FakePg()
    _stranded(world, DEAD, minutes=-1)

    assert rt_lease.release_predecessor(world, HOST, BOOTED) is None


def test_the_release_is_one_statement_on_the_lease_row() -> None:
    sql = " ".join(RT_LEASE_RELEASE_PREDECESSOR_SQL.lower().split())
    assert sql.startswith("update autodedup.rt_lease set expires_at = now() "
                          "where name = %(name)s::text")
    assert sql.endswith("returning holder")


def test_the_pass_releases_it_right_before_its_take_and_reports_it(tmp_path: Path) -> None:
    world = _seeded(tmp_path)
    host = socket.gethostname()
    dead, own = f"{host}:1:{BOOTED - 106}", f"{host}:1:{BOOTED + 60}"
    _stranded(world, dead)
    before = len(world.statements)

    out = run_incremental(lambda: world, holder=own, booted_epoch=BOOTED)

    assert out.get("skipped") is None and out["aborted"] == "", "the lane is not stranded"
    assert out["predecessor_released"] == dead
    statements = world.statements[before:]
    release = statements.index(RT_LEASE_RELEASE_PREDECESSOR_SQL)
    take = statements.index(RT_LEASE_TAKE_SQL)
    assert statements[release + 1:take] == [RT_SETTING_WRITE_SQL], "the halved cap, then take"
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (own, False), "the pass's own lease, given back"


def test_with_no_predecessor_the_take_follows_the_release_at_once(tmp_path: Path) -> None:
    world = _seeded(tmp_path)
    before = len(world.statements)

    out = run_incremental(lambda: world, booted_epoch=int(time.time()))

    statements = world.statements[before:]
    assert out["predecessor_released"] is None
    assert (statements.index(RT_LEASE_TAKE_SQL)
            == statements.index(RT_LEASE_RELEASE_PREDECESSOR_SQL) + 1)
    assert world.settings[claim_cap_key("rt")] == full_claim_cap("rt_seed"), "nothing halved"


def test_a_caller_that_names_no_boot_releases_nothing(tmp_path: Path) -> None:
    world = _seeded(tmp_path)
    _stranded(world, f"{socket.gethostname()}:1:{BOOTED - 106}")

    out = run_incremental(lambda: world)

    assert out["skipped"] == "leased"
    assert RT_LEASE_RELEASE_PREDECESSOR_SQL not in world.statements


# ------------------------------------------------------------------ (b) the claim cap


def test_the_death_halves_the_cap_in_a_commit_before_the_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release and the halved cap commit together, before the pass's transaction opens:
    a pass that dies the same way has already left the next claim at half."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    assert world.settings[key] == full_claim_cap("rt_seed")
    host = socket.gethostname()
    dead = f"{host}:1:1791617947"
    _stranded(world, dead)
    seen: dict[str, Any] = {}

    def dies_again(*_args: Any, **_kwargs: Any) -> Any:
        seen.update(in_tx=world.in_transaction, cap=dict(world.settings[key]))
        raise _Killed()

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", dies_again)
    with pytest.raises(_Killed) as raised:
        run_incremental(lambda: world, holder=f"{host}:1:{BOOTED + 60}", booted_epoch=BOOTED)

    halved = {**HALVED, "reason": f"{dead} died before 2026-10-10T07:40:53Z"}
    assert seen == {"in_tx": True, "cap": halved}, "committed BEFORE the pass's transaction"
    assert world.rolled_back >= 1 and world.settings[key] == halved
    assert world.settings_by[key].endswith(":halved")
    assert any(dead in note and "dead predecessor" in note for note in raised.value.__notes__)


def test_repeated_deaths_halve_the_cap_down_to_its_floor_and_no_lower() -> None:
    record = full_claim_cap()
    caps = []
    for _ in range(6):
        record = halve_claim_cap(record, DEAD, BOOTED)
        caps.append(record["cap"])
    assert caps == [250, 125, 62, 31, 25, 25] and CLAIM_CAP_FLOOR == 25
    assert halve_claim_cap(full_claim_cap(), DEAD, BOOTED) == HALVED


@pytest.mark.parametrize("stored, cap", [
    (None, 500), ("500", 500), ({"cap": "a lot"}, 500), ({"clean": 3}, 500),
    ({"cap": 3}, CLAIM_CAP_FLOOR), ({"cap": 9_000}, 500), ({"cap": 250, "clean": 4}, 250),
])
def test_the_cap_row_reads_inside_its_bounds(stored: Any, cap: int) -> None:
    assert read_claim_cap(stored)["cap"] == cap


def _claim(cap: int | None, *, rate_per_s: float, bootstrap: bool = True) -> tuple[Any, list]:
    db = FakePg()
    _scope_rows(db, "obec:563510", range(1, 801))
    work = _work(db, enter_slice=1000, bootstrap=bootstrap, pass_budget_s=525.0,
                 rate_per_s=rate_per_s, claim_cap=cap)
    entered = [item for item in work.claim(PASS_LIMITS.max_listings) if item.feed == "entered"]
    return work.claim_bound, entered


def test_the_cap_bounds_the_claim_under_the_count_and_over_the_clock() -> None:
    """07:10Z's numbers: the time budget allowed 693, so the count's 500 was the claim that
    died. Under a cap of 250 the claim is 250, and says what bound it."""
    bound, entered = _claim(250, rate_per_s=1.32)
    assert {k: bound[k] for k in ("max_listings", "cap", "by_time", "limit", "bound_by")} == {
        "max_listings": 500, "cap": 250, "by_time": 693, "limit": 250, "bound_by": "cap"}
    assert len(entered) == 250

    bound, entered = _claim(250, rate_per_s=0.1)
    assert (bound["limit"], bound["bound_by"]) == (52, "time") and len(entered) == 52

    bound, _ = _claim(500, rate_per_s=1.32)
    assert (bound["limit"], bound["bound_by"]) == (500, "count")
    bound, _ = _claim(None, rate_per_s=1.32)
    assert (bound["limit"], bound["bound_by"]) == (500, "count")


def test_the_cap_bounds_every_feeds_share_not_only_the_build() -> None:
    """Outside the build each feed takes a fifth of the claim: the cap shrinks every share."""
    _, uncapped = _claim(None, rate_per_s=10.0, bootstrap=False)
    _, capped = _claim(250, rate_per_s=10.0, bootstrap=False)
    assert (len(uncapped), len(capped)) == (100, 50)


def test_twenty_clean_passes_at_the_whole_cap_double_it_up_to_max_listings() -> None:
    record = {"cap": 125, "clean": 0, "reason": HALVED["reason"]}
    for n in range(1, CLAIM_CAP_RECOVER_PASSES):
        record = claim_cap_after_pass(record, 125)
        assert record == {"cap": 125, "clean": n, "reason": HALVED["reason"]}
    assert claim_cap_after_pass(record, 140) == {"cap": 250, "clean": 0,
                                                 "reason": "20 clean passes at 125"}
    nearly = {"cap": 300, "clean": CLAIM_CAP_RECOVER_PASSES - 1, "reason": "x"}
    assert claim_cap_after_pass(nearly, 300)["cap"] == PASS_LIMITS.max_listings, "never past"
    assert claim_cap_after_pass(full_claim_cap(), 500) is None, "at max_listings, no count"


def test_a_pass_short_of_the_cap_does_not_count_and_a_death_restarts_the_count() -> None:
    record = {"cap": 250, "clean": CLAIM_CAP_RECOVER_PASSES - 1, "reason": "x"}
    assert claim_cap_after_pass(record, 249) is None
    assert halve_claim_cap(record, DEAD, BOOTED) == {**HALVED, "cap": 125}


def test_a_committed_pass_at_the_whole_cap_counts_in_its_own_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incremental_lane, "CLAIM_CAP_FLOOR", 1)     # nine adverts in this world
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    world.settings[key] = {"cap": 3, "clean": CLAIM_CAP_RECOVER_PASSES - 2, "reason": "x"}
    in_tx = len(world.statements_in_tx)

    first = run_incremental(lambda: world)

    assert (first["claim_bound"]["limit"], first["claim_bound"]["bound_by"]) == (3, "cap")
    assert first["counts"]["claimed"] >= 3
    assert world.settings[key] == {"cap": 3, "clean": CLAIM_CAP_RECOVER_PASSES - 1,
                                   "reason": "x"}
    assert world.settings_by[key].endswith(":counted")
    assert RT_SETTING_WRITE_SQL in world.statements_in_tx[in_tx:], "in the pass's own commit"

    second = run_incremental(lambda: world)

    assert world.settings[key] == {"cap": 6, "clean": 0, "reason": "20 clean passes at 3"}
    assert second["claim_cap"] == world.settings[key]


def test_a_pass_that_rolls_back_counts_for_nothing(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incremental_lane, "CLAIM_CAP_FLOOR", 1)
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    record = {"cap": 3, "clean": 5, "reason": "x"}
    world.settings[key] = dict(record)
    original = incremental_lane.run_pass_bounded

    def released_mid_pass(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        rt_lease.release(world.other_session(), "worker:1:1")
        return result

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", released_mid_pass)
    out = run_incremental(lambda: world, holder="worker:1:1")

    assert out["aborted"] == rt_lease.LEASE_LOST and out["claim_cap"] == record
    assert world.settings[key] == record, "the count rolled back with the pass"


def test_a_fresh_seed_resets_the_cap_as_it_resets_the_rate(tmp_path: Path) -> None:
    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path / "first")
    key = claim_cap_key("rt")
    world.settings[key] = {**HALVED, "cap": CLAIM_CAP_FLOOR, "clean": 7}
    world.settings[pass_rate_key("rt")] = 0.02

    out = lane_world.seed_lane(world, tmp_path / "again", fresh="true")

    assert world.settings[key] == full_claim_cap("rt_seed")
    assert world.settings_by[key].endswith(":rt_seed")
    assert out["claim_cap"] == PASS_LIMITS.max_listings
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S
