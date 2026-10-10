"""E948: the lease a dead predecessor of this container left is released, and its death halves
the claim cap.

2026-10-10: a bootstrap pass took `autodedup.rt_lease` at 07:39:07Z as `0103ceb2490c:1:1791617947`
and the worker process died before 07:40:53Z, when Railway restarted it IN PLACE (the same
container hostname and pid, no deploy, no SIGTERM, so E941's release never ran; most likely the
OOM killer, on a 500-advert claim whose neighbourhood read grows with the store). The lease then
stranded the lane until its TTL, and the lane would soon claim 500 again. Pinned here: the
release (that row and only that row) and its read-only twin, its place right before the take,
the cap it halves in the same commit, the log line written before the pass that may die, the
limit the cap bounds, that no pass under the same limit raises the cap and a seed resets it, and
the brake: at the cap's floor the lease is left to its TTL, so a death no claim can cure never
loops the worker. The SQL itself runs against Postgres in `tests/test_rt_lease_fence_live.py`.

E948b, the same day: seven deaths left the cap at its floor under the 8 GB container with 18,471
of 38,416 adverts fingerprinted, and the operator is asked for 24 GB. The halvings answered the
smaller container, so the row records the limit it was halved under, and a limit grown by a
quarter since resets the cap before the pass; a seed would throw the fingerprints away. Pinned
in (d): the limit a halving records (never lowered by a death in a smaller container), end to
end through the release, its only caller, the reset and its commit, what never resets (an
unread limit, one of 0, a row halved while none was read), the 8 GB baseline of the row E948
left, whichever of this deploy and the resize comes first, the stamp of a seed's row, the
halving that follows a reset, and the boot that sees a dead predecessor and a grown limit at
once: the death was the smaller container's, so the lease is released and nothing halved.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from autodedup import incremental_lane, incremental_sql, rt_lease
from autodedup.incremental_lane import (
    CLAIM_CAP_FLOOR,
    E948_LIMIT_MB,
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
LIMIT = 7_629.4                          # the 8 GB container's cgroup limit, in MiB (E948b)
GROWN = 22_888.2                         # 24 GB, read the same way
HALVED = {"cap": 250, "reason": f"{DEAD} died before 2026-10-10T07:40:53Z", "limit_mb": LIMIT}
# The live row as E948 left it at 13:08Z under the 8 GB container: no `limit_mb` key at all.
LEFT_BY_E948 = {"cap": 25, "reason": "e320caaea376:1:1791637542 died before 2026-10-10T13:08:24Z"}


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
    claim at half, and the log names the death even though the pass never returns. The
    halved row records the container's memory limit, the one a bigger container's is
    measured against (E948b)."""
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
        run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                        memory_limit_mb=LIMIT)

    halved = {**HALVED, "reason": f"{dead} died before 2026-10-10T07:40:53Z"}
    assert halved["limit_mb"] == LIMIT
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
        record = halve_claim_cap(record, DEAD, BOOTED, LIMIT)
        caps.append(record["cap"])
    assert caps == [250, 125, 62, 31, 25, 25] and CLAIM_CAP_FLOOR == 25
    assert halve_claim_cap(full_claim_cap(), DEAD, BOOTED, LIMIT) == HALVED


@pytest.mark.parametrize("stored, cap", [
    (None, 500), ("500", 500), ({"cap": "a lot"}, 500), ({"reason": "x"}, 500),
    ({"cap": 3}, CLAIM_CAP_FLOOR), ({"cap": 9_000}, 500), ({"cap": 250, "reason": "x"}, 250),
])
def test_the_cap_row_reads_inside_its_bounds(stored: Any, cap: int) -> None:
    assert read_claim_cap(stored)["cap"] == cap


@pytest.mark.parametrize("stored, limit_mb", [
    ({"cap": 25}, None), ({"cap": 25, "limit_mb": None}, None), ({"cap": 25, "limit_mb": 0}, None),
    ({"cap": 25, "limit_mb": "a lot"}, None), ({"cap": 25, "limit_mb": True}, None),
    ({"cap": 25, "limit_mb": -1.0}, None),
    ({"cap": 25, "limit_mb": LIMIT}, LIMIT), ({"cap": 25, "limit_mb": 8192}, 8192.0),
])
def test_the_cap_row_reads_the_limit_it_records_or_none(stored: Any, limit_mb: Any) -> None:
    """E948b: a row that records no usable limit records none, never a number nobody read."""
    assert read_claim_cap(stored)["limit_mb"] == limit_mb


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


