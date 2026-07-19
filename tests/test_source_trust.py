"""The Python trust map and the SQL `source_trust_rank` CASE must never drift.

`toolkit/source_trust.py` and `migrations/311_source_trust_rank_and_sign_check.sql`
encode the SAME per-portal trust order in two languages. This test parses the
migration's CASE and asserts it equals the Python dict, so a change to one that
forgets the other fails CI (no live DB needed).
"""

from __future__ import annotations

import pathlib
import re

from toolkit.source_trust import (
    SOURCE_TRUST_RANK,
    UNKNOWN_SOURCE_RANK,
    source_trust_rank,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "311_source_trust_rank_and_sign_check.sql"
)


def _parse_sql_case() -> dict[str, int]:
    """Extract the WHEN '<source>' THEN <n> pairs from the SQL function body."""
    text = _MIGRATION.read_text()
    body = text[text.index("CREATE OR REPLACE FUNCTION source_trust_rank") :]
    body = body[: body.index("$$;", body.index("$$") + 2)]
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"WHEN\s+'([a-z_]+)'\s+THEN\s+(\d+)", body)
    }


def test_python_map_is_the_known_nine() -> None:
    assert SOURCE_TRUST_RANK == {
        "sreality": 1,
        "bezrealitky": 2,
        "idnes": 3,
        "mmreality": 4,
        "remax": 5,
        "maxima": 6,
        "ceskereality": 7,
        "realitymix": 8,
        "bazos": 9,
    }
    # Ranks are a dense, gap-free 1..9 permutation (no accidental dup/skip).
    assert sorted(SOURCE_TRUST_RANK.values()) == list(range(1, 10))
    assert UNKNOWN_SOURCE_RANK == 10


def test_sql_case_matches_python_map() -> None:
    assert _parse_sql_case() == SOURCE_TRUST_RANK


def test_helper_ranks_known_and_unknown() -> None:
    for src, rank in SOURCE_TRUST_RANK.items():
        assert source_trust_rank(src) == rank
    # Unknown, empty, and NULL/None all sort after every known portal.
    assert source_trust_rank("does_not_exist") == UNKNOWN_SOURCE_RANK
    assert source_trust_rank("") == UNKNOWN_SOURCE_RANK
    assert source_trust_rank(None) == UNKNOWN_SOURCE_RANK
    # Every known portal outranks (sorts before) any unknown source.
    assert max(SOURCE_TRUST_RANK.values()) < UNKNOWN_SOURCE_RANK
