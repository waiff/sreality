"""The Judge page's statements, EXECUTED (`autodedup/ui_sql.py`, the Judge page section).

PREPARE proves the statements compile; only seeded rows show which pairs the population holds,
which mark is a pair's headline, what each reason picks and where its cap cuts, and that the
order and the cursor page through every row once. Also the judge lane's stratum writes
(migration 576). Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
The pairs are synthetic listing ids: every join the page makes to `listings` is a LEFT JOIN.
"""

from __future__ import annotations

import hashlib
import itertools
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from autodedup import judge_sql
from autodedup import ui_sql as usql

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
GEN = "g-judge-live"
SEED = "v1"
SAMPLE = "ci:judge-sample"
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
_IDS = itertools.count(9_800_000_001, 2)

_BASE: dict[str, Any] = {
    "generation": GEN, "seed": SEED, "sample_size": 100, "judge_sure": 0.9, "reason": "all",
    "judge": None, "tier": None, "stratum": None, "obec": None, "cast_obce": None,
    "ruled": None, "operator": None, "engine": None,
}
_NO_CURSOR: dict[str, Any] = {
    "after_ruled": None, "after_block": None, "after_hash": None, "after_lo": None,
    "after_hi": None,
}


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


def _pair() -> tuple[int, int]:
    lo = next(_IDS)
    return lo, lo + 1


def _mark(cur: Any, pair: tuple[int, int], tier: str, verdict: str, *,
          confidence: float | None = 0.95, stratum: str | None = None, version: str = "j2",
          minutes: int = 0) -> None:
    cur.execute(
        "INSERT INTO autodedup.judgements (listing_lo, listing_hi, judge_version, tier, model,"
        " verdict, confidence, key_evidence, contradicting_evidence, stratum, created_at)"
        " VALUES (%s, %s, %s, %s, 'ci-model', %s, %s, ARRAY['klíč'], ARRAY['proti'], %s, %s)",
        (pair[0], pair[1], version, tier, verdict, confidence, stratum,
         T0 + timedelta(minutes=minutes)),
    )


def _rule(cur: Any, pair: tuple[int, int], verdict: str) -> None:
    cur.execute("INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict,"
                " decided_by) VALUES ('pair', %s, %s, %s, %s)", (*pair, verdict, OP))


def _rows(cur: Any, **over: Any) -> list[dict[str, Any]]:
    cur.execute(usql.JUDGEMENTS_SQL, {**_BASE, **_NO_CURSOR, "limit": 5000, **over})
    return [dict(zip(usql.JUDGED_PAIR_COLUMNS, row)) for row in cur.fetchall()]


def _by_pair(cur: Any, **over: Any) -> dict[tuple[int, int], dict[str, Any]]:
    return {(r["listing_lo"], r["listing_hi"]): r for r in _rows(cur, **over)}


def _reasons(row: dict[str, Any]) -> set[str]:
    return {name for name in ("sample", "operator", "engine", "unsure") if row[f"is_{name}"]}


def test_the_headline_is_the_best_tier_and_oss_is_not_the_judge(cur):
    agree, split, gold, abstain, oss = (_pair() for _ in range(5))
    _mark(cur, agree, "text", "same_property", confidence=0.8)
    _mark(cur, agree, "vision", "different_property", minutes=1, version="j1")
    _mark(cur, agree, "vision", "same_property", minutes=2)
    _mark(cur, split, "text", "same_property")
    _mark(cur, split, "vision", "different_property")
    _mark(cur, gold, "text", "different_property", minutes=5)
    _mark(cur, gold, "gold", "same_property", confidence=0.67)
    _mark(cur, abstain, "vision", "insufficient_evidence", confidence=None)
    _mark(cur, oss, "oss", "same_property")
    rows = _by_pair(cur)
    assert oss not in rows, "the rented arm is not the judge"
    # Newest within the best tier, whatever the version.
    assert (rows[agree]["judge_tier"], rows[agree]["judge_verdict"]) == ("vision",
                                                                         "same_property")
    assert rows[agree]["tiers_split"] is False and _reasons(rows[agree]) == set()
    assert rows[split]["tiers_split"] is True and _reasons(rows[split]) == {"unsure"}
    assert rows[gold]["judge_tier"] == "gold" and _reasons(rows[gold]) == {"unsure"}
    assert rows[abstain]["judge_verdict"] == "insufficient_evidence"
    assert _reasons(rows[abstain]) == {"unsure"}
    # A split gold vote is the first unsure pair the cap keeps.
    assert rows[gold]["primary_reason"] == "unsure" and rows[gold]["block"] == 1
    assert rows[agree]["block"] == 2 and rows[agree]["primary_reason"] is None