def test_no_pass_under_the_limit_it_was_halved_under_raises_the_cap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store keeps growing under a generation, so a size that died would die again: a pass
    in the container the cap was halved in that claimed the whole cap writes no cap row,
    however many there are. Only a seed, or a bigger container (d), raises it."""
    monkeypatch.setattr(incremental_lane, "CLAIM_CAP_FLOOR", 1)     # nine adverts in this world
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    record, written_by = {"cap": 3, "reason": "x", "limit_mb": LIMIT}, world.settings_by[key]
    world.settings[key] = dict(record)

    for _ in range(2):
        out = run_incremental(lambda: world, memory_limit_mb=LIMIT)
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
    once, and every death after that waits out the lease's TTL, as before E948. A restart in
    place keeps the container's limit, so E948b's reset never undoes the walk."""
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
                                  booted_epoch=booted, memory_limit_mb=LIMIT)
        except _Killed:
            released.append(True)
        else:
            assert (out["skipped"], out["predecessor_kept"]) == ("leased", dead)
            released.append(False)
        caps.append(world.settings[key]["cap"])

    assert released == [True] * 5 + [False] * 2
    assert caps == [250, 125, 62, 31, 25, 25, 25]
    assert world.settings[key]["limit_mb"] == LIMIT and world.settings_by[key].endswith(":halved")


# ------------------------------------------------------------------ (d) a bigger container (E948b)


def _capped(tmp_path: Path, record: dict[str, Any]) -> tuple[FakePg, str]:
    """A seeded world whose cap row is `record`, and the row's key."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    world.settings[key] = dict(record)
    return world, key


def _resets(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if "the claim cap reset from" in r.getMessage()]


@pytest.mark.parametrize("cap, limit_mb, recorded", [
    (500, 8_000.0, 8_000.0),    # the first death since a seed or a reset: its container's
    (500, 6_000.0, 6_000.0),    # a smaller one's too: a downsize after a reset is not sticky
    (250, 8_000.0, 8_000.0),    # a row already halved: the bigger of the two
    (250, 6_000.0, LIMIT),      # never lowered by a death in a smaller container
    (500, None, LIMIT),         # an unknown limit keeps the row's
    (250, None, LIMIT),
])
def test_a_halving_records_the_biggest_limit_its_deaths_happened_under(
        cap: int, limit_mb: float | None, recorded: float) -> None:
    """The halved row is what a later limit is measured against. A row a seed or a reset wrote
    records no death's limit, so the first death records its own container's; after that a
    death under a smaller limit must not lower it, or the bigger container would reset to a
    size that already died there."""
    row = {"cap": cap, "reason": "x", "limit_mb": LIMIT}
    assert halve_claim_cap(row, DEAD, BOOTED, limit_mb)["limit_mb"] == recorded


@pytest.mark.parametrize("record, died_under, halved", [
    # a 9 GB container (8,583.0 MiB), less than a quarter above 8 GB: its own, the bigger
    ({"cap": 250, "reason": "x", "limit_mb": LIMIT}, 8_583.0, (125, 8_583.0)),
    # a seed's row its first pass stamped at 6,000, then the first death, under 7,000: its own
    (full_claim_cap("rt_seed", 6_000.0), 7_000.0, (250, 7_000.0)),
    # a smaller container, an unread limit and one of 0: the row's
    ({"cap": 250, "reason": "x", "limit_mb": LIMIT}, 6_000.0, (125, LIMIT)),
    ({"cap": 250, "reason": "x", "limit_mb": LIMIT}, None, (125, LIMIT)),
    ({"cap": 250, "reason": "x", "limit_mb": LIMIT}, 0.0, (125, LIMIT)),
], ids=["9gb", "seed-6000-then-7000", "smaller", "unread", "zero"])
def test_a_restart_in_place_halves_under_the_limit_its_own_boot_reads(
        tmp_path: Path, record: dict[str, Any], died_under: float | None,
        halved: tuple[int, float]) -> None:
    """The release is the halving's only caller, so the limit a halving records is pinned
    where it runs: the row under a stranded lease of this host and the limit this boot reads,
    neither of which grows the row by a quarter. The first death since a seed or a reset
    records its container's limit, a later one the bigger of the row's and its own; anything
    else keeps the row's."""
    world, key = _capped(tmp_path, record)
    dead = _here(BOOTED - 106)
    _stranded(world, dead)

    out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                          memory_limit_mb=died_under)

    assert out["predecessor_released"] == dead and out.get("skipped") is None
    assert world.settings[key] == {"cap": halved[0], "limit_mb": halved[1],
                                   "reason": f"{dead} died before 2026-10-10T07:40:53Z"}
    assert world.settings_by[key].endswith(":halved") and out["claim_cap"]["cap"] == halved[0]


