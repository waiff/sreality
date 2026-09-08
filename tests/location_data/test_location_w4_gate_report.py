"""The W4 gate report: verdict logic, and the SQL classifier's parity with the Python one.

No database. The parity test is behavioural, not textual: every key the SQL names is fed to
W1's `sreality_payload_shape` one at a time and must land in the same bucket, and the
precedence (post-cutover before legacy) is asserted on a payload carrying both key sets.
"""

from __future__ import annotations

import re

import pytest

from location_data.claims_intake import sreality_payload_shape
from location_data.refetch_cohort import COHORT_LANE
from scripts.location_w4_gate_report import (
    _BEZREALITKY_SQL,
    _COHORT_STATE_SQL,
    _D3_COVERAGE_SQL,
    _LEGACY_SHARE_SQL,
    SHAPE_CASE_SQL,
    VERDICT_FAIL,
    VERDICT_NO_DATA,
    VERDICT_PASS,
    VERDICT_PORTAL_CAPPED,
    Arm,
    Report,
    decide_bezrealitky,
    decide_d3_coverage,
    decide_legacy_share,
    gather,
    overall,
    render,
    to_json,
)

# ---------------------------------------------------------------- SQL ↔ Python parity

def _arrays(sql: str) -> list[list[str]]:
    return [re.findall(r"'([a-z_]+)'", body) for body in re.findall(r"array\[(.*?)\]", sql)]


def test_every_post_cutover_key_the_sql_names_is_post_cutover_in_python():
    post, legacy = _arrays(SHAPE_CASE_SQL)
    for key in post:
        assert sreality_payload_shape({"locality": {key: 1}}) == "post_cutover", key


def test_every_legacy_key_the_sql_names_is_legacy_in_python():
    post, legacy = _arrays(SHAPE_CASE_SQL)
    for key in legacy:
        assert sreality_payload_shape({"locality": {key: 1}}) == "legacy", key


def test_the_sql_tests_post_cutover_before_legacy_like_python_does():
    post, legacy = _arrays(SHAPE_CASE_SQL)
    assert sreality_payload_shape({"locality": {post[0]: 1, legacy[0]: 1}}) == "post_cutover"
    assert SHAPE_CASE_SQL.index("'post_cutover'") < SHAPE_CASE_SQL.index("'legacy'")


def test_the_sql_has_both_absent_arms_python_has():
    """`sreality_payload_shape` returns `absent` twice: not-a-dict, and an object with neither
    key set. A three-way CASE drops the second bucket and the shares stop summing to 100."""
    assert sreality_payload_shape({"locality": {"unexpected": 1}}) == "absent"
    assert sreality_payload_shape({}) == "absent"
    assert SHAPE_CASE_SQL.count("'absent'") == 2
    assert "ELSE 'absent'" in SHAPE_CASE_SQL


def test_the_sql_is_not_null_blind_on_a_missing_locality_key():
    """`jsonb_typeof(NULL)` is NULL and `NULL <> 'object'` is NULL — a plain `<>` silently
    drops exactly the truncation cohort. Only `IS DISTINCT FROM` counts it."""
    assert "IS DISTINCT FROM 'object'" in SHAPE_CASE_SQL
    assert "<> 'object'" not in SHAPE_CASE_SQL


def test_the_legacy_share_is_measured_on_listings_not_the_cohort_table():
    assert "FROM listings" in _LEGACY_SHARE_SQL
    assert "location_enrichment_state" not in _LEGACY_SHARE_SQL
    assert "is_active" in _LEGACY_SHARE_SQL


def test_the_d3_denominator_keeps_pending_and_exhausted_rows():
    """'The refetched cohort' must count the rows that did NOT flip, or the arm passes by
    construction the moment one row is placed."""
    assert "IS DISTINCT FROM 'not_applicable'" in _D3_COVERAGE_SQL
    assert "'placed'" not in _D3_COVERAGE_SQL
    assert "l.is_active" in _D3_COVERAGE_SQL


@pytest.mark.parametrize("sql", [_D3_COVERAGE_SQL, _COHORT_STATE_SQL])
def test_cohort_reads_are_lane_scoped(sql: str):
    assert "lane = %(lane)s" in sql


