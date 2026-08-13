"""Offline contract tests for the W1v operator-correction producer.

No DB. These pin the properties the explorer review flagged as the two silent
failure modes of an operator write path, plus the vocabulary discipline:

1. The dirty_locations enqueue is UNCONDITIONAL — a restated correction
   (A -> B -> A) collides on the time-free fingerprint, inserts nothing, and an
   `ins`-gated enqueue would never fire (dead correction button).
2. Every ALLOWED claim type has an operator survivorship row in migration 400 —
   survivorship SKIPS claims with no policy row, so a missing row means the
   correction wins the pin but silently loses the field.
3. The SQL calls the named migration functions (location_value_norm,
   location_claim_fingerprint), never an inline transcription.
"""

from __future__ import annotations

import re
from pathlib import Path

from location_data import operator_corrections as oc

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_enqueue_is_not_gated_on_the_insert_cte():
    enqueue = oc._OPERATOR_CLAIM_SQL.split("enqueued AS")[1]
    assert "VALUES (%(listing_id)s, 'operator_edit')" in enqueue
    assert "FROM ins" not in enqueue.split(")")[0]


def test_sql_uses_the_named_migration_functions():
    assert "location_value_norm(" in oc._OPERATOR_CLAIM_SQL
    assert "location_claim_fingerprint(" in oc._OPERATOR_CLAIM_SQL
    # the fingerprint call passes value_norm, not a re-normalized expression
    assert "lower(" not in oc._OPERATOR_CLAIM_SQL
    assert "unaccent(" not in oc._OPERATOR_CLAIM_SQL


def test_operator_vocabulary():
    for fragment in (
        "'operator_input'", "'operator_manual'", "'operator'", "'operator_edit'",
    ):
        assert fragment in oc._OPERATOR_CLAIM_SQL, fragment
    assert "'exact'" in oc._OPERATOR_CLAIM_SQL  # claim_confidence


def test_every_allowed_claim_type_has_an_operator_policy_row():
    sql = (MIGRATIONS / "400_location_w1v_operator_field_policy.sql").read_text()
    fields = re.search(r"unnest\(array\[(.*?)\]", sql, re.S).group(1)
    seeded = set(re.findall(r"'([a-z_]+)'", fields))
    missing = set(oc.ALLOWED_CLAIM_TYPES) - seeded
    assert not missing, f"allowed correction types without a policy row: {missing}"
    assert "'operator', 'operator_manual', 50" in re.sub(r"\s+", " ", sql)


def test_migration_400_policy_rows_cover_the_full_v1_field_set():
    """400 must cover every field the v1 seed (383 + 388) arbitrates — a field
    with portal rows but no operator row would make corrections to it lose."""
    def fields_of(name: str) -> set[str]:
        sql = (MIGRATIONS / name).read_text()
        blocks = re.findall(
            r"insert into location_field_policy.*?unnest\(array\[(.*?)\]", sql, re.S
        )
        out: set[str] = set()
        for block in blocks:
            out |= set(re.findall(r"'([a-z_]+)'", block))
        return out

    v1_fields = fields_of("383_location_w1_resolutions.sql") | fields_of(
        "388_location_w1_projection_quality_columns.sql"
    )
    operator_fields = fields_of("400_location_w1v_operator_field_policy.sql")
    missing = v1_fields - operator_fields
    assert not missing, f"v1 fields without an operator row: {missing}"


def test_allowed_types_are_typed_column_claims():
    """Text corrections only in W1v — geometry claims need their own input
    surface; `coordinate` must stay out until that exists."""
    assert "coordinate" not in oc.ALLOWED_CLAIM_TYPES
    assert oc.ALLOWED_CLAIM_TYPES <= {
        "address_point_id", "street_name", "house_number_cp", "house_number_co",
        "psc", "obec_name", "cast_obce_name", "okres_name",
    }