def test_a_death_in_a_smaller_container_never_resets_the_bigger_one_to_a_size_that_died(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The review's S3: 500 and 250 died under 8 GB, then 125 dies under 6,000 MB. Had the row
    recorded 6,000, the 8 GB container, a quarter above it, would reset to 500, the size that
    died there first. The row keeps 7,629.4, so the next pass there claims 62 and logs nothing."""
    world, key = _capped(tmp_path, {"cap": 125, "reason": "x", "limit_mb": LIMIT})
    dead = _here(BOOTED - 106)
    _stranded(world, dead)

    out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                          memory_limit_mb=6_000.0)

    halved = {"cap": 62, "reason": f"{dead} died before 2026-10-10T07:40:53Z", "limit_mb": LIMIT}
    assert out["predecessor_released"] == dead
    assert world.settings[key] == halved and out["claim_cap"] == halved
    assert world.settings_by[key].endswith(":halved")
    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, memory_limit_mb=LIMIT)
    assert out["claim_bound"]["cap"] == 62 and world.settings[key] == halved
    assert not _resets(caplog)


def test_a_limit_grown_by_a_quarter_resets_the_cap_in_a_commit_before_the_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """2026-10-10: seven deaths left the cap at its floor under 8 GB, and the worker moves to
    24 GB. The first pass that sees the bigger limit resets the cap to `max_listings`, with
    the reason and the new limit, in a write that commits before the pass's transaction
    opens, and logs it then: a pass that dies anyway has already raised the next claim."""
    world, key = _capped(tmp_path, {"cap": CLAIM_CAP_FLOOR, "reason": "x", "limit_mb": LIMIT})
    seen: dict[str, Any] = {}

    def dies(*_args: Any, **_kwargs: Any) -> Any:
        seen.update(in_tx=world.in_transaction, cap=dict(world.settings[key]),
                    logged=[r.getMessage() for r in caplog.records])
        raise _Killed()

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", dies)
    before = int(time.time())
    with caplog.at_level(logging.WARNING), pytest.raises(_Killed):
        run_incremental(lambda: world, memory_limit_mb=GROWN)

    reset = world.settings[key]
    reason, at = reset["reason"].rsplit(" at ", 1)
    stamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert (reset["cap"], reason, reset["limit_mb"]) == (
        PASS_LIMITS.max_listings, "memory limit grew 7,629 → 22,888 MB", GROWN)
    assert before <= stamp.timestamp() <= time.time()
    assert seen["in_tx"] is True and seen["cap"] == reset, "committed BEFORE the pass"
    assert seen["logged"] == [
        f"AUTODEDUP: {reset['reason']}; the claim cap reset from 25 to 500 (E948b)"]
    assert world.rolled_back >= 1 and world.settings_by[key].endswith(":limit_grew")


def test_the_reset_cap_bounds_the_claim_and_resets_once(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The pass claims under the reset cap, its summary's `claim_cap` (the heartbeat's) names
    the reason, and a later pass under the same limit neither writes nor logs again."""
    world, key = _capped(tmp_path, {"cap": CLAIM_CAP_FLOOR, "reason": "x", "limit_mb": LIMIT})

    with caplog.at_level(logging.WARNING):
        first = run_incremental(lambda: world, memory_limit_mb=GROWN)
        written_by = world.settings_by[key]
        again = run_incremental(lambda: world, memory_limit_mb=GROWN)

    assert first["claim_bound"]["cap"] == PASS_LIMITS.max_listings
    assert first["claim_cap"]["reason"].startswith("memory limit grew 7,629 → 22,888 MB at ")
    assert first["claim_cap"] == again["claim_cap"] == world.settings[key]
    assert world.settings_by[key] == written_by and len(_resets(caplog)) == 1


@pytest.mark.parametrize("limit_mb, resets", [
    (10_000.0, True),       # grown by a quarter exactly: "at least"
    (9_999.9, False),       # just under it: no reset, however close
    (8_000.0, False),       # the container the cap was halved in
    (4_096.0, False),       # a smaller one: its own deaths halve under it
    (None, False),          # no limit read: nothing to compare
])
def test_only_a_limit_grown_by_a_quarter_resets_the_cap(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, limit_mb: float | None,
        resets: bool) -> None:
    record = {"cap": CLAIM_CAP_FLOOR, "reason": "x", "limit_mb": 8_000.0}
    world, key = _capped(tmp_path, record)
    written_by = world.settings_by[key]

    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, memory_limit_mb=limit_mb)

    row = world.settings[key]
    if resets:
        assert (row["cap"], row["limit_mb"]) == (PASS_LIMITS.max_listings, limit_mb)
        assert row["reason"].startswith("memory limit grew 8,000 → 10,000 MB at ")
    else:
        assert row == record and world.settings_by[key] == written_by
    assert out["claim_bound"]["cap"] == row["cap"] and out["claim_cap"] == row
    assert len(_resets(caplog)) == int(resets)


