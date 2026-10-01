"""Rule #3's policy, pure: the per-walk verify budget and the gone-rate breaker.

The budget NUMBERS are measured, not reasoned (migration 452). Sixty days of
real sweeps put the per-sweep share of a category at p95=1.8% and p99=3.4%, and
then the tail jumps to 86% -- routine churn and genuine incidents are two
populations with a wide gap between them. A first cut at 2%/500 sat inside the
churn population and would have bitten 446 times in 60 days on sreality and
idnes rentals, so these tests pin the real distribution, not a round number.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scraper import delist_policy

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
CAP = {"fraction": 0.10, "min_rows": 2000, "overrides": []}


def _b(setting: object, nominated: int, active: int, **scope: Any) -> delist_policy.VerifyBudget:
    kw: dict[str, Any] = {"source": "ceskereality", "category_main": "byt",
                          "category_type": "prodej", "subtype": None}
    kw.update(scope)
    return delist_policy.verify_budget(setting, nominated=nominated, active_rows=active, now=NOW, **kw)


# --- the calibration --------------------------------------------------------


@pytest.mark.parametrize(("nominated", "active", "queue", "deferred"), [
    (100, 10_000, 100, 0),            # routine
    (29_400, 78_718, 7_871, 21_529),  # the mass flip a repaired ceskereality walk unblocked
    (217, 552, 217, 0),               # sreality pozemek/drazba, 39%: small category, cap None
    (67, 615, 67, 0),                 # idnes dum/pronajem, 11%: small category, cap None
    (768, 8_431, 768, 0),             # idnes byt/pronajem 9.1%: churn
    (609, 8_458, 609, 0),             # idnes byt/pronajem 7.2%: churn
    (708, 12_695, 708, 0),            # sreality byt/pronajem 5.6%: churn
    (437, 27_170, 437, 0),            # idnes byt/prodej 1.6%: churn
    (9_557, 11_198, 1_119, 8_438),    # realitymix dum/prodej 86.3%: incident
    (734, 2_436, 243, 491),           # ceskereality komercni/prodej 30.1%: incident
    (1_175, 6_272, 627, 548),         # sreality pozemek/podil 18.7%: incident
    (330, 2_414, 241, 89),            # ceskereality 13.7%: incident
])
def test_the_calibration(nominated: int, active: int, queue: int, deferred: int) -> None:
    budget = _b(CAP, nominated, active)
    assert (budget.queue, budget.deferred) == (queue, deferred)
    assert budget.override is None
    if active < 2000:
        assert budget.cap is None
    else:
        assert budget.cap == max(1, int(active * 0.1))


def test_the_cap_edge_is_inclusive() -> None:
    assert _b(CAP, 1_000, 10_000).deferred == 0                       # exactly 10%
    b = _b(CAP, 1_001, 10_000)
    assert (b.queue, b.deferred) == (1_000, 1)


def test_the_cap_is_operator_tunable() -> None:
    assert _b({"fraction": 0.5, "min_rows": 100}, 4_000, 10_000).deferred == 0


@pytest.mark.parametrize("setting", [None, "not a json object", []])
def test_a_setting_that_is_not_an_object_means_the_defaults(setting: object, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scraper.delist_policy"):
        b = _b(setting, 5_000, 30_000)
    assert (b.queue, b.deferred) == (3_000, 2_000)
    assert caplog.records == []


def test_a_broken_setting_cannot_disarm_the_throttle(caplog) -> None:
    """A knob that fails open is not a knob, it is a hole."""
    with caplog.at_level(logging.WARNING, logger="scraper.delist_policy"):
        b = _b({"fraction": "not-a-number"}, 29_400, 78_718)
    assert (b.queue, b.deferred) == (7_871, 21_529)
    assert [r.getMessage() for r in caplog.records if "falling back to defaults" in r.getMessage()]


def test_a_broken_setting_also_drops_its_overrides() -> None:
    """The fallback is the defaults with NO lift: a half-parsed knob lifts nothing."""
    b = _b({"fraction": "x", "overrides": [_valve()]}, 29_400, 78_718)
    assert b.override is None and b.deferred == 21_529


# --- the operator lift ------------------------------------------------------


def _future() -> str:
    return (NOW + timedelta(days=2)).isoformat()


def _past() -> str:
    return (NOW - timedelta(days=1)).isoformat()


def _valve(**over: Any) -> dict[str, Any]:
    base = {"source": "ceskereality", "category_main": "byt", "category_type": "prodej",
            "max_rows": 30_000, "until": _future(), "reason": "verified by fetch"}
    base.update(over)
    return base


def _setting(*overrides: dict[str, Any]) -> dict[str, Any]:
    return {"fraction": 0.10, "min_rows": 2000, "overrides": list(overrides)}


def test_an_override_lifts_exactly_its_own_scope() -> None:
    valve = _valve()
    b = _b(_setting(valve), 29_400, 78_718)
    assert b.override == valve
    assert (b.queue, b.deferred) == (29_400, 0)


def test_an_override_does_not_lift_a_different_source() -> None:
    b = _b(_setting(_valve()), 29_400, 78_718, source="idnes")
    assert b.override is None and b.deferred == 21_529


def test_an_override_does_not_lift_a_different_category() -> None:
    b = _b(_setting(_valve()), 29_400, 78_718, category_main="dum")
    assert b.override is None and b.deferred == 21_529


def test_an_omitted_scope_field_is_a_wildcard_but_max_rows_still_binds() -> None:
    wide = _valve(category_main=None, category_type=None, max_rows=30_000)
    assert _b(_setting(wide), 29_400, 78_718).override == wide
    narrow = _valve(category_main=None, category_type=None, max_rows=1_000)
    assert _b(_setting(narrow), 29_400, 78_718).override is None


def test_an_expired_override_is_ignored() -> None:
    assert _b(_setting(_valve(until=_past())), 29_400, 78_718).override is None


def test_an_override_expiring_exactly_now_is_ignored() -> None:
    assert _b(_setting(_valve(until=NOW.isoformat())), 29_400, 78_718).override is None


def test_an_override_without_an_expiry_is_ignored() -> None:
    b = _b(_setting({"source": "ceskereality", "max_rows": 30_000}), 29_400, 78_718)
    assert b.override is None


def test_a_malformed_override_fails_shut_and_does_not_poison_the_others(caplog) -> None:
    bad = {"source": "ceskereality", "until": "not-a-date", "max_rows": "lots"}
    with caplog.at_level(logging.WARNING, logger="scraper.delist_policy"):
        b = _b(_setting(bad, _valve()), 29_400, 78_718)
    assert b.override is not None and b.deferred == 0
    assert len([r for r in caplog.records if "ignoring malformed override" in r.getMessage()]) == 1
    assert _b(_setting(bad), 29_400, 78_718).override is None


def test_overrides_that_are_not_a_list_are_ignored() -> None:
    setting = {"fraction": 0.10, "min_rows": 2000, "overrides": "all of them"}
    assert _b(setting, 29_400, 78_718).override is None


def test_a_naive_timestamp_is_read_as_utc() -> None:
    naive = (NOW + timedelta(days=2)).replace(tzinfo=None).isoformat()
    assert _b(_setting(_valve(until=naive)), 29_400, 78_718).override is not None


def test_a_subtype_named_override_matches_only_its_subtype() -> None:
    valve = _valve(category_main="dum", subtype="chata", source="bazos")
    scope = {"source": "bazos", "category_main": "dum"}
    assert _b(_setting(valve), 29_400, 78_718, **scope).override is None
    assert _b(_setting(valve), 29_400, 78_718, subtype="chata", **scope).override == valve


def test_an_override_without_category_main_matches_the_agenda_scope() -> None:
    valve = _valve(source="remax", category_main=None)
    assert _b(_setting(valve), 29_400, 78_718, source="remax", category_main=None).override == valve
    named = _valve(source="remax", category_main="byt")
    assert _b(_setting(named), 29_400, 78_718, source="remax", category_main=None).override is None


def test_the_override_is_consulted_only_above_the_cap() -> None:
    valve = _valve(max_rows=1)
    b = _b(_setting(valve), 100, 10_000)
    assert b.override is None and (b.queue, b.deferred) == (100, 0)


@pytest.mark.parametrize("nominated", [1, 199, 200, 201, 5_000])
@pytest.mark.parametrize("active", [0, 1_999, 2_000, 2_010, 50_000])
def test_the_budget_invariants(nominated: int, active: int) -> None:
    for setting in (CAP, _setting(_valve(max_rows=10_000))):
        b = _b(setting, nominated, active)
        assert b.queue + b.deferred == nominated
        assert 0 <= b.queue <= nominated
        if b.deferred > 0:
            assert b.override is None and b.cap is not None and b.queue == b.cap
        if b.override is not None:
            assert b.deferred == 0


# --- the gone-rate breaker --------------------------------------------------

VERIFY = -2
INGEST = 0


def _breaker() -> delist_policy.GoneRateBreaker:
    return delist_policy.GoneRateBreaker("bazos", exempt_priority=VERIFY)


def test_nineteen_gone_ingest_fetches_are_still_noise() -> None:
    b = _breaker()
    assert [b.observe(INGEST, "gone") for _ in range(19)] == [False] * 19
    assert not b.tripped


def test_twenty_of_twenty_trips_once_and_stays_tripped() -> None:
    b = _breaker()
    verdicts = [b.observe(INGEST, "gone") for _ in range(20)]
    assert verdicts == [False] * 19 + [True]
    assert b.tripped
    assert b.observe(INGEST, "gone") is False and b.tripped
    assert b.observe(INGEST, "ok") is False and b.tripped


def test_the_share_must_exceed_half() -> None:
    half = _breaker()
    for i in range(20):
        half.observe(INGEST, "gone" if i < 10 else "ok")
    assert not half.tripped                                   # 10 of 20 is not a majority
    over = _breaker()
    verdicts = [over.observe(INGEST, "gone" if i < 11 else "ok") for i in range(20)]
    assert over.tripped and verdicts.count(True) == 1         # 11 of 20 is


def test_presence_checks_never_count() -> None:
    """A backlog of truly dead listings legitimately reads 100% gone."""
    b = _breaker()
    assert not any(b.observe(VERIFY, "gone") for _ in range(100))
    assert not b.tripped and b.ingest_fetched == 0


def test_the_reason_text_is_pinned() -> None:
    """The runner logs it and the queue row's last_error carries it."""
    b = _breaker()
    for _ in range(20):
        b.observe(INGEST, "gone")
    assert b.reason == (
        "gone-rate breaker: 20 of 20 ingest fetches read gone this run "
        "-- the portal, not the market")


def test_the_breaker_never_logs(caplog) -> None:
    b = _breaker()
    with caplog.at_level(logging.DEBUG):
        for _ in range(25):
            b.observe(INGEST, "gone")
    assert caplog.records == []


def test_the_policy_never_imports_the_db_module() -> None:
    """db.py imports this module; the reverse import would be circular, and the policy is pure."""
    src = Path(delist_policy.__file__).read_text(encoding="utf-8")
    assert "from scraper import db" not in src
    assert "import psycopg" not in src
