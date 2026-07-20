"""Hermetic tests for scripts.apply_r2_unique_guards — no DB.

Guards against the exact drift class this refactor keeps rediscovering (a carrier
declared in one place and not another) plus the Postgres restriction that made
`constraint=True` unsafe for the pair caches: `ADD CONSTRAINT ... UNIQUE USING
INDEX` rejects expression indexes, so any guard using LEAST/GREATEST must stay
index-only or the live ALTER fails.
"""

from __future__ import annotations

from scripts.apply_r2_unique_guards import UNIQUE_GUARDS, _check_name
from toolkit.listing_identity import R2_CARRIERS

_CARRIER_TABLES = {c["table"] for c in R2_CARRIERS}


def test_every_guard_table_is_a_registered_carrier() -> None:
    for guard in UNIQUE_GUARDS:
        assert guard["table"] in _CARRIER_TABLES, guard["table"]


def test_guard_names_are_unique_and_within_postgres_identifier_limit() -> None:
    names = [g["name"] for g in UNIQUE_GUARDS]
    assert len(names) == len(set(names))
    for name in names:
        assert len(name) <= 63, name


def test_expression_index_guards_are_never_promoted_to_a_constraint() -> None:
    for guard in UNIQUE_GUARDS:
        uses_expression = "LEAST(" in guard["cols_sql"] or "GREATEST(" in guard["cols_sql"]
        if uses_expression:
            assert guard["constraint"] is False, guard["name"]


def test_check_name_stays_within_postgres_identifier_limit() -> None:
    for carrier in R2_CARRIERS:
        for _legacy, new in carrier["cols"]:
            assert len(_check_name(carrier["table"], new)) <= 63
