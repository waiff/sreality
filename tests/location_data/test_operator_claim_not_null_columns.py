"""Regression: the operator claim INSERT must never override a NOT NULL
column's default with an explicit NULL.

The first live correction failed with NotNullViolation on
`legacy_write_path_unknown` (NOT NULL DEFAULT false): the offline SQL-shape
tests and CI's PREPARE sweep both pass such a statement — PREPARE type-checks
but does not execute, and a fake connection cannot raise a constraint
(adversarial-review lesson). So this test does what those can't: it parses the
claims DDL for NOT-NULL-with-default columns and asserts the operator SQL
either supplies a literal value or omits the column entirely.
"""

from __future__ import annotations

import re
from pathlib import Path

from location_data import operator_corrections as oc

DDL = (Path(__file__).resolve().parents[2]
       / "migrations" / "382_location_w1_claims.sql").read_text()


def _claims_not_null_defaulted_columns() -> set[str]:
    block = DDL.split("create table location_claims (")[1].split("\n);")[0]
    out: set[str] = set()
    for line in block.splitlines():
        m = re.match(r"\s+([a-z_0-9]+)\s+\w+.*not null default", line)
        if m:
            out.add(m.group(1))
    return out


def test_operator_sql_never_nulls_a_defaulted_not_null_column():
    cols = _claims_not_null_defaulted_columns()
    assert cols, "DDL parse failed - no NOT NULL DEFAULT columns found"
    input_cte = oc._OPERATOR_CLAIM_SQL.split("), typed AS")[0]
    offenders = [
        c for c in cols
        if re.search(rf"NULL::\w+(\(\d+(,\d+)?\))?\s+AS\s+{c}\b", input_cte)
    ]
    assert not offenders, (
        f"operator claim SQL overrides NOT NULL DEFAULT column(s) with NULL: {offenders}"
    )


def test_legacy_write_path_unknown_is_false_for_operator_claims():
    assert re.search(r"false\s+AS\s+legacy_write_path_unknown", oc._OPERATOR_CLAIM_SQL)
