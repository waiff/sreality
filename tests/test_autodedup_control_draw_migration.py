"""Migration 577: the control draw's operator sample (B-j), 100 pairs sealed into eval_samples.

Offline half (no DB): the file seals exactly the pre-registered operator share of the three
committed pair lists `autodedup/pairs/g2_control_{trial,c17,c18}.json`, every pair under the
stratum its list stamps, with one sampling rate per stratum, through one idempotent insert and
two guards that raise. Live half (CI's migrations job, `TEST_DATABASE_URL`; every test rolls
back): the replay sealed the 100, a second run changes nothing, a lost row comes back, and a pair
sealed under another stratum makes the file raise instead of passing quietly.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SQL = (_ROOT / "migrations" / "577_autodedup_control_draw_sample.sql").read_text(encoding="utf-8")
_PAIRS = _ROOT / "autodedup" / "pairs"
_COHORTS = ("trial", "c17", "c18")

# PREREGISTRATION.md section 3 (the draw) and section 6 (the operator's share).
_DRAWN = {"adv_small": {"trial": 50, "c17": 50, "c18": 50},
          "adv_large": {"trial": 50, "c17": 50, "c18": 50},
          "cand": {"trial": 67, "c17": 67, "c18": 66}}
_OPERATOR = {"adv_small": {"trial": 10, "c17": 10, "c18": 10},
             "adv_large": {"trial": 10, "c17": 10, "c18": 10},
             "cand": {"trial": 14, "c17": 13, "c18": 13}}

_ROW = re.compile(
    r"\('(?P<stratum>[^']+)'(?:::text)?, (?P<lo>\d+)(?:::bigint)?, (?P<hi>\d+)(?:::bigint)?, "
    r"(?P<rate>[0-9.eE+-]+)(?:::double precision)?\)"
)


def _stratum(part: str, cohort: str) -> str:
    return f"g2:control_{part}_{cohort}"


def _code() -> str:
    """The file without its comments, lower-cased, whitespace squashed."""
    body = "\n".join(line.split("--")[0] for line in _SQL.splitlines())
    return re.sub(r"\s+", " ", body).strip().lower()


def _sealed() -> list[tuple[str, int, int, float]]:
    return [(m["stratum"], int(m["lo"]), int(m["hi"]), float(m["rate"]))
            for m in _ROW.finditer(_SQL)]


def _lists() -> dict[str, list[dict]]:
    return {c: json.loads((_PAIRS / f"g2_control_{c}.json").read_text(encoding="utf-8"))
            for c in _COHORTS}


def test_the_three_lists_are_the_preregistered_draw() -> None:
    seen: set[tuple[int, int]] = set()
    for cohort, rows in _lists().items():
        counts = Counter(r["stratum"] for r in rows)
        assert counts == {_stratum(p, cohort): n[cohort] for p, n in _DRAWN.items()}, cohort
        for r in rows:
            assert set(r) == {"lo", "hi", "stratum"} and r["lo"] < r["hi"], r
            pair = (r["lo"], r["hi"])
            assert pair not in seen, f"{pair} is drawn twice"
            seen.add(pair)
    assert len(seen) == 500


def test_no_drawn_pair_is_on_another_committed_list() -> None:
    drawn = {(r["lo"], r["hi"]) for rows in _lists().values() for r in rows}
    for path in sorted(_PAIRS.glob("*.json")):
        if path.stem.startswith("g2_control_"):
            continue
        for entry in json.loads(path.read_text(encoding="utf-8")):
            lo, hi = (entry["lo"], entry["hi"]) if isinstance(entry, dict) else entry
            assert (min(lo, hi), max(lo, hi)) not in drawn, (path.name, lo, hi)


def test_the_file_seals_the_operator_share_of_each_stratum() -> None:
    sealed = _sealed()
    assert len(sealed) == 100
    assert len({(lo, hi) for _, lo, hi, _ in sealed}) == 100
    listed = {(r["lo"], r["hi"]): r["stratum"] for rows in _lists().values() for r in rows}
    for stratum, lo, hi, _ in sealed:
        assert lo < hi
        assert listed.get((lo, hi)) == stratum, f"{lo}x{hi} is not listed under {stratum}"
    counts = Counter(s for s, _, _, _ in sealed)
    assert counts == {_stratum(p, c): n[c] for p, n in _OPERATOR.items() for c in _COHORTS}


def test_one_sampling_rate_per_stratum_below_the_operators_share_of_the_draw() -> None:
    rates: dict[str, set[float]] = {}
    for stratum, _, _, rate in _sealed():
        rates.setdefault(stratum, set()).add(rate)
    for part in _DRAWN:
        for cohort in _COHORTS:
            (rate,) = rates[_stratum(part, cohort)]
            # k of a population of N >= the n drawn: 0 < k / N <= k / n.
            assert 0 < rate <= _OPERATOR[part][cohort] / _DRAWN[part][cohort]


def test_it_is_one_idempotent_insert_with_two_guards_that_raise() -> None:
    code = _code()
    assert code.count("insert into autodedup.eval_samples") == 1
    assert "on conflict (listing_lo, listing_hi) do nothing" in code
    for forbidden in ("delete ", "update ", "drop ", "alter ", "create ", "truncate ", "grant ",
                      "revoke "):
        assert forbidden not in code, forbidden
    assert "if elsewhere > 0 then raise exception" in code
    assert "if inserted + already <> 100 then raise exception" in code
    assert code.startswith("set lock_timeout = '5s';") and code.rstrip().endswith(
        "reset lock_timeout;")


# ---------------------------------------------------------------- executed (CI migrations job)

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)


@pytest.fixture()
def cur():
    import psycopg

    conn = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=30000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def _present(cur) -> int:
    sealed = _sealed()
    cur.execute(
        "SELECT count(*) FROM autodedup.eval_samples e"
        " JOIN unnest(%s::text[], %s::bigint[], %s::bigint[]) AS d(stratum, lo, hi)"
        " ON e.listing_lo = d.lo AND e.listing_hi = d.hi AND e.stratum = d.stratum",
        ([s for s, _, _, _ in sealed], [lo for _, lo, _, _ in sealed],
         [hi for _, _, hi, _ in sealed]))
    return cur.fetchone()[0]


@_needs_db
def test_the_replay_sealed_the_100_and_a_second_run_changes_nothing(cur) -> None:
    assert _present(cur) == 100
    cur.execute("SELECT count(*) FROM autodedup.eval_samples")
    before = cur.fetchone()[0]
    cur.execute(_SQL)
    cur.execute("SELECT count(*) FROM autodedup.eval_samples")
    assert cur.fetchone()[0] == before
    assert _present(cur) == 100


@_needs_db
def test_a_lost_row_comes_back(cur) -> None:
    stratum, lo, hi, _ = _sealed()[0]
    cur.execute("DELETE FROM autodedup.eval_samples WHERE listing_lo = %s AND listing_hi = %s",
                (lo, hi))
    assert _present(cur) == 99
    cur.execute(_SQL)
    assert _present(cur) == 100


@_needs_db
def test_a_pair_sealed_under_another_stratum_makes_it_raise(cur) -> None:
    import psycopg

    _, lo, hi, _ = _sealed()[-1]
    cur.execute("UPDATE autodedup.eval_samples SET stratum = 'ci:other-draw'"
                " WHERE listing_lo = %s AND listing_hi = %s", (lo, hi))
    with pytest.raises(psycopg.errors.RaiseException, match="another stratum"):
        cur.execute(_SQL)