@pytest.mark.parametrize("limits, resets", [
    ([LIMIT], False),           # the 8 GB container its deaths happened in
    ([8_000.0], False),         # less than a quarter above it: its limit is stamped, not this one
    ([4_096.0], False),         # a smaller one: the same
    ([GROWN], True),            # 24 GB before any pass ran under 8 GB
    ([LIMIT, GROWN], True),     # and after one did
])
def test_the_row_e948_halved_grows_from_the_8_gb_limit_whichever_comes_first(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, limits: list[float],
        resets: bool) -> None:
    """The row E948 halved today records no limit, and E948 halved only in the 8 GB container
    (8,000,000,000 bytes), so that container's limit is the row's baseline, never the limit
    its first E948b pass happens to read: under 24 GB the cap resets whether this deploy or
    the resize comes first; under anything less the row is stamped with 7,629.4, its cap kept,
    nothing logged. Both are written before the take, so also while another writer holds the
    lease."""
    assert E948_LIMIT_MB == LIMIT == round(8_000_000_000 / 1_048_576, 1)
    world, key = _capped(tmp_path, LEFT_BY_E948)
    run_incremental(lambda: world)
    assert world.settings[key] == LEFT_BY_E948, "no limit read: nothing stamped"
    _stranded(world, "rt_seed:a-runner:1:1791620000")

    with caplog.at_level(logging.WARNING):
        outs = [run_incremental(lambda: world, memory_limit_mb=limit) for limit in limits]

    assert [out["skipped"] for out in outs] == ["leased"] * len(limits)
    row = world.settings[key]
    if resets:
        assert (row["cap"], row["limit_mb"]) == (PASS_LIMITS.max_listings, GROWN)
        assert row["reason"].startswith("memory limit grew 7,629 → 22,888 MB at ")
        assert world.settings_by[key].endswith(":limit_grew") and len(_resets(caplog)) == 1
    else:
        assert row == {**LEFT_BY_E948, "limit_mb": LIMIT}
        assert world.settings_by[key].endswith(":limit_stamped") and not _resets(caplog)


@pytest.mark.parametrize("seed_row", [
    full_claim_cap("rt_seed"),              # E948b's seed: `limit_mb` null
    {"cap": 500, "reason": "rt_seed"},      # E948's: no key at all, but nothing halved
])
@pytest.mark.parametrize("limit_mb", [LIMIT, GROWN])
def test_a_seeds_row_is_stamped_with_the_limit_its_first_pass_reads(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, seed_row: dict[str, Any],
        limit_mb: float) -> None:
    """A seed runs outside the worker's container, so its row records no limit, and claims the
    whole `max_listings`, so it has nothing to reset: the first pass that reads a limit stamps
    it, the cap kept, nothing logged, also while another writer holds the lease."""
    world, key = _capped(tmp_path, seed_row)
    run_incremental(lambda: world)
    assert world.settings[key] == seed_row, "no limit read: nothing stamped"
    _stranded(world, "rt_seed:a-runner:1:1791620000")

    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, memory_limit_mb=limit_mb)

    assert out["skipped"] == "leased"
    assert world.settings[key] == full_claim_cap("rt_seed", limit_mb)
    assert world.settings_by[key].endswith(":limit_stamped") and not _resets(caplog)