# ---------------------------------------------------------------- verdicts

def test_legacy_share_passes_strictly_under_two_percent():
    assert decide_legacy_share({"legacy": 199, "post_cutover": 9801}).verdict == VERDICT_PASS
    assert decide_legacy_share({"legacy": 200, "post_cutover": 9800}).verdict == VERDICT_FAIL
    assert decide_legacy_share({}).verdict == VERDICT_NO_DATA


def test_legacy_share_detail_names_all_three_buckets_so_they_can_be_summed():
    arm = decide_legacy_share({"legacy": 5, "post_cutover": 90, "absent": 5})
    assert "legacy 5" in arm.detail and "post_cutover 90" in arm.detail and "absent 5" in arm.detail
    assert arm.measured == "5.0 %"


def test_d3_coverage_counts_still_legacy_rows_against_the_gate():
    rows = [("placed", 95, 95), ("skipped", 5, 0)]
    arm = decide_d3_coverage(rows)
    assert arm.verdict == VERDICT_PASS and arm.measured == "95.0 %"
    arm = decide_d3_coverage([("placed", 94, 94), ("skipped", 6, 0)])
    assert arm.verdict == VERDICT_FAIL
    assert decide_d3_coverage([]).verdict == VERDICT_NO_DATA


def test_bezrealitky_below_threshold_is_portal_capped_not_failed_when_nothing_is_ours():
    share, remainder = decide_bezrealitky(active=5951, key_present=5818, key_absent=133, with_id=2861)
    assert share.verdict == VERDICT_PORTAL_CAPPED
    assert "2957 publish null" in share.detail
    assert remainder.verdict == VERDICT_FAIL and remainder.measured == "133"


def test_bezrealitky_remainder_passes_at_zero_and_share_passes_at_threshold():
    share, remainder = decide_bezrealitky(active=100, key_present=100, key_absent=0, with_id=95)
    assert share.verdict == VERDICT_PASS
    assert remainder.verdict == VERDICT_PASS
    assert decide_bezrealitky(0, 0, 0, 0)[0].verdict == VERDICT_NO_DATA


def test_overall_fails_on_any_fail_and_portal_capped_alone_does_not_fail():
    ok = Arm("a", VERDICT_PASS, "", "")
    capped = Arm("b", VERDICT_PORTAL_CAPPED, "", "")
    assert overall((ok, capped)) == VERDICT_PASS
    assert overall((ok, capped, Arm("c", VERDICT_FAIL, "", ""))) == VERDICT_FAIL
    assert overall((ok, Arm("d", VERDICT_NO_DATA, "", ""))) == VERDICT_NO_DATA


# ---------------------------------------------------------------- gather + render

class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql = " ".join(sql.split())
        self._conn.executed.append((self._sql, params))

    def fetchall(self):
        if "FROM listings WHERE source = 'sreality'" in self._sql:
            return [("legacy", 1), ("post_cutover", 99)]
        if "es.last_outcome" in self._sql and "GROUP BY es.last_outcome" in self._sql:
            return [("placed", 96, 96), ("skipped", 4, 0)]
        if "GROUP BY 1, 2, 3" in self._sql:
            return [("placed", False, True, 96), ("skipped", False, False, 4)]
        return []

    def fetchone(self):
        return (100, 100, 0, 96)


class _Conn:
    def __init__(self):
        self.executed: list = []

    def cursor(self):
        return _Cursor(self)

    def transaction(self):
        return _Cursor(self)


def test_gather_reads_only_and_renders_every_arm():
    conn = _Conn()
    report = gather(conn, statement_timeout_s=5)
    assert isinstance(report, Report)
    assert report.verdict == VERDICT_PASS
    assert [a.verdict for a in report.arms] == [VERDICT_PASS] * 4
    assert not [s for s, _ in conn.executed if s.startswith(("UPDATE", "INSERT", "DELETE"))]
    assert all("statement_timeout" in s for s, _ in conn.executed if "SET LOCAL" in s)
    text = "\n".join(render(report, generated_at="t"))
    assert "VERDICT: PASS" in text and COHORT_LANE in text
    payload = to_json(report, generated_at="t")
    assert payload["metadata"]["writes"] == [] and len(payload["arms"]) == 4
