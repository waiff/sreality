"""E948: the lease a dead predecessor of this container left is released, and its death halves
the claim cap.

2026-10-10: a bootstrap pass took `autodedup.rt_lease` at 07:39:07Z as `0103ceb2490c:1:1791617947`
and the worker process died before 07:40:53Z, when Railway restarted it IN PLACE (the same
container hostname and pid, no deploy, no SIGTERM, so E941's release never ran; most likely the
OOM killer, on a 500-advert claim whose neighbourhood read grows with the store). The lease then
stranded the lane until its TTL, and the lane would soon claim 500 again. Pinned here: the
release (that row and only that row) and its read-only twin, its place right before the take,
the cap it halves in the same commit, the log line written before the pass that may die, the
limit the cap bounds, that no pass raises the cap and a seed resets it, and the brake: at the
cap's floor the lease is left to its TTL, so a death no claim can cure never loops the worker.
The SQL itself runs against Postgres in `tests/test_rt_lease_fence_live.py`.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup import incremental_lane, incremental_sql, rt_lease
from autodedup.incremental_lane import (
    CLAIM_CAP_FLOOR,
    LANE_NAME,
    PASS_LIMITS,
    PASS_RATE_PER_S,
    claim_cap_key,
    full_claim_cap,
    halve_claim_cap,
    pass_rate_key,
    read_claim_cap,
    run_incremental,
)
from autodedup.incremental_sql import (
    RT_LEASE_PREDECESSOR_SQL,
    RT_LEASE_RELEASE_PREDECESSOR_SQL,
    RT_LEASE_TAKE_SQL,
    RT_SETTING_WRITE_SQL,
)
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_rt_bootstrap import _scope_rows, _work

HOST = "0103ceb2490c"
PID = 1                                  # the worker is the container's first process
DEAD = "0103ceb2490c:1:1791617947"      # took the lease at 2026-10-10 07:39:07Z
BOOTED = 1_791_618_053                   # the restart in place, 07:40:53Z
HALVED = {"cap": 250, "reason": f"{DEAD} died before 2026-10-10T07:40:53Z"}


def _here(second: int) -> str:
    """A worker holder of THIS test process: its hostname, its pid, the second given."""
    return f"{socket.gethostname()}:{os.getpid()}:{second}"


def _stranded(world: FakePg, holder: str, minutes: float = 38.0) -> None:
    world.lease[LANE_NAME] = {"holder": holder,
                              "expires_at": world.now + timedelta(minutes=minutes)}


def _seeded(tmp_path: Path) -> FakePg:
    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    return world


class _Killed(BaseException):
    """The OOM killer, as a pass sees it: no `except Exception` runs, nothing after is written."""


def _dies(*_args: Any, **_kwargs: Any) -> Any:
    raise _Killed()


# ------------------------------------------------------------------ (a) the release


def test_a_dead_predecessors_live_lease_is_released_once_and_named() -> None:
    world = FakePg()
    _stranded(world, DEAD)

    assert rt_lease.predecessor(world, HOST, PID, BOOTED) == DEAD
    assert rt_lease.current(world)["live"] is True, "the read twin ends nothing"
    assert rt_lease.release_predecessor(world, HOST, PID, BOOTED) == DEAD
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (DEAD, False)
    assert rt_lease.release_predecessor(world, HOST, PID, BOOTED) is None, "released once"
    assert rt_lease.take(world, f"{HOST}:1:{BOOTED + 60}", 2_400), "the lane is free at once"


@pytest.mark.parametrize("holder", [
    f"{HOST}:1:{BOOTED}",                  # this process's own: minted at its boot second
    f"{HOST}:1:{BOOTED + 600}",            # a start newer than this boot
    f"{HOST}:7:1791617947",                # another process on this host: a live worker
    "5d1c0ffee123:1:1791617947",           # another container
    f"rt_seed:{HOST}:1:1791617947",        # a seed
    "dispatch:gh-18234567890",             # a live apply or unapply
    f"{HOST}:1:nineteen",                  # not the worker's shape: no cast is attempted
])
def test_no_other_writers_lease_is_ever_named_or_released(holder: str) -> None:
    world = FakePg()
    _stranded(world, holder)

    assert rt_lease.predecessor(world, HOST, PID, BOOTED) is None
    assert rt_lease.release_predecessor(world, HOST, PID, BOOTED) is None
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (holder, True)


def test_a_lease_the_predecessor_gave_back_is_no_death() -> None:
    """A pass that ended released its lease and left its holder on the row: only a LIVE row
    is a process that died holding it, so a clean restart halves nothing."""
    world = FakePg()
    _stranded(world, DEAD, minutes=-1)

    assert rt_lease.predecessor(world, HOST, PID, BOOTED) is None
    assert rt_lease.release_predecessor(world, HOST, PID, BOOTED) is None


def test_the_release_and_its_read_twin_are_one_predicate_on_the_lease_row() -> None:
    def flat(sql: str) -> str:
        return " ".join(sql.lower().split())

    where = flat(incremental_sql._RT_LEASE_PREDECESSOR_WHERE)
    release, read = flat(RT_LEASE_RELEASE_PREDECESSOR_SQL), flat(RT_LEASE_PREDECESSOR_SQL)
    assert release == f"update autodedup.rt_lease set expires_at = now() {where} returning holder"
    assert read == f"select holder from autodedup.rt_lease {where}"
    assert where.startswith("where name = %(name)s::text")


def test_the_pass_releases_it_right_before_its_take_and_reports_it(tmp_path: Path) -> None:
    world = _seeded(tmp_path)
    dead, own = _here(BOOTED - 106), _here(BOOTED + 60)
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
    _stranded(world, _here(BOOTED - 106))

    out = run_incremental(lambda: world)

    assert out["skipped"] == "leased"
    assert RT_LEASE_RELEASE_PREDECESSOR_SQL not in world.statements
    assert RT_LEASE_PREDECESSOR_SQL not in world.statements


# ------------------------------------------------------------------ (b) the claim cap


def test_the_death_halves_the_cap_and_logs_it_in_a_commit_before_the_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """The release and the halved cap commit together, before the pass's transaction opens,
    and the line is written then: a pass that dies the same way has already left the next
    claim at half, and the log names the death even though the pass never returns."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    assert world.settings[key] == full_claim_cap("rt_seed")
    dead = _here(1791617947)
    _stranded(world, dead)
    seen: dict[str, Any] = {}

    def dies_again(*_args: Any, **_kwargs: Any) -> Any:
        seen.update(in_tx=world.in_transaction, cap=dict(world.settings[key]),
                    logged=[r.getMessage() for r in caplog.records])
        raise _Killed()

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", dies_again)
    with caplog.at_level(logging.WARNING), pytest.raises(_Killed):
        run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED)

    halved = {**HALVED, "reason": f"{dead} died before 2026-10-10T07:40:53Z"}
    assert seen["in_tx"] is True and seen["cap"] == halved, "committed BEFORE the pass"
    assert seen["logged"] == [
        f"AUTODEDUP: a predecessor of this container died holding autodedup.rt_lease: {dead}; "
        "released, and the claim cap halved from 500 to 250 (E948)"], "logged before it, too"
    assert world.rolled_back >= 1 and world.settings[key] == halved
    assert world.settings_by[key].endswith(":halved")