def test_a_seeds_row_takes_a_bigger_limit_silently_and_a_death_under_it_halves(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A seed's row claims the whole `max_listings`, so a container a quarter bigger than the
    limit its first pass stamped has nothing to reset: the row only takes the new limit, its
    reason kept, nothing logged. It must take it, though: a death under the bigger limit then
    halves, where a row still naming the smaller one would read that death as the smaller
    container's at every boot and never halve."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    run_incremental(lambda: world, memory_limit_mb=LIMIT)
    assert world.settings[key] == full_claim_cap("rt_seed", LIMIT)

    with caplog.at_level(logging.WARNING):
        run_incremental(lambda: world, memory_limit_mb=GROWN)
    assert world.settings[key] == full_claim_cap("rt_seed", GROWN)
    assert world.settings_by[key].endswith(":limit_stamped") and not caplog.records

    dead = _here(BOOTED - 106)
    _stranded(world, dead)
    out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                          memory_limit_mb=GROWN)
    assert out["predecessor_released"] == dead
    assert (world.settings[key]["cap"], world.settings[key]["limit_mb"]) == (250, GROWN)


def test_after_a_reset_a_death_halves_from_max_listings_again(tmp_path: Path) -> None:
    """The reset raises the cap; it never switches the halving off: in the bigger container
    each death halves it again, from 500, under the bigger limit."""
    world, key = _capped(tmp_path, {"cap": CLAIM_CAP_FLOOR, "reason": "x", "limit_mb": LIMIT})
    run_incremental(lambda: world, memory_limit_mb=GROWN)
    assert world.settings[key]["cap"] == PASS_LIMITS.max_listings
    caps: list[tuple[int, float]] = []

    for restart in range(2):
        booted = BOOTED + 1_000 * restart
        dead = _here(booted - 106)
        _stranded(world, dead)
        out = run_incremental(lambda: world, holder=_here(booted + 60), booted_epoch=booted,
                              memory_limit_mb=GROWN)
        assert out["predecessor_released"] == dead
        caps.append((world.settings[key]["cap"], world.settings[key]["limit_mb"]))

    assert caps == [(250, GROWN), (125, GROWN)]


GREW = "memory limit grew 7,629 → 22,888 MB at "


@pytest.mark.parametrize("record, reason, written_by", [
    ({"cap": CLAIM_CAP_FLOOR, "reason": "x", "limit_mb": LIMIT}, GREW, ":limit_grew"),
    (LEFT_BY_E948, GREW, ":limit_grew"),                    # the review's (a): today's row
    (full_claim_cap("rt_seed", LIMIT), "rt_seed", ":limit_stamped"),    # nothing to reset
], ids=["halved-under-8gb", "left-by-e948", "seeds-row"])
def test_in_one_boot_a_grown_limit_releases_the_dead_predecessor_without_halving(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, record: dict[str, Any],
        reason: str, written_by: str) -> None:
    """A dead predecessor and a limit grown by a quarter, seen by the same boot. Every pass
    fits the row to its own limit and commits that before its take, so the growth proves the
    predecessor ran under the smaller limit: its death was the smaller container's, and the
    bigger one keeps all of `max_listings`. Its lease is released and logged, and the cap row
    is the fit's, never halved. The fit comes first: the other order would record the bigger
    limit on a halved row and swallow the growth, and at the floor leave the lease to its TTL."""
    world, key = _capped(tmp_path, record)
    dead = _here(BOOTED - 106)
    _stranded(world, dead)
    before = len(world.statements)

    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                              memory_limit_mb=GROWN)

    assert out.get("skipped") is None and out["predecessor_released"] == dead
    row = world.settings[key]
    assert (row["cap"], row["limit_mb"]) == (PASS_LIMITS.max_listings, GROWN)
    assert row["reason"].startswith(reason) and world.settings_by[key].endswith(written_by)
    assert out["claim_bound"]["cap"] == PASS_LIMITS.max_listings and out["claim_cap"] == row
    statements = world.statements[before:]
    release = statements.index(RT_LEASE_RELEASE_PREDECESSOR_SQL)
    assert statements[release - 1] == RT_SETTING_WRITE_SQL, "the fit, then the release"
    logged = [r.getMessage() for r in caplog.records if r.name == incremental_lane.__name__]
    assert logged[-1] == (
        f"AUTODEDUP: a predecessor of this container died holding autodedup.rt_lease: {dead}; "
        "released, and the claim cap kept at 500: it ran under a smaller memory limit (E948b)")
    assert len(_resets(caplog)) == int(written_by == ":limit_grew")


