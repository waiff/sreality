"""The ceiling on how much of a category one sweep may delist (migration 451).

mark_inactive never had one: it flipped every unseen active row of a category in
a single statement, however many that was. That was survivable only because the
completeness gate kept the dangerous cases from ever running — a coincidence,
not a safety property, and the coincidence ends every time a portal's walk is
repaired. Fixing coverage is the SAME EVENT as authorising the mass flip it
unblocks: ceskereality's rebuilt walk went from 85.7% to 99.8% on byt/prodej in
one deploy, and ~29,400 rows became eligible the moment its flag allows it.
idnes has identical exposure the first time its walk ever completes.

The NUMBERS are measured, not reasoned (migration 452). Sixty days of real
sweeps put the per-sweep share of a category at p95=1.8% and p99=3.4%, and then
the tail jumps to 86% — routine churn and genuine incidents are two populations
with a wide gap between them. The first cut at 2%/500 sat inside the churn
population and would have tripped 446 times in 60 days, latching delisting shut
on sreality and idnes rentals. A breaker calibrated into the noise is an outage
generator, so these tests pin the real distribution, not a plausible-sounding
round number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scraper import db


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM app_settings" in s:
            self._rows = [(self._conn.setting,)] if self._conn.setting is not None else []
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, setting: dict[str, Any] | None = None) -> None:
        self.setting = setting
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def _allowed(conn, candidates: int, active_rows: int) -> bool:
    return db._delist_flip_allowed(
        conn, source="ceskereality", category_main="byt", category_type="prodej",
        subtype=None, candidates=candidates, active_rows=active_rows,
    )


def test_a_routine_sweep_is_allowed() -> None:
    conn = _Conn()
    assert _allowed(conn, candidates=100, active_rows=10_000) is True
    assert not any("delist_flip_refusals" in s for s, _ in conn.executed)


def test_the_mass_flip_is_refused() -> None:
    """The live shape: 78,718 active against 48,235 declared, so a newly-complete
    walk would flip ~29,400 in one pass."""
    conn = _Conn()
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_refusal_is_recorded_not_merely_logged() -> None:
    """An Actions log expires. The whole lesson of this sprint is that a signal
    nothing can query is a signal nobody receives."""
    conn = _Conn()
    _allowed(conn, candidates=29_400, active_rows=78_718)
    insert = next((p for s, p in conn.executed if "delist_flip_refusals" in s), None)
    assert insert is not None
    assert 29_400 in insert and 78_718 in insert


def test_small_categories_are_exempt() -> None:
    """The small categories are the churny ones. sreality pozemek/drazba holds
    ~600 live rows and legitimately turns over 6-39% of them in one sweep because
    auctions end on a date; idnes dum/pronajem does the same at ~630. Policing
    those is noise, so the floor is on CATEGORY SIZE, not on the ceiling."""
    conn = _Conn()
    assert _allowed(conn, candidates=217, active_rows=552) is True   # pozemek/drazba, 39%
    assert _allowed(conn, candidates=67, active_rows=615) is True    # dum/pronajem, 11%


def test_routine_churn_on_the_big_portals_is_not_policed() -> None:
    """The regression the first calibration would have caused. Every one of these
    is a real sweep from the last 60 days on a healthy portal; at 2% all four are
    refused, and because the cap latches, delisting stalls there permanently."""
    conn = _Conn()
    assert _allowed(conn, candidates=768, active_rows=8_431) is True   # idnes byt/pronajem 9.1%
    assert _allowed(conn, candidates=609, active_rows=8_458) is True   # idnes byt/pronajem 7.2%
    assert _allowed(conn, candidates=708, active_rows=12_695) is True  # sreality byt/pronajem 5.6%
    assert _allowed(conn, candidates=437, active_rows=27_170) is True  # idnes byt/prodej 1.6%


def test_the_real_incidents_are_refused() -> None:
    """The other side of the same calibration: the four distinct events in that
    window that a human should have been interrupted for."""
    conn = _Conn()
    assert _allowed(conn, candidates=9_557, active_rows=11_198) is False  # realitymix 86.3%
    assert _allowed(conn, candidates=734, active_rows=2_436) is False     # ceskereality 30.1%
    assert _allowed(conn, candidates=1_175, active_rows=6_272) is False   # sreality 18.7%
    assert _allowed(conn, candidates=330, active_rows=2_414) is False     # ceskereality 13.7%


def test_the_boundary_is_inclusive() -> None:
    conn = _Conn()
    assert _allowed(conn, candidates=1_000, active_rows=10_000) is True   # exactly 10%
    assert _allowed(conn, candidates=1_001, active_rows=10_000) is False


def test_the_cap_is_operator_tunable() -> None:
    conn = _Conn(setting={"fraction": 0.5, "min_rows": 100})
    assert _allowed(conn, candidates=4_000, active_rows=10_000) is True


def test_a_broken_setting_cannot_disarm_the_cap() -> None:
    """A knob that fails open is not a knob, it is a hole. Garbage in
    app_settings must fall back to the baked defaults, never to 'allow'."""
    conn = _Conn(setting={"fraction": "not-a-number"})
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_settings_read_failure_cannot_disarm_the_cap() -> None:
    class _Exploding(_Conn):
        def cursor(self):  # noqa: ANN201
            raise RuntimeError("pooler said no")

    assert _allowed(_Exploding(), candidates=29_400, active_rows=78_718) is False


# --- the release valve -------------------------------------------------------
#
# A refusal does not clear itself: the unswept rows keep aging, so the next sweep
# proposes MORE and is refused again. That latch is correct breaker behaviour —
# an auto-reclosing breaker defeats the purpose — but a breaker with no reset is
# a permanent stall, and the only reset migration 451 offered was raising the
# global ceiling, which disarms the guard for every portal at once.


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _valve(**over: Any) -> dict[str, Any]:
    base = {"source": "ceskereality", "category_main": "byt", "category_type": "prodej",
            "max_rows": 30_000, "until": _future(), "reason": "verified by fetch"}
    base.update(over)
    return base


def _setting(*overrides: dict[str, Any]) -> dict[str, Any]:
    return {"fraction": 0.10, "min_rows": 2000, "overrides": list(overrides)}


def test_the_latch_stays_shut_without_an_override() -> None:
    conn = _Conn(setting=_setting())
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_an_override_releases_exactly_its_own_scope() -> None:
    conn = _Conn(setting=_setting(_valve()))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is True
    assert not any("delist_flip_refusals" in s for s, _ in conn.executed)


def test_an_override_does_not_release_a_different_scope() -> None:
    """The whole point of scoping: releasing ceskereality's flats must not
    release idnes, or another category of the same portal."""
    conn = _Conn(setting=_setting(_valve()))
    assert db._delist_flip_allowed(
        conn, source="idnes", category_main="byt", category_type="prodej",
        subtype=None, candidates=29_400, active_rows=78_718,
    ) is False
    conn = _Conn(setting=_setting(_valve()))
    assert db._delist_flip_allowed(
        conn, source="ceskereality", category_main="dum", category_type="prodej",
        subtype=None, candidates=29_400, active_rows=78_718,
    ) is False


def test_an_omitted_scope_field_is_a_wildcard_but_max_rows_still_binds() -> None:
    """A portal-wide release is legitimate after a portal-wide verification, and
    max_rows is what keeps it from being a blank cheque."""
    conn = _Conn(setting=_setting(_valve(category_main=None, category_type=None,
                                         max_rows=30_000)))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is True
    conn = _Conn(setting=_setting(_valve(category_main=None, category_type=None,
                                         max_rows=1_000)))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_an_expired_override_is_ignored() -> None:
    """An override that outlives its investigation stops being one. This is the
    property that keeps the valve from silently becoming a permanent hole."""
    conn = _Conn(setting=_setting(_valve(until=_past())))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_an_override_without_an_expiry_is_ignored() -> None:
    conn = _Conn(setting=_setting({"source": "ceskereality", "max_rows": 30_000}))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_malformed_override_fails_shut_and_does_not_poison_the_others() -> None:
    """The valve fails shut like the cap it releases — and one bad entry must not
    stop a later good one from being read."""
    conn = _Conn(setting=_setting({"source": "ceskereality", "until": "not-a-date",
                                   "max_rows": "lots"}, _valve()))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is True
    conn = _Conn(setting=_setting({"source": "ceskereality", "until": "not-a-date",
                                   "max_rows": "lots"}))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_overrides_that_are_not_a_list_are_ignored() -> None:
    conn = _Conn(setting={"fraction": 0.10, "min_rows": 2000, "overrides": "all of them"})
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_naive_timestamp_is_read_as_utc_not_crashed_on() -> None:
    naive = (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None).isoformat()
    conn = _Conn(setting=_setting(_valve(until=naive)))
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is True