def test_the_operators_word_is_the_rulings_pages_own(cur):
    typed, implied, vetoed, unsure = (_pair() for _ in range(4))
    for pair in (typed, implied, vetoed, unsure):
        _mark(cur, pair, "vision", "same_property")
    _rule(cur, typed, "different")
    cur.execute("INSERT INTO autodedup.verdicts (kind, cluster_key, verdict, decided_by,"
                " generation, member_ids) VALUES ('cluster', %s, 'same', %s, %s, %s)",
                (implied[0], OP, GEN, [implied[0], implied[1]]))
    cur.execute("INSERT INTO autodedup.must_not_link (listing_lo, listing_hi, source, reason)"
                " VALUES (%s, %s, 'operator', 'ci')", vetoed)
    _rule(cur, unsure, "unsure")
    rows = _by_pair(cur)
    assert (rows[typed]["operator_source"], rows[typed]["operator_agreement"]) == (
        "pair", "disagrees")
    assert "operator" in _reasons(rows[typed])
    assert (rows[implied]["operator_source"], rows[implied]["operator_agreement"]) == (
        "implied", "agrees")
    assert (rows[vetoed]["operator_verdict"], rows[vetoed]["operator_agreement"]) == (
        "different", "disagrees")
    # "Nevím" is a word on the pair (the row opens, it sorts with the ruled) but no verdict.
    assert rows[unsure]["ruled"] is True and rows[unsure]["operator_agreement"] == "none"
    assert all(rows[p]["ruled"] for p in (typed, implied, vetoed, unsure))
    ruled = _rows(cur, ruled=True)
    assert {(r["listing_lo"], r["listing_hi"]) for r in ruled} >= {typed, implied, vetoed,
                                                                   unsure}


def test_the_engine_reason_is_a_sure_judge_against_the_live_grouping(cur):
    merged, apart, timid, torn = (_pair() for _ in range(4))
    _mark(cur, merged, "vision", "different_property", confidence=0.95)
    _mark(cur, apart, "vision", "same_property", confidence=0.92)
    _mark(cur, timid, "vision", "same_property", confidence=0.5)
    _mark(cur, torn, "text", "different_property")
    _mark(cur, torn, "vision", "same_property", confidence=0.99)
    for listing_id in merged:
        cur.execute("INSERT INTO autodedup.cluster_members (generation, cluster_key,"
                    " listing_id) VALUES (%s, %s, %s)", (GEN, merged[0], listing_id))
    for listing_id in (*apart, *timid, *torn):
        cur.execute("INSERT INTO autodedup.rt_fp (generation, listing_id) VALUES (%s, %s)",
                    (GEN, listing_id))
    rows = _by_pair(cur)
    assert (rows[merged]["engine_view"], rows[merged]["engine_agreement"]) == (
        "together", "disagrees")
    assert _reasons(rows[merged]) == {"engine"}
    assert (rows[apart]["engine_view"], _reasons(rows[apart])) == ("apart", {"engine"})
    assert rows[timid]["engine_agreement"] == "disagrees" and _reasons(rows[timid]) == set()
    assert _reasons(rows[torn]) == {"unsure"}, "an unsure pair is never the engine reason"
    # A suspected false merge comes first under the engine reason's cap.
    engine = [r for r in _rows(cur, reason="engine")
              if (r["listing_lo"], r["listing_hi"]) in (merged, apart)]
    assert len(engine) == 2
    # Nothing else in another generation: unseen, no engine reason.
    assert _by_pair(cur, generation="g-other")[merged]["engine_view"] == "unseen"


def _seal(cur: Any, n: int) -> list[tuple[int, int]]:
    pairs = [_pair() for _ in range(n)]
    for lo, hi in pairs:
        cur.execute("INSERT INTO autodedup.eval_samples (stratum, listing_lo, listing_hi,"
                    " sampling_rate) VALUES (%s, %s, %s, 0.01)", (SAMPLE, lo, hi))
    return pairs