def test_a_lease_the_floor_left_to_its_ttl_is_released_by_a_boot_under_a_bigger_limit(
        tmp_path: Path) -> None:
    """The review's (b): at the floor under 8 GB a restart in place names its dead
    predecessor's lease and leaves it to the TTL, as E948 says; before the TTL ends the
    container restarts in place under 24 GB. That boot resets the cap, releases the lease and
    halves nothing; the growth is then spent, so the next death, under 24 GB, halves again."""
    world, key = _capped(tmp_path, LEFT_BY_E948)
    dead = _here(BOOTED - 106)
    _stranded(world, dead)

    out = run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED,
                          memory_limit_mb=LIMIT)
    assert (out["skipped"], out["predecessor_kept"]) == ("leased", dead)
    assert world.settings[key] == {**LEFT_BY_E948, "limit_mb": LIMIT}

    resized = BOOTED + 600
    out = run_incremental(lambda: world, holder=_here(resized + 60), booted_epoch=resized,
                          memory_limit_mb=GROWN)
    assert out.get("skipped") is None and out["predecessor_released"] == dead
    row = world.settings[key]
    assert (row["cap"], row["limit_mb"]) == (PASS_LIMITS.max_listings, GROWN)
    assert row["reason"].startswith(GREW) and world.settings_by[key].endswith(":limit_grew")

    again = resized + 1_000
    _stranded(world, _here(again - 106))
    out = run_incremental(lambda: world, holder=_here(again + 60), booted_epoch=again,
                          memory_limit_mb=GROWN)
    assert out["predecessor_released"] == _here(again - 106)
    assert (world.settings[key]["cap"], world.settings[key]["limit_mb"]) == (250, GROWN)


@pytest.mark.parametrize("limit_mb", [LIMIT, GROWN])
def test_a_row_halved_while_no_limit_was_read_takes_the_next_one_and_resets_nothing(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, limit_mb: float) -> None:
    """E948b writes `limit_mb` on every row, null when nothing was read: a seed's row halved by
    a death whose boot read no limit records none. Where that death happened is unknown, so the
    next limit read is stamped and resets nothing; only a halved row without the key, E948's,
    is measured from the 8 GB limit."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    dead = _here(BOOTED - 106)
    _stranded(world, dead)
    run_incremental(lambda: world, holder=_here(BOOTED + 60), booted_epoch=BOOTED)
    halved = {"cap": 250, "reason": f"{dead} died before 2026-10-10T07:40:53Z", "limit_mb": None}
    assert world.settings[key] == halved

    with caplog.at_level(logging.WARNING):
        out = run_incremental(lambda: world, memory_limit_mb=limit_mb)

    assert world.settings[key] == {**halved, "limit_mb": limit_mb}
    assert world.settings_by[key].endswith(":limit_stamped") and not _resets(caplog)
    assert out["claim_bound"]["cap"] == 250


@pytest.mark.parametrize("limit_mb", [0.0, -1.0])
def test_a_limit_of_zero_or_below_is_no_limit(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, limit_mb: float) -> None:
    """A reading of 0 or below is unknown, as it is on the row: stamped, the row would read back
    without a limit, and every pass would write it again."""
    world = _seeded(tmp_path)
    key = claim_cap_key("rt")
    written_by = world.settings_by[key]

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            run_incremental(lambda: world, memory_limit_mb=limit_mb)

    assert world.settings[key] == full_claim_cap("rt_seed")
    assert world.settings_by[key] == written_by and not _resets(caplog)
