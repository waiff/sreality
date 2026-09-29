"""The Judge page's statements, EXECUTED (`autodedup/ui_sql.py`, the Judge page section).

PREPARE proves the statements compile; only seeded rows show which pairs the population holds,
which mark is a pair's headline, what each reason picks and where its cap cuts, and that the
order and the cursor page through every row once. Also the judge lane's stratum writes
(migration 576: training provenance, which the page does not read). Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
The pairs are synthetic listing ids: every join the page makes to `listings` is a LEFT JOIN.
"""

from __future__ import annotations

import hashlib
import itertools
import json
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
    "judge": None, "tier": None, "ruled": None, "operator": None, "engine": None,
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
            c.execute(usql.JIT_OFF)
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


def _rule(cur: Any, pair: tuple[int, int], verdict: str, *, note: str | None = None,
          reasons: tuple[str, ...] = ()) -> None:
    cur.execute("INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict,"
                " decided_by, note, reasons) VALUES ('pair', %s, %s, %s, %s, %s, %s)",
                (*pair, verdict, OP, note, list(reasons)))


def _group(cur: Any, key: int, *listing_ids: int) -> None:
    for listing_id in listing_ids:
        cur.execute("INSERT INTO autodedup.cluster_members (generation, cluster_key,"
                    " listing_id) VALUES (%s, %s, %s)", (GEN, key, listing_id))


def _stored(cur: Any, pair: tuple[int, int], zone: str, score: float) -> None:
    cur.execute("INSERT INTO autodedup.pairs (generation, listing_lo, listing_hi, probes, score,"
                " zone, decision) VALUES (%s, %s, %s, ARRAY['ci'], %s, %s, %s)",
                (GEN, *pair, score, zone, f"{zone}:score"))


def _rows(cur: Any, **over: Any) -> list[dict[str, Any]]:
    cur.execute(usql.JUDGEMENTS_SQL, {**_BASE, **_NO_CURSOR, "limit": 5000, **over})
    return [dict(zip(usql.JUDGED_PAIR_COLUMNS, row)) for row in cur.fetchall()]


def _by_pair(cur: Any, **over: Any) -> dict[tuple[int, int], dict[str, Any]]:
    return {(r["listing_lo"], r["listing_hi"]): r for r in _rows(cur, **over)}


def _reasons(row: dict[str, Any]) -> set[str]:
    return {name for name in ("sample", "operator", "engine", "unsure") if row[f"is_{name}"]}