def _seeded(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return sorted(pairs, key=lambda p: (
        hashlib.md5(f"{p[0]}:{p[1]}{SEED}".encode()).hexdigest(), p[0], p[1]))


def test_the_sample_is_the_first_100_of_the_sealed_draw_in_the_seeded_order(cur):
    pairs = _seal(cur, 120)
    _mark(cur, pairs[0], "vision", "same_property", stratum=None)
    rows = _rows(cur, reason="sample", stratum=SAMPLE)
    assert [(r["listing_lo"], r["listing_hi"]) for r in rows] == _seeded(pairs)[:100]
    assert all(r["block"] == 0 and r["primary_reason"] == "sample" for r in rows)
    assert all(r["judge_verdict"] is None for r in rows
               if (r["listing_lo"], r["listing_hi"]) != pairs[0])
    # The judged sample pair keeps the draw's name when its mark names no list.
    whole = _by_pair(cur, stratum=SAMPLE)
    assert len(whole) == 120 and whole[pairs[0]]["stratum"] == SAMPLE
    # A ruled pair sinks to the bottom of the order and still counts in the sample.
    first = _seeded(pairs)[0]
    _rule(cur, first, "same")
    rows = _rows(cur, reason="sample", stratum=SAMPLE)
    assert (rows[-1]["listing_lo"], rows[-1]["listing_hi"]) == first
    cur.execute(usql.VALIDATION_JUDGE_SAMPLE_SQL, {"seed": SEED, "sample_size": 100})
    assert cur.fetchone() == (100, 1, 0)
    cur.execute(usql.VALIDATION_JUDGE_TOTAL_SQL, {"seed": SEED, "sample_size": 100})
    n, n_reviewed, _ = cur.fetchone()
    assert n >= 120 and n_reviewed >= 1


def test_the_cursor_pages_through_every_row_once_in_order(cur):
    pairs = _seal(cur, 30)
    for i, pair in enumerate(pairs[:12]):
        _mark(cur, pair, "vision", "same_property" if i % 2 else "different_property")
    _rule(cur, pairs[3], "different")
    everything = _rows(cur, stratum=SAMPLE)
    keys = [(int(r["ruled"]), r["block"], r["sort_hash"], r["listing_lo"], r["listing_hi"])
            for r in everything]
    assert keys == sorted(keys) and len(keys) == 30
    seen: list[tuple[int, ...]] = []
    cursor = dict(_NO_CURSOR)
    while True:
        cur.execute(usql.JUDGEMENTS_SQL, {**_BASE, **cursor, "stratum": SAMPLE, "limit": 7})
        page = [dict(zip(usql.JUDGED_PAIR_COLUMNS, row)) for row in cur.fetchall()]
        seen += [(int(r["ruled"]), r["block"], r["sort_hash"], r["listing_lo"],
                  r["listing_hi"]) for r in page]
        if len(page) < 7:
            break
        last = page[-1]
        cursor = {"after_ruled": int(last["ruled"]), "after_block": last["block"],
                  "after_hash": last["sort_hash"], "after_lo": last["listing_lo"],
                  "after_hi": last["listing_hi"]}
    assert seen == keys


def test_the_facets_count_the_current_filter_and_every_reason(cur):
    pairs = _seal(cur, 5)
    _mark(cur, pairs[0], "text", "same_property", stratum="g2:s1_ladder_only_edge")
    _mark(cur, pairs[0], "vision", "different_property")
    cur.execute(usql.JUDGEMENTS_FACETS_SQL, {**_BASE, "reason": "suggested"})
    facets: dict[tuple[str, Any], int] = {(f, v): n for f, v, n in cur.fetchall()}
    assert facets[("reason", "sample")] >= 5
    assert facets[("reason", "unsure")] >= 1
    assert facets[("reason", "suggested")] == facets[("total", None)]
    assert facets[("stratum", "g2:s1_ladder_only_edge")] == 1
    assert facets[("judge", "none")] >= 4 and facets[("tier", "none")] >= 4
    cur.execute(usql.JUDGED_TOWNS_SQL, {"seed": SEED, "limit": 40})
    cur.fetchall()


def test_a_judged_or_sampled_pair_the_engine_never_stored_can_be_ruled(cur):
    judged, sampled, nothing = _pair(), _pair(), _pair()
    _mark(cur, judged, "text", "same_property")
    cur.execute("INSERT INTO autodedup.eval_samples (stratum, listing_lo, listing_hi,"
                " sampling_rate) VALUES (%s, %s, %s, 0.01)", (SAMPLE, *sampled))
    for pair, expected in ((judged, True), (sampled, True), (nothing, False)):
        cur.execute(usql.PAIR_EXISTS_SQL, {"listing_lo": pair[0], "listing_hi": pair[1]})
        assert (cur.fetchone() is not None) is expected, pair


def test_the_lane_stamps_a_mark_once_and_the_first_list_keeps_it(cur):
    pair = _pair()
    params = {
        "listing_lo": pair[0], "listing_hi": pair[1], "judge_version": "j2", "tier": "vision",
        "model": "ci-model", "verdict": "same_property", "confidence": 0.9,
        "unit_discriminator": None, "key_evidence": [], "contradicting_evidence": [],
        "developer_project_suspected": None, "llm_call_id": None, "cost_usd": None,
        "stratum": None,
    }
    cur.execute(judge_sql.JUDGEMENT_UPSERT_SQL, params)
    stamp = {"judge_version": "j2", "tier": "vision", "los": [pair[0]], "his": [pair[1]]}
    cur.execute(judge_sql.JUDGEMENT_STRATUM_STAMP_SQL, {**stamp, "strata": ["g2:s2_mf_only_edge"]})
    cur.execute(judge_sql.JUDGEMENT_STRATUM_STAMP_SQL, {**stamp, "strata": ["g2:other"]})
    cur.execute(judge_sql.JUDGEMENT_UPSERT_SQL, {**params, "stratum": "g2:retry"})
    cur.execute("SELECT stratum FROM autodedup.judgements WHERE listing_lo = %s"
                " AND listing_hi = %s", pair)
    assert cur.fetchone() == ("g2:s2_mf_only_edge",)