def test_the_release_and_the_halving_share_one_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A release that committed without its halving would never be halved: the next pass
    finds no predecessor. So a halving that fails takes the release back with it."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    dead = _here(BOOTED - 106)
    _stranded(world, dead)

    def fails(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the cap's write failed")

    monkeypatch.setattr(incremental_lane, "_write_claim_cap", fails)
    with pytest.raises(RuntimeError):
        run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED)

    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (dead, True), "rolled back with the halving"
    assert world.settings[key] == full_claim_cap("rt_seed")


def test_repeated_deaths_halve_the_cap_down_to_its_floor_and_no_lower() -> None:
    record = full_claim_cap()
    caps = []
    for _ in range(6):
        record = halve_claim_cap(record, DEAD, BOOTED)
        caps.append(record["cap"])
    assert caps == [250, 125, 62, 31, 25, 25] and CLAIM_CAP_FLOOR == 25
    assert halve_claim_cap(full_claim_cap(), DEAD, BOOTED) == HALVED


@pytest.mark.parametrize("stored, cap", [
    (None, 500), ("500", 500), ({"cap": "a lot"}, 500), ({"reason": "x"}, 500),
    ({"cap": 3}, CLAIM_CAP_FLOOR), ({"cap": 9_000}, 500), ({"cap": 250, "reason": "x"}, 250),
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


def test_the_cap_bounds_the_limit_under_the_count_and_over_the_clock() -> None:
    """07:10Z's numbers: the time budget allowed 693, so the count's 500 was the limit of the
    claim that died. Under a cap of 250 the limit is 250, and says what bound it."""
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


def test_outside_the_build_the_cap_shrinks_each_feeds_fifth() -> None:
    """During the build the entered feed takes the whole limit and the other feeds a fifth each
    on top; outside it the entered feed's share is a fifth too, so the cap shrinks it with the
    limit. The revived feed alone takes every revived id of its slice, whatever the limit."""
    _, uncapped = _claim(None, rate_per_s=10.0, bootstrap=False)
    _, capped = _claim(250, rate_per_s=10.0, bootstrap=False)
    assert (len(uncapped), len(capped)) == (100, 50)


def test_no_pass_raises_the_cap_only_a_seed_does(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """The store keeps growing under a generation, so a size that died would die again: a pass
    that claimed the whole cap writes no cap row, however many there are."""
    monkeypatch.setattr(incremental_lane, "CLAIM_CAP_FLOOR", 1)     # nine adverts in this world
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    record, written_by = {"cap": 3, "reason": "x"}, world.settings_by[key]
    world.settings[key] = dict(record)

    for _ in range(2):
        out = run_incremental(lambda: world)
        assert (out["claim_bound"]["limit"], out["claim_bound"]["bound_by"]) == (3, "cap")
        assert out["counts"]["claimed"] >= 3 and out["claim_cap"] == record

    assert world.settings[key] == record and world.settings_by[key] == written_by


def test_a_fresh_seed_resets_the_cap_as_it_resets_the_rate(tmp_path: Path) -> None:
    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path / "first")
    key = claim_cap_key("rt")
    world.settings[key] = {**HALVED, "cap": CLAIM_CAP_FLOOR}
    world.settings[pass_rate_key("rt")] = 0.02

    out = lane_world.seed_lane(world, tmp_path / "again", fresh="true")

    assert world.settings[key] == full_claim_cap("rt_seed")
    assert world.settings_by[key].endswith(":rt_seed")
    assert out["claim_cap"] == PASS_LIMITS.max_listings
    assert world.settings[pass_rate_key("rt")] == PASS_RATE_PER_S


# ------------------------------------------------------------------ (c) the brake at the floor


def test_at_the_floor_the_dead_predecessors_lease_is_left_to_its_ttl_and_named(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A death the floor's claim did not prevent is no claim's to cure: the lease is read, never
    ended, the skip says whose it is and why it stands, and nothing is halved or logged."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    world.settings[key] = {"cap": CLAIM_CAP_FLOOR, "reason": "x"}
    dead = _here(BOOTED - 106)
    _stranded(world, dead)
    before = len(world.statements)

    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED)

    assert out["skipped"] == "leased"
    assert (out["predecessor_kept"], out["predecessor_released"]) == (dead, None)
    assert out["reason"].startswith(
        "a dead predecessor of this container holds autodedup.rt_lease, left to its TTL at "
        f"the claim cap's floor ({CLAIM_CAP_FLOOR}; E948): held by '{dead}'")
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == (dead, True), "it ends by its TTL"
    statements = world.statements[before:]
    assert RT_LEASE_PREDECESSOR_SQL in statements
    assert RT_LEASE_RELEASE_PREDECESSOR_SQL not in statements
    assert world.settings[key] == {"cap": CLAIM_CAP_FLOOR, "reason": "x"}
    assert not [r for r in caplog.records if r.name == incremental_lane.__name__]


def test_a_death_that_halving_cannot_cure_releases_early_only_down_to_the_floor(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every restart in place used to free the lease at boot and run the pass that kills the
    whole worker again. Now five deaths walk the cap from 500 to the floor, each released at
    once, and every death after that waits out the lease's TTL, as before E948."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    monkeypatch.setattr(incremental_lane, "run_pass_bounded", _dies)
    released: list[bool] = []
    caps: list[int] = []

    for restart in range(7):
        booted = BOOTED + 1_000 * restart
        dead = _here(booted - 106)
        _stranded(world, dead)
        try:
            out = run_incremental(lambda: world, holder=_here(booted + 60),
                                  booted_epoch=booted)
        except _Killed:
            released.append(True)
        else:
            assert (out["skipped"], out["predecessor_kept"]) == ("leased", dead)
            released.append(False)
        caps.append(world.settings[key]["cap"])

    assert released == [True] * 5 + [False] * 2
    assert caps == [250, 125, 62, 31, 25, 25, 25]