def _only(rows: list[dict[str, Any]], pairs: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """This test's own pairs, in the statement's order (the replay may hold other draws)."""
    mine = set(pairs)
    return [r for r in rows if (r["listing_lo"], r["listing_hi"]) in mine]


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
    assert _reasons(rows[agree]) == set() and rows[agree]["block"] == 2
    assert _reasons(rows[split]) == {"unsure"}, "text and vision said different words"
    assert rows[gold]["judge_tier"] == "gold" and _reasons(rows[gold]) == {"unsure"}
    assert rows[gold]["block"] == 1, "a split gold vote (2 of 3) is suggested"
    assert rows[abstain]["judge_verdict"] == "insufficient_evidence"
    assert _reasons(rows[abstain]) == {"unsure"}


def test_a_unanimous_gold_vote_is_sure_and_may_contradict_the_engine(cur):
    """Unsure is a SPLIT: three gold votes of three (1.0) are the surest word the judge has, so
    they are never "Soudce si nebyl jistý" and may carry the engine reason."""
    calm, merged = _pair(), _pair()
    _mark(cur, calm, "gold", "same_property", confidence=1.0)
    _mark(cur, merged, "gold", "different_property", confidence=1.0)
    _group(cur, merged[0], *merged)
    rows = _by_pair(cur)
    assert _reasons(rows[calm]) == set() and rows[calm]["block"] == 2
    assert rows[merged]["engine_view"] == "together"
    assert _reasons(rows[merged]) == {"engine"}
    assert merged in _by_pair(cur, reason="engine")
    assert calm not in _by_pair(cur, reason="unsure")


def test_the_operators_word_is_the_rulings_pages_own(cur):
    typed, implied, vetoed, unsure = (_pair() for _ in range(4))
    for pair in (typed, implied, vetoed, unsure):
        _mark(cur, pair, "vision", "same_property")
    _rule(cur, typed, "different", note="jiné patro", reasons=("floor_differs",))
    cur.execute("INSERT INTO autodedup.verdicts (kind, cluster_key, verdict, decided_by,"
                " generation, member_ids, note, reasons) VALUES ('cluster', %s, 'same', %s, %s,"
                " %s, 'celá skupina', ARRAY['identical_photos'])",
                (implied[0], OP, GEN, [implied[0], implied[1]]))
    cur.execute("INSERT INTO autodedup.must_not_link (listing_lo, listing_hi, source, reason)"
                " VALUES (%s, %s, 'operator', 'ci')", vetoed)
    _rule(cur, unsure, "unsure")
    rows = _by_pair(cur)
    disagree, agree = _by_pair(cur, operator="disagrees"), _by_pair(cur, operator="agrees")
    assert typed in disagree and "operator" in _reasons(rows[typed])
    assert implied in agree and rows[implied]["operator_verdict"] == "same"
    assert vetoed in disagree and rows[vetoed]["operator_verdict"] == "different"
    # The pair's own note and codes travel with its word; a group's or a veto's never do.
    assert (rows[typed]["operator_note"], rows[typed]["operator_reasons"]) == (
        "jiné patro", ["floor_differs"])
    for other in (implied, vetoed):
        assert (rows[other]["operator_note"], rows[other]["operator_reasons"]) == (None, None)
    # "Nevím" is a stored word (the buttons show it) but not a ruling: the pair stays open.
    assert rows[unsure]["operator_verdict"] == "unsure" and rows[unsure]["ruled"] is False
    assert unsure not in disagree and unsure not in agree
    ruled = _by_pair(cur, ruled=True)
    assert all(pair in ruled for pair in (typed, implied, vetoed)) and unsure not in ruled


def test_the_engine_reason_is_a_sure_judge_against_the_live_grouping(cur):
    merged, apart, timid, torn = (_pair() for _ in range(4))
    _mark(cur, merged, "vision", "different_property", confidence=0.95)
    _mark(cur, apart, "vision", "same_property", confidence=0.92)
    _mark(cur, timid, "vision", "same_property", confidence=0.5)
    _mark(cur, torn, "text", "different_property")
    _mark(cur, torn, "vision", "same_property", confidence=0.99)
    _group(cur, merged[0], *merged)
    for listing_id in (*apart, *timid, *torn):
        cur.execute("INSERT INTO autodedup.rt_fp (generation, listing_id) VALUES (%s, %s)",
                    (GEN, listing_id))
    rows = _by_pair(cur)
    disagree = _by_pair(cur, engine="disagrees")
    assert rows[merged]["engine_view"] == "together" and merged in disagree
    assert _reasons(rows[merged]) == {"engine"}
    assert (rows[apart]["engine_view"], _reasons(rows[apart])) == ("apart", {"engine"})
    assert timid in disagree and _reasons(rows[timid]) == set()
    assert _reasons(rows[torn]) == {"unsure"}, "an unsure pair is never the engine reason"
    # A suspected false merge comes first under the engine reason's cap.
    engine = [r for r in _rows(cur, reason="engine")
              if (r["listing_lo"], r["listing_hi"]) in (merged, apart)]
    assert len(engine) == 2
    # Nothing else in another generation: unseen, no engine reason.
    assert _by_pair(cur, generation="g-other")[merged]["engine_view"] == "unseen"


def test_a_pair_the_engine_holds_together_reads_no_reason_it_was_not_merged(cur):
    """A merge-zone pair in one group, and a band pair joined into one group through a third
    advert: both are merged, so neither carries "why the engine did not merge them"."""
    routes = pytest.importorskip("api.routes.autodedup")
    direct, bridged = _pair(), _pair()
    third = bridged[1] + 1
    for pair in (direct, bridged):
        _mark(cur, pair, "vision", "different_property")
    _stored(cur, direct, "merge", 0.97)
    _stored(cur, bridged, "band", 0.61)
    _stored(cur, (bridged[0], third), "merge", 0.95)
    _stored(cur, (bridged[1], third), "merge", 0.96)
    _group(cur, direct[0], *direct)
    _group(cur, bridged[0], *bridged, third)
    rows = _by_pair(cur)
    for pair, zone in ((direct, "merge"), (bridged, "band")):
        assert (rows[pair]["engine_view"], rows[pair]["zone"]) == ("together", zone)
        assert routes._judged_pair(rows[pair])["why_not_merged"] is None


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
    # The replay may already hold other sealed draws (a data migration's control draw): the
    # counter is read before and after, and this draw adds exactly its own first 100.
    cur.execute(usql.VALIDATION_JUDGE_SAMPLE_SQL, {"seed": SEED, "sample_size": 100})
    base_n, base_reviewed, base_not_same = cur.fetchone()
    pairs = _seal(cur, 120)
    _mark(cur, pairs[0], "vision", "same_property", stratum="g2:s1_ladder_only_edge")
    rows = _only(_rows(cur, reason="sample"), pairs)
    assert [(r["listing_lo"], r["listing_hi"]) for r in rows] == _seeded(pairs)[:100]
    assert all(r["block"] == 0 and _reasons(r) >= {"sample"} for r in rows)
    assert all(r["judge_verdict"] is None for r in rows
               if (r["listing_lo"], r["listing_hi"]) != pairs[0])
    # A mark requested under a pair list stays in its sealed draw's sample: the draw defines it.
    assert len(_only(_rows(cur), pairs)) == 120
    # A ruled pair sinks to the bottom of the order and still counts in the sample.
    first = _seeded(pairs)[0]
    _rule(cur, first, "same")
    rows = _only(_rows(cur, reason="sample"), pairs)
    assert (rows[-1]["listing_lo"], rows[-1]["listing_hi"]) == first
    cur.execute(usql.VALIDATION_JUDGE_SAMPLE_SQL, {"seed": SEED, "sample_size": 100})
    assert cur.fetchone() == (base_n + 100, base_reviewed + 1, base_not_same)
    cur.execute(usql.VALIDATION_JUDGE_TOTAL_SQL, {"seed": SEED, "sample_size": 100})
    n, n_reviewed, _ = cur.fetchone()
    assert n >= 120 and n_reviewed >= 1


def test_the_cursor_pages_through_every_row_once_in_order(cur):
    pairs = _seal(cur, 30)
    for i, pair in enumerate(pairs[:12]):
        _mark(cur, pair, "vision", "same_property" if i % 2 else "different_property")
    _rule(cur, pairs[3], "different")
    everything = _rows(cur)
    keys = [(int(r["ruled"]), r["block"], r["sort_hash"], r["listing_lo"], r["listing_hi"])
            for r in everything]
    assert keys == sorted(keys) and len(_only(everything, pairs)) == 30
    seen: list[tuple[int, ...]] = []
    cursor = dict(_NO_CURSOR)
    while True:
        cur.execute(usql.JUDGEMENTS_SQL, {**_BASE, **cursor, "limit": 7})
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
    assert ("reason", "operator") not in facets, "a reason a row carries, not a selection"
    assert facets[("tier", "none")] >= 4
    # The counts count what the page lists.
    assert facets[("total", None)] == len(_rows(cur, reason="suggested"))


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



def test_the_route_runs_the_judge_statements_without_jit(cur):
    """JIT compiled ~900 functions for 2.9 s to run this plan in 1 ms (the measurement behind
    `JIT_OFF`); the route's own transaction turns it off, and the plan then carries no JIT."""
    cur.execute(usql.JIT_OFF)
    cur.execute("EXPLAIN (ANALYZE, FORMAT TEXT) " + usql.JUDGEMENTS_SQL,
                {**_BASE, **_NO_CURSOR, "limit": 26})
    plan = "\n".join(row[0] for row in cur.fetchall())
    assert "JIT:" not in plan


# A generation far larger than the population, as at the country-wide roll-out: the engine's view
# must read it for the judged adverts only (the planner probes its indexes), never all of it.
BIG = "g-judge-scale"
BIG_PAIRS, BIG_FPS, BIG_MEMBERS = 40_000, 20_000, 10_000
_GENERATION_TABLES = ("pairs", "rt_fp", "cluster_members")


def _rows_read(cur: Any, sql: str, params: dict[str, Any]) -> int:
    """Rows the plan's scans of the generation's three tables returned, over every loop."""
    cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
    raw = cur.fetchone()[0]
    plan = raw if isinstance(raw, list) else json.loads(raw)
    total, stack = 0, [plan[0]["Plan"]]
    while stack:
        node = stack.pop()
        if node.get("Relation Name") in _GENERATION_TABLES:
            total += int(node["Actual Rows"]) * int(node["Actual Loops"])
        stack.extend(node.get("Plans", []))
    return total


def _engine_reference(cur: Any, ids: list[int]) -> dict[int, int | None]:
    """The generation-wide definition (the proposed splits' own CTE), read for these adverts."""
    grouped = usql.PROPOSED_SPLIT_ADVERTS_SQL.split(", touched AS (", 1)[0]
    cur.execute(grouped + " SELECT g.listing_id, g.cluster_key FROM grouped g"
                " WHERE g.listing_id = ANY(%(ids)s)", {"generation": BIG, "ids": ids})
    return dict(cur.fetchall())


def test_the_engine_view_reads_the_generation_for_the_judged_adverts_only(cur):
    # The population is this test's alone (rolled back), so the planner sees a dozen adverts
    # against a generation of 70,000 rows, as it would see thousands against millions.
    cur.execute("DELETE FROM autodedup.judgements")
    cur.execute("DELETE FROM autodedup.eval_samples")
    cur.execute("INSERT INTO autodedup.pairs (generation, listing_lo, listing_hi, probes)"
                " SELECT %s, 9700000000 + 2 * i, 9700000001 + 2 * i, ARRAY['ci']"
                " FROM generate_series(0, %s - 1) AS i", (BIG, BIG_PAIRS))
    cur.execute("INSERT INTO autodedup.rt_fp (generation, listing_id)"
                " SELECT %s, 9710000000 + i FROM generate_series(0, %s - 1) AS i",
                (BIG, BIG_FPS))
    cur.execute("INSERT INTO autodedup.cluster_members (generation, cluster_key, listing_id)"
                " SELECT %s, i / 2, 9720000000 + i FROM generate_series(0, %s - 1) AS i",
                (BIG, BIG_MEMBERS))
    together, split, stored, printed, unseen, half, twice, twice_apart = (
        _pair() for _ in range(8))
    for pair in (together, split, stored, printed, unseen, half, twice, twice_apart):
        _mark(cur, pair, "vision", "different_property")

    def member(key: int, listing_id: int) -> None:
        cur.execute("INSERT INTO autodedup.cluster_members (generation, cluster_key, listing_id)"
                    " VALUES (%s, %s, %s)", (BIG, key, listing_id))

    for listing_id in together:
        member(9_000_001, listing_id)
    member(9_000_002, split[0])
    member(9_000_003, split[1])
    # One advert a stored pair's LOW side only, the other a HIGH side only.
    cur.execute("INSERT INTO autodedup.pairs (generation, listing_lo, listing_hi, probes) VALUES"
                " (%s, %s, 9990000001, ARRAY['ci']), (%s, 9600000001, %s, ARRAY['ci'])",
                (BIG, stored[0], BIG, stored[1]))
    for listing_id in (*printed, half[0]):
        cur.execute("INSERT INTO autodedup.rt_fp (generation, listing_id) VALUES (%s, %s)",
                    (BIG, listing_id))
    # In two groups (never expected): the larger key is the one it holds.
    member(9_000_010, twice[0])
    member(9_000_011, twice[0])
    member(9_000_011, twice[1])
    member(9_000_020, twice_apart[0])
    member(9_000_021, twice_apart[0])
    member(9_000_020, twice_apart[1])
    cur.execute("ANALYZE autodedup.pairs, autodedup.rt_fp, autodedup.cluster_members,"
                " autodedup.judgements, autodedup.eval_samples")

    rows = _by_pair(cur, generation=BIG)
    expected = {together: "together", split: "apart", stored: "apart", printed: "apart",
                unseen: "unseen", half: "unseen", twice: "together", twice_apart: "apart"}
    assert {pair: rows[pair]["engine_view"] for pair in expected} == expected
    # The same answer as the generation-wide definition, advert by advert.
    ids = [listing_id for pair in expected for listing_id in pair]
    ref = _engine_reference(cur, ids)
    for lo, hi in expected:
        view = rows[(lo, hi)]["engine_view"]
        if lo in ref and hi in ref and ref[lo] is not None and ref[lo] == ref[hi]:
            assert view == "together"
        elif lo in ref and hi in ref:
            assert view == "apart"
        else:
            assert view == "unseen"

    # The plan reads a few rows per judged advert, and the generation-wide read all of them.
    page = _rows_read(cur, usql.JUDGEMENTS_SQL,
                      {**_BASE, **_NO_CURSOR, "generation": BIG, "limit": 26})
    counts = _rows_read(cur, usql.JUDGEMENTS_FACETS_SQL, {**_BASE, "generation": BIG})
    assert page <= 4 * len(ids) + 26 and counts <= 4 * len(ids), (page, counts)
    whole = _rows_read(cur, usql.PROPOSED_SPLIT_ADVERTS_SQL,
                       {"generation": BIG, "property_id": None})
    assert whole >= BIG_PAIRS + BIG_FPS + BIG_MEMBERS, whole
